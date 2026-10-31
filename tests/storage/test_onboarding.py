"""Onboarding authorization, atomicity and races against real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import time
import uuid

import psycopg
import pytest

from postern.pg import Postgres, literal
from test_service_api import api, runtime


def uid(): return str(uuid.uuid4())


@pytest.fixture(scope='module')
def joiner(db):
    db.execute('CREATE ROLE onboarding_test LOGIN IN ROLE postern_onboarding_service;')
    return Postgres({**db.env,'PGUSER':'onboarding_test'})


def login(m,actor):
    value=uid();m.db.execute(f"SELECT postern.register_login('{value}','{getattr(m,actor)}',{int(time.time())+3600});")
    return value


def create(joiner,session,key=None,title='New space',check=True):
    h=hashlib.sha256(title.encode()).hexdigest()
    return joiner.execute(f"SELECT postern.create_space('{session}','{key or uid()}',{literal(title)},'{h}');",check=check)


def invite(f,actor='a',role='member',recipient='c',operation=None):
    value={'operation_id':operation or uid(),'recipient':{'authority':'fixture','account_id':getattr(f.model,recipient)},'role':role,'expires_in_seconds':3600}
    response=f.client.post(f.url+'/invitations',headers=f.session(actor),json=value)
    return response,value


def accept(joiner,session,invitation,check=True,**changes):
    inv={**invitation,**changes};h=hashlib.sha256(inv['secret'].encode()).hexdigest()
    return joiner.execute(f"SELECT postern.accept_invitation('{session}','{inv['space_id']}','{inv['invitation_id']}',decode('{h}','hex'));",check=check)


def preview(joiner,session,invitation,check=True,**changes):
    inv={**invitation,**changes};h=hashlib.sha256(inv['secret'].encode()).hexdigest()
    return joiner.execute(f"SELECT postern.preview_invitation('{session}','{inv['space_id']}','{inv['invitation_id']}',decode('{h}','hex'));",check=check)


def test_preview_discloses_only_to_recipient_without_joining(api,joiner):
    f=api;m=f.model;session=login(m,'c');inv=invite(f)[0].json()
    for other,changes in [(login(m,'b'),{}),(session,{'secret':'incorrect'}),(session,{'space_id':m.t}),(session,{'invitation_id':uid()})]:
        assert preview(joiner,other,inv,check=False,**changes).returncode
    value=json.loads(preview(joiner,session,inv).stdout)
    assert set(value)=={'space_id','invitation_id','title','role','state','already_member','expires_at'}
    assert value['space_id']==m.s and value['role']=='member' and value['already_member'] is False
    assert m.db.scalar(f"SELECT state FROM postern.invitations WHERE id='{inv['invitation_id']}';")=='pending'
    assert m.db.scalar(f"SELECT count(*) FROM postern.memberships WHERE space_id='{m.s}' AND principal_id='{m.c}';")=='0'
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE target_id='{inv['invitation_id']}' AND action='invitation.previewed';")=='1'
    # The internal composite-returning helper never becomes a runtime API.
    assert joiner.execute(f"SELECT postern.checked_invitation('{session}','{m.s}','{inv['invitation_id']}',NULL);",check=False).returncode
    with fail_events(m.db): assert preview(joiner,session,inv,check=False).returncode
    admin=invite(f,role='admin')[0].json()
    assert accept(joiner,session,inv).returncode==0
    value=json.loads(preview(joiner,session,admin).stdout)
    assert value['already_member'] is True and value['role']=='member'
    assert json.loads(preview(joiner,session,inv).stdout)['state']=='accepted'


@pytest.mark.parametrize('change',['revoke','expire','demote','generation'])
@pytest.mark.parametrize('role',['member','admin'])
def test_preview_is_not_an_acceptance_authorization(api,joiner,change,role):
    f=api;m=f.model;session=login(m,'c')
    if role=='admin': m.db.execute(f"UPDATE postern.memberships SET role='owner' WHERE id='{m.md}';")
    response,_=invite(f,actor='d',role=role);assert response.status_code==200,response.text
    inv=response.json()
    assert preview(joiner,session,inv).returncode==0
    if change=='revoke':
        assert f.client.post(f.url+'/invitations/'+inv['invitation_id']+'/revoke',headers=f.session('a'),json={'operation_id':uid()}).status_code==200
    elif change=='expire':
        m.db.execute(f"UPDATE postern.invitations SET created_at=now()-interval '2 days',expires_at=now()-interval '1 day' WHERE id='{inv['invitation_id']}';")
    elif change=='demote': m.db.execute(f"UPDATE postern.memberships SET role='member' WHERE id='{m.md}';")
    else: m.db.execute('UPDATE postern.authority SET generation=gen_random_uuid();')
    assert preview(joiner,session,inv,check=False).returncode
    assert accept(joiner,session,inv,check=False).returncode
    assert m.db.scalar(f"SELECT count(*) FROM postern.memberships WHERE space_id='{m.s}' AND principal_id='{m.c}';")=='0'


@contextmanager
def fail_events(db,table='activity'):
    db.execute(f"CREATE FUNCTION postern.fail_onboarding() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic failure'; END $$; CREATE TRIGGER fail_onboarding BEFORE INSERT ON postern.{table} FOR EACH ROW EXECUTE FUNCTION postern.fail_onboarding();")
    try: yield
    finally: db.execute(f'DROP TRIGGER fail_onboarding ON postern.{table}; DROP FUNCTION postern.fail_onboarding();')


def test_creation_requires_entitlement_and_is_atomic(model,joiner):
    m=model;session=login(m,'b');key=uid()
    assert 'creation not enabled' in create(joiner,session,key,check=False).stderr
    m.db.execute(f"INSERT INTO postern.creation_entitlements VALUES('{m.b}',2);")
    first=json.loads(create(joiner,session,key).stdout)
    assert json.loads(create(joiner,session,key).stdout)==first
    assert create(joiner,session,key,title='Different',check=False).returncode
    second=json.loads(create(joiner,session).stdout)
    assert second['owner_id']==first['owner_id'] and second['space_id']!=first['space_id']
    assert 'creation limit reached' in create(joiner,session,check=False).stderr
    assert m.db.scalar(f"SELECT count(*) FROM postern.space_creations WHERE principal_id='{m.b}';")=='2'
    assert m.db.scalar(f"SELECT role FROM postern.memberships WHERE space_id='{first['space_id']}' AND principal_id='{m.b}';")=='owner'
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{first['space_id']}' AND action='space.created';")=='1'
    assert m.db.scalar(f"SELECT count(*) FROM postern.outbox WHERE space_id='{first['space_id']}';")=='1'
    # The role cannot approve itself or inspect another account's records.
    for query in ('SELECT * FROM postern.creation_entitlements',f"INSERT INTO postern.creation_entitlements VALUES('{m.c}',10)",
                  'SELECT * FROM postern.invitations','SELECT * FROM postern.account_logins','SET ROLE postern_owner'):
        assert joiner.execute(query,check=False).returncode


def test_concurrent_creation_respects_account_limit(model,joiner):
    m=model;session=login(m,'b');m.db.execute(f"INSERT INTO postern.creation_entitlements VALUES('{m.b}',1);")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:create(joiner,session,check=False),range(4)))
    assert sum(r.returncode==0 for r in results)==1
    assert all(r.returncode==0 or 'creation limit reached' in r.stderr for r in results)
    assert m.db.scalar(f"SELECT count(*) FROM postern.space_creations WHERE principal_id='{m.b}';")=='1'


@pytest.mark.parametrize('table',['activity','outbox'])
def test_creation_failure_rolls_back_owner_quota_and_space(model,joiner,table):
    m=model;session=login(m,'b');key=uid();m.db.execute(f"INSERT INTO postern.creation_entitlements VALUES('{m.b}',1);")
    with fail_events(m.db,table): assert create(joiner,session,key,check=False).returncode
    assert m.db.scalar(f"SELECT count(*) FROM postern.owners WHERE principal_id='{m.b}';")=='0'
    assert m.db.scalar(f"SELECT count(*) FROM postern.space_creations WHERE principal_id='{m.b}';")=='0'
    assert create(joiner,session,key).returncode==0


def test_invite_role_recipient_scope_and_secret_storage(api,joiner):
    f=api;m=f.model
    assert invite(f,actor='b')[0].status_code==403
    assert invite(f,actor='d',role='admin')[0].status_code==403
    response,body=invite(f,operation=uid());assert response.status_code==200,response.text
    inv=response.json();h=f.session('a')
    again=f.client.post(f.url+'/invitations',headers=h,json=body)
    assert again.json()==inv # lost response, even after a new space session
    assert f.client.post(f.url+'/invitations',headers=h,json={**body,'role':'admin'}).status_code==409
    listing=f.client.get(f.url+'/invitations',headers=h)
    assert listing.status_code==200 and 'secret' not in listing.text and inv['secret'] not in listing.text
    assert f.client.get(f.url+'/invitations',headers=f.session()).status_code==403
    assert accept(joiner,login(m,'d'),inv,check=False).returncode # forwarded secret
    assert accept(joiner,login(m,'c'),inv,space_id=m.t,check=False).returncode
    assert m.db.scalar(f"SELECT encode(secret_hash,'hex') FROM postern.invitations WHERE id='{inv['invitation_id']}';")==hashlib.sha256(inv['secret'].encode()).hexdigest()
    stored=m.db.scalar(f"SELECT result::text FROM postern.operations WHERE space_id='{m.s}'; SELECT detail::text FROM postern.activity WHERE space_id='{m.s}'; SELECT payload::text FROM postern.outbox WHERE space_id='{m.s}';")
    assert inv['secret'] not in stored


def test_acceptance_retry_race_never_duplicates_or_upgrades(api,joiner):
    f=api;m=f.model;session=login(m,'c')
    member=invite(f)[0].json();admin=invite(f,role='admin')[0].json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:json.loads(accept(joiner,session,member).stdout),range(2)))
    assert results[0]==results[1] and results[0]['role']=='member'
    promoted=json.loads(accept(joiner,session,admin).stdout)
    assert promoted==results[0]
    assert m.db.scalar(f"SELECT count(*) FROM postern.memberships WHERE space_id='{m.s}' AND principal_id='{m.c}' AND state='active';")=='1'
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='membership.joined';")=='1'
    assert f.client.get(f.url+'/resources',headers=f.session('c')).json()['resources']==[]
    own=json.loads(joiner.scalar(f"SELECT postern.my_spaces('{session}',NULL);"))
    assert {s['space_id'] for s in own['spaces']}=={m.s,m.t}
    assert own['account_reference']=={'authority':'fixture','account_id':m.c}


def test_invite_revocation_expiry_generation_and_inviter_changes(api,joiner):
    f=api;m=f.model;session=login(m,'c');h=f.session('a')
    inv=invite(f)[0].json();path=f.url+'/invitations/'+inv['invitation_id']+'/revoke';op={'operation_id':uid()}
    first=f.client.post(path,headers=h,json=op);assert first.status_code==200
    assert f.client.post(path,headers=h,json=op).json()==first.json()
    assert accept(joiner,session,inv,check=False).returncode
    expired=invite(f)[0].json()
    m.db.execute(f"UPDATE postern.invitations SET created_at=now()-interval '2 days',expires_at=now()-interval '1 day' WHERE id='{expired['invitation_id']}';")
    assert accept(joiner,session,expired,check=False).returncode
    demoted=invite(f,actor='d')[0].json()
    m.db.execute(f"UPDATE postern.memberships SET role='member' WHERE id='{m.md}'; UPDATE postern.memberships SET role='admin' WHERE id='{m.md}';")
    assert accept(joiner,session,demoted,check=False).returncode
    restored=invite(f)[0].json();m.db.execute("UPDATE postern.authority SET generation=gen_random_uuid();")
    assert accept(joiner,login(m,'c'),restored,check=False).returncode


def test_readmission_requires_new_invite_and_membership(api,joiner):
    f=api;m=f.model;session=login(m,'c')
    first=invite(f)[0].json();pending=invite(f)[0].json()
    original=json.loads(accept(joiner,session,first).stdout);old=f.session('c')
    revoked=f.client.post(f.url+'/memberships/'+original['membership_id']+'/revoke',headers=f.session('a'),json={'operation_id':uid()})
    assert revoked.status_code==200
    assert accept(joiner,session,first,check=False).returncode
    assert accept(joiner,session,pending,check=False).returncode
    renewed=invite(f)[0].json();new=json.loads(accept(joiner,session,renewed).stdout)
    assert new['membership_id']!=original['membership_id']
    assert f.client.get(f.url+'/resources',headers=old).status_code==401


@pytest.mark.parametrize('table',['activity','outbox'])
def test_acceptance_rolls_back_on_required_record_failure(api,joiner,table):
    f=api;m=f.model;session=login(m,'c');inv=invite(f)[0].json()
    with fail_events(m.db,table): assert accept(joiner,session,inv,check=False).returncode
    assert m.db.scalar(f"SELECT state FROM postern.invitations WHERE id='{inv['invitation_id']}';")=='pending'
    assert m.db.scalar(f"SELECT count(*) FROM postern.memberships WHERE space_id='{m.s}' AND principal_id='{m.c}';")=='0'
    assert accept(joiner,session,inv).returncode==0


def test_pending_invites_are_bounded_and_invalid_input_is_refused(api):
    f=api;m=f.model;m.db.execute('UPDATE postern.onboarding_policy SET max_pending_invitations=1;')
    try:
        assert invite(f)[0].status_code==200
        assert invite(f)[0].status_code==409
        _,body=invite(f)
        for patch in ({'role':'owner'},{'expires_in_seconds':True},{'expires_in_seconds':604801},{'recipient':{'email':'person@example.invalid'}}):
            assert f.client.post(f.url+'/invitations',headers=f.session('a'),json={**body,**patch}).status_code==422
    finally: m.db.execute('UPDATE postern.onboarding_policy SET max_pending_invitations=100;')


def test_waiting_acceptance_rechecks_invitation_revocation(api,joiner):
    f=api;m=f.model;session=login(m,'c');inv=invite(f)[0].json()
    with psycopg.connect(host=m.db.env['PGHOST'],port=int(m.db.env['PGPORT']),user=m.db.env['PGUSER'],dbname='postgres') as control:
        control.execute('SELECT 1 FROM postern.spaces WHERE id=%s FOR UPDATE',(m.s,))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(accept,joiner,session,inv,False)
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                if m.db.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename='onboarding_test' AND wait_event_type='Lock');")=='t': break
                time.sleep(.02)
            else: raise AssertionError('acceptance did not reach deliberate lock')
            control.execute("UPDATE postern.invitations SET state='revoked' WHERE id=%s",(inv['invitation_id'],));control.commit()
            result=pending.result(timeout=5)
            assert result.returncode and 'invitation unavailable' in result.stderr
    assert m.db.scalar(f"SELECT count(*) FROM postern.memberships WHERE space_id='{m.s}' AND principal_id='{m.c}';")=='0'


def test_upgrade_from_two_revokes_unissued_pending_invites(db):
    from pathlib import Path
    from postern.pg import apply_schema
    name='onboarding_upgrade_'+uuid.uuid4().hex;db.execute(f'CREATE DATABASE {name};');old=Postgres(db.env,name)
    for number,file in [(1,'001_spaces.sql'),(2,'002_service_sessions.sql')]:
        source=Path(__file__).resolve().parents[2]/'postern/sql'/file
        digest=hashlib.sha256(source.read_bytes()).hexdigest()
        old.execute('BEGIN;'+source.read_text()+f"INSERT INTO postern.schema_migrations(version,sha256) VALUES({number},'{digest}'); COMMIT;")
    p,o,s,m,i=[uid() for _ in range(5)]
    old.execute(f"""BEGIN;
      INSERT INTO postern.principals(id,authority,account_id) VALUES('{p}','old-namespace','preserved');
      INSERT INTO postern.owners(id,kind,principal_id,label) VALUES('{o}','person','{p}','Old owner');
      INSERT INTO postern.spaces(id,owner_id,title,state) VALUES('{s}','{o}','Existing space','active');
      INSERT INTO postern.memberships(space_id,id,principal_id,role) VALUES('{s}','{m}','{p}','owner');
      INSERT INTO postern.invitations(space_id,id,inviter_id,recipient_id,role,secret_hash,expires_at)
      VALUES('{s}','{i}','{m}','{p}','member',decode(repeat('a',64),'hex'),now()+interval '1 day'); COMMIT;""")
    generation=old.scalar('SELECT generation FROM postern.authority;')
    apply_schema(old)
    assert old.scalar('SELECT generation FROM postern.authority;')==generation
    assert old.scalar('SELECT title FROM postern.spaces;')=='Existing space'
    assert old.scalar('SELECT state FROM postern.invitations;')=='revoked'
    assert old.scalar('SELECT generation FROM postern.invitations;')==generation
    assert old.scalar('SELECT count(*) FROM postern.creation_entitlements;')=='0'


def test_my_spaces_pages_and_rechecks_membership(model,joiner):
    m=model;session=login(m,'b')
    m.db.execute(f"BEGIN; INSERT INTO postern.spaces(owner_id,title,state) SELECT '{m.owner}','Space '||n,'active' FROM generate_series(1,55) n; INSERT INTO postern.memberships(space_id,principal_id,role) SELECT id,'{m.a}','owner' FROM postern.spaces WHERE owner_id='{m.owner}' AND title LIKE 'Space %'; INSERT INTO postern.memberships(space_id,principal_id,role) SELECT id,'{m.b}','member' FROM postern.spaces WHERE owner_id='{m.owner}' AND title LIKE 'Space %'; COMMIT;")
    first=json.loads(joiner.scalar(f"SELECT postern.my_spaces('{session}',NULL);"))
    assert len(first['spaces'])==50 and first['next_after']
    second=json.loads(joiner.scalar(f"SELECT postern.my_spaces('{session}','{first['next_after']}');"))
    assert len(second['spaces'])==6 and second['next_after'] is None
    assert not {x['space_id'] for x in first['spaces']} & {x['space_id'] for x in second['spaces']}
    assert m.t not in {x['space_id'] for x in first['spaces']+second['spaces']}


def test_invitation_rejects_unpaired_surrogate_without_server_error(api):
    f=api
    body={'operation_id':uid(),'recipient':{'authority':'bad\ud800','account_id':f.model.c},'role':'member','expires_in_seconds':3600}
    response=f.client.post(f.url+'/invitations',headers={**f.session('a'),'Content-Type':'application/json'},content=json.dumps(body))
    assert response.status_code==422
