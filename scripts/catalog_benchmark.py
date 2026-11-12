"""Concentrated native-catalog benchmark on an owned, disposable PostgreSQL cluster.

Run: .venv/bin/python -m scripts.catalog_benchmark --output .context/catalog-benchmark/run
Requires PostgreSQL 17+ CLI tools and .[test,postgres]. It applies the actual core
migrations, seeds synthetic spaces and calls postern.resource_catalog through a
restricted resource login with the service's five-second statement deadline. It
never accepts a connection string or real data. Results are local measurements,
not quotas or a hosted capacity guarantee.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import statistics
import sys
import time
import uuid

import psycopg

from postern.pg import apply_schema
from scripts.local_postgres import local_postgres

RUNTIME = 'catalog_benchmark_runtime'
AUDIENCE = 'urn:postern:catalog-benchmark'
DEADLINES = '-c statement_timeout=5000 -c lock_timeout=3000'  # as postern.spaces.SpaceService
TITLE = "CASE WHEN i%%1000=0 THEN 'Payroll policy '||i ELSE 'Handbook chapter '||i END"  # psycopg-escaped
CATALOG = 'SELECT postern.resource_catalog(%s,%s,%s,%s,%s,%s)'
# Authorization calls, then grant rows read or fetched through the table and any of its indexes.
WORK = """SELECT pg_stat_get_xact_function_calls('postern.can_resource(uuid,uuid,text)'::regprocedure),
 pg_stat_get_xact_function_calls('postern.current_role(uuid)'::regprocedure),
 (SELECT sum(pg_stat_get_xact_tuples_returned(c.oid)+pg_stat_get_xact_tuples_fetched(c.oid)) FROM pg_class c
  WHERE c.oid='postern.grants'::regclass OR c.oid IN (SELECT indexrelid FROM pg_index WHERE indrelid='postern.grants'::regclass))"""
# Independent of the implementation under test: one set-based statement per case. Only r.id is
# correlated, so PostgreSQL can hash the grant check instead of probing it for every resource.
ORACLE = """SELECT coalesce(json_agg(r.id ORDER BY r.id),'[]') FROM postern.resources r
 WHERE r.space_id=%(s)s AND (%(q)s::text='' OR r.search @@ plainto_tsquery('simple',%(q)s))
 AND (%(role)s::text IN ('owner','admin') OR (r.state='published' AND EXISTS(
  SELECT 1 FROM postern.grants g WHERE g.space_id=%(s)s AND g.resource_id=r.id AND g.state='active'
  AND 'discover'=ANY(g.actions) AND (g.expires_at IS NULL OR g.expires_at>now())
  AND (g.all_members OR g.membership_id=%(m)s OR g.audience_id IN (SELECT am.audience_id
   FROM postern.audience_members am WHERE am.space_id=%(s)s AND am.membership_id=%(m)s)))))"""
CASES = [
    # Hidden-heavy: every resource is granted to some other member.
    ('hot', 'none', '', 'first'), ('hot', 'none', 'payroll', 'first'), ('hot', 'one', '', 'first'),
    ('hot', 'audience', '', 'first'), ('hot', 'admin', '', 'first'), ('hot', 'admin', '', 'last'),
    ('hot', 'admin', 'payroll', 'first'),
    # Everything shared with all members.
    ('open', 'reader', '', 'first'), ('open', 'reader', '', 'last'), ('open', 'reader', 'payroll', 'first'),
    # Grants that must not count: expired, or on drafts.
    ('stale', 'reader', '', 'first'), ('stale', 'owner', '', 'first'),
    # The earlier uniform pilot shape.
    ('pilot', 'none', '', 'first'),
]


def connect(db, user=None, options='-c statement_timeout=600000'):
    return psycopg.connect(host=db.env['PGHOST'], port=int(db.env['PGPORT']), user=user or db.env['PGUSER'],
                           dbname=db.database, options=options)


def space(conn, name, roles):
    """Active space with named actors {actor: role}; the first owner owns the space."""
    created = {'space': str(uuid.uuid4()), 'actors': {}}
    for actor, role in roles.items():
        created['actors'][actor] = {'principal': str(uuid.uuid4()), 'membership': str(uuid.uuid4()),
                                    'account': f'{name}/{actor}', 'role': role}
        conn.execute("INSERT INTO postern.principals(id,authority,account_id) VALUES(%s,'benchmark',%s)",
                     (created['actors'][actor]['principal'], f'{name}/{actor}'))
    owner, record = next(a for a in created['actors'].values() if a['role'] == 'owner'), str(uuid.uuid4())
    conn.execute("INSERT INTO postern.owners(id,kind,principal_id,label) VALUES(%s,'person',%s,%s)", (record, owner['principal'], name))
    conn.execute("INSERT INTO postern.spaces(id,owner_id,title,state) VALUES(%s,%s,%s,'active')", (created['space'], record, name))
    for actor in created['actors'].values():
        conn.execute('INSERT INTO postern.memberships(space_id,id,principal_id,role) VALUES(%s,%s,%s,%s)',
                     (created['space'], actor['membership'], actor['principal'], actor['role']))
    return created


def bulk(conn, space_id, prefix, members, per_member):
    """Members that each hold direct grants on their own published resources."""
    conn.execute('CREATE TEMP TABLE bulk_members AS SELECT k,gen_random_uuid() AS principal,gen_random_uuid() AS membership '
                 'FROM generate_series(1,%s) k', (members,))
    conn.execute("INSERT INTO postern.principals(id,authority,account_id) SELECT principal,'benchmark',%s::text||k FROM bulk_members", (prefix,))
    # The deferred active-owner check rescans the space's memberships for every inserted row,
    # which is quadratic for bulk members. Commit the space and its owner, then skip that check.
    conn.commit()
    conn.execute('ALTER TABLE postern.memberships DISABLE TRIGGER membership_owner')
    conn.execute("INSERT INTO postern.memberships(space_id,id,principal_id,role) SELECT %s,membership,principal,'member' FROM bulk_members", (space_id,))
    conn.execute('ALTER TABLE postern.memberships ENABLE TRIGGER membership_owner')
    conn.execute('CREATE TEMP TABLE bulk_resources AS SELECT i,gen_random_uuid() AS resource,(i-1)/%s+1 AS k '
                 'FROM generate_series(1,%s) i', (per_member, members*per_member))
    conn.execute(f"INSERT INTO postern.resources(space_id,id,kind,title,state,adapter) SELECT %s,resource,'document',{TITLE},"
                 "'published','benchmark' FROM bulk_resources", (space_id,))
    conn.execute("INSERT INTO postern.grants(space_id,resource_id,membership_id,actions) SELECT %s,r.resource,m.membership,"
                 "ARRAY['discover','view'] FROM bulk_resources r JOIN bulk_members m USING(k)", (space_id,))
    conn.execute('DROP TABLE bulk_members,bulk_resources')


def bulk_actor(conn, space_id, account):
    principal, membership = conn.execute("SELECT p.id::text,m.id::text FROM postern.principals p JOIN postern.memberships m "
        "ON m.principal_id=p.id AND m.space_id=%s WHERE p.authority='benchmark' AND p.account_id=%s", (space_id, account)).fetchone()
    return {'principal': principal, 'membership': membership, 'account': account, 'role': 'member'}


def seed(conn, resources, large):
    workloads = {}
    hot = workloads['hot'] = space(conn, 'hot', {'owner': 'owner', 'admin': 'admin', 'none': 'member', 'audience': 'member'})
    bulk(conn, hot['space'], 'hot/bulk/', resources, 1)
    hot['actors']['one'] = bulk_actor(conn, hot['space'], 'hot/bulk/1')
    audience = str(uuid.uuid4())
    conn.execute("INSERT INTO postern.audiences(space_id,id,label) VALUES(%s,%s,'Benchmark audience')", (hot['space'], audience))
    conn.execute('INSERT INTO postern.audience_members VALUES(%s,%s,%s)', (hot['space'], audience, hot['actors']['audience']['membership']))
    conn.execute("INSERT INTO postern.grants(space_id,resource_id,audience_id,actions) SELECT space_id,id,%s,ARRAY['discover'] "
                 'FROM postern.resources WHERE space_id=%s ORDER BY id LIMIT 25', (audience, hot['space']))
    shared = workloads['open'] = space(conn, 'open', {'owner': 'owner', 'reader': 'member'})
    conn.execute(f"INSERT INTO postern.resources(space_id,kind,title,state,adapter) SELECT %s,'document',{TITLE},'published',"
                 "'benchmark' FROM generate_series(1,%s) i", (shared['space'], resources))
    conn.execute("INSERT INTO postern.grants(space_id,resource_id,all_members,actions) SELECT space_id,id,true,ARRAY['discover'] "
                 'FROM postern.resources WHERE space_id=%s', (shared['space'],))
    stale = workloads['stale'] = space(conn, 'stale', {'owner': 'owner', 'reader': 'member'})
    conn.execute(f"INSERT INTO postern.resources(space_id,kind,title,state,adapter) SELECT %s,'document',{TITLE},"
                 "CASE WHEN i%%2=1 THEN 'draft' ELSE 'published' END,'benchmark' FROM generate_series(1,%s) i", (stale['space'], resources))
    conn.execute("INSERT INTO postern.grants(space_id,resource_id,all_members,actions,expires_at) SELECT space_id,id,true,"
                 "ARRAY['discover','view'],CASE WHEN state='published' THEN now()-interval '1 hour' END "
                 'FROM postern.resources WHERE space_id=%s', (stale['space'],))
    for n in range(1, 101):
        pilot = space(conn, f'pilot-{n}', {'owner': 'owner', **({'none': 'member'} if n == 1 else {})})
        bulk(conn, pilot['space'], f'pilot-{n}/bulk/', 100, 10)
        if n == 1:
            workloads['pilot'] = pilot
    if large:
        big = workloads['large'] = space(conn, 'large', {'owner': 'owner', 'admin': 'admin', 'none': 'member'})
        bulk(conn, big['space'], 'large/bulk/', large, 1)
        big['actors']['one'] = bulk_actor(conn, big['space'], 'large/bulk/1')
    conn.commit()
    return workloads


def session(conn, generation, space_id, actor):
    login, token_hash = str(uuid.uuid4()), hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    conn.execute("SELECT postern.register_login(%s,%s,extract(epoch FROM now()+interval '1 hour')::bigint)", (login, actor['principal']))
    conn.execute("SELECT postern.exchange_session(%s,%s,%s,%s,%s,%s,'benchmark',%s,%s,%s,"
                 "extract(epoch FROM clock_timestamp()+interval '30 seconds')::bigint)",
                 (token_hash, space_id, AUDIENCE, str(uuid.uuid4()), login, actor['principal'], actor['account'], generation, actor['membership']))
    conn.commit()
    return token_hash


def timed(runtime, params):
    began = time.perf_counter()
    try:
        value = runtime.execute(CATALOG, params).fetchone()[0]
        runtime.commit()
        return time.perf_counter()-began, value
    except psycopg.errors.QueryCanceled:
        runtime.rollback()
        return time.perf_counter()-began, None


def work(db, params):
    """Count authorization calls and grant rows read inside one rolled-back catalog request."""
    with connect(db, options='-c statement_timeout=30000') as conn:
        conn.execute("SET LOCAL track_functions='all'")
        conn.execute(f'SET LOCAL ROLE {RUNTIME}')
        try:
            conn.execute(CATALOG, params)
        except psycopg.errors.QueryCanceled:
            return None
        conn.execute('RESET ROLE')
        found = [int(n or 0) for n in conn.execute(WORK).fetchone()]
        conn.rollback()
        return dict(zip(['can_resource', 'current_role', 'grant_rows'], found))


def measure(db, admin, runtime, generation, workloads, case, repeat):
    name, actor_name, query, page = case
    workload = workloads[name]
    actor = workload['actors'][actor_name]
    # Never auto-prepare the oracle: a generic plan cannot fold the role test and degrades badly.
    expected = admin.execute(ORACLE, {'s': workload['space'], 'q': query, 'role': actor['role'], 'm': actor['membership']},
                             prepare=False).fetchone()[0]
    after = expected[-51] if page == 'last' and len(expected) > 51 else None
    rest = expected[expected.index(after)+1:] if after else expected
    params = (session(admin, generation, workload['space'], actor), workload['space'], AUDIENCE, query, after, None)
    samples, results = [], []
    for attempt in range(repeat+1):
        elapsed, value = timed(runtime, params)
        if attempt:  # The first call is a warm-up.
            samples.append(elapsed*1000)
        results.append(value)
        if value is None and attempt >= 2:
            break
    returned = [[item['id'] for item in value['items']] for value in results if value is not None]
    counted = work(db, params)
    return {'workload': name, 'actor': actor_name, 'role': actor['role'], 'query': query, 'page': page,
            'authorized_total': len(expected), 'expected_items': len(rest[:51]),
            'returned_items': len(returned[-1]) if returned else None,
            # Judged on completed calls only; a call cut off by the deadline returns nothing to compare.
            'correct': all(ids == rest[:51] for ids in returned) if returned else None,
            'timeouts': sum(value is None for value in results), 'runs': len(results),
            'median_ms': round(statistics.median(samples), 2), 'max_ms': round(max(samples), 2),
            'samples_ms': [round(s, 2) for s in samples], 'work': counted}


def write(output, report):
    (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    rows = ['| Workload | Actor | Query | Page | Authorized | Returned | Correct | Timeouts | Median ms | Max ms | can_resource calls | Grant rows read |',
            '| --- | --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |']
    for c in report.get('cases', []):
        counted = f"{c['work']['can_resource']} | {c['work']['grant_rows']}" if c['work'] else 'timeout | timeout'
        rows.append(f"| {c['workload']} | {c['actor']} ({c['role']}) | {c['query'] or '—'} | {c['page']} | {c['authorized_total']} | "
                    f"{c['returned_items'] if c['returned_items'] is not None else '—'} | {({True: 'yes', False: 'NO'}).get(c['correct'], '—')} | "
                    f"{c['timeouts']}/{c['runs']} | {c['median_ms']} | {c['max_ms']} | {counted} |")
    (output/'report.md').write_text(f"# Catalog benchmark: {report['result']}\n\n" + '\n'.join(rows) + '\n')


def run(output, resources, repeat, large):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    # Never inherit libpq service files, passwords or connection defaults.
    for key in [k for k in os.environ if k.startswith('PG')]:
        del os.environ[key]
    began = time.monotonic()
    report = {'scope': 'Owned synthetic PostgreSQL cluster; actual core migrations and postern.resource_catalog through a '
                       'restricted resource login with the service deadlines. No network, objects or concurrent traffic. '
                       'Local measurements, not quotas or hosted capacity.',
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'host': {'os': platform.system(), 'release': platform.release(), 'machine': platform.machine(),
                       'python': platform.python_version(), 'psycopg': psycopg.__version__},
              'parameters': {'resources': resources, 'repeat': repeat, 'large': large},
              'load_average': {'start': os.getloadavg()}, 'result': 'INCOMPLETE', 'cases': []}
    try:
        with local_postgres(output/'postgres.log') as db:
            report['migrations_sha256'] = apply_schema(db)
            db.execute(f"UPDATE postern.authority SET state='active',generation=gen_random_uuid(); "
                       f'CREATE ROLE {RUNTIME} LOGIN IN ROLE postern_resource_service;')
            with connect(db) as admin:
                report['postgres'] = admin.execute('SELECT version()').fetchone()[0]
                report['settings'] = dict(admin.execute("SELECT name,setting FROM pg_settings WHERE name IN "
                    "('shared_buffers','work_mem','fsync','synchronous_commit','jit','max_parallel_workers_per_gather')").fetchall())
                seeded = time.monotonic()
                workloads = seed(admin, resources, large)
                admin.execute('ANALYZE')
                admin.commit()
                report['seed_seconds'] = round(time.monotonic()-seeded, 1)
                report['counts'] = dict(zip(['spaces', 'principals', 'memberships', 'resources', 'grants'], admin.execute(
                    'SELECT (SELECT count(*) FROM postern.spaces),(SELECT count(*) FROM postern.principals),'
                    '(SELECT count(*) FROM postern.memberships),(SELECT count(*) FROM postern.resources),'
                    '(SELECT count(*) FROM postern.grants)').fetchone()))
                generation = admin.execute('SELECT generation::text FROM postern.authority').fetchone()[0]
                cases = CASES + ([('large', 'none', '', 'first'), ('large', 'one', '', 'first'), ('large', 'admin', '', 'last')] if large else [])
                with connect(db, user=RUNTIME, options=DEADLINES) as runtime:
                    for case in cases:  # Keep completed cases if a later one is interrupted.
                        report['cases'].append(measure(db, admin, runtime, generation, workloads, case, repeat))
                        write(output, report)
                        print(json.dumps({k: report['cases'][-1][k] for k in ('workload', 'actor', 'query', 'page', 'correct',
                                                                               'timeouts', 'median_ms')}, ensure_ascii=False), flush=True)
        failures = [c for c in report['cases'] if c['correct'] is not True or c['timeouts']]
        report['result'] = 'PASS' if not failures else 'FAIL'
    except Exception as error:
        report['result'] = 'ERROR'
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        report['elapsed_seconds'] = round(time.monotonic()-began, 1)
        report['load_average']['end'] = os.getloadavg()
        write(output, report)
    print(json.dumps({k: report[k] for k in ('result', 'counts', 'elapsed_seconds') if k in report}, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New output directory; existing directories are refused')
    parser.add_argument('--resources', type=int, default=10000, help='Resources in each concentrated space')
    parser.add_argument('--repeat', type=int, default=5, help='Timed calls per case after one warm-up')
    parser.add_argument('--large', type=int, default=0, help='Optionally add one space with this many resources')
    args = parser.parse_args()
    # Turn termination into SystemExit so the owned cluster is still stopped and removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    run(args.output, args.resources, args.repeat, args.large)
