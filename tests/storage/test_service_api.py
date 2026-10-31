"""Real PostgreSQL, restricted login, actual FastAPI routes and signed handoffs.

No account implementation is distributed here: the signer is a synthetic authority.
The private account project separately runs the two-service integration contract.
"""
import hashlib
import secrets
import time
import uuid
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
import jwt
import pytest

from postern.app import create_app
from postern.config import Config
from postern.pg import Postgres
from postern.spaces import SpaceService, SpaceTrust
from postern.storage import FakeStore


@pytest.fixture(scope='module')
def runtime(db):
    db.execute('CREATE ROLE resource_api_test LOGIN IN ROLE postern_resource_service;')
    return {k: v for k, v in dict(host=db.env['PGHOST'], port=int(db.env['PGPORT']), user='resource_api_test', dbname='postgres').items()}


@pytest.fixture
def api(model, runtime, tmp_path):
    m = model
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    trust = SpaceTrust('urn:fixture:accounts', 'urn:fixture:resources', 'test-key', public, secrets.token_bytes(32))
    service = SpaceService(runtime, trust, max_size=1, invitation_key=secrets.token_bytes(32))
    store = FakeStore(tmp_path/'cloud')
    store.write_allowlist([['email', 'name', 'role']])
    f = SimpleNamespace(model=m, service=service, key=key, trust=trust)
    def assertion(actor='b', space=None, changes=None, headers=None):
        login = str(uuid.uuid4()); now = int(time.time())
        m.db.execute(f"SELECT postern.register_login('{login}','{getattr(m,actor)}',{now+3600});")
        claims = dict(iss=trust.issuer, aud=trust.audience, sub=getattr(m, actor), iat=now, nbf=now, exp=now+60,
                      jti=str(uuid.uuid4()), login_id=login, space_id=space or m.s, generation=m.generation,
                      account_authority='fixture', account_id=getattr(m, actor))
        claims['membership_id']=m.db.scalar(f"SELECT id FROM postern.memberships WHERE space_id='{space or m.s}' AND principal_id='{getattr(m,actor)}' AND state='active';") or str(uuid.uuid4())
        claims.update(changes or {})
        return jwt.encode(claims, key, algorithm='EdDSA', headers=headers or {'kid': trust.kid, 'typ': 'postern-space+jwt'})
    f.assertion = assertion
    with TestClient(create_app(Config(data_dir=tmp_path/'data', mode='demo'), store=store, spaces=service)) as client:
        f.client = client
        f.url = f'/v1/spaces/{m.s}'
        def session(actor='b', space=None):
            response = client.post(f'/v1/spaces/{space or m.s}/session/exchange', json={'assertion': assertion(actor, space)})
            assert response.status_code == 200, response.text
            return {'Authorization': 'Bearer '+response.json()['token']}
        f.session = session
        yield f


def test_native_catalog_and_private_boundaries(api, runtime):
    f=api; m=f.model; h=f.session()
    response=f.client.get(f.url+'/resources', headers=h)
    assert response.status_code==200
    assert {r['title'] for r in response.json()['resources']}=={'Visible คู่มือพนักงาน','Audience'}
    assert response.headers['cache-control']=='no-store'
    assert 'binding' not in response.text and 'fixture' not in response.text
    assert f.client.get(f.url+'/resources',params={'query':'Visible'},headers=h).json()['resources'][0]['id']==m.r
    assert f.client.get(f.url+'/resources',params={'query':"'; SELECT secret --"},headers=h).json()['resources']==[]
    assert f.client.get(f.url+'/resources/'+m.r,headers=h).status_code==200
    for resource in (m.hidden,m.foreign,str(uuid.uuid4())):
        assert f.client.get(f.url+'/resources/'+resource,headers=h).status_code==404
    assert f.client.get(f'/v1/spaces/{m.t}/resources',headers=h).status_code==401
    assert f.client.get('/api/session',headers=h).status_code==401
    assert f.client.get(f.url+'/resources',headers={'Authorization':'Bearer pa_'+secrets.token_urlsafe(32)}).status_code==401
    # Runtime cannot forge context or bypass the audited functions with raw SQL.
    db=Postgres({**m.db.env,'PGUSER':runtime['user']})
    for query in ('SELECT * FROM postern.principals', 'SELECT title FROM postern.resources',
                  'SELECT * FROM postern.resource_sessions', 'SELECT postern.open_resource_request(NULL,NULL,NULL)',
                  'SET ROLE postern_owner', "SELECT postern.register_principal('x','y')"):
        assert db.execute(query,check=False).returncode


