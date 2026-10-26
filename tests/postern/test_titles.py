"""Title changes preserve content identity, activity, visibility and recovery."""
import asyncio
import json

import pytest

from postern.db import Database
from postern.recover import restore_catalog
from postern.service import Service
from conftest import ADMIN, READER, create_post, events, open_view, ready_post, sign_in


@pytest.fixture(autouse=True)
def pause_worker(monkeypatch):
    async def paused(_service):
        await asyncio.Event().wait()
    monkeypatch.setattr(Service, 'background_loop', paused)


def rename(client, headers, post, title='Updated training guide'):
    return client.post(f"/api/admin/posts/{post['id']}/rename", headers=headers, json={'title': title})


def published(client, service, headers):
    post, item = ready_post(client, headers, service, publish=False)
    assert client.post(f"/api/admin/posts/{post['id']}/publish", headers=headers).status_code == 202
    client.portal.call(service.process_operation)
    return post, item


@pytest.mark.parametrize('status', ['draft', 'published', 'archived'])
def test_rename_preserves_content_and_recovers_title(client, service, store, tmp_path, status):
    admin = sign_in(client, ADMIN)
    reader = sign_in(client)
    if status == 'draft':
        post, item = ready_post(client, admin, service, publish=False)
    else:
        post, item = published(client, service, admin)
        view = open_view(client, reader, item)
        assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 200
        if status == 'archived':
            client.post(f"/api/admin/posts/{post['id']}/archive", headers=admin)
            client.portal.call(service.process_operation)
    other = create_post(client, admin)
    before = next(p for p in client.get('/api/admin/posts', headers=admin).json()['posts'] if p['id'] == post['id'])
    before_events = events(service, 'page_served')
    before_items = store.catalog()['items']
    result = rename(client, admin, post, '  Updated training guide  ')
    assert result.status_code == 200
    after = result.json()
    assert after['title'] == 'Updated training guide'
    for field in ('id', 'items', 'status', 'category', 'body', 'created_at', 'published_at'):
        assert after[field] == before[field]
    assert after['title_sync_pending'] == (status != 'draft')
    assert events(service, 'page_served') == before_events
    unchanged = next(p for p in client.get('/api/admin/posts', headers=admin).json()['posts'] if p['id'] == other['id'])
    assert unchanged['title'] == other['title']
    audit = json.loads(events(service, 'admin_rename_post')[0]['payload'])
    assert audit['email'] == ADMIN and audit['detail'] == {'from': before['title'], 'to': after['title']}
    visible = client.get('/api/posts', headers=reader).json()['posts']
    if status == 'published':
        assert visible[0]['title'] == after['title']
        assert visible[0]['items'][0]['progress']['opened_pages'] == [0]
        assert client.get(f"/api/views/{view['id']}/page/1.jpg", headers=reader).status_code == 200
    else:
        assert not visible
    if status == 'draft':
        client.post(f"/api/admin/posts/{post['id']}/publish", headers=admin)
    client.portal.call(service.process_operation)
    if status != 'draft':
        assert store.catalog()['items'] == before_items
    recovered = Database(tmp_path/'recovered.sqlite3')
    try:
        restore_catalog(recovered, store)
        saved = recovered.read(lambda db: dict(db.execute('SELECT * FROM posts WHERE id=?', (post['id'],)).fetchone()))
        assert saved['title'] == after['title']
        assert saved['status'] == ('published' if status == 'draft' else status)
    finally:
        recovered.close()


def test_validation_authorization_and_noop(client, service):
    admin = sign_in(client, ADMIN)
    post = create_post(client, admin)
    assert rename(client, {}, post).status_code == 401
    assert rename(client, sign_in(client), post).status_code == 403
    for title in ('', '   ', 'x'*121):
        assert rename(client, admin, post, title).status_code == 422
    assert rename(client, admin, {'id': 'missing'}).status_code == 403
    assert rename(client, admin, post, post['title']).json()['revision'] == post['revision']
    assert not events(service, 'admin_rename_post')
    assert rename(client, admin, post, 'x'*120).status_code == 200


def test_pending_publication_or_category_rejects_title_change(client, service):
    admin = sign_in(client, ADMIN)
    post, _ = ready_post(client, admin, service, publish=False)
    client.post(f"/api/admin/posts/{post['id']}/publish", headers=admin)
    assert rename(client, admin, post).json()['error'] == 'post_busy'
    client.portal.call(service.process_operation)
    client.post('/api/admin/categories/rename', headers=admin, json={'category': 'Policies', 'name': 'Welcome'})
    assert rename(client, admin, post).json()['error'] == 'post_busy'
    assert not events(service, 'admin_rename_post')
    client.portal.call(service.process_operation)
    assert rename(client, admin, post).status_code == 200
    assert client.post('/api/admin/categories/rename', headers=admin, json={'category': 'Welcome', 'name': 'Introduction'}).json()['error'] == 'category_busy'


def test_title_backup_retries_after_ack_loss_and_admin_departure(client, service, store):
    admin = sign_in(client, ADMIN)
    post, _ = published(client, service, admin)
    assert rename(client, admin, post).status_code == 200
    store.available = False
    client.portal.call(service.process_operation)
    current = client.get('/api/admin/posts', headers=admin).json()['posts'][0]
    assert current['title_sync_pending'] and current['error'] and current['status'] == 'published'
    assert rename(client, admin, post, 'Another title').json()['error'] == 'post_busy'
    store.available = True
    store.lose_commit_ack = True
    store.write_allowlist([['email', 'name', 'role'], [READER, 'Reader', 'reader']])
    client.portal.call(service.refresh)
    for _ in range(2):
        service.db.submit(lambda db: db.execute('UPDATE operations SET next_try=0')).result()
        client.portal.call(service.process_operation)
    assert len(store.catalog()['posts']) == 1
    assert store.catalog()['posts'][0]['title'] == 'Updated training guide'
    assert not service.db.read(lambda db: db.execute("SELECT 1 FROM operations WHERE status='pending'").fetchone())


def test_archive_supersedes_pending_title_backup(client, service, store):
    admin = sign_in(client, ADMIN)
    post, _ = published(client, service, admin)
    rename(client, admin, post)
    client.post(f"/api/admin/posts/{post['id']}/archive", headers=admin)
    client.portal.call(service.process_operation)
    saved = store.catalog()['posts'][0]
    assert saved['status'] == 'archived' and saved['title'] == 'Updated training guide'
    assert not client.get('/api/posts', headers=sign_in(client)).json()['posts']
