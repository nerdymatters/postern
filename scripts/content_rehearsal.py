"""Exercise native content with owned synthetic PostgreSQL, objects and training.

Requires the repository's test/postgres extras, PostgreSQL17+, Poppler, ffmpeg,
and the configured platform sandbox. No existing database or data root is accepted.
The output directory contains synthetic fixtures and should remain private.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
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


def pytest_addoption(parser):
    parser.addoption('--postern-content-inventory')


def pytest_configure(config):
    output = config.getoption('--postern-content-inventory')
    if output:
        config.pluginmanager.register(_Inventory(Path(output)))


class _Inventory:
    """Record collection before selection and every actual execution phase."""

    def __init__(self, output):
        self.output = output
        self.data = {'schema': 1, 'collected': [], 'selected': [], 'deselected': [],
                     'collection_problems': [], 'reports': [], 'exit_status': None}

    def pytest_itemcollected(self, item):
        self.data['collected'].append(item.nodeid)

    def pytest_collection_finish(self, session):
        self.data['selected'] = [item.nodeid for item in session.items]

    def pytest_deselected(self, items):
        self.data['deselected'].extend(item.nodeid for item in items)

    def pytest_collectreport(self, report):
        if not report.passed:
            self.data['collection_problems'].append(report.nodeid)

    def pytest_runtest_logreport(self, report):
        self.data['reports'].append({'nodeid': report.nodeid, 'when': report.when,
                                    'outcome': report.outcome, 'wasxfail': hasattr(report, 'wasxfail')})

    def pytest_sessionfinish(self, session, exitstatus):
        self.data['exit_status'] = int(exitstatus)
        self.output.write_text(json.dumps(self.data, indent=2) + '\n')


def _complete_inventory(inventory, suites):
    if (type(inventory) is not dict or set(inventory) != {
            'schema', 'collected', 'selected', 'deselected', 'collection_problems', 'reports', 'exit_status'}
            or type(inventory['schema']) is not int or inventory['schema'] != 1
            or type(inventory['exit_status']) is not int or inventory['exit_status'] != 0
            or any(type(inventory[key]) is not list for key in
                   ('collected', 'selected', 'deselected', 'collection_problems', 'reports'))):
        return False
    collected = inventory['collected']
    if (not collected or any(type(node) is not str or '::' not in node for node in collected)
            or len(set(collected)) != len(collected)
            or inventory['selected'] != collected or inventory['deselected'] or inventory['collection_problems']
            or {node.split('::', 1)[0] for node in collected} != set(TESTS)):
        return False
    phases = []
    for report in inventory['reports']:
        if (type(report) is not dict or set(report) != {'nodeid', 'when', 'outcome', 'wasxfail'}
                or type(report['nodeid']) is not str or type(report['when']) is not str
                or report['outcome'] != 'passed' or report['wasxfail'] is not False):
            return False
        phases.append((report['nodeid'], report['when']))
    expected = Counter((node, phase) for node in collected for phase in ('setup', 'call', 'teardown'))
    return (Counter(phases) == expected and bool(suites)
            and sum(int(suite['tests']) for suite in suites) == len(collected)
            and all(int(suite['tests']) > 0 and all(suite[key] == '0' for key in ('failures', 'errors', 'skipped'))
                    for suite in suites))


def _stop_process(process):
    # Repeated termination signals must not abandon bounded cleanup. SIGTERM
    # enters through the CLI's exceptional-exit path, like KeyboardInterrupt.
    main_thread = threading.current_thread() is threading.main_thread()
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)} if main_thread else {}
    try:
        for sig, grace in ((signal.SIGINT, 30), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass  # The child may have exited between wait and signaling.
            try:
                process.wait(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            raise TimeoutError('rehearsal child stop could not be confirmed')
        # A reaped leader alone does not prove its descendants stopped.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        from postern.content_render import _wait_group_stopped
        asyncio.run(_wait_group_stopped(process.pid, time.monotonic() + 2))
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


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
    command = [sys.executable, '-m', 'pytest', '-c', str(root/'pyproject.toml'), '-o', 'addopts=',
               '-p', 'scripts.content_rehearsal', '--postern-content-inventory', str(output/'inventory.json'),
               *TESTS, '-q', '--basetemp', str(output/'fixtures'),
               '--junitxml', str(output/'results.xml')]
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'}}
    environment.update(PYTHONPATH=str(root), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    try:
        with (output/'tests.log').open('w') as log:
            process = subprocess.Popen(command, cwd=root, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                report['exit_code'] = process.wait(timeout=900)
            except subprocess.TimeoutExpired:
                report['error'] = 'rehearsal timed out'
                _stop_process(process)
                report['exit_code'] = process.returncode
            except BaseException as error:
                try:
                    _stop_process(process)
                except BaseException as cleanup_error:
                    error.add_note(f'rehearsal cleanup failed: {cleanup_error}')
                raise
    except OSError:
        report['exit_code'] = None
        report['error'] = 'rehearsal process unavailable'
    report['inputs_unchanged'] = before == fingerprint()
    report['suites'] = []
    report['inventory_complete'] = False
    try:
        report['suites'] = [{key: suite.attrib.get(key) for key in ('tests', 'failures', 'errors', 'skipped', 'time')}
                            for suite in ET.parse(output/'results.xml').getroot().iter('testsuite')]
        inventory = json.loads((output/'inventory.json').read_text())
        report['inventory_complete'] = _complete_inventory(inventory, report['suites'])
        if report['inventory_complete']:
            report['executed_tests'] = inventory['collected']
    except (OSError, ET.ParseError, ValueError, TypeError, KeyError):
        report['error'] = 'rehearsal evidence incomplete'
    report['result'] = 'PASS' if ('error' not in report and report['exit_code'] == 0
        and report['inputs_unchanged'] and report['inventory_complete']) else 'FAIL'
    (output/'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f"content rehearsal: {report['result']} ({output/'report.json'})")
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New private evidence directory')
    raise SystemExit(run(parser.parse_args().output))
