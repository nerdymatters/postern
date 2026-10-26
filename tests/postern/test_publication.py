"""Publication ordering and reconciliation (ENG-D4).

The operation worker is stepped by hand here. Its three-second cadence and its retry backoff would
otherwise decide the outcome of every ordering assertion.
"""
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from postern.app import create_app
from postern.service import Service
from postern.storage import ITEM_COLUMNS, POST_COLUMNS

from conftest import ADMIN, READER, create_post, events, seed_derived, sign_in

DEPUTY = 'deputy@example.invalid'
ORIGINAL = b'%PDF-1.4 postern original\n' + bytes(range(256)) * 16


@pytest.fixture
def allowlist():
    return [['email', 'name', 'role'], [ADMIN, 'Demo administrator', 'admin'],
            [DEPUTY, 'Deputy administrator', 'admin'], [READER, 'Demo reader', 'reader']]


async def _stop_background(service):
    loops = [t for t in service.tasks if 'background_loop' in t.get_coro().__qualname__]
    for task in loops:
        task.cancel()
    await asyncio.gather(*loops, return_exceptions=True)


@pytest.fixture
def run_operations(client):
    client.portal.call(_stop_background, client.service)

    def step(times=1):
        for _ in range(times):
            client.portal.call(client.service.process_operation)
    return step


def add_item(service, post_id, *, status='ready', data=ORIGINAL, filename='policy.pdf', pages=2, drive_file_id=None):
    """An uploaded attachment with its staged original and derived pages, without running the converter."""
    item_id = str(uuid.uuid4())
    (service.config.data_dir / 'staging' / (item_id + '.pdf')).write_bytes(data)
    seed_derived(service, item_id, pages=pages)
    digest = hashlib.sha256(data).hexdigest()

    def insert(db):
        db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,drive_file_id,derive_status,sort)"
                   " VALUES (?,?,?,'pdf',?,?,?,?,?,0)",
                   (item_id, post_id, filename, digest, len(data), pages, drive_file_id, status))
    service.db.submit(insert).result(timeout=5)
    return item_id


def set_status(service, item_id, status):
    service.db.submit(lambda db: db.execute('UPDATE items SET derive_status=? WHERE id=?', (status, item_id))).result(timeout=5)


def retry_now(service):
    """Expire the bounded backoff without waiting for it."""
    service.db.submit(lambda db: db.execute("UPDATE operations SET next_try=0 WHERE status='pending'")).result(timeout=5)


def item_row(service, item_id):
    return service.db.read(lambda db: dict(db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()))


def operations(service, post_id):
    return service.db.read(lambda db: [dict(r) for r in db.execute(
        'SELECT * FROM operations WHERE post_id=? ORDER BY created_at', (post_id,))])


def publish(client, headers, post_id):
    response = client.post(f'/api/admin/posts/{post_id}/publish', headers=headers)
    assert response.status_code == 202, response.text
    return response.json()


def admin_post(client, headers, post_id):
    posts = client.get('/api/admin/posts', headers=headers).json()['posts']
    return next(p for p in posts if p['id'] == post_id)


def wall(client, headers):
    response = client.get('/api/posts', headers=headers)
    assert response.status_code == 200, response.text
    return [p['id'] for p in response.json()['posts']]


def catalog(store):
    """Read the committed catalog from disk so the unavailability switch stays set."""
    return json.loads((store.root / 'catalog.json').read_text())


def catalog_posts(store, post_id):
    return [p for p in catalog(store)['posts'] if p['id'] == post_id]


def drafted(client, service, admin, **item):
    post = create_post(client, admin)
    return post['id'], add_item(service, post['id'], **item)


def demote_the_admin(client, service, store, allowlist):
    """Take the publishing admin's role away and make the change current."""
    store.write_allowlist([row if row[0] != ADMIN else [ADMIN, 'Demo administrator', 'reader'] for row in allowlist])
    assert client.portal.call(service.refresh) is True


@contextlib.contextmanager
def restarted(config, store, clock):
    """A second process over the same data directory, with its worker stepped by hand as above."""
    service = Service(config, store=store, clock=clock)
    with TestClient(create_app(config, service=service)) as client:
        client.service = service
        client.portal.call(_stop_background, service)
        yield client, service


def test_publish_needs_an_attachment(client, service, run_operations):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post = create_post(client, admin)
    response = client.post(f"/api/admin/posts/{post['id']}/publish", headers=admin)
    assert response.status_code == 422, response.text
    assert response.json()['error'] == 'attachment_required'
    assert 'attachment' in response.json()['message'].lower()
    assert operations(service, post['id']) == []
    assert admin_post(client, admin, post['id'])['status'] == 'draft'
    assert wall(client, sign_in(client, READER)) == []


