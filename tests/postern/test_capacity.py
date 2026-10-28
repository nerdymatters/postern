"""Capacity regressions with synthetic identities; never contact Google or production."""
import concurrent.futures
import threading
import sqlite3

import pytest

from conftest import READER, sign_in
from postern.auth import Refusal


@pytest.mark.parametrize('failure', [sqlite3.OperationalError('database unavailable'), OSError('read failed')])
def test_catalog_read_failure_returns_retryable_error_without_content(client, service, monkeypatch, failure):
    import postern.service as module
    headers = sign_in(client)
    original = module.annotate
    def fail(*args):
        raise failure
    monkeypatch.setattr(module, 'annotate', fail)
    response = client.get('/api/posts', headers=headers)
    assert response.status_code == 503
    assert response.json()['error'] == 'catalog_unavailable'
    assert 'posts' not in response.json()
    assert response.headers['cache-control'] == 'no-store'
    monkeypatch.setattr(module, 'annotate', original)
    assert client.get('/api/posts', headers=headers).status_code == 200


def test_fifty_distinct_staff_can_sign_in_from_one_restaurant(client, service, store):
    emails = [f'staff{i}@example.invalid' for i in range(50)]
    store.write_allowlist([['email', 'name', 'role']] + [[email, email, 'reader'] for email in emails])
    client.portal.call(service.refresh)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(lambda email: client.post('/api/auth/demo', json={'email': email}), emails))
    assert [r.status_code for r in responses] == [200]*50
    assert {r.json()['email'] for r in responses} == set(emails)


def test_account_limit_survives_changing_network_address(client, service):
    for _ in range(10):
        sign_in(client, consent=False)
    # Verified-account limits are independent of the IP burst budget.
    service.limits = {key: value for key, value in service.limits.items() if key.startswith('account:')}
    response = client.post('/api/auth/demo', json={'email': READER})
    assert response.status_code == 429


def test_reader_history_does_not_block_writer_and_revocation_wins(client, service, monkeypatch):
    import postern.service as module
    headers = sign_in(client)
    entered, release = threading.Event(), threading.Event()
    original = module.annotate
    def slow(db, posts, email):
        entered.set()
        assert release.wait(5)
        return original(db, posts, email)
    monkeypatch.setattr(module, 'annotate', slow)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(client.get, '/api/posts', headers=headers)
        assert entered.wait(3)
        try:
            service.db.submit(lambda db: db.execute('UPDATE sessions SET revoked=1 WHERE email=?', (READER,))).result(timeout=1)
        finally:
            release.set()
        assert pending.result(5).status_code == 403


@pytest.mark.parametrize('refused', [False, True], ids=['completed read', 'refused read'])
def test_every_history_read_closes_its_database_connection(service, monkeypatch, refused):
    # Each unclosed reader kept a file descriptor until the API ran out and refused new connections.
    import postern.db as module
    opened, connect = [], module.sqlite3.connect
    def tracked(*args, **kwargs):
        opened.append(connect(*args, **kwargs))
        return opened[-1]
    monkeypatch.setattr(module.sqlite3, 'connect', tracked)
    def operation(db):
        db.execute('SELECT count(*) FROM members').fetchone()
        if refused:
            raise Refusal(403, 'access_denied', 'Your access is no longer available.')
    for _ in range(3):
        try:
            service.db.read(operation)
        except Refusal:
            assert refused
    assert len(opened) == 3
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute('SELECT 1')


def test_catalog_returns_retryable_error_when_no_reader_can_open(client, service, monkeypatch):
    # Value: protects=staff get the retryable 503 catalog_unavailable when SQLite cannot open a reader, as at the open-file limit;
    #   fails_when=read() opens its connection inside the try, so cleanup raises UnboundLocalError and masks the sqlite3 error;
    #   why_new=every existing read failure is raised after the reader opened; seam=none;
    #   no_claim=file-descriptor counts, the writer, or how other read callers map the error
    import postern.db as module
    headers = sign_in(client)
    connect = module.sqlite3.connect
    def exhausted(*args, **kwargs):
        raise sqlite3.OperationalError('unable to open database file')
    monkeypatch.setattr(module.sqlite3, 'connect', exhausted)
    response = client.get('/api/posts', headers=headers)
    monkeypatch.setattr(module.sqlite3, 'connect', connect)
    assert response.status_code == 503 and response.json()['error'] == 'catalog_unavailable'
    assert 'posts' not in response.json()
    assert client.get('/api/posts', headers=headers).status_code == 200


def test_catalog_drops_content_archived_during_history_read(client, service, monkeypatch):
    import postern.service as module
    from postern.recover import import_events
    from test_training_progress import seed_item
    headers = sign_in(client)
    post, _ = seed_item(service)
    # Value: protects=archiving content mid-read never hides a certificate, which does not depend on the catalog;
    #   fails_when=the snapshot drops the certificate along with archived content; why_new=this race's reader had none; seam=none
    service.db.submit(lambda db: import_events(db, [{'event_id': 'final-pass', 'event': 'quiz_attempt', 'ts': 50, 'email': READER,
        'detail': {'chapter': 0, 'score': 16, 'total': 20, 'passed': True}}])).result()
    before = client.get('/api/posts', headers=headers).json()['certificate']
    assert before is not None
    entered, release = threading.Event(), threading.Event()
    original = module.annotate
    def slow(db, posts, email):
        assert db.execute('PRAGMA query_only').fetchone()[0] == 1
        entered.set()
        assert release.wait(5)
        return original(db, posts, email)
    monkeypatch.setattr(module, 'annotate', slow)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(client.get, '/api/posts', headers=headers)
        assert entered.wait(3)
        try:
            service.db.submit(lambda db: db.execute("UPDATE posts SET status='archived' WHERE id=?", (post,))).result(timeout=1)
        finally:
            release.set()
        response = pending.result(5)
    assert response.status_code == 200 and response.json()['posts'] == [] and response.json()['certificate'] == before


def test_ip_burst_limit_remains_bounded(client, service):
    for _ in range(300):
        service.rate_limit('ip:192.0.2.1')
    with pytest.raises(Refusal) as refused:
        service.rate_limit('ip:192.0.2.1')
    assert refused.value.status == 429


def test_catalog_hides_snapshot_when_recovery_starts_during_history_read(client, service, monkeypatch):
    import postern.service as module
    from postern.db import set_meta
    from test_training_progress import seed_item
    from postern.recover import import_events
    headers = sign_in(client)
    seed_item(service)
    # Value: protects=a certificate read before recovery starts is withheld once it has; fails_when=disclose() drops
    #   its recovery check; why_new=the race test's reader had no certificate; seam=none
    service.db.submit(lambda db: import_events(db, [{'event_id': 'final-pass', 'event': 'quiz_attempt', 'ts': 50, 'email': READER,
        'detail': {'chapter': 0, 'score': 16, 'total': 20, 'passed': True}}])).result()
    assert client.get('/api/posts', headers=headers).json()['certificate'] is not None
    entered, release = threading.Event(), threading.Event()
    original = module.annotate
    def slow(db, posts, email):
        assert posts
        entered.set()
        assert release.wait(5)
        return original(db, posts, email)
    monkeypatch.setattr(module, 'annotate', slow)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(client.get, '/api/posts', headers=headers)
        assert entered.wait(3)
        try:
            service.db.submit(lambda db: set_meta(db, 'recovery_mode', True)).result(timeout=1)
        finally:
            release.set()
        response = pending.result(5)
    assert response.status_code == 200 and response.json()['posts'] == [] and response.json()['certificate'] is None
