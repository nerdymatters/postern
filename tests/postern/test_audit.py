"""ENG-D7: content that cannot be logged is never served.

Failures are injected at the writer, never at the HTTP layer: the real writer thread opens its real
transaction and then fails it the way an unwritable database, a saturated queue and a blown 1.5 s
bound do. Every assertion about "no content" is made against the bytes the client received.
"""
from __future__ import annotations
import contextlib
import sqlite3
import threading
import time

from conftest import ADMIN, READER, events, open_view, ready_post, sign_in

JPEG = b'\xff\xd8\xff'


@contextlib.contextmanager
def broken_audit(service):
    """Every queued transaction fails inside the writer, as it does on a read-only database."""
    original = service.db.submit

    def unwritable(db):
        raise sqlite3.OperationalError('attempt to write a readonly database')

    service.db.submit = lambda operation: original(unwritable)
    try:
        yield
    finally:
        service.db.submit = original


@contextlib.contextmanager
def stalled_writer(service):
    """Occupy the single writer thread so everything queued behind it waits on the clock."""
    running, release = threading.Event(), threading.Event()

    def occupy(db):
        running.set()
        release.wait(10)

    service.db.submit(occupy)
    assert running.wait(2), 'the writer never picked the blocking job up'
    try:
        yield
    finally:
        release.set()


def reading_room(client):
    """An admin, a published post with a ready attachment, and a reader holding an open view."""
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post, item_id = ready_post(client, admin, client.service)
    reader = sign_in(client, READER)
    view = open_view(client, reader, item_id)
    return {'admin': admin, 'reader': reader, 'post': post, 'item_id': item_id, 'view': view,
            'urls': {'api': '/api/posts',
                     'thumbnail': f"/api/posts/{post['id']}/thumb.jpg",
                     'page': f"/api/views/{view['id']}/page/0.jpg"}}


def test_a_failed_audit_write_sends_zero_protected_bytes(client):
    room = reading_room(client)
    with broken_audit(client.service):
        refused = {name: client.get(url, headers=room['reader']) for name, url in room['urls'].items()}

    for name, response in refused.items():
        body = response.json()
        assert response.status_code == 503, (name, response.status_code, response.text)
        assert set(body) == {'error', 'message'} and body['error'] == 'audit_unavailable', (name, body)
        assert response.headers['content-type'].startswith('application/json'), name
        assert JPEG not in response.content, (name, response.content[:64])
        assert room['post']['title'].encode() not in response.content, name
    assert events(client.service, 'item_open') == []
    assert events(client.service, 'page_served') == []

    # The same three URLs do carry protected bytes once the audit recovers, so the refusals above
    # withheld something real; the page is the only one of the three that counts as an open.
    for name, url in room['urls'].items():
        served = client.get(url, headers=room['reader'])
        assert served.status_code == 200, (name, served.text)
        assert served.content[:64] not in refused[name].content, name
        if name == 'api':
            assert room['post']['title'].encode() in served.content
        else:
            assert served.content[:3] == JPEG, name
    assert [e['view_id'] for e in events(client.service, 'item_open')] == [room['view']['id']]


def test_a_saturated_write_queue_fails_closed(client):
    room = reading_room(client)
    with stalled_writer(client.service):
        for _ in range(client.service.db.queue.maxsize):
            client.service.db.submit(lambda db: None)
        assert client.service.db.queue.full()
        started = time.monotonic()
        refused = client.get(room['urls']['page'], headers=room['reader'])
        waited = time.monotonic() - started

    assert refused.status_code == 503 and refused.json()['error'] == 'audit_unavailable', refused.text
    assert set(refused.json()) == {'error', 'message'}
    assert waited < 1.0, waited  # a full queue refuses at once rather than spending the 1.5 s bound
    assert JPEG not in refused.content
    assert events(client.service, 'item_open') == []
    assert events(client.service, 'page_served') == []