def test_a_converting_attachment_is_never_reader_visible(client, service, store, run_operations):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    reader = sign_in(client, READER)
    post_id, item_id = drafted(client, service, admin, status='converting')
    operation = publish(client, admin, post_id)

    run_operations(3)
    assert admin_post(client, admin, post_id)['status'] == 'publishing'
    assert client.get(operation['status_url'], headers=admin).json()['status'] == 'pending'
    assert wall(client, reader) == []
    assert catalog_posts(store, post_id) == []
    view = client.post(f'/api/items/{item_id}/views', json={'attempt_id': str(uuid.uuid4())}, headers=reader)
    assert view.status_code == 403 and view.json()['error'] == 'access_denied'

    set_status(service, item_id, 'ready')
    run_operations()
    assert admin_post(client, admin, post_id)['status'] == 'published'
    assert wall(client, reader) == [post_id]


def test_a_failed_attachment_stops_the_publication_with_a_repair_message(client, service, store, run_operations):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    reader = sign_in(client, READER)
    post_id, item_id = drafted(client, service, admin, status='failed')
    publish(client, admin, post_id)

    run_operations(2)
    blocked = admin_post(client, admin, post_id)
    assert blocked['status'] == 'publishing' and 'failed to prepare' in blocked['error']
    assert catalog_posts(store, post_id) == []
    assert wall(client, reader) == []
    assert events(service, 'admin_publish') == []
    # A permanent error stays pending with a repair action rather than retrying forever or publishing.
    assert operations(service, post_id)[0]['status'] == 'pending'

    set_status(service, item_id, 'ready')
    retry_now(service)
    run_operations()
    assert admin_post(client, admin, post_id)['status'] == 'published'
    assert wall(client, reader) == [post_id]


def test_publication_waits_for_the_remote_commit(client, service, store, run_operations):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    reader = sign_in(client, READER)
    post_id, item_id = drafted(client, service, admin)
    operation = publish(client, admin, post_id)

    store.available = False
    run_operations()
    pending = admin_post(client, admin, post_id)
    # ENG-D4 asks for Waiting for Google, not a bare failure flag.
    assert pending['status'] == 'publishing' and 'Google' in pending['error']
    assert catalog_posts(store, post_id) == []
    assert wall(client, reader) == []
    view = client.post(f'/api/items/{item_id}/views', json={'attempt_id': str(uuid.uuid4())}, headers=reader)
    assert view.status_code == 403 and view.json()['error'] == 'access_denied'
    record = operations(service, post_id)[0]
    assert record['status'] == 'pending' and record['attempts'] == 1
    assert 0 < record['next_try'] - time.time() <= 60
    assert events(service, 'admin_publish') == []

    store.available = True
    retry_now(service)
    run_operations()
    published = admin_post(client, admin, post_id)
    assert published['status'] == 'published' and published['error'] is None
    assert published['published_at'] == pending['published_at']
    assert [p['status'] for p in catalog_posts(store, post_id)] == ['published']
    assert wall(client, reader) == [post_id]
    assert client.get(operation['status_url'], headers=admin).json()['status'] == 'confirmed'
    assert len(events(service, 'admin_publish')) == 1


def test_a_lost_commit_acknowledgement_is_reconciled_not_duplicated(client, service, store, run_operations):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    reader = sign_in(client, READER)
    post_id, item_id = drafted(client, service, admin)
    operation = publish(client, admin, post_id)

    store.lose_commit_ack = True
    run_operations()
    unconfirmed = admin_post(client, admin, post_id)
    assert unconfirmed['status'] == 'publishing' and unconfirmed['error']
    # The remote row landed; only the acknowledgement was lost, and that alone shows nobody anything.
    assert len(catalog_posts(store, post_id)) == 1
    assert wall(client, reader) == []
    assert events(service, 'admin_publish') == []

    retry_now(service)
    run_operations()
    confirmed = admin_post(client, admin, post_id)
    assert confirmed['status'] == 'published'
    assert confirmed['published_at'] == unconfirmed['published_at']
    assert len(events(service, 'admin_publish')) == 1
    assert len(operations(service, post_id)) == 1
    assert client.get(operation['status_url'], headers=admin).json()['status'] == 'confirmed'
    assert [p['revision'] for p in catalog_posts(store, post_id)] == [confirmed['revision']]
    assert [i['id'] for i in catalog(store)['items'] if i['post_id'] == post_id] == [item_id]
    assert wall(client, reader) == [post_id]

    run_operations(2)
    assert len(events(service, 'admin_publish')) == 1
    assert len(catalog_posts(store, post_id)) == 1


