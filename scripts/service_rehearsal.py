"""Verify the opt-in service adapters using owned, synthetic environments only.

Core checks need PostgreSQL 17+ and .[test,postgres]. An explicitly supplied
private account checkout adds two-service HTTP tests and the D1/Worker regressions.
No existing database connection or production data is accepted.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from importlib.metadata import version
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def run(output, accounts=None):
    root=Path(__file__).resolve().parents[1]
    output=output.resolve();output.mkdir(parents=True,exist_ok=False,mode=0o700)
    inputs=[root/p for p in ('postern/app.py','postern/spaces.py','postern/pg.py','postern/migration_bundle.py',
        'pyproject.toml','scripts/local_postgres.py','scripts/service_rehearsal.py')]
    inputs+=sorted((root/'postern/sql').glob('*.sql'))+sorted((root/'tests/storage').glob('*.py'))
    stages=[('resource', [sys.executable,'-m','pytest','tests/storage','-q','--basetemp',str(output/'resource-fixtures'),
                         '--junitxml',str(output/'resource.xml')],root)]
    if accounts is not None:
        accounts=accounts.resolve()
        if json.loads((accounts/'package.json').read_text()).get('private') is not True:
            raise ValueError('Explicit private account checkout required')
        for folder,pattern in [('src','*.ts'),('postgresql','*.sql'),('tests','*.ts'),('tests/postgres','*.py')]:
            inputs+=sorted((accounts/folder).rglob(pattern))
        inputs += [accounts/p for p in ('package.json','bun.lock','tsconfig.json','wrangler.jsonc','scripts/isolated-accounts.ts') if (accounts/p).is_file()]
        stages += [('combined',[sys.executable,'-m','pytest',str(accounts/'tests/postgres'),'-q','--basetemp',str(output/'combined-fixtures'),
                                '--junitxml',str(output/'combined.xml')],root),
                   ('accounts-types',['bun','run','typecheck'],accounts),
                   ('accounts-d1',['bun','test'],accounts),
                   ('accounts-worker',['bun','run','build'],accounts)]
    def hashes():
        return {('private-accounts/'+str(p.relative_to(accounts)) if accounts and p.is_relative_to(accounts) else str(p.relative_to(root))):
                hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    report={'scope':'isolated native adapters; synthetic accounts and metadata only; no hosted cutover',
            'private_account_contract':accounts is not None,'inputs':hashes(),'stages':[],
            'toolchain':{name:version(name) for name in ('psycopg','psycopg-pool','PyJWT','fastapi','pytest')}}
    report['toolchain']['python']=sys.version.split()[0]
    report['toolchain']['postgres']=subprocess.check_output(['postgres','--version'],text=True).strip()
    if accounts is not None: report['toolchain']['bun']=subprocess.check_output(['bun','--version'],text=True).strip()
    env={**os.environ,'PYTHONPATH':str(root),'WRANGLER_SEND_METRICS':'false'}
    for name,command,cwd in stages:
        with (output/(name+'.log')).open('w') as log:
            result=subprocess.run(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=300)
        stage={'name':name,'exit_code':result.returncode}
        if (output/(name+'.xml')).exists():
            stage['suites']=[{k:s.attrib.get(k) for k in ('tests','failures','errors','skipped','time')}
                for s in ET.parse(output/(name+'.xml')).getroot().iter('testsuite')]
        report['stages'].append(stage)
        print(f'{name}: '+('PASS' if result.returncode==0 else 'FAIL'),flush=True)
    report['inputs_unchanged']=hashes()==report['inputs']
    report['result']='PASS' if report['inputs_unchanged'] and all(s['exit_code']==0 and all(x['skipped']=='0' for x in s.get('suites',[])) for s in report['stages']) else 'FAIL'
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    return 0 if report['result']=='PASS' else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True,help='New evidence directory')
    parser.add_argument('--accounts-source',type=Path,help='Optional explicit private account checkout; never auto-discovered')
    args=parser.parse_args()
    raise SystemExit(run(args.output,args.accounts_source))