def test_the_1500_ms_audit_bound_fails_closed(client):
    room = reading_room(client)
    with stalled_writer(client.service):
        started = time.monotonic()
        refused = client.get(room['urls']['page'], headers=room['reader'])
        waited = time.monotonic() - started

    assert refused.status_code == 503 and refused.json()['error'] == 'audit_unavailable', refused.text
    assert 1.5 <= waited < 3.0, waited  # queue plus write waiting is bounded at 1500 ms
    assert JPEG not in refused.content
    assert events(client.service, 'item_open') == []
    assert events(client.service, 'page_served') == []


def test_a_contended_write_lock_fails_closed(client):
    """No monkeypatching here: another writer simply holds the database past the bounded wait."""
    room = reading_room(client)
    blocker = sqlite3.connect(client.service.db.path, timeout=1)
    blocker.execute('BEGIN IMMEDIATE')
    blocker.execute("INSERT INTO meta(key,value) VALUES ('contention','1')")
    try:
        refused = client.get(room['urls']['page'], headers=room['reader'])
    finally:
        blocker.rollback()
        blocker.close()

    assert refused.status_code == 503 and refused.json()['error'] == 'audit_unavailable', refused.text
    assert JPEG not in refused.content
    assert events(client.service, 'item_open') == []
    assert client.get(room['urls']['page'], headers=room['reader']).status_code == 200


def test_health_reports_the_audit_failure_and_recovers(client):
    room = reading_room(client)
    assert client.get('/health').json()['audit'] == 'ready'

    with broken_audit(client.service):
        assert client.get(room['urls']['page'], headers=room['reader']).status_code == 503
        public = client.get('/health')
        assert public.status_code == 200 and public.json()['audit'] == 'unavailable'
        assert client.get('/api/config').json()['status']['audit'] == 'unavailable'
        # /api/admin/health is itself a protected body, so it fails closed like every other one.
        # Its refusal code is the failure state the admin screen renders; what it must never do is
        # report a healthy audit while writes are failing.
        refused = client.get('/api/admin/health', headers=room['admin'])
        assert refused.status_code == 503 and refused.json()['error'] == 'audit_unavailable', refused.text

    assert client.get(room['urls']['page'], headers=room['reader']).status_code == 200
    assert client.get('/health').json()['audit'] == 'ready'
    recovered = client.get('/api/admin/health', headers=room['admin'])
    assert recovered.status_code == 200 and recovered.json()['audit'] == 'ready', recovered.text


def test_the_attempt_is_durable_when_no_body_ever_reaches_the_reader(client):
    room = reading_room(client)
    view_id = room['view']['id']

    # Two ways a request can be authorized and audited and still produce nothing: a page outside the
    # document, and a derivative that disappeared between the audit commit and the read.
    outside = client.get(f'/api/views/{view_id}/page/99.jpg', headers=room['reader'])
    assert outside.status_code == 404 and outside.json()['error'] == 'page_not_found', outside.text
    (client.service.config.data_dir / 'derived' / room['item_id'] / 'page-1.jpg').unlink()
    evicted = client.get(f'/api/views/{view_id}/page/1.jpg', headers=room['reader'])
    assert evicted.status_code == 409 and evicted.json()['error'] == 'preparing', evicted.text
    assert JPEG not in outside.content and JPEG not in evicted.content

    attempts = [e for e in events(client.service, 'page_requested') if e['view_id'] == view_id]
    assert len(attempts) == 2
    assert events(client.service, 'item_open') == []
    assert events(client.service, 'page_served') == []
    view = client.service.db.read(lambda db: dict(db.execute('SELECT * FROM views WHERE id=?', (view_id,)).fetchone()))
    assert view['first_served_at'] is None and view['served_pages'] == '[]'

    report = client.get(f"/api/admin/posts/{room['post']['id']}/report", headers=room['admin']).json()
    person = next(p for p in report['people'] if p['email'] == READER)
    assert person['status'] == 'not_opened' and person['attempts'] == 1 and report['opened'] == 0
