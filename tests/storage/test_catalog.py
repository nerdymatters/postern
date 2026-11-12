"""Native catalog pages: exact can_resource decisions, cursors, and work that ignores resources granted to others."""
import random
from types import SimpleNamespace
import uuid

from psycopg.types.json import Jsonb
import pytest

from scripts.catalog_benchmark import AUDIENCE, CATALOG, DEADLINES, WORK, bulk, connect, session, space

RUNTIME = 'catalog_test_runtime'
ORACLE = """SELECT coalesce(jsonb_agg(x ORDER BY x.id),'[]') FROM (SELECT id,kind,title,state,content_revision
 FROM postern.resources WHERE space_id=%(s)s AND postern.can_resource(%(s)s,id,'discover')
 AND (%(q)s::text='' OR search @@ plainto_tsquery('simple',%(q)s))) x"""


@pytest.fixture(scope='module')
def env(db):
    db.execute(f"UPDATE postern.authority SET state='active',generation=gen_random_uuid(); CREATE ROLE {RUNTIME} LOGIN IN ROLE postern_resource_service;")
    with connect(db) as admin, connect(db, RUNTIME, DEADLINES) as runtime:
        generation = admin.execute('SELECT generation::text FROM postern.authority').fetchone()[0]
        admin.commit()
        yield SimpleNamespace(db=db, admin=admin, runtime=runtime, generation=generation)


def pages(f, token, s, q):
    """Follow 50-item pages exactly as SpaceService.catalog builds its cursors."""
    items, after, context = [], None, None
    while True:
        value = f.runtime.execute(CATALOG, (token, s, AUDIENCE, q, after, None if context is None else Jsonb(context))).fetchone()[0]
        f.runtime.commit()
        if len(value['items']) <= 50:
            return items + value['items']
        items += value['items'][:50]
        after, context = items[-1]['id'], value['context']


def authorized(f, actor, s, q):
    f.admin.execute("SELECT set_config('postern.actor_id',%s,true),set_config('postern.space_id',%s,true),"
                    "set_config('postern.generation',%s,true)", (actor['principal'], s, f.generation))
    value = f.admin.execute(ORACLE, {'s': s, 'q': q}).fetchone()[0]
    f.admin.rollback()
    return value


