"""Controlled concurrent HTTP disclosure checks on the selected real reducer.

These small deterministic interleavings complement the representative load
campaign; they do not measure capacity or impersonate a live staff check.
"""
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN, READER, events, open_view, ready_post, sign_in
from postern.app import create_app
from postern.db import AuditUnavailable, set_meta


@pytest.fixture
def client(config, service):
    # A small CI host must still run every interleaving. In particular, held
    # responses cannot consume workers needed by the real reader operations.
    def loop_factory():
        loop = asyncio.new_event_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        return loop
    with TestClient(create_app(config, service=service),
                    backend_options={'loop_factory': loop_factory}) as client:
        yield client


@contextmanager
def inflight(client, service, monkeypatch, route, headers, operation_name):
    """Hold eight completed snapshots before their actual disclosure checks."""
    arrived = threading.Event()
    release = client.portal.call(asyncio.Event)
    count = 0
    read = service.read_progress

    async def hold(operation):
        nonlocal count
        result = await read(operation)
        if operation.__qualname__ == operation_name:
            count += 1
            if count == 8:
                arrived.set()
            # Waiting must leave the default executor free for application I/O,
            # even on a runner with fewer workers than concurrent requests.
            await asyncio.wait_for(release.wait(), 10)
        return result

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=8) as pool:
        patch.setattr(service, 'read_progress', hold)
        pending = [pool.submit(client.get, route, headers=headers) for _ in range(8)]
        try:
            assert arrived.wait(10), 'all requests must reach the disclosure boundary'
            assert not any(future.done() for future in pending)
            yield pending, lambda: client.portal.call(release.set)
        finally:
            client.portal.call(release.set)
        assert count == 8


@pytest.mark.parametrize('interruption,status', [
    ('none', 200), ('revoke', 403), ('role', 403), ('consent', 403),
    ('recovery', 409), ('audit', 503),
])
def test_inflight_reports_recheck_access_while_reading_continues(
        client, service, monkeypatch, interruption, status):
    admin, reader = sign_in(client, ADMIN), sign_in(client)
    post, item = ready_post(client, admin, service, pages=8)
    view = open_view(client, reader, item)
    report_count = 8
    with inflight(client, service, monkeypatch, '/api/admin/progress', admin,
                  'Service.training_progress.<locals>.query') as (pending, release):
        try:
            # Serving bytes still records exact durable page evidence while
            # every report is in flight. No sleeps or elapsed-time assertions.
            for page in range(8):
                response = client.get(f"/api/views/{view['id']}/page/{page}.jpg", headers=reader)
                assert response.status_code == 200
                assert response.headers['content-type'] == 'image/jpeg'
                assert response.content.startswith(b'\xff\xd8')
            catalog = client.get('/api/posts', headers=reader)
            assert catalog.status_code == 200
            progress = next(p for p in catalog.json()['posts'] if p['id'] == post['id'])['items'][0]['progress']
            assert progress['opened_pages'] == list(range(8))
            assert len(events(service, 'page_served')) == 8
            assert not events(service, 'training_progress_viewed')

            if interruption == 'revoke':
                service.db.submit(lambda db: db.execute(
                    'UPDATE sessions SET revoked=1 WHERE email=?', (ADMIN,))).result(5)
            elif interruption == 'role':
                service.db.submit(lambda db: db.execute(
                    "UPDATE members SET role='reader' WHERE email=?", (ADMIN,))).result(5)
            elif interruption == 'consent':
                service.db.submit(lambda db: db.execute(
                    'UPDATE sessions SET consent_version=NULL WHERE email=?', (ADMIN,))).result(5)
            elif interruption == 'recovery':
                service.db.submit(lambda db: set_meta(db, 'recovery_mode', True)).result(5)
            with monkeypatch.context() as failure:
                if interruption == 'audit':
                    def unavailable(_operation):
                        future = Future()
                        future.set_exception(AuditUnavailable('synthetic writer failure'))
                        return future
                    failure.setattr(service.db, 'submit', unavailable)
                release()
                responses = [future.result(10) for future in pending]
        finally:
            release()

    for response in responses:
        assert response.status_code == status, response.text
        if interruption == 'none':
            people = {person['email']: person for person in response.json()['people']}
            assert set(people) == {ADMIN, READER}
            # Each snapshot predates the new pages; it cannot invent progress.
            assert people[READER]['items'][0]['opened_pages'] == 0
        else:
            assert 'people' not in response.json()
            assert 'items' not in response.json()
    assert len(events(service, 'training_progress_viewed')) == (report_count if interruption == 'none' else 0)
    assert len(events(service, 'page_served')) == 8


