"""Backend selection, real HTTP protection and rollback across the same durable store."""
import importlib.util
import json
import os

import pytest

from conftest import ADMIN, READER, open_view, ready_post, sign_in
from postern import progress_backend
from postern.db import event, set_meta
from postern.service import Service


def test_python_needs_no_native_module_and_invalid_selection_fails_early(monkeypatch, config):
    def unavailable(_name):
        raise ImportError('synthetic missing wheel')
    monkeypatch.setattr(progress_backend.importlib, 'import_module', unavailable)
    progress_backend.native.cache_clear()
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'python')
    assert progress_backend.selected() == 'python'
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'rust')
    with pytest.raises(RuntimeError, match='verified'):
        Service(config)
    assert not (config.data_dir / 'postern.sqlite3').exists()
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'typo')
    with pytest.raises(RuntimeError, match='python or rust'):
        Service(config)
    progress_backend.native.cache_clear()


@pytest.mark.parametrize('api,checkpoint,method', [(99, 1, True), (1, None, True), (1, 99, True), (1, 1, False)])
def test_incompatible_module_refuses_selection(monkeypatch, api, checkpoint, method):
    from types import SimpleNamespace
    progress_backend.native.cache_clear()
    monkeypatch.setattr(progress_backend.importlib, 'import_module', lambda _: SimpleNamespace(
        API_VERSION=api, CHECKPOINT_VERSION=checkpoint, reduce_incremental=(lambda *args: None) if method else None))
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'rust')
    with pytest.raises(RuntimeError, match='incompatible'):
        progress_backend.selected()
    progress_backend.native.cache_clear()


native_required = pytest.mark.skipif(importlib.util.find_spec('postern_progress_native') is None,
                                     reason='optional native wheel not installed')


@native_required
def test_restart_rollback_keeps_activity_and_certificate(config, store, clock, monkeypatch):
    from fastapi.testclient import TestClient
    from postern.app import create_app
    from postern import honors
    from postern.recover import import_events

    def durable(db):
        return [tuple(r) for r in db.execute(
            "SELECT id,payload FROM events WHERE type IN ('page_served','quiz_attempt','certificate_name') ORDER BY seq")]

    for phase, backend in enumerate(('python', 'rust', 'python')):
        monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', backend)
        service = Service(config, store=store, clock=clock)
        with TestClient(create_app(config, service=service)) as client:
            if phase == 0:
                admin = sign_in(client, ADMIN)
                post, item = ready_post(client, admin, service, pages=3)
                reader = sign_in(client)
                view = open_view(client, reader, item)
                assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 200
            elif phase == 1:
                assert service.progress_history.full_replays == 1
                assert service.progress_history.retained_bytes > 0
                view = open_view(client, reader, item)
                assert client.get(f"/api/views/{view['id']}/page/2.jpg", headers=reader).status_code == 200
                service.db.submit(lambda db: import_events(db, [
                    {'event_id': 'first-pass', 'event': 'quiz_attempt', 'ts': 100, 'email': READER,
                     'detail': {'chapter': 0, 'score': 16, 'total': 20, 'passed': True}},
                    {'event_id': 'saved-name', 'event': 'certificate_name', 'ts': 101, 'email': READER,
                     'detail': {'name': 'Synthetic Reader'}},
                ])).result()
                snapshot = service.db.read(durable)
                schema = service.db.read(lambda db: db.execute('PRAGMA schema_version').fetchone()[0])
            response = client.get('/api/posts', headers=reader)
            assert response.status_code == 200
            data = response.json()
            progress = next(p for p in data['posts'] if p['id'] == post['id'])['items'][0]['progress']
            if phase >= 1:
                assert progress['opened_pages'] == [0, 2]
                assert data['certificate'] == {'number': honors.number('first-pass', 100), 'awarded_at': 100,
                                               'name': 'Synthetic Reader', 'name_confirmed': True}
                assert service.db.read(durable) == snapshot
                assert service.db.read(lambda db: db.execute('PRAGMA schema_version').fetchone()[0]) == schema
            else:
                assert progress['opened_pages'] == [0]


@native_required
def test_failed_startup_prime_closes_writer_and_progress_executor(config, store, clock, monkeypatch):
    from fastapi.testclient import TestClient
    from postern.app import create_app
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'rust')
    service = Service(config, store=store, clock=clock)
    def failed(*args):
        raise RuntimeError('injected checkpoint failure')
    monkeypatch.setattr(progress_backend.native(), 'reduce_incremental', failed)
    with pytest.raises(RuntimeError, match='injected checkpoint failure'):
        with TestClient(create_app(config, service=service)):
            pytest.fail('startup failure must not admit requests')
    assert not service.db.thread.is_alive()
    assert service.progress_history.stamp is None
    with pytest.raises(RuntimeError, match='shutdown'):
        service.progress_executor.submit(lambda: None)


