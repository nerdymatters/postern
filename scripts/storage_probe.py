"""Bounded, synthetic PostgreSQL experiment for M2-06; never connects to an existing server.

Run: .venv/bin/python -m scripts.storage_probe --output .context/storage-evaluation/run
Requires PostgreSQL 17+ CLI tools on PATH. Creates its own Unix-socket-only cluster,
uses no production credentials, and stops/removes that cluster in a finally block.
The schema is an experiment, not an application migration or a capacity promise.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import selectors
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid


def command(args, *, env=None, data=None, timeout=60, check=True):
    result = subprocess.run(args, input=data, text=True, capture_output=True, env=env, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{Path(args[0]).name} failed: {result.stderr[-3000:]}')
    return result


def sql_args(env, database='postgres'):
    return ['psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-d', database]


def sql(env, text, *, database='postgres', check=True):
    return command(sql_args(env, database), env=env, data=text, check=check)


def catalog(space=1, account=201, after=0, term='handbook', count=False, substring=False):
    # All interpolated inputs are constants controlled by this experiment, never user input.
    prefix = f"""WITH me AS MATERIALIZED (
      SELECT m.id,m.role FROM memberships m JOIN spaces s ON s.id=m.space_id
      WHERE m.space_id={space} AND m.account_id={account} AND m.active AND s.active
    ), allowed AS (
      SELECT g.resource_id FROM grants g JOIN me ON me.id=g.membership_id
       WHERE g.space_id={space} AND (g.expires_at IS NULL OR g.expires_at>now())
      UNION SELECT g.resource_id FROM grants g JOIN audience_members am
       ON am.space_id=g.space_id AND am.audience_id=g.audience_id JOIN me ON me.id=am.membership_id
       WHERE g.space_id={space} AND (g.expires_at IS NULL OR g.expires_at>now())
      UNION SELECT g.resource_id FROM grants g CROSS JOIN me
       WHERE g.space_id={space} AND g.all_members AND (g.expires_at IS NULL OR g.expires_at>now())
      UNION SELECT r.id FROM resources r CROSS JOIN me WHERE r.space_id={space} AND me.role IN ('owner','admin')
    ) SELECT """
    select = 'count(*)' if count else 'r.id,r.title'
    search = f"r.title ILIKE '%{term}%'" if substring else f"r.search @@ websearch_to_tsquery('simple','{term}')"
    return prefix + select + f""" FROM resources r JOIN allowed a ON a.resource_id=r.id
      WHERE r.space_id={space} AND r.published AND r.id>{after}
      AND {search}""" + ('' if count else ' ORDER BY r.id LIMIT 51')


class Control:
    """One private psql connection used only to hold/release a race barrier."""
    def __init__(self, env):
        self.process = subprocess.Popen(sql_args(env), env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    def send(self, statement):
        marker = 'done_' + uuid.uuid4().hex
        self.process.stdin.write(statement + ';\n\\echo ' + marker + '\n')
        self.process.stdin.flush()
        # psql's quiet mode prints only tuples and the explicit marker.
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline-time.monotonic())):
                    break
                if self.process.stdout.readline().strip() == marker:
                    return
            raise RuntimeError('Private race barrier timed out')
        finally:
            selector.close()

    def close(self):
        try:
            self.process.communicate('\\q\n', timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.communicate()


def wait_locked(env, application):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if sql(env, f"SELECT count(*) FROM pg_stat_activity WHERE application_name='{application}' AND wait_event_type='Lock';").stdout.strip() == '1':
            return
        time.sleep(.02)
    raise RuntimeError(f'{application} never reached its lock barrier')


def race(env, first, second, *, independent=False):
    control = Control(env)
    children = []
    try:
        # DO avoids tuple output before the marker, keeping the pipe barrier unambiguous.
        control.send('DO $$ BEGIN PERFORM pg_advisory_lock(876543); END $$')
        def launch(name, query):
            child = subprocess.Popen(sql_args(env), env={**env, 'PGAPPNAME': name},
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            child.stdin.write(query); child.stdin.close(); child.stdin = None
            children.append(child)
            return child
        one = launch('m2_probe_first', "BEGIN; SELECT id FROM spaces WHERE id=1 FOR UPDATE; SELECT pg_advisory_xact_lock(876543);" + first + 'COMMIT;')
        wait_locked(env, 'm2_probe_first')
        two = launch('m2_probe_second', second)
        if independent:
            out2, err2 = two.communicate(timeout=5)
        else:
            wait_locked(env, 'm2_probe_second')
        control.send('DO $$ BEGIN PERFORM pg_advisory_unlock(876543); END $$')
        out1, err1 = one.communicate(timeout=5)
        if not independent:
            out2, err2 = two.communicate(timeout=5)
        return [(one.returncode, out1, err1), (two.returncode, out2, err2)]
    finally:
        control.close()
        for child in children:
            if child.poll() is None:
                child.kill(); child.communicate()


def measure(env, query, destination):
    samples=[]
    for _ in range(21):
        plan=json.loads(sql(env, 'EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+query+';').stdout)[0]
        samples.append(plan['Execution Time'])
    destination.write_text(json.dumps(plan, indent=2)+'\n')
    # First sample is a warm-up. These are server execution times, not HTTP/network latency.
    values=sorted(samples[1:])
    return {'samples':len(values),'median_ms':statistics.median(values),'p95_ms':values[18],
            'max_ms':values[-1],'rows':plan['Plan']['Actual Rows']}


def verify_legacy(env, folder):
    from postern.db import Database
    from postern.recover import import_events
    from postern.honors import certificate
    records=[{'event_id':'original-pass','event':'quiz_attempt','ts':1700000000,'email':'legacy@example.invalid',
              'name':'Original Name','detail':{'chapter':0,'score':20,'total':20,'passed':True}},
             {'event_id':'name-a','event':'certificate_name','ts':1700000001,'email':'legacy@example.invalid','detail':{'name':'Asha Rivera'}},
             {'event_id':'name-z','event':'certificate_name','ts':1700000001,'email':'legacy@example.invalid','detail':{'name':'อาชา Santos'}}]
    encoded=[json.dumps(r,ensure_ascii=False,separators=(',',':')).encode() for r in records]
    for row, raw in zip(records,encoded):
        sql(env,f"INSERT INTO legacy_events VALUES('synthetic-installation','{row['event_id']}',decode('{raw.hex()}','hex'));")
    restored=[bytes.fromhex(line) for line in sql(env,"SELECT encode(original,'hex') FROM legacy_events ORDER BY event_id DESC;").stdout.splitlines()]
    assert sorted(restored)==sorted(encoded)
    results=[]
    for name,rows in [('before',records),('after',[json.loads(raw) for raw in restored])]:
        db=Database(folder/(name+'.sqlite3'))
        try:
            def write(conn):
                conn.execute("INSERT INTO members VALUES('legacy@example.invalid','Roster Name','reader')")
                import_events(conn,rows)
            db.submit(write).result()
            results.append(db.read(lambda conn:certificate(conn,'legacy@example.invalid')))
        finally:
            db.close()
    assert results[0] is not None and results[0]['name']=='อาชา Santos'
    assert results[0]['number']=='PT-2023-C0BABAA1' and results[0]['awarded_at']==1700000000
    assert results[0]==results[1]
    return {'raw_events':len(encoded),'bytes_unchanged':True,'certificate_unchanged':True,
            'expected_sha256':sorted(hashlib.sha256(raw).hexdigest() for raw in encoded)}


def run(output):
    if not __debug__:
        raise RuntimeError('Run without Python -O: assertions are the experiment checks')
    output.mkdir(parents=True,exist_ok=False)
    schema=Path(__file__).with_suffix('.sql')
    began=time.monotonic()
    report={'scope':'synthetic local PostgreSQL experiment; not production load or complete space authorization',
            'workload':{'spaces':100,'accounts':10000,'resources':100000,'events':1000000,'largest_space_resources':50000},
            'schema_sha256':hashlib.sha256(schema.read_bytes()).hexdigest(),
            'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'host':{'os':platform.system(),'release':platform.release(),'machine':platform.machine()}, 'checks':{}}
    # Never inherit connection settings, service files, passwords or client options.
    env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
    home=tempfile.mkdtemp(prefix='postern-m2-')
    cluster=Path(home)/'db'
    env.update(PGHOST=home,PGPORT='54872',PGUSER='m2_probe',PGCONNECT_TIMEOUT='3',
               PGOPTIONS='-c statement_timeout=15000 -c lock_timeout=10000',PGAPPNAME='m2_probe')
    stopped=False
    try:
        report['version']=command(['postgres','--version']).stdout.strip()
        command(['initdb','-D',str(cluster),'-U','m2_probe','--auth=trust','--encoding=UTF8','--locale=C'])
        with (cluster/'postgresql.conf').open('a') as config:
            config.write(f"\nlisten_addresses=''\nunix_socket_directories='{home}'\nunix_socket_permissions=0700\nport=54872\nshared_buffers='64MB'\nwork_mem='4MB'\nmaintenance_work_mem='32MB'\nmax_connections=12\nmax_wal_size='256MB'\n")
        command(['pg_ctl','-D',str(cluster),'-l',str(output.resolve()/'postgres.log'),'-w','start'])
        report['durability']=json.loads(sql(env,"SELECT json_build_object('fsync',current_setting('fsync'),'synchronous_commit',current_setting('synchronous_commit'),'full_page_writes',current_setting('full_page_writes'));").stdout)
        sql(env,schema.read_text())
        for first in range(1,1000001,100000):
            sql(env,f"""INSERT INTO events SELECT (i-1)%100+1,i,(i-1)%10000+1,1700000000+i,
                         jsonb_build_object('event','view','resource',i%100000+1)
                         FROM generate_series({first},{first+99999}) i;""")
        sql(env,'ANALYZE;')
        checks=report['checks']
        assert sql(env,catalog(count=True)+';').stdout.strip()=='10004'
        checks['authorized_count']=10004
        assert sql(env,catalog(term='foreign',count=True)+';').stdout.strip()=='0'
        assert sql(env,catalog(account=9999,count=True)+';').stdout.strip()=='0'
        checks['foreign_search_and_nonmember_denied']=True
        sql(env,"INSERT INTO grants(space_id,resource_id,membership_id,expires_at) VALUES(1,11,201,now()-interval '1 second');")
        assert sql(env,catalog(count=True)+';').stdout.strip()=='10004'
        checks['expired_grant_denied']=True
        assert sql(env,catalog(term='พนักงาน',count=True)+';').stdout.strip()=='0'
        assert sql(env,catalog(term='พนักงาน',substring=True)+';').stdout.strip().startswith('12|')
        assert sql(env,catalog(term='พนักงาน',substring=True,count=True)+';').stdout.strip()=='1'
        checks['thai_substring_scoped_fulltext_segmentation_gap']=True
        bad=sql(env,"INSERT INTO grants(space_id,resource_id,membership_id) VALUES(2,50001,201);",check=False)
        assert bad.returncode and 'foreign key' in bad.stderr
        checks['cross_space_foreign_key']=True
        grants_before=sql(env,'SELECT count(*) FROM grants;').stdout
        bulk=sql(env,"BEGIN; INSERT INTO grants(space_id,resource_id,membership_id) SELECT 1,i,401 FROM generate_series(200,299) i; INSERT INTO grants(space_id,resource_id,membership_id) VALUES(2,50001,201); COMMIT;",check=False)
        assert bulk.returncode and 'foreign key' in bulk.stderr
        assert sql(env,'SELECT count(*) FROM grants;').stdout==grants_before
        checks['failed_bulk_grants_roll_back']=True
        rls=sql(env,"SET ROLE probe_reader; SELECT count(*) FROM resources; BEGIN; SET LOCAL postern.space_id='1'; SELECT count(*) FROM resources WHERE space_id=2; SELECT count(*) FROM resources; COMMIT; SELECT count(*) FROM resources;").stdout.splitlines()
        assert rls==['0','0','50000','0'],rls
        checks['resource_rls_and_context_reset']=True
        before=sql(env,'SELECT revision FROM spaces WHERE id=1;').stdout.strip()
        failed=sql(env,"BEGIN; UPDATE spaces SET revision=revision+1 WHERE id=1; INSERT INTO spaces(id) VALUES(1); COMMIT;",check=False)
        assert failed.returncode and sql(env,'SELECT revision FROM spaces WHERE id=1;').stdout.strip()==before
        checks['failed_write_rolls_back']=True
        idempotent=sql(env,"SELECT probe_mutate(1,1,'lost-response','payload-a'); SELECT probe_mutate(1,1,'lost-response','payload-a');").stdout.splitlines()
        assert idempotent==['1','1']
        assert sql(env,"SELECT probe_mutate(1,1,'lost-response','payload-b');",check=False).returncode
        assert sql(env,'SELECT count(*) FROM audit;').stdout.strip()=='1'
        checks['idempotency_and_audit']=True
        owner_guard="""DO $$ BEGIN IF (SELECT count(*) FROM memberships WHERE space_id=1 AND active AND role='owner')<=1 THEN RAISE EXCEPTION 'last owner'; END IF; END $$;"""
        first=owner_guard+"UPDATE memberships SET active=false WHERE space_id=1 AND id=1;"
        second="BEGIN; SELECT id FROM spaces WHERE id=1 FOR UPDATE;"+owner_guard+"UPDATE memberships SET active=false WHERE space_id=1 AND id=101; COMMIT;"
        outcomes=race(env,first,second)
        assert outcomes[0][0]==0 and outcomes[1][0]!=0 and 'last owner' in outcomes[1][2],outcomes
        assert sql(env,"SELECT count(*) FROM memberships WHERE space_id=1 AND active AND role='owner';").stdout.strip()=='1'
        checks['concurrent_last_owner_protected']=True
        replay=sql(env,"SELECT probe_mutate(1,1,'lost-response','payload-a');",check=False)
        assert replay.returncode and 'forbidden' in replay.stderr
        checks['revoked_actor_cannot_replay_result']=True
        outcomes=race(env,"UPDATE memberships SET active=false WHERE space_id=1 AND id=301;",
                      "SELECT probe_mutate(1,301,'revoked-admin','payload');")
        assert outcomes[0][0]==0 and outcomes[1][0]!=0 and 'forbidden' in outcomes[1][2],outcomes
        checks['queued_mutation_rechecks_revocation']=True
        outcomes=race(env,'SELECT 1;',"SELECT probe_mutate(2,1,'independent','payload');",independent=True)
        assert outcomes[0][0]==outcomes[1][0]==0,outcomes
        checks['different_space_writes_independent']=True
        queries={'scoped_search':catalog(),'scoped_later_page':catalog(after=40000),
                 'thai_title_substring':catalog(term='พนักงาน',substring=True),
                 'authorized_count':catalog(count=True),
                 'actor_history':"SELECT id,occurred_at FROM events WHERE space_id=1 AND actor=201 ORDER BY occurred_at DESC,id DESC LIMIT 51",
                 'space_activity':"SELECT actor,count(*) FROM events WHERE space_id=1 GROUP BY actor"}
        report['queries']={name:measure(env,query,output/(name+'.plan.json')) for name,query in queries.items()}
        report['database_bytes']=int(sql(env,"SELECT pg_database_size('postgres');").stdout.strip())
        report['legacy']=verify_legacy(env,output)
        dump=output/'synthetic.dump'
        command(['pg_dump','-Fc','-d','postgres','-f',str(dump.resolve())],env=env)
        command(['createdb','restored'],env=env)
        command(['pg_restore','--exit-on-error','-d','restored',str(dump.resolve())],env=env)
        counts="SELECT json_build_array((SELECT count(*) FROM resources),(SELECT count(*) FROM events),(SELECT count(*) FROM memberships),(SELECT count(*) FROM audit));"
        assert sql(env,counts).stdout==sql(env,counts,database='restored').stdout
        assert sql(env,"SELECT encode(original,'hex') FROM legacy_events ORDER BY event_id;").stdout==sql(env,"SELECT encode(original,'hex') FROM legacy_events ORDER BY event_id;",database='restored').stdout
        old_generation=sql(env,'SELECT generation FROM spaces WHERE id=1;').stdout
        sql(env,'UPDATE spaces SET active=false,generation=gen_random_uuid();',database='restored')
        assert sql(env,catalog(count=True)+';',database='restored').stdout.strip()=='0'
        assert sql(env,'SELECT generation FROM spaces WHERE id=1;',database='restored').stdout!=old_generation
        checks['logical_restore_and_quarantine']=True
        report['result']='PASS'
    except Exception as error:
        report['result']='FAIL'
        report['error']=str(error) or type(error).__name__
        raise
    finally:
        if (cluster/'postmaster.pid').exists():
            try:
                result=command(['pg_ctl','-D',str(cluster),'-m','fast','-w','stop'],timeout=15,check=False)
                stopped=result.returncode==0
            except subprocess.TimeoutExpired:
                stopped=False
        else:
            stopped=True
        if stopped:
            shutil.rmtree(home)
        else:
            report['result']='FAIL'
            report['cleanup_error']='Private cluster not stopped; retained at '+home
        report['elapsed_seconds']=round(time.monotonic()-began,3)
        (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    if not stopped:
        raise RuntimeError('Private cluster cleanup failed; inspect report')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True,help='New output directory; existing directories are refused')
    args=parser.parse_args()
    run(args.output)