@pytest.mark.parametrize('changes', [dict(aud='elsewhere'),dict(iss='elsewhere'),dict(exp=1),
    dict(exp=int(time.time())+600),dict(account_id='wrong'),dict(generation=str(uuid.uuid4())),dict(sub=str(uuid.uuid4()))])
def test_handoff_rejects_bad_claims(api,changes):
    response=api.client.post(api.url+'/session/exchange',json={'assertion':api.assertion(changes=changes)})
    assert response.status_code==401
    assert 'token' not in response.json()


def test_handoff_replay_logout_and_live_login(api):
    f=api; assertion=f.assertion()
    response=f.client.post(f.url+'/session/exchange',json={'assertion':assertion}); assert response.status_code==200
    h={'Authorization':'Bearer '+response.json()['token']}
    assert f.client.post(f.url+'/session/exchange',json={'assertion':assertion}).status_code==401
    assert f.client.post(f.url+'/session/signout',headers=h).status_code==200
    assert f.client.get(f.url+'/resources',headers=h).status_code==401
    h=f.session(); raw=h['Authorization'][7:]; digest=hashlib.sha256(raw.encode()).hexdigest()
    f.model.db.execute(f"UPDATE postern.account_logins SET revoked=true WHERE id=(SELECT login_id FROM postern.resource_sessions WHERE token_hash='{digest}');")
    assert f.client.get(f.url+'/resources',headers=h).status_code==401
    # Pool size one: success after rejected transactions uses a clean context.
    assert f.client.get(f.url+'/resources',headers=f.session('a')).status_code==200


def test_cursor_binding_and_policy_revision(api):
    f=api; m=f.model
    m.db.execute(f"INSERT INTO postern.resources(space_id,kind,title,state,adapter) SELECT '{m.s}','document','Visible '||n,'published','test' FROM generate_series(1,55) n;")
    h=f.session('a'); first=f.client.get(f.url+'/resources',headers=h).json(); cursor=first['next_cursor']
    assert len(first['resources'])==50 and cursor
    second=f.client.get(f.url+'/resources',headers=h,params={'cursor':cursor}); assert second.status_code==200
    assert not {r['id'] for r in first['resources']} & {r['id'] for r in second.json()['resources']}
    assert f.client.get(f.url+'/resources',headers=f.session('b'),params={'cursor':cursor}).status_code==422
    assert f.client.get(f.url+'/resources',headers=h,params={'cursor':cursor,'query':'changed'}).status_code==422
    m.db.execute(f"UPDATE postern.spaces SET policy_revision=policy_revision+1 WHERE id='{m.s}';")
    assert f.client.get(f.url+'/resources',headers=h,params={'cursor':cursor}).status_code==422


def test_atomic_revoke_and_required_audit(api):
    f=api;m=f.model;owner=f.session('a');member=f.session();operation={'operation_id':str(uuid.uuid4())}
    path=f.url+'/memberships/'+m.mb+'/revoke'
    assert f.client.post(path,json=operation,headers=member).status_code==403
    last=f.client.post(f.url+'/memberships/'+m.ma+'/revoke',json=operation,headers=owner)
    assert last.status_code==409
    assert f.client.get(f.url+'/resources',headers=owner).status_code==200
    m.db.execute("CREATE FUNCTION postern.fail_service_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected audit failure'; END $$; CREATE TRIGGER fail_service_audit BEFORE INSERT ON postern.activity FOR EACH ROW EXECUTE FUNCTION postern.fail_service_audit();")
    try:
        denied=f.client.get(f.url+'/resources',headers=member)
        assert denied.status_code==503 and 'Visible' not in denied.text
        assert f.client.post(path,json=operation,headers=owner).status_code==503
        assert m.db.scalar(f"SELECT state FROM postern.memberships WHERE id='{m.mb}';")=='active'
    finally: m.db.execute('DROP TRIGGER fail_service_audit ON postern.activity; DROP FUNCTION postern.fail_service_audit();')
    first=f.client.post(path,json=operation,headers=owner)
    assert first.status_code==200,first.text
    assert f.client.post(path,json=operation,headers=owner).json()==first.json()
    assert f.client.get(f.url+'/resources',headers=member).status_code==401
    assert f.client.get(f.url+'/resources',headers=owner).status_code==200


