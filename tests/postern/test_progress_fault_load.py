"""Deliberate serving/cleanup regressions must prevent a successful probe report."""
import json
import time
from types import SimpleNamespace

import pytest

from postern.auth import Refusal
from postern import service as service_module
from scripts import progress_fault_load as probe


def test_final_disclosure_bypass_fails_the_sustained_probe(tmp_path, monkeypatch):
    original = service_module.Service.execute
    async def bypass(service, *args, **kwargs):
        try:
            return await original(service, *args, **kwargs)
        except Refusal:
            if kwargs.get('event_name') == 'training_progress_viewed':
                return None
            raise
    monkeypatch.setattr(service_module.Service, 'execute', bypass)
    output = tmp_path/'refusal'
    with pytest.raises(AssertionError):
        probe.run(output)
    report = json.loads((output/'report.json').read_text())
    assert report['status'] == 'failed'
    assert [phase['phase'] for phase in report['phases']] == ['normal']


def test_acknowledged_but_unwritten_page_fails_the_sustained_probe(tmp_path, monkeypatch):
    original = service_module.event
    def missing(db, kind, *args, **kwargs):
        if kind == 'page_served':
            return 'synthetic-unwritten-page'
        return original(db, kind, *args, **kwargs)
    monkeypatch.setattr(service_module, 'event', missing)
    output = tmp_path/'missing'
    with pytest.raises(AssertionError):
        probe.run(output)
    report = json.loads((output/'report.json').read_text())
    assert report['status'] == 'failed'
    assert [phase['phase'] for phase in report['phases']] == ['normal', 'role', 'revoke', 'consent', 'quiz', 'media', 'export']


def test_final_cleanup_failure_cannot_leave_success_receipt(tmp_path, monkeypatch):
    original = probe.Running.close
    closed = []
    def broken(app):
        original(app)
        closed.append(app)
        if len(closed) == 2:
            raise RuntimeError('injected final cleanup failure')
    monkeypatch.setattr(probe.Running, 'close', broken)
    output = tmp_path/'cleanup'
    with pytest.raises(RuntimeError, match='cleanup failed'):
        probe.run(output)
    report = json.loads((output/'report.json').read_text())
    assert report['status'] == 'failed' and report['cleanup_errors'] == ['RuntimeError']
    assert len(closed) == 2
    assert all(not app.thread.is_alive() and not app.service.db.thread.is_alive() for app in closed)


def test_sustained_cpu_guard_fails_the_fault_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.psutil, 'cpu_percent', lambda *args, **kwargs: 100.)
    output = tmp_path/'cpu'
    with pytest.raises(AssertionError):
        probe.run(output)
    report = json.loads((output/'report.json').read_text())
    assert report['status'] == 'failed'
    assert 'host CPU exceeded70% for five seconds' in report['failures']


def test_wrong_reader_progress_after_rollback_fails(tmp_path, monkeypatch):
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'rust')
    pytest.importorskip('postern_progress_native')
    original = service_module.annotate
    def broken(db, posts, email):
        result = original(db, posts, email)
        if probe.progress_backend.selected() == 'python':
            for post in result:
                for item in post['items']:
                    item['progress']['opened_pages'] = []
        return result
    monkeypatch.setattr(service_module, 'annotate', broken)
    output = tmp_path/'rollback'
    with pytest.raises(AssertionError):
        probe.run(output)
    assert json.loads((output/'report.json').read_text())['status'] == 'failed'


def test_rollback_start_failure_keeps_cleanup_ownership(tmp_path, monkeypatch):
    original = probe.Running.start
    started = []
    def broken(app):
        started.append(app)
        original(app)
        if len(started) == 2:
            raise RuntimeError('injected rollback start failure')
        return app
    monkeypatch.setattr(probe.Running, 'start', broken)
    output = tmp_path/'startup'
    with pytest.raises(RuntimeError, match='injected rollback start failure'):
        probe.run(output)
    report = json.loads((output/'report.json').read_text())
    assert report['status'] == 'failed' and report['phase'] == 'rollback-start'
    assert len(started) == 2
    assert all(not app.thread.is_alive() and not app.service.db.thread.is_alive() for app in started)


def test_late_successful_health_response_fails(tmp_path, monkeypatch):
    original = probe.httpx.Client.get
    def delayed(client, url, **kwargs):
        if str(url).startswith('http://127.0.0.1:') and str(url).endswith('/health'):
            time.sleep(2.05)  # gstack-timing-ok: exceed the unchanged two-second wall-health deadline with a successful response.
        return original(client, url, **kwargs)
    monkeypatch.setattr(probe.httpx.Client, 'get', delayed)
    output = tmp_path/'health'
    with pytest.raises(AssertionError):
        probe.run(output)
    report = json.loads((output/'report.json').read_text())
    assert report['status'] == 'failed' and 'health contract' in report['failures']
    assert any(sample.get('health_ms', 0) > 2000 for sample in report['guard_samples'])


def test_rollback_memory_breach_fails(tmp_path, monkeypatch):
    original_start = probe.Running.start
    original_memory = probe.psutil.virtual_memory
    starts = []
    def start(app):
        starts.append(app)
        original_start(app)
        if len(starts) == 2:
            time.sleep(.75)  # gstack-timing-ok: leave one sampling interval for the injected rollback-only memory breach.
        return app
    monkeypatch.setattr(probe.Running, 'start', start)
    monkeypatch.setattr(probe.psutil, 'virtual_memory', lambda: SimpleNamespace(available=512*1024**2)
                        if len(starts) == 2 else original_memory())
    output = tmp_path/'rollback-memory'
    with pytest.raises(AssertionError):
        probe.run(output)
    report = json.loads((output/'report.json').read_text())
    assert report['status'] == 'failed' and 'memory guard' in report['failures']
    assert all(not app.thread.is_alive() and not app.service.db.thread.is_alive() for app in starts)