def test_repeating_a_publish_request_returns_the_same_operation(client, service, store, run_operations):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post_id, _ = drafted(client, service, admin)

    first = publish(client, admin, post_id)
    queued = admin_post(client, admin, post_id)
    second = publish(client, admin, post_id)
    assert second['operation_id'] == first['operation_id']
    assert second['status_url'] == first['status_url']
    assert len(operations(service, post_id)) == 1
    assert admin_post(client, admin, post_id)['published_at'] == queued['published_at']

    run_operations()
    after = publish(client, admin, post_id)
    assert after['operation_id'] == first['operation_id'] and after['status'] == 'published'
    assert len(operations(service, post_id)) == 1
    assert admin_post(client, admin, post_id)['published_at'] == queued['published_at']
    assert len(events(service, 'admin_publish')) == 1
    assert len(catalog_posts(store, post_id)) == 1


def test_an_archive_beats_an_older_pending_publish(client, service, store, run_operations):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    reader = sign_in(client, READER)
    post_id, item_id = drafted(client, service, admin)
    operation = publish(client, admin, post_id)

    assert client.post(f'/api/admin/posts/{post_id}/archive', headers=admin).json() == {'status': 'archived'}
    assert client.get(operation['status_url'], headers=admin).json()['status'] == 'cancelled'

    run_operations()
    assert admin_post(client, admin, post_id)['status'] == 'archived'
    assert [p['status'] for p in catalog_posts(store, post_id)] == ['archived']
    assert wall(client, reader) == []
    view = client.post(f'/api/items/{item_id}/views', json={'attempt_id': str(uuid.uuid4())}, headers=reader)
    assert view.status_code == 403 and view.json()['error'] == 'access_denied'

    # The cancelled publish job is delivered once more, as a crashed worker's would be.
    service.db.submit(lambda db: db.execute("UPDATE operations SET status='pending',next_try=0 WHERE id=?",
                                            (operation['operation_id'],))).result(timeout=5)
    run_operations(2)
    assert client.get(operation['status_url'], headers=admin).json()['status'] == 'cancelled'
    assert admin_post(client, admin, post_id)['status'] == 'archived'
    assert [p['status'] for p in catalog_posts(store, post_id)] == ['archived']
    assert events(service, 'admin_publish') == []
    assert wall(client, reader) == []


def test_publish_reports_its_operation_through_the_status_url(client, service, store, run_operations):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post_id, _ = drafted(client, service, admin)

    response = client.post(f'/api/admin/posts/{post_id}/publish', headers=admin)
    assert response.status_code == 202
    assert response.headers['cache-control'] == 'no-store'
    accepted = response.json()
    assert accepted['status'] == 'publishing'
    assert accepted['status_url'] == f"/api/admin/operations/{accepted['operation_id']}"

    pending = client.get(accepted['status_url'], headers=admin)
    assert pending.status_code == 200
    assert pending.json()['kind'] == 'publish'
    assert pending.json()['status'] == 'pending'
    assert pending.json()['post']['status'] == 'publishing'

    run_operations()
    confirmed = client.get(accepted['status_url'], headers=admin).json()
    assert confirmed['status'] == 'confirmed'
    assert confirmed['post']['status'] == 'published'


def test_originals_reach_the_store_intact_and_only_once(client, service, store, run_operations):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post_id, item_id = drafted(client, service, admin)
    publish(client, admin, post_id)

    store.lose_upload_ack = True
    run_operations()
    assert admin_post(client, admin, post_id)['status'] == 'publishing'

    retry_now(service)
    run_operations()
    assert admin_post(client, admin, post_id)['status'] == 'published'

    stored = item_row(service, item_id)
    body = (store.root / 'files' / stored['drive_file_id']).read_bytes()
    assert body == ORIGINAL
    assert hashlib.sha256(body).hexdigest() == stored['sha256']
    metadata = json.loads((store.root / 'files' / (stored['drive_file_id'] + '.meta')).read_text())
    assert metadata['sha256'] == stored['sha256'] and metadata['item_id'] == item_id
    assert len(list((store.root / 'files').glob('*'))) == 2
    assert not (service.config.data_dir / 'staging' / (item_id + '.pdf')).exists()


def test_a_stored_original_with_wrong_bytes_is_rejected(client, service, store, run_operations):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    reader = sign_in(client, READER)
    identity = 'preallocated-drive-id'
    post_id, item_id = drafted(client, service, admin, drive_file_id=identity)
    (store.root / 'files' / identity).write_bytes(ORIGINAL[:-1] + b'!')
    publish(client, admin, post_id)

    run_operations()
    rejected = admin_post(client, admin, post_id)
    assert rejected['status'] == 'publishing' and rejected['error']
    assert catalog_posts(store, post_id) == []
    assert wall(client, reader) == []
    assert events(service, 'admin_publish') == []

    (store.root / 'files' / identity).write_bytes(ORIGINAL)
    retry_now(service)
    run_operations()
    assert admin_post(client, admin, post_id)['status'] == 'published'
    assert wall(client, reader) == [post_id]


