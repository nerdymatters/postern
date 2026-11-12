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
    # A resource's state is not a stale page: its own code, so clients do not reload in a loop.
    response=grant(f,resource=m.draft,all_members=True,actor=actor)
    assert response.status_code==409 and response.json()['error']=='not_shareable'
    m.db.execute(f"UPDATE postern.resources SET supported_actions=ARRAY['discover'] WHERE id='{m.hidden}';")
    response=grant(f,all_members=True,actor=actor)
    assert response.status_code==409 and response.json()['error']=='not_shareable'
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
    for query in ['SELECT * FROM postern.grants','SELECT * FROM postern.audiences',"SELECT postern.stale_grant_reason(NULL)",
                  'SELECT * FROM postern.stale_grants(NULL,NULL,1)']:
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
    # Seconds of margin before expiry: the steps before it take about 0.25 s locally.
    f=api;end=int(time.time())+4;key=uid();rev=revision(f)
    first=okay(grant(f,all_members=True,expires=end,key=key,rev=rev))
    assert access(f)['actions']==['discover','view']
    time.sleep(max(0,end-time.time())+.05)
    assert access(f)['actions']==[]
    assert okay(grant(f,all_members=True,expires=end,key=key,rev=rev))==first
    assert access(f)['actions']==[]
    # Value: protects=stored grant terms cannot be extended or changed in place, even by the schema owner;
    # fails_when=grant_guard stops comparing expires_at/actions or is dropped; why_new=only its
    # revoked-to-active branch was tested; seam=none; no_claim=direct owner SQL stands in for a future writer
    for term in ['expires_at=NULL',"actions=ARRAY['discover']"]:
        result=f.model.db.execute(f"UPDATE postern.grants SET {term} WHERE id='{first['target']}';",check=False)
        assert result.returncode and 'immutable' in result.stderr
    assert access(f)['actions']==[]
    assert okay(change(f,'/grants/cleanup'))['retired_count']==2 # includes fixture's expired grant


def test_sharing_writes_never_reach_another_space(api):
    # Value: protects=an owner or admin cannot revoke, retire or inspect grants of another space by ID;
    # fails_when=the space_id filter is dropped from grant.revoke, cleanup or resource_access;
    # why_new=test_scoped_targets_and_publication covers grant/add/archive/list targets, not revoke,
    # cleanup or access; seam=none; no_claim=none beyond protects
    f=api;m=f.model;stale,usable=uid(),uid()
    m.db.execute(f"INSERT INTO postern.grants(space_id,id,resource_id,all_members,actions,expires_at) VALUES('{m.t}','{stale}','{m.foreign}',true,ARRAY['discover'],now()-interval '1 second');"
                 f"INSERT INTO postern.grants(space_id,id,resource_id,membership_id,actions) VALUES('{m.t}','{usable}','{m.foreign}','{m.mtb}',ARRAY['discover','view']);")
    for actor in ['a','d']:
        assert change(f,f'/grants/{usable}/revoke',actor=actor).status_code==404
        assert get(f,f'/resources/{m.foreign}/access/{m.mb}',actor).status_code==404
    assert okay(change(f,'/grants/cleanup'))['retired_count']==1 # this space's expired fixture grant only
    assert m.db.scalar(f"SELECT count(*) FROM postern.grants WHERE space_id='{m.t}' AND state='active';")=='2'
    audit=m.db.rows(f"SELECT detail FROM postern.activity WHERE action='grant.cleanup' AND space_id IN ('{m.s}','{m.t}')")
    assert len(audit)==1 and stale not in json.dumps(audit)


def test_cleanup_retires_ended_recipients_and_keeps_empty_groups(api):
    # Value: protects=cleanup retires grants to ended memberships or archived groups and keeps grants to
    # empty active groups for future members; fails_when=stale_grant_reason loses a recipient branch or
    # treats an empty group as stale; why_new=the cleanup test only yields expired/no_discover/unpublished;
    # seam=none; no_claim=ended recipients are written with direct SQL (sharing functions revoke these themselves)
    f=api;m=f.model;old,archived,empty=uid(),uid(),uid();ended,dropped,kept=uid(),uid(),uid()
    m.db.execute(f"INSERT INTO postern.memberships VALUES('{m.s}','{old}','{m.c}','member','revoked');"
                 f"INSERT INTO postern.audiences(space_id,id,label,state) VALUES('{m.s}','{archived}','Old','archived'),('{m.s}','{empty}','Future','active');"
                 f"INSERT INTO postern.grants(space_id,id,resource_id,membership_id,audience_id,actions) VALUES"
                 f"('{m.s}','{ended}','{m.hidden}','{old}',NULL,ARRAY['discover']),('{m.s}','{dropped}','{m.hidden}',NULL,'{archived}',ARRAY['discover']),"
                 f"('{m.s}','{kept}','{m.hidden}',NULL,'{empty}',ARRAY['discover']);")
    preview={x['id']:x['reason'] for x in okay(get(f,'/grants/cleanup'))['items']}
    assert preview[ended]=='membership_ended' and preview[dropped]=='audience_archived' and kept not in preview
    assert {g['id']:g['stale_reason'] for g in okay(get(f,f'/resources/{m.hidden}/grants'))['items']}=={
        ended:'membership_ended',dropped:'audience_archived',kept:None}
    assert okay(change(f,'/grants/cleanup'))['retired_count']==3 # with the fixture's expired grant
    assert [g['id'] for g in okay(get(f,f'/resources/{m.hidden}/grants'))['items']]==[kept]
    assert access(f)['actions']==[]
    okay(change(f,f'/audiences/{empty}/add',{'membership_id':m.mb}))
    assert access(f)['actions']==['discover'] and [x['id'] for x in access(f)['sources']]==[kept]