def test_concurrent_catalogs_keep_current_quiz_locks_and_durable_certificate(
        client, service, monkeypatch):
    from postern import honors
    from postern.recover import import_events
    admin, reader = sign_in(client, ADMIN), sign_in(client)
    first, _ = ready_post(client, admin, service, pages=1)
    second, _ = ready_post(client, admin, service, pages=1)
    def chapters(db):
        for number, post in enumerate((first, second), 1):
            db.execute('UPDATE posts SET category=? WHERE id=?',
                       (f'SOP Chapter {number}', post['id']))
    service.db.submit(chapters).result(5)
    baseline = client.get('/api/posts', headers=reader).json()
    assert next(p for p in baseline['posts'] if p['id'] == second['id'])['locked_by'] == 1
    assert baseline['certificate'] is None
    with inflight(client, service, monkeypatch, '/api/posts', reader,
                  'Service.posts.<locals>.snapshot') as (pending, release):
        restored = [
            {'event_id': 'concurrent-chapter-pass', 'event': 'quiz_attempt', 'ts': 100, 'email': READER,
             'detail': {'chapter': 1, 'score': 5, 'total': 5, 'passed': True}},
            {'event_id': 'concurrent-final-pass', 'event': 'quiz_attempt', 'ts': 101, 'email': READER,
             'detail': {'chapter': 0, 'score': 20, 'total': 20, 'passed': True}},
            {'event_id': 'concurrent-name', 'event': 'certificate_name', 'ts': 102, 'email': READER,
             'detail': {'name': 'Synthetic Reader'}},
        ]
        assert service.db.submit(lambda db: import_events(db, restored)).result(5) == 3
        assert service.db.submit(lambda db: import_events(db, restored)).result(5) == 0
        release()
        for future in pending:
            response = future.result(10)
            assert response.status_code == 200
            body = response.json()
            assert next(p for p in body['posts'] if p['id'] == second['id'])['locked_by'] is None
            assert next(q for q in body['quizzes'] if q['chapter'] == 1)['passed']
            assert body['certificate'] is None  # Snapshot may lag, never invent an award.
    fresh = client.get('/api/posts', headers=reader).json()
    assert fresh['certificate'] == {
        'number': honors.number('concurrent-final-pass', 101), 'awarded_at': 101,
        'name': 'Synthetic Reader', 'name_confirmed': True,
    }
    assert len(events(service, 'quiz_attempt')) == 2
    assert len(events(service, 'certificate_name')) == 1


def test_concurrent_reports_preserve_pages_across_media_export_failure_and_restore(
        client, service, store, monkeypatch, tmp_path):
    from postern import progress, recover
    from postern.config import Config
    from postern.db import Database
    admin, reader = sign_in(client, ADMIN), sign_in(client)
    _, item = ready_post(client, admin, service, pages=2)
    view = open_view(client, reader, item)
    async def stop_export_loop():
        tasks = [t for t in service.tasks if 'background_loop' in t.get_coro().__qualname__]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    client.portal.call(stop_export_loop)
    # Drain any batch created before publication completed, then seal page 0.
    for _ in range(3):
        client.portal.call(service.export)
    assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 200
    client.portal.call(service.export)

    with inflight(client, service, monkeypatch, '/api/admin/progress', admin,
                  'Service.training_progress.<locals>.query') as (pending, release):
        queued = []
        with monkeypatch.context() as media:
            def missing(*_args):
                raise FileNotFoundError('synthetic missing derivative')
            media.setattr(service, 'composite', missing)
            media.setattr(service, 'schedule_derive', queued.append)
            failed = client.get(f"/api/views/{view['id']}/page/1.jpg", headers=reader)
            assert failed.status_code == 409 and failed.json()['error'] == 'preparing'
            assert queued == [item]
            assert len(events(service, 'page_served')) == 1
        # The private fixture still has its page; model a completed re-derivation.
        service.db.submit(lambda db: db.execute("UPDATE items SET derive_status='ready' WHERE id=?", (item,))).result(5)
        assert client.get(f"/api/views/{view['id']}/page/1.jpg", headers=reader).status_code == 200
        before = [(row['id'], row['payload']) for row in events(service, 'page_served')]
        with monkeypatch.context() as cloud:
            def unavailable(*_args, **_kwargs):
                raise OSError('synthetic cloud unavailable')
            cloud.setattr(store, 'put_file', unavailable)
            with pytest.raises(OSError, match='synthetic cloud unavailable'):
                client.portal.call(service.export)
        assert [(row['id'], row['payload']) for row in events(service, 'page_served')] == before
        assert service.db.read(lambda db: db.execute('SELECT count(*) FROM batches WHERE confirmed_at IS NULL').fetchone()[0]) == 1
        client.portal.call(service.export)
        assert service.db.read(lambda db: db.execute('SELECT count(*) FROM batches WHERE confirmed_at IS NULL OR summary_confirmed=0').fetchone()[0]) == 0
        release()
        assert all(future.result(10).status_code == 200 for future in pending)

    restored = Config(data_dir=tmp_path/'restored', mode='demo', notice_version='1').validate()
    recover.restore(restored, store, log=lambda _message: None)
    database = Database(restored.data_dir/'postern.sqlite3')
    try:
        rows = database.read(lambda db: [tuple(row) for row in db.execute(
            "SELECT id,payload FROM events WHERE type='page_served' ORDER BY seq")])
        # Export sorts JSON keys and recovery assigns new local sequence numbers.
        # Event identity and every payload field, including clocks, are durable.
        assert len(rows) == len(before) == 2
        assert {identity: json.loads(payload) for identity, payload in rows} == {
            identity: json.loads(payload) for identity, payload in before}
        state = database.read(lambda db: progress.history(db, {item: {'kind': 'pdf', 'pages': 2}}, READER))
        assert state[READER, item]['pages'] == {0, 1}
    finally:
        database.close()
