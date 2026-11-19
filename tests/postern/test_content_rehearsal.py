"""Acceptance receipts require the complete suite and owned-child cleanup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import pytest

from scripts import content_rehearsal as rehearsal


@pytest.fixture
def small_suite(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    (root / 'scripts').mkdir(parents=True)
    source = Path(rehearsal.__file__).read_bytes()
    (root / 'scripts/content_rehearsal.py').write_bytes(source)
    (root / 'pyproject.toml').write_text('[tool.pytest.ini_options]\naddopts = "-k test_one"\n')
    tests = ('tests/test_one.py', 'tests/test_two.py')
    (root / 'tests').mkdir()
    for index, path in enumerate(tests):
        (root / path).write_text(f'def test_{"one" if index == 0 else "two"}():\n    assert {index}+1 == {index+1}\n')
    monkeypatch.setattr(rehearsal, '__file__', str(root / 'scripts/content_rehearsal.py'))
    monkeypatch.setattr(rehearsal, 'TESTS', tests)
    return root, tmp_path / 'evidence'


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, RuntimeError])
def test_interruption_stops_real_owned_child(tmp_path, monkeypatch, interruption):
    # Value: protects=exceptional runner exits stop and reap their detached child;
    # fails_when=cleanup handles only timeouts; why_new=successful rehearsals never interrupt wait;
    # seam=none; no_claim=PostgreSQL fixture cleanup; child is a harmless sleeping Python process.
    real_popen = subprocess.Popen
    children = []

    class Interrupted:
        def __init__(self, *args, **kwargs):
            self.child = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            children.append(self.child)
            self.first = True
        @property
        def pid(self): return self.child.pid
        @property
        def returncode(self): return self.child.returncode
        def poll(self): return self.child.poll()
        def wait(self, timeout=None):
            if self.first:
                self.first = False
                raise interruption('injected supervisor interruption')
            return self.child.wait(timeout=timeout)

    monkeypatch.setattr(rehearsal.subprocess, 'Popen', Interrupted)
    try:
        with pytest.raises(interruption, match='injected supervisor interruption'):
            rehearsal.run(tmp_path / 'evidence')
        assert len(children) == 1
        assert children[0].poll() is not None, 'runner returned while its child was alive'
        with pytest.raises(ProcessLookupError):
            os.killpg(children[0].pid, 0)
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)


def test_ambient_selection_does_not_shorten_acceptance(small_suite, monkeypatch):
    # Value: protects=acceptance executes every selected module despite ambient pytest options;
    # fails_when=environment or config addopts deselect required cases; why_new=prior wrapper only required one success;
    # seam=none; no_claim=real native content checks; two tiny modules exercise the actual runner and pytest.
    root, output = small_suite
    monkeypatch.setenv('PYTEST_ADDOPTS', '-k test_one')
    assert rehearsal.run(output) == 0
    report = json.loads((output / 'report.json').read_text())
    cases = list(ET.parse(output / 'results.xml').getroot().iter('testcase'))
    assert {case.attrib['name'] for case in cases} == {'test_one', 'test_two'}
    assert report['result'] == 'PASS' and report['inputs_unchanged']


@pytest.mark.parametrize('stop_signal', [signal.SIGINT, signal.SIGTERM])
def test_cli_signal_stops_detached_pytest(tmp_path, stop_signal):
    # Value: protects=CLI stop signals propagate through cleanup to detached pytest;
    # fails_when=SIGTERM exits the supervisor without its child teardown;
    # why_new=API exception tests do not exercise installed CLI signal handlers;
    # seam=none; no_claim=native fixture correctness; owned tiny modules replace content tests.
    root = tmp_path / 'source'
    (root / 'scripts').mkdir(parents=True)
    (root / 'scripts/content_rehearsal.py').write_bytes(Path(rehearsal.__file__).read_bytes())
    (root / 'pyproject.toml').write_text('[tool.pytest.ini_options]\n')
    marker = tmp_path / 'child.pid'
    for index, path in enumerate(rehearsal.TESTS):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        body = (f'from pathlib import Path\nimport os,time\ndef test_wait():\n'
                f'    Path({str(marker)!r}).write_text(str(os.getpid()))\n    time.sleep(60)\n') if index == 0 else 'def test_complete():\n    assert 1 + 1 == 2\n'
        target.write_text(body)
    output = tmp_path / 'evidence'
    with (tmp_path / 'supervisor.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'scripts.content_rehearsal', '--output', str(output)],
                                   cwd=root, env={**os.environ, 'PYTHONPATH': str(root)},
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        child = None
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists(), (tmp_path / 'supervisor.log').read_text()
            child = int(marker.read_text())
            process.send_signal(stop_signal)
            assert process.wait(timeout=10) != 0
            with pytest.raises(ProcessLookupError):
                os.killpg(child, 0)
            assert not (output / 'report.json').exists()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if child is not None:
                try:
                    os.killpg(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize('damage', ['deselected', 'missing-phase', 'duplicate-phase', 'foreign-case', 'bad-json', 'bad-xml', 'source-drift'])
def test_incomplete_or_changed_receipts_fail(small_suite, monkeypatch, damage):
    # Value: protects=positive child exit cannot certify incomplete, corrupt or changed acceptance evidence;
    # fails_when=receipt validation ignores missing work or source drift; why_new=stored successful receipts lack negative cases;
    # seam=none; no_claim=hostile pytest process integrity; controlled post-exit damage models broken evidence.
    root, output = small_suite
    real_popen = subprocess.Popen

    class Damaged:
        def __init__(self, *args, **kwargs): self.child = real_popen(*args, **kwargs)
        @property
        def pid(self): return self.child.pid
        @property
        def returncode(self): return self.child.returncode
        def poll(self): return self.child.poll()
        def wait(self, timeout=None):
            result = self.child.wait(timeout=timeout)
            path = output / 'inventory.json'
            if damage == 'bad-xml': (output / 'results.xml').write_text('<broken')
            elif damage == 'source-drift': (root / 'tests/test_one.py').write_text('def test_one():\n    assert False\n')
            elif damage == 'bad-json': path.write_text('{broken')
            else:
                inventory = json.loads(path.read_text())
                if damage == 'deselected': inventory['deselected'].append(inventory['collected'][0])
                elif damage == 'missing-phase': inventory['reports'].pop()
                elif damage == 'duplicate-phase': inventory['reports'].append(inventory['reports'][0])
                elif damage == 'foreign-case': inventory['reports'][0]['nodeid'] = 'tests/foreign.py::test_other'
                path.write_text(json.dumps(inventory))
            return result

    monkeypatch.setattr(rehearsal.subprocess, 'Popen', Damaged)
    assert rehearsal.run(output) == 1
    report = json.loads((output / 'report.json').read_text())
    assert report['result'] == 'FAIL'