def randomized(f, rng):
    roles = {'owner': 'owner', 'admin': 'admin', **{f'm{i}': 'member' for i in range(10)}, 'gone': 'member'}
    main, other = space(f.admin, 'catalog-main', roles), space(f.admin, 'catalog-other', {'owner': 'owner'})
    s, actors = main['space'], main['actors']
    # A former member keeps stale grants and audience rows, then returns under a new membership ID.
    gone = actors.pop('gone')
    f.admin.execute("UPDATE postern.memberships SET state='revoked' WHERE space_id=%s AND id=%s", (s, gone['membership']))
    actors['back'] = {**gone, 'membership': str(uuid.uuid4())}
    f.admin.execute("INSERT INTO postern.memberships(space_id,id,principal_id,role) VALUES(%s,%s,%s,'member')",
                    (s, actors['back']['membership'], gone['principal']))
    # The same people in another space where everything is shared and searchable.
    for name in ('m0', 'm1', 'admin'):
        f.admin.execute("INSERT INTO postern.memberships(space_id,principal_id,role) VALUES(%s,%s,'member')", (other['space'], actors[name]['principal']))
    f.admin.execute("INSERT INTO postern.resources(space_id,kind,title,state,adapter) SELECT %s,'document','alpha beta '||i,'published','test' "
                    'FROM generate_series(1,40) i', (other['space'],))
    f.admin.execute("INSERT INTO postern.grants(space_id,resource_id,all_members,actions) SELECT space_id,id,true,ARRAY['discover'] "
                    'FROM postern.resources WHERE space_id=%s', (other['space'],))
    words, resources = ['alpha', 'beta', 'gamma', 'payroll', 'คู่มือ'], []
    for i in range(160):
        resources.append(str(uuid.uuid4()))
        f.admin.execute('INSERT INTO postern.resources(space_id,id,kind,title,state,supported_actions,adapter) VALUES(%s,%s,%s,%s,%s,%s,%s)',
            (s, resources[-1], rng.choice(['document', 'video']), ' '.join(rng.sample(words, rng.randint(1, 3)))+f' {i}',
             rng.choices(['published', 'draft', 'archived'], [7, 2, 1])[0],
             rng.choice([['discover', 'view'], ['discover'], ['discover', 'view', 'download']]), 'test'))
    audiences = [str(uuid.uuid4()) for _ in range(4)]
    for audience in audiences:
        f.admin.execute("INSERT INTO postern.audiences(space_id,id,label) VALUES(%s,%s,'Group')", (s, audience))
    memberships = [a['membership'] for a in actors.values()] + [gone['membership']]
    for membership in memberships:
        for audience in rng.sample(audiences, rng.randint(0, 2)):
            f.admin.execute('INSERT INTO postern.audience_members VALUES(%s,%s,%s)', (s, audience, membership))
    actions = [['discover'], ['discover', 'view'], ['discover', 'view', 'download'], ['download'], ['download', 'export'], ['discover', 'export']]
    for _ in range(300):
        target = rng.choice(['membership', 'audience', 'all'])
        f.admin.execute('INSERT INTO postern.grants(space_id,resource_id,membership_id,audience_id,all_members,actions,state,expires_at) '
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,now()+%s*interval '1 hour')",
            (s, rng.choice(resources), rng.choice(memberships) if target == 'membership' else None,
             rng.choice(audiences) if target == 'audience' else None, target == 'all', rng.choice(actions),
             rng.choices(['active', 'revoked'], [4, 1])[0], rng.choice([None, None, None, -1, 1])))
    # Enough shared resources that members, not only admins, need several pages.
    for resource in rng.sample(resources, 70):
        f.admin.execute("INSERT INTO postern.grants(space_id,resource_id,all_members,actions) VALUES(%s,%s,true,ARRAY['discover'])", (s, resource))
    f.admin.commit()
    return s, actors


def test_catalog_pages_match_can_resource(env):
    s, actors = randomized(env, random.Random(20261031))
    seen = {}
    for name, actor in actors.items():
        token = session(env.admin, env.generation, s, actor)
        for q in ['', 'alpha', 'beta gamma', 'คู่มือ', 'payroll alpha', 'missing']:
            expected = authorized(env, actor, s, q)
            assert pages(env, token, s, q) == expected, (name, q)
            seen[name, q] = len(expected)
    assert seen['owner', ''] == seen['admin', ''] == 160
    assert min(seen[f'm{i}', ''] for i in range(10)) > 50
    # Readmission revives neither the former membership's direct grants nor its audiences.
    shared = env.admin.execute("SELECT count(DISTINCT r.id) FROM postern.resources r JOIN postern.grants g ON g.space_id=r.space_id "
        "AND g.resource_id=r.id WHERE r.space_id=%s AND r.state='published' AND g.all_members AND g.state='active' "
        "AND 'discover'=ANY(g.actions) AND (g.expires_at IS NULL OR g.expires_at>now())", (s,)).fetchone()[0]
    env.admin.rollback()
    assert seen['back', ''] == shared


def counted(f, token, s):
    with connect(f.db) as conn:
        conn.execute("SET LOCAL track_functions='all'")
        conn.execute(f'SET LOCAL ROLE {RUNTIME}')
        value = conn.execute(CATALOG, (token, s, AUDIENCE, '', None, None)).fetchone()[0]
        conn.execute('RESET ROLE')
        work = conn.execute(WORK).fetchone()
        conn.rollback()
    return (len(value['items']), *(int(n or 0) for n in work))


