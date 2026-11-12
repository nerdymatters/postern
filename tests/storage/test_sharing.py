"""Sharing through real restricted PostgreSQL sessions and HTTP routes."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time
import threading

import psycopg
import pytest

from postern.pg import Postgres
from test_service_api import api, runtime
from test_onboarding import fail_events, uid


def revision(f):
    return int(f.model.db.scalar(f"SELECT policy_revision FROM postern.spaces WHERE id='{f.model.s}';"))


def change(f, path, data=None, *, actor='a', headers=None, key=None, rev=None):
    return f.client.post(f.url+path, headers=headers or f.session(actor), json={
        'operation_id': key or uid(), 'expected_revision': revision(f) if rev is None else rev, **(data or {})})


def grant(f, resource=None, *, member=None, group=None, all_members=False, actions=None, expires=None, **kwargs):
    return change(f, '/resources/'+(resource or f.model.hidden)+'/grants', {
        'membership_id': member, 'audience_id': group, 'all_members': all_members,
        'actions': actions if actions is not None else ['discover', 'view'], 'expires_at': expires}, **kwargs)


def get(f, path, actor='a', **kwargs):
    return f.client.get(f.url+path, headers=f.session(actor), **kwargs)


def okay(response):
    assert response.status_code == 200, response.text
    return response.json()


def access(f, resource=None, member=None):
    return okay(get(f, '/resources/'+(resource or f.model.hidden)+'/access/'+(member or f.model.mb)))


def test_audience_lifecycle_and_additive_access(api):
    f=api; m=f.model
    group=okay(change(f, '/audiences', {'label':'  Readers  '}, actor='d'))['target']
    assert any(a['id']==group and a['label']=='Readers' for a in okay(get(f,'/audiences'))['items'])
    assert okay(get(f,f'/audiences/{group}/members'))['items']==[]
    okay(change(f,f'/audiences/{group}/rename',{'label':'Reviewers'}))
    okay(change(f,f'/audiences/{group}/add',{'membership_id':m.mb}))
    assert [x['id'] for x in okay(get(f,f'/audiences/{group}/members'))['items']]==[m.mb]
    grouped=okay(grant(f,group=group,actions=['discover']))['target']
    direct=okay(grant(f,member=m.mb))['target']
    assert access(f)['actions']==['discover','view']
    assert {x['id'] for x in access(f)['sources']}=={grouped,direct}
    okay(change(f,f'/grants/{direct}/revoke'))
    assert access(f)['actions']==['discover']
    okay(change(f,f'/audiences/{group}/remove',{'membership_id':m.mb}))
    assert access(f)['actions']==[]
    okay(change(f,f'/audiences/{group}/add',{'membership_id':m.mb}))
    all_id=okay(grant(f,all_members=True))['target']
    okay(change(f,f'/audiences/{group}/archive'))
    assert access(f)['actions']==['discover','view']
    assert [x['id'] for x in access(f)['sources']]==[all_id]
    assert m.db.scalar(f"SELECT state FROM postern.grants WHERE id='{grouped}';")=='revoked'
    assert get(f,f'/audiences/{group}/members').status_code==404
    assert grant(f,group=group).status_code==404
    assert change(f,f'/audiences/{group}/rename',{'label':'Back'}).status_code==404
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='audience.archive';")=='1'


def test_grants_never_inherit_and_effective_access_matches_policy(api):
    f=api; m=f.model
    m.db.execute(f"INSERT INTO postern.resource_children VALUES('{m.s}','{m.hidden}','{m.draft}',0);")
    okay(grant(f,all_members=True))
    for actor, member in [('a',m.ma),('b',m.mb),('d',m.md)]:
        for resource in [m.r,m.hidden,m.draft,m.expired,m.group_resource]:
            result=access(f,resource,member)
            for action in ['discover','view']:
                allowed=m.db.scalar(m.context(actor)+f"SELECT postern.can_resource('{m.s}','{resource}','{action}');")=='t'
                assert (action in result['actions'])==allowed
    visible={r['id'] for r in okay(get(f,'/resources',actor='b'))['resources']}
    assert m.hidden in visible and m.draft not in visible
    assert access(f,m.draft)['sources']==[]
    assert access(f,m.draft,m.ma)['actions']==['discover','view'] # role access remains


@pytest.mark.parametrize('actor',['a','d'])
def test_scoped_targets_and_publication(api,actor):
    f=api;m=f.model
    assert grant(f,member=m.mtb,actor=actor).status_code==404
    assert grant(f,resource=m.foreign,all_members=True,actor=actor).status_code==404
    assert change(f,f'/audiences/{m.group}/add',{'membership_id':m.mtb},actor=actor).status_code==404
    assert change(f,f'/audiences/{uid()}/archive',actor=actor).status_code==404
    assert get(f,f'/resources/{m.foreign}/grants',actor).status_code==404
    assert get(f,f'/resources/{m.r}/access/{m.mtb}',actor).status_code==404
    assert grant(f,resource=m.draft,all_members=True,actor=actor).status_code==409
    m.db.execute(f"UPDATE postern.resources SET supported_actions=ARRAY['discover'] WHERE id='{m.hidden}';")
    assert grant(f,all_members=True,actor=actor).status_code==409
    assert grant(f,all_members=True,actions=['discover'],actor=actor).status_code==200


def test_members_and_runtime_cannot_manage_or_discover_sharing(api,runtime):
    f=api;m=f.model
    for path in ['/audiences',f'/audiences/{m.group}/members',f'/resources/{m.hidden}/grants','/grants/cleanup',f'/resources/{m.r}/access/{m.mb}']:
        response=get(f,path,'b'); assert response.status_code==403 and m.hidden not in response.text
    assert change(f,'/audiences',{'label':'No'},actor='b').status_code==403
    assert grant(f,all_members=True,actor='b').status_code==403
    assert change(f,'/grants/cleanup',actor='b').status_code==403
    h=f.session('a')
    assert f.client.get(f'/v1/spaces/{m.t}/audiences',headers=h).status_code==401
    db=Postgres({**m.db.env,'PGUSER':runtime['user']})
    for query in ['SELECT * FROM postern.grants','SELECT * FROM postern.audiences',"SELECT postern.stale_grant_reason(NULL)"]:
        assert db.execute(query,check=False).returncode
    with fail_events(m.db):
        for path in ['/audiences','/grants/cleanup',f'/resources/{m.r}/grants',f'/resources/{m.r}/access/{m.mb}']:
            result=f.client.get(f.url+path,headers=h)
            assert result.status_code==503 and 'Group' not in result.text


def test_stale_cleanup_is_bounded_durable_and_never_resurrects(api):
    f=api;m=f.model
    m.db.execute(f"""INSERT INTO postern.grants(space_id,resource_id,all_members,actions)
      SELECT '{m.s}','{m.draft}',true,ARRAY['discover'] FROM generate_series(1,501);
      INSERT INTO postern.grants(space_id,resource_id,all_members,actions) VALUES('{m.s}','{m.r}',true,ARRAY['download']);""")
    before={x['id'] for x in okay(get(f,'/resources','b'))['resources']}
    preview=okay(get(f,'/grants/cleanup')); assert len(preview['items'])==50 and preview['next_cursor']
    key=uid();rev=revision(f);first=okay(change(f,'/grants/cleanup',key=key,rev=rev))
    assert first['retired_count']==500
    assert okay(change(f,'/grants/cleanup',key=key,rev=rev))==first
    assert okay(change(f,'/grants/cleanup'))['retired_count']==3
    assert okay(get(f,'/grants/cleanup'))['items']==[]
    assert {x['id'] for x in okay(get(f,'/resources','b'))['resources']}==before
    assert m.db.scalar(f"SELECT count(*) FROM postern.grants WHERE space_id='{m.s}' AND state='revoked';")=='503'
    rows=m.db.rows(f"SELECT detail FROM postern.activity WHERE space_id='{m.s}' AND action='grant.cleanup'")
    assert sum(len(r['detail']['retired']) for r in rows)==503
    assert {g['reason'] for r in rows for g in r['detail']['retired']}=={'expired','no_discover','unpublished'}
    m.db.execute(f"UPDATE postern.resources SET state='published' WHERE id='{m.draft}';")
    assert access(f,m.draft)['actions']==[]
    assert m.db.execute(f"UPDATE postern.grants SET state='active' WHERE space_id='{m.s}' AND state='revoked';",check=False).returncode
    okay(grant(f,resource=m.draft,all_members=True))
    assert access(f,m.draft)['actions']==['discover','view']


@pytest.mark.parametrize('table',['activity','outbox'])
@pytest.mark.parametrize('operation',['create','archive','cleanup','grant'])
def test_sharing_rolls_back_on_audit_or_outbox_failure(api,table,operation):
    f=api;m=f.model;h=f.session('a');key=uid();rev=revision(f)
    path,data={'create':('/audiences',{'label':'Rollback'}),'archive':(f'/audiences/{m.group}/archive',{}),
        'cleanup':('/grants/cleanup',{}),'grant':(f'/resources/{m.hidden}/grants',{'membership_id':m.mb,'audience_id':None,'all_members':False,'actions':['discover'],'expires_at':None})}[operation]
    before=m.db.rows(f"SELECT id,state FROM postern.grants WHERE space_id='{m.s}' ORDER BY id")
    with fail_events(m.db,table): assert change(f,path,data,headers=h,key=key,rev=rev).status_code==503
    assert revision(f)==rev and m.db.rows(f"SELECT id,state FROM postern.grants WHERE space_id='{m.s}' ORDER BY id")==before
    assert m.db.scalar(f"SELECT count(*) FROM postern.operations WHERE key='{key}';")=='0'
    result=okay(change(f,path,data,headers=h,key=key,rev=rev))
    assert okay(change(f,path,data,headers=h,key=key,rev=rev))==result
    assert revision(f)==rev+1


def test_request_validation_and_idempotency(api):
    f=api;m=f.model;key=uid();rev=revision(f)
    first=okay(grant(f,member=m.mb,key=key,rev=rev))
    assert okay(grant(f,member=m.mb,key=key,rev=rev))==first
    assert grant(f,all_members=True,key=key,rev=rev).status_code==409
    assert change(f,'/audiences',{'label':'Wrong action'},key=key,rev=rev).status_code==409
    okay(change(f,f"/grants/{first['target']}/revoke"))
    assert okay(grant(f,member=m.mb,key=key,rev=rev))==first
    assert access(f)['actions']==[]
    for args in [{'actions':[]},{'actions':['view']},{'actions':['discover','download']},{'actions':['discover','discover']},
                 {'actions':[{}]},{'member':m.mb},{'all_members':1},{'expires':True},{'expires':-1},{'expires':1}]:
        assert grant(f,**{'all_members':True,**args}).status_code==422
    for label in ['', ' ', 'x'*201, '\u0000', 3]:
        assert change(f,'/audiences',{'label':label}).status_code==422
    assert f.client.post(f.url+'/audiences', headers={**f.session('a'),'Content-Type':'application/json'}, content=json.dumps({'operation_id':uid(),'expected_revision':revision(f),'label':'\ud800'})).status_code==422
    assert change(f,'/audiences',{'label':'Fine'},rev=True).status_code==422
    assert change(f,'/audiences',{'label':'Fine','actor_id':m.a}).status_code==422


def test_pagination_binds_actor_target_list_and_revision(api):
    f=api;m=f.model;h=f.session('a')
    m.db.execute(f"INSERT INTO postern.audiences(space_id,label) SELECT '{m.s}',n::text FROM generate_series(1,55)n;")
    first=okay(f.client.get(f.url+'/audiences',headers=h));cursor=first['next_cursor']
    second=okay(f.client.get(f.url+'/audiences',headers=h,params={'cursor':cursor}))
    assert len(first['items'])==50 and len(second['items'])==6
    assert not {x['id'] for x in first['items']} & {x['id'] for x in second['items']}
    assert get(f,'/audiences','d',params={'cursor':cursor}).status_code==422
    assert f.client.get(f.url+'/grants/cleanup',headers=h,params={'cursor':cursor}).status_code==422
    okay(change(f,'/audiences',{'label':'New'}))
    assert f.client.get(f.url+'/audiences',headers=h,params={'cursor':cursor}).status_code==422
    m.db.execute(f"INSERT INTO postern.grants(space_id,resource_id,all_members,actions) SELECT '{m.s}','{m.r}',true,ARRAY['discover'] FROM generate_series(1,55)n;")
    grants=okay(get(f,f'/resources/{m.r}/grants'))
    assert get(f,f'/resources/{m.hidden}/grants',params={'cursor':grants['next_cursor']}).status_code==422
    result=access(f,m.r);assert len(result['sources'])==50 and result['more_sources']


def test_readmission_never_rejoins_audience_or_direct_grants(api):
    f=api;m=f.model
    okay(grant(f,member=m.mb))
    okay(change(f,f'/memberships/{m.mb}/remove'))
    new=uid();m.db.execute(f"INSERT INTO postern.memberships(space_id,id,principal_id,role) VALUES('{m.s}','{new}','{m.b}','member');")
    assert access(f,member=new)['actions']==[]
    assert access(f,m.group_resource,new)['actions']==[]
    assert change(f,f'/audiences/{m.group}/add',{'membership_id':m.mb}).status_code==404
    assert grant(f,member=m.mb).status_code==404


@pytest.mark.parametrize('mutation',['demote','remove_target','unpublish'])
def test_queued_share_rechecks_authority_and_target(api,mutation):
    f=api;m=f.model;h=f.session('d')
    with psycopg.connect(host=m.db.env['PGHOST'],port=int(m.db.env['PGPORT']),user=m.db.env['PGUSER'],dbname='postgres') as control:
        control.execute('SELECT 1 FROM postern.spaces WHERE id=%s FOR UPDATE',(m.s,))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(grant,f,member=m.mb,headers=h,rev=0)
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                if m.db.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename='resource_api_test' AND wait_event_type='Lock');")=='t':break
                time.sleep(.02)
            else: raise AssertionError('share did not reach lock')
            if mutation=='demote': control.execute("UPDATE postern.memberships SET role='member' WHERE id=%s",(m.md,))
            elif mutation=='remove_target': control.execute("UPDATE postern.memberships SET state='revoked' WHERE id=%s",(m.mb,))
            else: control.execute("UPDATE postern.resources SET state='draft' WHERE id=%s",(m.hidden,))
            control.commit()
            assert pending.result(timeout=5).status_code=={'demote':403,'remove_target':404,'unpublish':409}[mutation]
    assert m.db.scalar(f"SELECT count(*) FROM postern.grants WHERE resource_id='{m.hidden}';")=='0'


def test_concurrent_writers_cannot_both_use_the_same_revision(api):
    f=api;m=f.model;h=f.session('a');rev=revision(f)
    token=hashlib.sha256(h['Authorization'][7:].encode()).hexdigest()
    barrier=threading.Barrier(2)
    def create(_):
        # Separate restricted connections, not the HTTP fixture's one-slot pool.
        with psycopg.connect(**f.service.pool.kwargs) as conn:
            barrier.wait(timeout=5)
            try:
                conn.execute('SELECT postern.resource_sharing_change(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (token,m.s,f.trust.audience,uid(),'a'*64,rev,'audience.create',None,psycopg.types.json.Jsonb({'label':'Readers'})))
                return 'committed'
            except psycopg.Error as error:
                conn.rollback()
                return error.sqlstate
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(create,range(2)))
    assert sorted(results)==['PT413','committed']


def test_live_expiry_denies_before_cleanup_and_retry_does_not_extend_it(api):
    f=api;end=int(time.time())+2;key=uid();rev=revision(f)
    first=okay(grant(f,all_members=True,expires=end,key=key,rev=rev))
    assert access(f)['actions']==['discover','view']
    time.sleep(max(0,end-time.time())+.05)
    assert access(f)['actions']==[]
    assert okay(grant(f,all_members=True,expires=end,key=key,rev=rev))==first
    assert access(f)['actions']==[]
    assert okay(change(f,'/grants/cleanup'))['retired_count']==2 # includes fixture's expired grant