def test_restore_generation_fences_sessions_and_assertions(api):
    f=api; h=f.session(); assertion=f.assertion()
    f.model.db.execute("UPDATE postern.authority SET generation=gen_random_uuid();")
    assert f.client.get(f.url+'/resources',headers=h).status_code==401
    assert f.client.post(f.url+'/session/exchange',json={'assertion':assertion}).status_code==401


def test_readmission_never_revives_old_credentials(api):
    f=api;m=f.model;old=f.session();pending=f.assertion();owner=f.session('a')
    response=f.client.post(f.url+'/memberships/'+m.mb+'/revoke',headers=owner,json={'operation_id':str(uuid.uuid4())})
    assert response.status_code==200
    m.db.execute(f"INSERT INTO postern.memberships(space_id,principal_id,role) VALUES('{m.s}','{m.b}','member');")
    assert f.client.get(f.url+'/resources',headers=old).status_code==401
    assert f.client.post(f.url+'/session/exchange',json={'assertion':pending}).status_code==401
    assert f.client.get(f.url+'/resources',headers=f.session()).status_code==200


def test_pool_clears_context_after_commit_and_rollback(api):
    f=api;h=f.session('a')
    assert f.client.get(f.url+'/resources',headers=h).status_code==200
    async def settings():
        async with f.service.pool.connection() as conn:
            return await (await conn.execute("SELECT nullif(current_setting('postern.actor_id',true),''),nullif(current_setting('postern.space_id',true),''),nullif(current_setting('postern.generation',true),'')")).fetchone()
    assert f.client.portal.call(settings)==(None,None,None)
    result=f.client.post(f.url+'/memberships/'+f.model.ma+'/revoke',headers=h,json={'operation_id':str(uuid.uuid4())})
    assert result.status_code==409
    assert f.client.portal.call(settings)==(None,None,None)


@pytest.mark.parametrize('mutation',['membership','account'])
def test_waiting_request_rechecks_revocation_after_lock(api,runtime,mutation):
    from concurrent.futures import ThreadPoolExecutor
    import psycopg
    f=api;m=f.model;h=f.session()
    with psycopg.connect(host=m.db.env['PGHOST'],port=int(m.db.env['PGPORT']),user=m.db.env['PGUSER'],dbname='postgres') as control:
        table,column,value=('spaces','id',m.s) if mutation=='membership' else ('principals','id',m.b)
        control.execute(f'SELECT 1 FROM postern.{table} WHERE {column}=%s FOR UPDATE',(value,))
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending=executor.submit(f.client.get,f.url+'/resources',headers=h)
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                waiting=m.db.scalar(f"SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename='{runtime['user']}' AND wait_event_type='Lock');")
                if waiting=='t': break
                time.sleep(.02)
            else: raise AssertionError('request never reached the deliberate lock')
            if mutation=='membership':
                control.execute("SELECT set_config('postern.actor_id',%s,true),set_config('postern.space_id',%s,true),set_config('postern.generation',%s,true)",(m.a,m.s,m.generation))
                control.execute('SELECT postern.revoke_member(%s,%s,%s,%s)',(m.s,m.mb,str(uuid.uuid4()),'a'*64))
            else:
                control.execute("SELECT postern.disable_principal(%s,'suspended')",(m.b,))
            control.commit()
            result=pending.result(timeout=5)
            assert result.status_code==401 and 'Visible' not in result.text


def test_resource_configuration_rejects_private_signing_key():
    key=Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    with pytest.raises(ValueError,match='public key'):
        SpaceTrust('issuer','audience','kid',key,secrets.token_bytes(32))