@native_required
def test_legacy_catalog_error_stays_at_request_boundary_after_restart(config, store, clock, monkeypatch):
    from fastapi.testclient import TestClient
    from postern.app import create_app
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'rust')
    first = Service(config, store=store, clock=clock)
    with TestClient(create_app(config, service=first)) as client:
        admin = sign_in(client, ADMIN)
        _post, item = ready_post(client, admin, first, pages=3)
        first.db.submit(lambda db: (
            event(db, 'page_served', email=READER, item_id=item, detail={'page': 0}),
            db.execute('UPDATE items SET pages=? WHERE id=?', ('legacy', item)))).result(5)
    restarted = Service(config, store=store, clock=clock)
    with TestClient(create_app(config, service=restarted)) as client:
        assert restarted.progress_history.stamp is None
        assert client.get('/health').status_code == 200
        with pytest.raises(TypeError):
            client.get('/api/admin/progress', headers=admin)
    assert not restarted.db.thread.is_alive()


@native_required
def test_a_rust_service_records_its_own_staff_report_checks(config, store, clock, monkeypatch):
    # Value: protects=a Rust-serving API writes its own check record at startup and compares an extended staff report with Python; fails_when=the service never configures the record or never compares; why_new=unit tests drive HistoryCache directly; seam=none; no_claim=watchdog reaction (selection tests)
    from fastapi.testclient import TestClient
    from postern.app import create_app
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'rust')
    service = Service(config, store=store, clock=clock)
    check = config.data_dir / progress_backend.CHECK
    with TestClient(create_app(config, service=service)) as client:
        start = json.loads(check.read_text())
        assert (start['pid'], start['checks'], start['native_errors'], start['mismatches']) == (os.getpid(), 0, 0, 0)
        admin = sign_in(client, ADMIN)
        post, item = ready_post(client, admin, service, pages=3)
        reader = sign_in(client)
        view = open_view(client, reader, item)
        for page in (0, 1):
            assert client.get(f"/api/views/{view['id']}/page/{page}.jpg", headers=reader).status_code == 200
            assert client.get('/api/admin/progress', headers=admin).status_code == 200
    assert service.progress_history.delta_replays >= 1
    record = json.loads(check.read_text())
    assert (record['checks'], record['mismatches'], record['last_check'], record['native_errors']) == (1, 0, 'match', 0)


@native_required
def test_http_progress_and_rollback_preserve_new_activity(client, service, monkeypatch):
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'python')
    admin = sign_in(client, ADMIN)
    post, item = ready_post(client, admin, service, pages=3)
    reader = sign_in(client)
    view = open_view(client, reader, item)
    assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 200
    def progress():
        response = client.get('/api/posts', headers=reader)
        assert response.status_code == 200
        return next(p for p in response.json()['posts'] if p['id'] == post['id'])['items'][0]['progress']
    before = progress()
    schema_before = service.db.read(lambda db: db.execute('PRAGMA schema_version').fetchone()[0])
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'rust')
    assert progress() == before
    assert client.get(f"/api/views/{view['id']}/page/2.jpg", headers=reader).status_code == 200
    after = progress()
    assert after['opened_pages'] == [0, 2]
    # Keep a first passing exam event and saved name across the switch, too.
    def award(db):
        identity = event(db, 'quiz_attempt', email=READER, timestamp=100,
                         detail={'chapter': 0, 'score': 100, 'passed': True, 'total': 10})
        event(db, 'certificate_name', email=READER, timestamp=101, detail={'name': 'Synthetic Reader'})
        return identity
    identity = service.db.submit(award).result()
    rows_before = service.db.read(lambda db: list(db.execute("SELECT id,payload FROM events WHERE type IN ('page_served','quiz_attempt','certificate_name') ORDER BY seq")))
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'python')
    assert progress() == after
    rows_after = service.db.read(lambda db: list(db.execute("SELECT id,payload FROM events WHERE type IN ('page_served','quiz_attempt','certificate_name') ORDER BY seq")))
    assert [tuple(r) for r in rows_after] == [tuple(r) for r in rows_before]
    assert any(r['id'] == identity for r in rows_after)
    assert service.db.read(lambda db: db.execute('PRAGMA schema_version').fetchone()[0]) == schema_before
    assert client.get('/api/admin/progress', headers=reader).status_code == 403
    assert client.get('/api/posts').status_code == 401


@native_required
@pytest.mark.parametrize('interruption,status', [('revoke', 403), ('recovery', 409), ('audit', 503)])
def test_native_replay_does_not_bypass_final_disclosure_gate(client, service, monkeypatch, interruption, status):
    from postern.db import AuditUnavailable
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'rust')
    admin = sign_in(client, ADMIN)
    ready_post(client, admin, service)
    module = progress_backend.native()
    method = 'reduce_incremental' if service.progress_history is not None else 'reduce'
    original = getattr(module, method)
    def interrupt(*args):
        result = original(*args)
        if interruption == 'revoke':
            service.db.submit(lambda db: db.execute('UPDATE sessions SET revoked=1 WHERE email=?', (ADMIN,))).result()
        elif interruption == 'recovery':
            service.db.submit(lambda db: set_meta(db, 'recovery_mode', True)).result()
        else:
            def failed(_operation):
                import concurrent.futures
                future = concurrent.futures.Future()
                future.set_exception(AuditUnavailable('synthetic audit failure'))
                return future
            monkeypatch.setattr(service.db, 'submit', failed)
        return result
    monkeypatch.setattr(module, method, interrupt)
    response = client.get('/api/admin/progress', headers=admin)
    assert response.status_code == status
    assert 'people' not in response.json()
