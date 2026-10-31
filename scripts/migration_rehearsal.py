"""Run the production-schema rehearsal against owned, disposable PostgreSQL only.

Requires PostgreSQL 17+ CLI tools and the project's test and postgres dependencies. Retains
synthetic bundles, test logs and JUnit results; creates no production connection.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def run(output):
    output=output.resolve(); output.mkdir(parents=True,mode=0o700,exist_ok=False)
    root=Path(__file__).resolve().parents[1]
    inputs=['postern/pg.py','postern/migration_bundle.py','postern/sql/001_spaces.sql','postern/sql/002_service_sessions.sql','postern/sql/003_space_onboarding.sql','postern/sql/004_invitation_preview.sql','postern/sql/005_membership_management.sql','postern/sql/006_ownership_transfer.sql','postern/spaces.py','postern/app.py',
            'scripts/local_postgres.py','scripts/migration_rehearsal.py']
    inputs += [str(p.relative_to(root)) for p in sorted((root/'tests/storage').glob('*.py'))]
    report={'scope':'synthetic core-schema migration, not hosted cutover or PITR',
            'inputs':{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in inputs}}
    with (output/'pytest.log').open('w') as log:
        result=subprocess.run([sys.executable,'-m','pytest','tests/storage','-q','--basetemp',str(output/'fixtures'),
                               '--junitxml',str(output/'junit.xml')],cwd=root,stdout=log,stderr=subprocess.STDOUT,timeout=300)
    if (output/'junit.xml').exists():
        suites=ET.parse(output/'junit.xml').getroot().iter('testsuite')
        report['tests']=[{key:s.attrib.get(key) for key in ['tests','failures','errors','skipped','time']} for s in suites]
    report['result']='PASS' if result.returncode==0 else 'FAIL'
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    return result.returncode


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True,help='New directory for synthetic evidence; existing paths refused')
    raise SystemExit(run(parser.parse_args().output))
