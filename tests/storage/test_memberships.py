"""Live PostgreSQL membership authority, disclosure, concurrency and recovery."""
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import time
import psycopg
import pytest
from postern.pg import Postgres
from test_service_api import api, runtime
from test_onboarding import joiner, login, uid, fail_events, invite, accept


def listing(f, actor='a'):
    return f.client.get(f.url+'/memberships',headers=f.session(actor))


def change(f, member=None, role='admin', actor='a', operation=None, revision=None, remove=False, headers=None):
    value={'operation_id':operation or uid(),'expected_revision':int(f.model.db.scalar(f"SELECT policy_revision FROM postern.spaces WHERE id='{f.model.s}';")) if revision is None else revision}
    if not remove: value['role']=role
    return f.client.post(f.url+'/memberships/'+(member or f.model.mb)+('/remove' if remove else '/role'),headers=headers or f.session(actor),json=value)


def leave(joiner,session,space,member,key=None,check=True):
    fingerprint=hashlib.sha256(f"leave:{space}:{member}".encode()).hexdigest()
    return joiner.execute(f"SELECT postern.leave_space('{session}','{space}','{member}','{key or uid()}','{fingerprint}');",check=check)


def test_roster_is_scoped_audited_and_not_a_member_directory(api):
    f=api;m=f.model
    value=listing(f);assert value.status_code==200,value.text
    assert value.headers['cache-control']=='no-store'
    rows=value.json()['members'];assert {r['membership_id'] for r in rows}=={m.ma,m.mb,m.md}
    assert all(set(r)=={'membership_id','role','authority','account_id','is_self','can_change_role','can_remove'} for r in rows)
    assert m.c not in value.text and 'email' not in value.text
    owner=next(r for r in rows if r['is_self']);assert not owner['can_remove'] and not owner['can_change_role']
    admin=listing(f,'d').json();assert all(not r['can_change_role'] for r in admin['members'])
    assert [r['membership_id'] for r in admin['members'] if r['can_remove']]==[m.mb]
    assert listing(f,'b').status_code==403
    assert f.client.get(f'/v1/spaces/{m.t}/memberships',headers=f.session('a')).status_code==401
    h=f.session('a')
    with fail_events(m.db):
        failed=f.client.get(f.url+'/memberships',headers=h);assert failed.status_code==503 and m.b not in failed.text


def test_owner_role_change_is_atomic_live_and_idempotent(api):
    f=api;m=f.model;old=f.session('b');key=uid();rev=0
    assert change(f,actor='b').status_code==403
    assert change(f,actor='d').status_code==403
    assert change(f,member=m.ma).status_code==403
    assert change(f,role='owner').status_code==422
    assert change(f,member=m.mt).status_code==404
    first=change(f,operation=key,revision=rev);assert first.status_code==200,first.text
    assert first.json()['role']=='admin'
    assert change(f,operation=key,revision=rev).json()==first.json()
    assert change(f,operation=key,role='member',revision=rev).status_code==409
    assert f.client.get(f.url+'/resources',headers=old).status_code==401
    assert listing(f,'b').status_code==200
    assert change(f,member=m.md,revision=rev).status_code==409
    fresh=f.session('b');pending=invite(f,actor='b')[0].json()
    assert change(f,role='member').status_code==200
    assert f.client.get(f.url+'/memberships',headers=fresh).status_code==401
    assert listing(f,'b').status_code==403
    assert m.db.scalar(f"SELECT state FROM postern.invitations WHERE id='{pending['invitation_id']}';")=='revoked'
    # Replaying a prior promotion receipt cannot promote a subsequently demoted member.
    assert change(f,operation=key,revision=rev).json()==first.json()
    assert m.db.scalar(f"SELECT role FROM postern.memberships WHERE id='{m.mb}';")=='member'