def test_unchanged_group_membership_records_no_access_change(api):
    # Value: protects=adding a current member or removing an absent one writes no audit event, outbox entry
    # or new policy revision, and its retry returns the same receipt; fails_when=the ROW_COUNT check is dropped
    # and no-op changes reach the audit trail and invalidate every cursor; why_new=red-team#3 (QA capture 005
    # showed 4 events for 2 real changes); seam=none; no_claim=none beyond protects
    f=api;m=f.model
    okay(change(f,f'/audiences/{m.group}/remove',{'membership_id':m.md}))  # never a member: no change
    rev=revision(f);count=lambda: m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action LIKE 'audience.%';")
    outbox=lambda: m.db.scalar(f"SELECT count(*) FROM postern.outbox WHERE space_id='{m.s}';")
    events,queued,key=count(),outbox(),uid()
    first=okay(change(f,f'/audiences/{m.group}/add',{'membership_id':m.mb},key=key,rev=rev))  # fixture member already
    assert first['revision']==rev and revision(f)==rev and count()==events and outbox()==queued
    assert okay(change(f,f'/audiences/{m.group}/add',{'membership_id':m.mb},key=key,rev=rev))==first
    okay(change(f,f'/audiences/{m.group}/remove',{'membership_id':m.md}))
    assert revision(f)==rev and count()==events
    okay(change(f,f'/audiences/{m.group}/remove',{'membership_id':m.mb}))
    assert revision(f)==rev+1 and int(count())==int(events)+1


def test_stale_preview_pages_across_reasons_and_empty_cleanup_changes_nothing(api):
    # Value: protects=the indexed stale paths together page every stale grant once, in ID order, across a
    # page boundary, and a cleanup that finds nothing writes no event or revision; fails_when=a path's own
    # limit or the cursor drops or repeats a grant, or an empty batch still counts as a policy change;
    # why_new=the earlier pagination test has one reason on one path; adversarial review found the empty
    # batch bumping the revision; seam=none; no_claim=query cost (see scripts/grant_cleanup_benchmark.py)
    f=api;m=f.model
    m.db.execute(f"""INSERT INTO postern.grants(space_id,resource_id,all_members,actions,expires_at)
      SELECT '{m.s}','{m.r}',true,ARRAY['discover'],now()-interval '1 minute' FROM generate_series(1,40);
      INSERT INTO postern.grants(space_id,resource_id,all_members,actions) SELECT '{m.s}','{m.draft}',true,ARRAY['discover'] FROM generate_series(1,40);
      INSERT INTO postern.grants(space_id,resource_id,all_members,actions) SELECT '{m.s}','{m.r}',true,ARRAY['download'] FROM generate_series(1,5);
      INSERT INTO postern.grants(space_id,resource_id,all_members,actions) SELECT '{m.s}','{m.hidden}',true,ARRAY['discover'] FROM generate_series(1,30);""")
    stale=m.db.rows(f"SELECT id::text FROM postern.grants g WHERE space_id='{m.s}' AND state='active' AND postern.stale_grant_reason(g) IS NOT NULL ORDER BY id")
    first=okay(get(f,'/grants/cleanup')); assert len(first['items'])==50 and first['next_cursor']
    second=okay(get(f,'/grants/cleanup',params={'cursor':first['next_cursor']}))
    assert [x['id'] for x in first['items']+second['items']]==[r['id'] for r in stale] and len(stale)==86 # with the fixture's expired grant
    assert okay(change(f,'/grants/cleanup'))['retired_count']==86
    rev=revision(f);events=m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='grant.cleanup';")
    empty=okay(change(f,'/grants/cleanup'))
    assert empty['retired_count']==0 and empty['revision']==rev and revision(f)==rev
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='grant.cleanup';")==events


def test_access_inspection_records_the_member_inspected(api):
    # Value: protects=the audit row for an access inspection names whose access was disclosed; fails_when=the
    # disclosure records only the resource; why_new=adversarial review; seam=none; no_claim=none beyond protects
    f=api;m=f.model
    access(f,m.hidden,m.mb)
    row=m.db.rows(f"SELECT target_id::text,detail FROM postern.activity WHERE space_id='{m.s}' AND action='resource.access_inspected'")
    assert row==[{'target_id':m.hidden,'detail':{'membership_id':m.mb}}]
