"""Exercise native content with owned synthetic PostgreSQL, objects and training.

Requires the repository's test/postgres extras, PostgreSQL17+, Poppler, ffmpeg,
and the configured platform sandbox. No existing database or data root is accepted.
The output directory contains synthetic fixtures and should remain private.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET


TESTS = (
    'tests/postern/test_content_objects.py',
    'tests/postern/test_content_render.py',
    'tests/postern/test_content_prepare.py',
    'tests/storage/test_content_schema.py',
    'tests/storage/test_content_api.py',
    'tests/storage/test_content_deadlines.py',
    'tests/storage/test_space_training.py',
)


def run(output: Path) -> int:
    root = Path(__file__).resolve().parents[1]
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    inputs = (sorted((root/'postern').rglob('*.py')) + sorted((root/'postern/sql').glob('*.sql'))
              + [path for path in (root/'postern/sample_training').rglob('*') if path.is_file()]
              + sorted((root/'config/sandbox').glob('*'))
              + [root/path for path in (*TESTS, 'tests/postern/conftest.py', 'tests/storage/conftest.py',
                 'scripts/local_postgres.py', 'scripts/content_rehearsal.py', 'pyproject.toml')])
    def fingerprint():
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in inputs if path.is_file()}
    before = fingerprint()
    report = {'scope': 'owned synthetic content and dual-authority training; no production migration or cloud rollout',
              'inputs': before, 'toolchain': {name: version(name) for name in
              ('psycopg', 'psycopg-pool', 'PyJWT', 'fastapi', 'pytest', 'pillow')},
              'python': sys.version.split()[0], 'tests': list(TESTS)}
    command = [sys.executable, '-m', 'pytest', *TESTS, '-q', '--basetemp', str(output/'fixtures'),
               '--junitxml', str(output/'results.xml')]
    try:
        with (output/'tests.log').open('w') as log:
            process = subprocess.Popen(command, cwd=root, env={**os.environ, 'PYTHONPATH': str(root)},
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                report['exit_code'] = process.wait(timeout=900)
            except subprocess.TimeoutExpired:
                report['error'] = 'rehearsal timed out'
                # Let pytest unwind its owned clusters and workers before force.
                for sig, grace in ((signal.SIGINT, 30), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
                    if process.poll() is not None:
                        break
                    os.killpg(process.pid, sig)
                    try:
                        process.wait(timeout=grace)
                    except subprocess.TimeoutExpired:
                        continue
                report['exit_code'] = process.returncode
    except OSError:
        report['exit_code'] = None
        report['error'] = 'rehearsal process unavailable'
    report['inputs_unchanged'] = before == fingerprint()
    report['suites'] = []
    if (output/'results.xml').is_file():
        report['suites'] = [{key: suite.attrib.get(key) for key in ('tests', 'failures', 'errors', 'skipped', 'time')}
                            for suite in ET.parse(output/'results.xml').getroot().iter('testsuite')]
    report['result'] = 'PASS' if ('error' not in report and report['exit_code'] == 0 and report['inputs_unchanged'] and report['suites']
        and all(int(s['tests']) > 0 and all(s[key] == '0' for key in ('failures', 'errors', 'skipped'))
                for s in report['suites'])) else 'FAIL'
    (output/'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f"content rehearsal: {report['result']} ({output/'report.json'})")
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New private evidence directory')
    raise SystemExit(run(parser.parse_args().output))