@pytest.mark.parametrize('table',['activity','outbox'])
def test_role_change_rollback_preserves_session_and_invitation(api,table):
    f=api;m=f.model;old=f.session('d');pending=invite(f,actor='d')[0].json();revision=int(m.db.scalar(f"SELECT policy_revision FROM postern.spaces WHERE id='{m.s}';"))
    key=uid();owner=f.session('a')
    with fail_events(m.db,table): assert change(f,member=m.md,role='member',operation=key,revision=revision,headers=owner).status_code==503
    assert m.db.scalar(f"SELECT role FROM postern.memberships WHERE id='{m.md}';")=='admin'
    assert m.db.scalar(f"SELECT state FROM postern.invitations WHERE id='{pending['invitation_id']}';")=='pending'
    assert f.client.get(f.url+'/memberships',headers=old).status_code==200
    assert change(f,member=m.md,role='member',operation=key,revision=revision).status_code==200


def test_remove_has_current_role_boundaries_and_stale_revision_guard(api):
    f=api;m=f.model;old=f.session('b');key=uid()
    assert change(f,remove=True,actor='d',member=m.ma).status_code==403
    assert change(f,remove=True,actor='d',member=m.md).status_code==403
    assert change(f,remove=True,member=m.ma).status_code==403 # use self-departure
    assert change(f,remove=True,revision=999).status_code==409
    first=change(f,remove=True,actor='d',operation=key,revision=0);assert first.status_code==200,first.text
    assert change(f,remove=True,actor='d',operation=key,revision=0).json()==first.json()
    assert f.client.get(f.url+'/resources',headers=old).status_code==401
    assert m.db.scalar(f"SELECT count(*) FROM postern.audience_members WHERE membership_id='{m.mb}';")=='0'
    assert m.db.scalar(f"SELECT count(*) FROM postern.grants WHERE membership_id='{m.mb}' AND state='active';")=='0'
    assert {r['membership_id'] for r in listing(f).json()['members']}=={m.ma,m.md}


def test_leave_uses_live_account_and_exact_membership_after_readmission(api,joiner):
    f=api;m=f.model;session=login(m,'b');old=f.session('b');key=uid()
    assert leave(joiner,session,m.s,m.ma,check=False).returncode
    assert leave(joiner,session,m.t,m.mb,check=False).returncode
    first=json.loads(leave(joiner,session,m.s,m.mb,key).stdout)
    assert json.loads(leave(joiner,session,m.s,m.mb,key).stdout)==first
    assert f.client.get(f.url+'/resources',headers=old).status_code==401
    assert leave(joiner,session,m.s,m.mb,check=False).returncode
    inv=invite(f,recipient='b')[0].json();new=json.loads(accept(joiner,session,inv).stdout)
    assert new['membership_id']!=m.mb
    assert json.loads(leave(joiner,session,m.s,m.mb,key).stdout)==first
    assert m.db.scalar(f"SELECT state FROM postern.memberships WHERE id='{new['membership_id']}';")=='active'
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE target_id='{m.mb}' AND action='membership.left';")=='1'
    assert f.client.get(f.url+'/resources',headers=f.session('b')).json()['resources']==[]
    m.db.execute(f"UPDATE postern.account_logins SET revoked=true WHERE id='{session}';")
    assert leave(joiner,session,m.s,m.mb,key,check=False).returncode


@pytest.mark.parametrize('table',['activity','outbox'])
def test_leave_rollback_restores_all_access_and_last_owner_is_protected(api,joiner,table):
    f=api;m=f.model;owner=login(m,'a');member=login(m,'b');h=f.session('b');key=uid()
    assert 'active space requires an active owner' in leave(joiner,owner,m.s,m.ma,check=False).stderr
    assert f.client.get(f.url+'/session',headers=f.session('a')).json()['can_leave'] is False
    assert f.client.get(f.url+'/session',headers=h).json()['can_leave'] is True
    with fail_events(m.db,table): assert leave(joiner,member,m.s,m.mb,key,check=False).returncode
    assert m.db.scalar(f"SELECT state FROM postern.memberships WHERE id='{m.mb}';")=='active'
    assert len(f.client.get(f.url+'/resources',headers=h).json()['resources'])==2
    assert leave(joiner,member,m.s,m.mb,key).returncode==0


