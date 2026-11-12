"""Measure stale-grant retirement on an owned PostgreSQL cluster, never a live DSN.

Run: .venv/bin/python -m scripts.grant_cleanup_benchmark --output .context/grant-cleanup
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time
import uuid

from psycopg.types.json import Jsonb

from postern.pg import apply_schema
from scripts.local_postgres import local_postgres
from scripts.catalog_benchmark import AUDIENCE, CATALOG, DEADLINES, WORK, connect, session, space


def run(output):
    output=output.resolve(); output.mkdir(parents=True,exist_ok=False)
    with local_postgres(output/'postgres.log') as db:
        apply_schema(db)
        db.execute("UPDATE postern.authority SET state='active'; CREATE ROLE cleanup_runtime LOGIN IN ROLE postern_resource_service;")
        with connect(db) as admin, connect(db,'cleanup_runtime',DEADLINES) as runtime:
            model=space(admin,'cleanup',{'owner':'owner','reader':'member'}); s=model['space']
            generation=admin.execute('SELECT generation::text FROM postern.authority').fetchone()[0]
            admin.execute("""WITH resources AS (INSERT INTO postern.resources(space_id,kind,title,state,adapter)
              SELECT %s,'document','Stale '||n,CASE WHEN n%%2=0 THEN 'published' ELSE 'draft' END,'test'
              FROM generate_series(1,10000) n RETURNING space_id,id,state)
              INSERT INTO postern.grants(space_id,resource_id,all_members,actions,expires_at)
              SELECT space_id,id,true,ARRAY['discover','view'],CASE WHEN state='published' THEN now()-interval '1 hour' END FROM resources""",(s,))
            admin.execute("""WITH resource AS (INSERT INTO postern.resources(space_id,kind,title,state,adapter)
              VALUES(%s,'document','Still shared','published','test') RETURNING space_id,id)
              INSERT INTO postern.grants(space_id,resource_id,all_members,actions)
              SELECT space_id,id,true,ARRAY['discover','view'] FROM resource""",(s,))
            admin.execute('ANALYZE postern.grants,postern.resources');admin.commit()
            tokens={name:session(admin,generation,s,actor) for name,actor in model['actors'].items()}
            params=(tokens['reader'],s,AUDIENCE,'',None,None)
            def measure():
                times=[]; items=None
                for _ in range(7):
                    start=time.perf_counter(); items=runtime.execute(CATALOG,params).fetchone()[0]['items'];runtime.commit()
                    times.append((time.perf_counter()-start)*1000)
                with connect(db) as probe:
                    probe.execute("SET LOCAL track_functions='all'; SET LOCAL ROLE cleanup_runtime")
                    probe.execute(CATALOG,params);probe.execute('RESET ROLE')
                    work=probe.execute(WORK).fetchone();probe.rollback()
                return {'median_ms':round(statistics.median(times),2),'grant_rows_read':int(work[2] or 0),'items':items}
            before=measure(); batches=[]; revision=0
            while True:
                start=time.perf_counter()
                result=runtime.execute('SELECT postern.resource_sharing_change(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (tokens['owner'],s,AUDIENCE,str(uuid.uuid4()),'a'*64,revision,'grant.cleanup',None,Jsonb({}))).fetchone()[0]
                runtime.commit(); revision=result['revision']
                batches.append({'retired':result['retired_count'],'ms':round((time.perf_counter()-start)*1000,2)})
                if result['retired_count']<500:break
            after=measure()
            assert before['items']==after['items'] and len(after['items'])==1
            assert sum(b['retired'] for b in batches)==10000 and after['grant_rows_read']<before['grant_rows_read']
            assert admin.execute("SELECT count(*) FROM postern.grants WHERE space_id=%s AND state='revoked'",(s,)).fetchone()[0]==10000
            # Many usable grants and one stale one: the preview and cleanup follow the stale-grant
            # paths, so their cost must not grow with the usable grants.
            admin.execute("""WITH resources AS (INSERT INTO postern.resources(space_id,kind,title,state,adapter)
              SELECT %s,'document','Shared '||n,'published','test' FROM generate_series(1,100000) n RETURNING space_id,id)
              INSERT INTO postern.grants(space_id,resource_id,all_members,actions,expires_at)
              SELECT space_id,id,true,ARRAY['discover','view'],CASE WHEN row_number() OVER ()=1 THEN now()-interval '1 hour' END FROM resources""",(s,))
            admin.execute('ANALYZE postern.grants,postern.resources');admin.commit()
            previews=[]
            for _ in range(5):
                start=time.perf_counter()
                found=runtime.execute('SELECT postern.resource_sharing(%s,%s,%s,%s,%s,%s,%s)',(tokens['owner'],s,AUDIENCE,'cleanup',None,None,None)).fetchone()[0]['items']
                runtime.commit(); previews.append((time.perf_counter()-start)*1000)
            start=time.perf_counter()
            result=runtime.execute('SELECT postern.resource_sharing_change(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (tokens['owner'],s,AUDIENCE,str(uuid.uuid4()),'a'*64,revision,'grant.cleanup',None,Jsonb({}))).fetchone()[0]
            runtime.commit()
            healthy={'usable_grants':100001,'stale_found':len(found),'preview_median_ms':round(statistics.median(previews),2),
                'cleanup_ms':round((time.perf_counter()-start)*1000,2),'retired':result['retired_count']}
            assert len(found)==1 and result['retired_count']==1
            root=Path(__file__).resolve().parents[1]
            inputs=[*sorted((root/'postern/sql').glob('*.sql')), Path(__file__).resolve(),root/'scripts/catalog_benchmark.py']
            report={'result':'PASS','scope':'synthetic local PostgreSQL; not a hosted capacity claim','before':before,'after':after,'batches':batches,'healthy':healthy,
                'inputs':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}}
            (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps({k:v for k,v in report.items() if k!='inputs'},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args().output)