def granted_elsewhere(f, s, n):
    """Resources the reader cannot reach: other members' direct grants, revoked grants, another audience."""
    bulk(f.admin, s, f'others/{uuid.uuid4()}/', n, 1)
    audience = str(uuid.uuid4())
    f.admin.execute("INSERT INTO postern.audiences(space_id,id,label) VALUES(%s,%s,'Others')", (s, audience))
    for target, state in [('all', 'revoked'), ('audience', 'active')]:
        f.admin.execute("WITH r AS (INSERT INTO postern.resources(space_id,kind,title,state,adapter) SELECT %s,'document','Hidden '||i,'published','test' "
                        'FROM generate_series(1,%s) i RETURNING space_id,id) INSERT INTO postern.grants(space_id,resource_id,audience_id,all_members,actions,state) '
                        "SELECT space_id,id,%s,%s,ARRAY['discover'],%s FROM r", (s, n, audience if target == 'audience' else None, target == 'all', state))
    f.admin.execute('ANALYZE postern.grants,postern.resources,postern.memberships,postern.audience_members')
    f.admin.commit()


def test_catalog_work_ignores_resources_granted_to_others(env):
    w = space(env.admin, 'catalog-hot', {'owner': 'owner', 'admin': 'admin', 'reader': 'member'})
    s, reader, audience = w['space'], w['actors']['reader'], str(uuid.uuid4())
    env.admin.execute("INSERT INTO postern.audiences(space_id,id,label) VALUES(%s,%s,'Readers')", (s, audience))
    env.admin.execute('INSERT INTO postern.audience_members VALUES(%s,%s,%s)', (s, audience, reader['membership']))
    env.admin.execute("WITH r AS (INSERT INTO postern.resources(space_id,kind,title,state,adapter) SELECT %s,'document','Visible '||i,'published','test' "
                      'FROM generate_series(1,3) i RETURNING space_id,id,title) INSERT INTO postern.grants(space_id,resource_id,membership_id,audience_id,all_members,actions) '
                      "SELECT space_id,id,CASE WHEN title='Visible 1' THEN %s::uuid END,CASE WHEN title='Visible 2' THEN %s::uuid END,title='Visible 3',"
                      "ARRAY['discover'] FROM r", (s, reader['membership'], audience))
    env.admin.commit()
    tokens = {name: session(env.admin, env.generation, s, actor) for name, actor in w['actors'].items()}
    granted_elsewhere(env, s, 100)
    first = counted(env, tokens['reader'], s)
    granted_elsewhere(env, s, 300)
    # Four times as many resources for others: the same authorization calls and grant rows.
    assert counted(env, tokens['reader'], s) == first
    assert first[:2] == (3, 3)
    # Grants that reach the reader but cannot show anything are read, never shown.
    for state, actions, offset in [('draft', ['discover'], None), ('published', ['discover'], -1), ('published', ['download'], None)]:
        env.admin.execute("WITH r AS (INSERT INTO postern.resources(space_id,kind,title,state,adapter) SELECT %s,'document','Unusable '||i,%s,'test' "
                          'FROM generate_series(1,50) i RETURNING space_id,id) INSERT INTO postern.grants(space_id,resource_id,all_members,actions,expires_at) '
                          "SELECT space_id,id,true,%s,now()+%s*interval '1 hour' FROM r", (s, state, actions, offset))
    env.admin.commit()
    assert counted(env, tokens['reader'], s)[:2] == (3, 3)
    assert counted(env, tokens['admin'], s)[:2] == (51, 51)
    before = counted(env, tokens['reader'], s)
    result = env.runtime.execute('SELECT postern.resource_sharing_change(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (tokens['owner'], s, AUDIENCE, str(uuid.uuid4()), 'a'*64, 0, 'grant.cleanup', None, Jsonb({}))).fetchone()[0]
    env.runtime.commit()
    assert result['retired_count'] == 150
    after = counted(env, tokens['reader'], s)
    assert after[:2] == before[:2] and after[3] < before[3]