def test_two_owners_cannot_both_leave(api,joiner):
    f=api;m=f.model;m.db.execute(f"UPDATE postern.memberships SET role='owner' WHERE id='{m.md}';")
    a,d=login(m,'a'),login(m,'d')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(leave,joiner,a,m.s,m.ma,None,False),pool.submit(leave,joiner,d,m.s,m.md,None,False)]
        results=[x.result(timeout=10) for x in futures]
    assert sum(r.returncode==0 for r in results)==1
    assert m.db.scalar(f"SELECT count(*) FROM postern.memberships WHERE space_id='{m.s}' AND role='owner' AND state='active';")=='1'


def test_waiting_role_change_rechecks_caller_authority(api):
    f=api;m=f.model;h=f.session('a')
    # Promote another owner so a privileged fixture may demote the caller.
    m.db.execute(f"UPDATE postern.memberships SET role='owner' WHERE id='{m.md}';")
    with psycopg.connect(host=m.db.env['PGHOST'],port=int(m.db.env['PGPORT']),user=m.db.env['PGUSER'],dbname='postgres') as control:
        control.execute('SELECT 1 FROM postern.spaces WHERE id=%s FOR UPDATE',(m.s,))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(f.client.post,f.url+'/memberships/'+m.mb+'/role',headers=h,json={'operation_id':uid(),'expected_revision':0,'role':'admin'})
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                if m.db.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename='resource_api_test' AND wait_event_type='Lock');")=='t':break
                time.sleep(.02)
            else: raise AssertionError('mutation did not reach deliberate lock')
            control.execute("UPDATE postern.memberships SET role='member' WHERE id=%s",(m.ma,));control.commit()
            assert pending.result(timeout=5).status_code==403
    assert m.db.scalar(f"SELECT role FROM postern.memberships WHERE id='{m.mb}';")=='member'


def test_member_pagination_is_bounded_and_bound_to_policy_and_actor(api):
    f=api;m=f.model;owner=f.session('a')
    m.db.execute(f"WITH p AS (INSERT INTO postern.principals(authority,account_id) SELECT 'page-{m.s}',n::text FROM generate_series(1,55) n RETURNING id) INSERT INTO postern.memberships(space_id,principal_id,role) SELECT '{m.s}',id,'member' FROM p;")
    first=f.client.get(f.url+'/memberships',headers=owner).json();assert len(first['members'])==50 and first['next_cursor']
    cursor=first['next_cursor'];second=f.client.get(f.url+'/memberships',headers=owner,params={'cursor':cursor}).json()
    assert len(second['members'])==8 and second['next_cursor'] is None
    assert not {x['membership_id'] for x in first['members']} & {x['membership_id'] for x in second['members']}
    assert f.client.get(f.url+'/memberships',headers=f.session('d'),params={'cursor':cursor}).status_code==422
    assert f.client.get(f.url+'/resources',headers=owner,params={'cursor':cursor}).status_code==422
    assert change(f,revision=first['policy_revision']).status_code==200
    assert f.client.get(f.url+'/memberships',headers=owner,params={'cursor':cursor}).status_code==422


def test_mutation_body_validation_and_concurrent_revision(api):
    f=api;m=f.model;owner=f.session('a');path=f.url+'/memberships/'+m.mb+'/role'
    body={'operation_id':uid(),'expected_revision':0,'role':'admin'}
    for patch in [{'expected_revision':True},{'expected_revision':-1},{'expected_revision':2**53},{'role':'owner'},{'principal_id':m.a}]:
        assert f.client.post(path,headers=owner,json={**body,**patch}).status_code==422
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:f.client.post(path,headers=owner,json={**body,'operation_id':uid()}),range(2)))
    assert sorted(r.status_code for r in responses)==[200,409]
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE target_id='{m.mb}' AND action='membership.role_changed';")=='1'