def test_an_original_that_never_reached_the_store_cannot_publish(client, service, store, run_operations):
    """ENG-D4: every included attachment needs its original acknowledged in Drive, not merely a Drive id."""
    admin = sign_in(client, ADMIN, 'Demo administrator')
    reader = sign_in(client, READER)
    post_id, item_id = drafted(client, service, admin, drive_file_id='allocated-but-never-uploaded')
    # The id was allocated and recorded, then the upload was lost and the staged copy went with it.
    (service.config.data_dir / 'staging' / (item_id + '.pdf')).unlink()
    publish(client, admin, post_id)

    run_operations(2)
    assert list((store.root / 'files').glob('*')) == []
    assert admin_post(client, admin, post_id)['status'] == 'publishing'
    assert catalog_posts(store, post_id) == []
    assert wall(client, reader) == []
    assert events(service, 'admin_publish') == []


def test_the_committed_snapshot_can_rebuild_the_post(client, service, store, run_operations):
    """ENG-D5 restores a fresh Mac from this snapshot, so it must carry every serialized column."""
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post_id, item_id = drafted(client, service, admin)
    publish(client, admin, post_id)
    run_operations()

    post = catalog_posts(store, post_id)[0]
    item = next(i for i in catalog(store)['items'] if i['id'] == item_id)
    assert [column for column in POST_COLUMNS if post.get(column) is None] == ['error']
    assert [column for column in ITEM_COLUMNS if item.get(column) is None] == ['derive_error']
    assert post['status'] == 'published' and post['publish_op_id'] and post['revision'] == item['revision']
    assert item['sha256'] == hashlib.sha256(ORIGINAL).hexdigest() and item['bytes'] == len(ORIGINAL)


def test_an_operation_whose_actor_lost_admin_is_cancelled(client, service, store, run_operations, allowlist):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    deputy = sign_in(client, DEPUTY, 'Deputy administrator')
    reader = sign_in(client, READER)
    post_id, _ = drafted(client, service, admin)
    operation = publish(client, admin, post_id)

    demote_the_admin(client, service, store, allowlist)

    run_operations(2)
    cancelled = client.get(operation['status_url'], headers=deputy).json()
    assert cancelled['status'] == 'cancelled' and cancelled['error']
    assert cancelled['post']['status'] != 'published'
    assert catalog_posts(store, post_id) == []
    assert events(service, 'admin_publish') == []
    assert wall(client, reader) == []


def test_a_cancelled_operation_leaves_the_post_publishable(client, service, store, run_operations, allowlist):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    deputy = sign_in(client, DEPUTY, 'Deputy administrator')
    reader = sign_in(client, READER)
    post_id, _ = drafted(client, service, admin)
    publish(client, admin, post_id)
    demote_the_admin(client, service, store, allowlist)
    run_operations(2)

    # ENG-D4: a permanent failure must leave a repair action, not a post nobody can ever publish.
    retry = publish(client, deputy, post_id)
    retry_now(service)
    run_operations(2)
    assert admin_post(client, deputy, post_id)['status'] == 'published'
    assert client.get(retry['status_url'], headers=deputy).json()['status'] == 'confirmed'
    assert len(events(service, 'admin_publish')) == 1
    assert wall(client, reader) == [post_id]


@pytest.mark.parametrize('interruption', ['before the remote commit', 'after the remote commit'])
def test_an_interrupted_publication_finishes_once_after_a_restart(config, store, clock, interruption):
    lost_before = interruption.startswith('before')
    with restarted(config, store, clock) as (client, service):
        admin = sign_in(client, ADMIN, 'Demo administrator')
        post_id, item_id = drafted(client, service, admin)
        publish(client, admin, post_id)
        store.available = not lost_before
        store.lose_commit_ack = not lost_before
        client.portal.call(service.process_operation)
        interrupted = admin_post(client, admin, post_id)
        assert interrupted['status'] == 'publishing'
        assert len(catalog_posts(store, post_id)) == (0 if lost_before else 1)
        store.available = True

    with restarted(config, store, clock) as (client, service):
        admin = sign_in(client, ADMIN, 'Demo administrator')
        retry_now(service)
        client.portal.call(service.process_operation)
        finished = admin_post(client, admin, post_id)
        assert finished['status'] == 'published' and finished['error'] is None
        assert finished['published_at'] == interrupted['published_at']
        assert len(events(service, 'admin_publish')) == 1
        assert len(operations(service, post_id)) == 1
        assert [p['revision'] for p in catalog_posts(store, post_id)] == [finished['revision']]
        assert [i['id'] for i in catalog(store)['items'] if i['post_id'] == post_id] == [item_id]
        assert wall(client, sign_in(client, READER)) == [post_id]
