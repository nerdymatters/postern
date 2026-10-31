"""Ownership handoffs against real PostgreSQL with restricted service roles."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time

import psycopg
import pytest

from postern.pg import literal, Postgres
from test_service_api import api, runtime
from test_onboarding import joiner, login, uid, fail_events, invite


def call(db, name, *args, check=True):
    types={'propose_ownership':['uuid','uuid','uuid','text','integer','bigint','uuid','text'],
           'decide_ownership':['uuid','uuid','uuid','text','uuid','text'], 'ownership_status':['uuid','uuid']}[name]
    result=db.execute('SELECT postern.'+name+'('+','.join(('NULL' if v is None else literal(str(v)))+'::'+t for v,t in zip(args,types))+');',check=False)
    if check: assert result.returncode==0,result.stderr
    return result


def propose(db, m, session, *, target=None, role='admin', ttl=3600, revision=None, key=None, check=True):
    args=(m.s,target or m.mb,role,ttl,int(m.db.scalar(f"SELECT policy_revision FROM postern.spaces WHERE id='{m.s}';")) if revision is None else revision)
    digest=hashlib.sha256(json.dumps(args).encode()).hexdigest()
    return call(db,'propose_ownership',session,*args,key or uid(),digest,check=check)


def decide(db,m,session,transfer,action='accept',key=None,check=True):
    digest=hashlib.sha256(json.dumps([m.s,transfer,action]).encode()).hexdigest()
    return call(db,'decide_ownership',session,m.s,transfer,action,key or uid(),digest,check=check)


def status(db,m,session,check=True):
    return call(db,'ownership_status',session,m.s,check=check)


def test_acceptance_changes_only_selected_space_and_preserves_history(api,joiner):
    f=api;m=f.model;a,b=login(m,'a'),login(m,'b');old_a,old_b=f.session('a'),f.session('b');sibling=f.session('a',m.t)
    # Two spaces share one responsibility record; transferring A must not rewrite it.
    m.db.execute(f"UPDATE postern.spaces SET owner_id='{m.owner}' WHERE id='{m.t}';")
    invitation=invite(f)[0].json()
    op=uid();rev=int(m.db.scalar(f"SELECT policy_revision FROM postern.spaces WHERE id='{m.s}';"))
    offered=json.loads(propose(joiner,m,a,key=op,revision=rev).stdout);t=offered['transfer_id']
    assert json.loads(propose(joiner,m,a,key=op,revision=rev).stdout)==offered
    assert propose(joiner,m,a,key=op,revision=rev,role='member',check=False).returncode
    assert m.db.scalar(f"SELECT role FROM postern.memberships WHERE id='{m.mb}';")=='member'
    assert m.db.scalar(f"SELECT owner_id FROM postern.spaces WHERE id='{m.s}';")==m.owner
    preview=json.loads(status(joiner,m,b).stdout)
    assert preview['transfer']['is_recipient'] and not preview['transfer']['can_cancel']
    accept_key=uid();first=json.loads(decide(joiner,m,b,t,key=accept_key).stdout)
    assert first=={'transfer_id':t,'state':'accepted'}
    assert json.loads(decide(joiner,m,b,t,key=accept_key).stdout)==first
    assert json.loads(propose(joiner,m,a,key=op,revision=rev).stdout)['state']=='accepted'
    assert decide(joiner,m,b,t,key=accept_key,action='decline',check=False).returncode
    assert m.db.scalar(f"SELECT role FROM postern.memberships WHERE id='{m.ma}';")=='admin'
    assert m.db.scalar(f"SELECT role FROM postern.memberships WHERE id='{m.mb}';")=='owner'
    assert m.db.scalar(f"SELECT o.principal_id FROM postern.spaces s JOIN postern.owners o ON o.id=s.owner_id WHERE s.id='{m.s}';")==m.b
    assert m.db.scalar(f"SELECT owner_id FROM postern.spaces WHERE id='{m.t}';")==m.owner
    assert m.db.scalar(f"SELECT principal_id FROM postern.owners WHERE id='{m.owner}';")==m.a
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE target_id='{t}' AND action='ownership.accepted';")=='1'
    assert m.db.scalar(f"SELECT state FROM postern.invitations WHERE id='{invitation['invitation_id']}';")=='revoked'
    assert m.db.scalar(f"SELECT count(*) FROM postern.audience_members WHERE membership_id='{m.mb}';")=='1'
    assert m.db.scalar(f"SELECT count(*) FROM postern.grants WHERE membership_id='{m.mb}' AND state='active';")=='1'
    for h in (old_a,old_b): assert f.client.get(f.url+'/resources',headers=h).status_code==401
    assert f.client.get(f'/v1/spaces/{m.t}/resources',headers=sibling).status_code==200
    assert len(f.client.get(f.url+'/resources',headers=f.session('b')).json()['resources'])==5
    # An old accepted receipt does not undo a later role change.
    m.db.execute(f"BEGIN; UPDATE postern.memberships SET role='owner' WHERE id='{m.ma}'; UPDATE postern.memberships SET role='member' WHERE id='{m.mb}'; COMMIT;")
    assert json.loads(decide(joiner,m,b,t,key=accept_key).stdout)==first
    assert m.db.scalar(f"SELECT role FROM postern.memberships WHERE id='{m.mb}';")=='member'
    m.db.execute(f"UPDATE postern.memberships SET state='revoked' WHERE id='{m.mb}'; INSERT INTO postern.memberships(space_id,principal_id,role) VALUES('{m.s}','{m.b}','member');")
    assert decide(joiner,m,b,t,key=accept_key,check=False).returncode


def test_authority_exact_membership_and_no_member_directory(model,joiner):
    m=model;a,b,c,d=[login(m,x) for x in 'abcd']
    for session in (b,d): assert 'forbidden' in propose(joiner,m,session,check=False).stderr
    for target in (m.ma,m.mt,uid()): assert propose(joiner,m,a,target=target,check=False).returncode
    assert propose(joiner,m,a,role='owner',check=False).returncode
    assert propose(joiner,m,a,revision=999,check=False).returncode
    assert status(joiner,m,c,check=False).returncode
    t=json.loads(propose(joiner,m,a).stdout)['transfer_id']
    assert json.loads(status(joiner,m,d).stdout)['transfer'] is None
    assert propose(joiner,m,a,target=m.md,check=False).returncode
    for session in (a,c,d): assert decide(joiner,m,session,t,check=False).returncode
    assert decide(joiner,m,b,t,'cancel',check=False).returncode
    for query in ('SELECT * FROM postern.ownership_transfers','UPDATE postern.ownership_transfers SET state=\'accepted\'',
                  'SELECT postern.transfer_available(NULL)', 'SET ROLE postern_owner'):
        assert joiner.execute(query,check=False).returncode
    restricted=Postgres({**m.db.env,'PGUSER':'postern_test_reader'})
    assert status(restricted,m,a,check=False).returncode


@pytest.mark.parametrize('action,actor,state',[('cancel','a','cancelled'),('decline','b','declined')])
@pytest.mark.parametrize('role',['admin','member'])
def test_terminal_decisions_retry_without_side_effects(model,joiner,action,actor,state,role):
    m=model;a,b=login(m,'a'),login(m,'b');t=json.loads(propose(joiner,m,a,role=role).stdout)['transfer_id'];key=uid()
    session=a if actor=='a' else b
    first=json.loads(decide(joiner,m,session,t,action,key).stdout)
    assert first['state']==state and json.loads(decide(joiner,m,session,t,action,key).stdout)==first
    assert decide(joiner,m,b,t,check=False).returncode
    assert json.loads(status(joiner,m,a).stdout)['transfer'] is None
    assert m.db.scalar(f"SELECT owner_id FROM postern.spaces WHERE id='{m.s}';")==m.owner
    assert m.db.scalar(f"SELECT role FROM postern.memberships WHERE id='{m.ma}';")=='owner'
    assert m.db.scalar(f"SELECT role FROM postern.memberships WHERE id='{m.mb}';")=='member'
    assert json.loads(propose(joiner,m,a).stdout)['transfer_id']!=t


@pytest.mark.parametrize('change',['expiry','recipient_role','proposer_role','recipient_removed','suspended','generation','owner_changed','space_suspended'])
@pytest.mark.parametrize('role',['admin','member'])
def test_stale_proposals_cannot_be_accepted(model,joiner,change,role):
    m=model;a,b=login(m,'a'),login(m,'b');t=json.loads(propose(joiner,m,a,role=role).stdout)['transfer_id']
    queries={
      'expiry':f"UPDATE postern.ownership_transfers SET created_at=now()-interval '2 days',expires_at=now()-interval '1 day' WHERE id='{t}';",
      'recipient_role':f"UPDATE postern.memberships SET role='admin' WHERE id='{m.mb}'; UPDATE postern.memberships SET role='member' WHERE id='{m.mb}';",
      'proposer_role':f"BEGIN; UPDATE postern.memberships SET role='owner' WHERE id='{m.md}'; UPDATE postern.memberships SET role='admin' WHERE id='{m.ma}'; COMMIT;",
      'recipient_removed':f"UPDATE postern.memberships SET state='revoked' WHERE id='{m.mb}'; INSERT INTO postern.memberships(space_id,principal_id,role) VALUES('{m.s}','{m.b}','member');",
      'suspended':f"SELECT postern.disable_principal('{m.b}','suspended');",
      'generation':"UPDATE postern.authority SET generation=gen_random_uuid();",
      'owner_changed':f"UPDATE postern.spaces SET owner_id='{m.owner2}' WHERE id='{m.s}'; UPDATE postern.spaces SET owner_id='{m.owner}' WHERE id='{m.s}';",
      'space_suspended':f"UPDATE postern.spaces SET state='suspended' WHERE id='{m.s}'; UPDATE postern.spaces SET state='active' WHERE id='{m.s}';",
    }
    m.db.execute(queries[change])
    owner=m.db.scalar(f"SELECT owner_id FROM postern.spaces WHERE id='{m.s}';")
    memberships=m.db.rows(f"SELECT * FROM postern.memberships WHERE space_id='{m.s}' ORDER BY id")
    assert decide(joiner,m,b,t,check=False).returncode
    assert m.db.scalar(f"SELECT owner_id FROM postern.spaces WHERE id='{m.s}';")==owner
    assert m.db.rows(f"SELECT * FROM postern.memberships WHERE space_id='{m.s}' ORDER BY id")==memberships
    assert m.db.scalar(f"SELECT state FROM postern.ownership_transfers WHERE id='{t}';")!='accepted'
    if change not in ('generation','suspended'):
        assert json.loads(status(joiner,m,b).stdout)['transfer'] is None


@pytest.mark.parametrize('table',['activity','outbox'])
def test_failed_audit_rolls_back_entire_handoff(api,joiner,table):
    f=api;m=f.model;a,b=login(m,'a'),login(m,'b');ha,hb=f.session('a'),f.session('b');op=uid()
    with fail_events(m.db,table): assert propose(joiner,m,a,key=op,revision=0,check=False).returncode
    assert m.db.scalar(f"SELECT count(*) FROM postern.ownership_transfers WHERE space_id='{m.s}';")=='0'
    t=json.loads(propose(joiner,m,a,key=op,revision=0).stdout)['transfer_id'];key=uid()
    with fail_events(m.db,table):
        assert decide(joiner,m,b,t,key=key,check=False).returncode
        if table=='activity': assert status(joiner,m,b,check=False).returncode
    assert m.db.scalar(f"SELECT state FROM postern.ownership_transfers WHERE id='{t}';")=='pending'
    assert m.db.scalar(f"SELECT owner_id FROM postern.spaces WHERE id='{m.s}';")==m.owner
    assert m.db.scalar(f"SELECT count(*) FROM postern.owners WHERE principal_id='{m.b}';")=='0'
    for h in (ha,hb): assert f.client.get(f.url+'/resources',headers=h).status_code==200
    assert decide(joiner,m,b,t,key=key).returncode==0


def test_competing_decisions_and_duplicate_acceptance_are_serialized(model,joiner):
    m=model;a,b=login(m,'a'),login(m,'b');t=json.loads(propose(joiner,m,a).stdout)['transfer_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda v:decide(joiner,m,v[0],t,v[1],check=False),[(a,'cancel'),(b,'accept')]))
    assert sum(r.returncode==0 for r in results)==1
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE target_id='{t}' AND action IN ('ownership.accepted','ownership.cancelled');")=='1'
    # Independent proposal for duplicate successful acceptance with the same key.
    if m.db.scalar(f"SELECT state FROM postern.ownership_transfers WHERE id='{t}';")=='accepted':
        t=json.loads(propose(joiner,m,b,target=m.ma,role='member').stdout)['transfer_id'];session=a
    else: t=json.loads(propose(joiner,m,a,role='member').stdout)['transfer_id'];session=b
    key=uid()
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:decide(joiner,m,session,t,key=key),range(2)))
    assert results[0].stdout==results[1].stdout
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE target_id='{t}' AND action='ownership.accepted';")=='1'


def test_acceptance_waits_then_rechecks_and_cannot_revive_membership(model,joiner):
    m=model;a,b=login(m,'a'),login(m,'b');t=json.loads(propose(joiner,m,a).stdout)['transfer_id']
    with psycopg.connect(host=m.db.env['PGHOST'],port=int(m.db.env['PGPORT']),user=m.db.env['PGUSER'],dbname='postgres') as control:
        control.execute('SELECT 1 FROM postern.spaces WHERE id=%s FOR UPDATE',(m.s,))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(decide,joiner,m,b,t,check=False)
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                if m.db.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename='onboarding_test' AND wait_event_type='Lock');")=='t': break
                time.sleep(.02)
            else: raise AssertionError('acceptance did not reach deliberate lock')
            control.execute("UPDATE postern.memberships SET state='revoked' WHERE id=%s",(m.mb,));control.commit()
            assert pending.result(timeout=5).returncode
    assert m.db.scalar(f"SELECT owner_id FROM postern.spaces WHERE id='{m.s}';")==m.owner
