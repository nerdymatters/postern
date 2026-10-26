"""Category renames preserve identities, visibility, activity and recovery data."""
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


def rename(client, headers, category='Policies', name='Team handbook'):
    return client.post('/api/admin/categories/rename', headers=headers, json={'category': category, 'name': name})


def published(client, service, headers):
    post, item = ready_post(client, headers, service, publish=False)
    assert client.post(f"/api/admin/posts/{post['id']}/publish", headers=headers).status_code == 202
    client.portal.call(service.process_operation)
    return post, item


def test_rename_preserves_progress_visibility_and_catalog_recovery(client, service, store, tmp_path):
    admin = sign_in(client, ADMIN)
    reader = sign_in(client)
    post, item = published(client, service, admin)
    archived, _ = published(client, service, admin)
    client.post(f"/api/admin/posts/{archived['id']}/archive", headers=admin)
    client.portal.call(service.process_operation)
    draft = create_post(client, admin)
    view = open_view(client, reader, item)
    assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 200
    before_items = store.catalog()['items']
    before_events = events(service, 'page_served')
    result = rename(client, admin)
    assert result.json() == {'name': 'Team handbook', 'updated': 3}
    posts = client.get('/api/admin/posts', headers=admin).json()['posts']
    assert {p['category'] for p in posts} == {'Team handbook'}
    assert {p['id']: p['status'] for p in posts} == {post['id']: 'published', archived['id']: 'archived', draft['id']: 'draft'}
    assert sum(p['category_sync_pending'] for p in posts) == 2
    visible = client.get('/api/posts', headers=reader).json()['posts']
    assert [p['id'] for p in visible] == [post['id']]
    assert visible[0]['items'][0]['progress']['opened_pages'] == [0]
    assert events(service, 'page_served') == before_events
    audit = json.loads(events(service, 'admin_rename_category')[0]['payload'])
    assert audit['email'] == ADMIN and set(audit['detail']['post_ids']) == {p['id'] for p in posts}
    client.portal.call(service.process_operation)
    client.portal.call(service.process_operation)
    assert store.catalog()['items'] == before_items
    assert {p['category'] for p in store.catalog()['posts']} == {'Team handbook'}
    assert not any(p['category_sync_pending'] for p in client.get('/api/admin/posts', headers=admin).json()['posts'])
    recovered = Database(tmp_path/'recovered.sqlite3')
    try:
        restore_catalog(recovered, store)
        assert recovered.read(lambda db: db.execute("SELECT count(*) FROM posts WHERE category='Team handbook'").fetchone()[0]) == 2
    finally:
        recovered.close()


@pytest.mark.parametrize('source', ['module1', 'MODULE 1', ' Module 1 '])
def test_rename_matches_the_reading_room_group(client, service, source):
    admin = sign_in(client, ADMIN)
    create_post(client, admin, category=source)
    create_post(client, admin, category='Module 1')
    assert rename(client, admin, 'Module 1', 'Orientation').json()['updated'] == 2
    assert len(events(service, 'admin_rename_category')) == 1


def test_general_collision_validation_and_reader_denial(client, service):
    admin = sign_in(client, ADMIN)
    create_post(client, admin, category='')
    create_post(client, admin, category='General')
    create_post(client, admin, category='MODULE2')
    assert rename(client, sign_in(client), 'General', 'Welcome').status_code == 403
    assert rename(client, {}, 'General', 'Welcome').status_code == 401
    assert rename(client, admin, 'General', ' module 2 ').status_code == 409
    for value in (' ', 'x'*201, 'module'+'1'*194):
        assert rename(client, admin, 'General', value).status_code == 422
    assert rename(client, admin, 'Missing', 'Welcome').status_code == 409
    assert not events(service, 'admin_rename_category')
    assert rename(client, admin, 'General', 'Welcome').json()['updated'] == 2


def test_pending_publication_rejects_entire_group_rename(client, service):
    admin = sign_in(client, ADMIN)
    first = create_post(client, admin)
    second, _ = ready_post(client, admin, service, publish=False)
    client.post(f"/api/admin/posts/{second['id']}/publish", headers=admin)
    assert rename(client, admin).json()['error'] == 'category_busy'
    assert {p['category'] for p in client.get('/api/admin/posts', headers=admin).json()['posts']} == {'Policies'}
    assert not events(service, 'admin_rename_category')


def test_cloud_retry_ack_loss_and_departed_actor_still_back_up_committed_name(client, service, store):
    admin = sign_in(client, ADMIN)
    post, _ = published(client, service, admin)
    assert rename(client, admin).status_code == 200
    store.available = False
    client.portal.call(service.process_operation)
    current = client.get('/api/admin/posts', headers=admin).json()['posts'][0]
    assert current['category_sync_pending'] and current['status'] == 'published'
    assert rename(client, admin, 'Team handbook', 'Another name').json()['error'] == 'category_busy'
    store.available = True
    store.lose_commit_ack = True
    store.write_allowlist([['email','name','role'], [READER,'Reader','reader']])
    client.portal.call(service.refresh)
    for _ in range(2):
        service.db.submit(lambda db: db.execute('UPDATE operations SET next_try=0')).result()
        client.portal.call(service.process_operation)
    catalog = store.catalog()['posts']
    assert len(catalog) == 1 and catalog[0]['category'] == 'Team handbook'
    assert service.db.read(lambda db: db.execute("SELECT count(*) FROM operations WHERE status='pending'").fetchone()[0]) == 0


def test_archive_supersedes_pending_rename_without_losing_new_category(client, service, store):
    admin = sign_in(client, ADMIN)
    post, _ = published(client, service, admin)
    rename(client, admin)
    client.post(f"/api/admin/posts/{post['id']}/archive", headers=admin)
    client.portal.call(service.process_operation)
    result = store.catalog()['posts'][0]
    assert result['status'] == 'archived' and result['category'] == 'Team handbook'
    assert client.get('/api/posts', headers=sign_in(client)).json()['posts'] == []


def test_same_category_is_noop_and_boundary_name_is_normalized(client, service):
    admin = sign_in(client, ADMIN)
    post = create_post(client, admin)
    assert rename(client, admin, 'Policies', ' Policies ').json() == {'name': 'Policies', 'updated': 0}
    current = client.get('/api/admin/posts', headers=admin).json()['posts'][0]
    assert current['revision'] == post['revision']
    assert not events(service, 'admin_rename_category')
    assert rename(client, admin, 'Policies', 'x'*40).json()['updated'] == 1
    assert rename(client, admin, 'x'*40, 'module9').json() == {'name': 'Module 9', 'updated': 1}


def test_recovery_mode_blocks_category_and_title_mutations(client, service, store):
    from postern.db import set_meta
    admin = sign_in(client, ADMIN)
    post, _ = published(client, service, admin)
    before = service.db.read(lambda db: [tuple(r) for r in db.execute('SELECT * FROM posts')])
    catalog = store.catalog()
    service.db.submit(lambda db: set_meta(db, 'recovery_mode', True)).result()
    assert rename(client, admin).status_code == 403
    assert client.post(f"/api/admin/posts/{post['id']}/rename", headers=admin, json={'title': 'Blocked'}).status_code == 403
    assert service.db.read(lambda db: [tuple(r) for r in db.execute('SELECT * FROM posts')]) == before
    assert not service.db.read(lambda db: db.execute("SELECT 1 FROM operations WHERE status='pending'").fetchone())
    assert not events(service, 'admin_rename_category')
    assert not events(service, 'admin_rename_post')
    assert store.catalog() == catalog


def test_reader_listing_skips_admin_backup_status_queries(client, service):
    admin = sign_in(client, ADMIN)
    reader = sign_in(client)
    post, _ = published(client, service, admin)
    assert rename(client, admin).status_code == 200
    statements = []
    service.db.submit(lambda db: db.set_trace_callback(statements.append)).result()
    try:
        visible = client.get('/api/posts', headers=reader).json()['posts']
        assert visible[0]['category'] == 'Team handbook'
        assert not visible[0]['category_sync_pending']
        assert not any('FROM operations' in sql for sql in statements)
        statements.clear()
        posts = client.get('/api/admin/posts', headers=admin).json()['posts']
        assert posts[0]['category_sync_pending']
        assert sum('FROM operations' in sql for sql in statements) == 1
    finally:
        service.db.submit(lambda db: db.set_trace_callback(None)).result()


@pytest.mark.parametrize('raw,expected', [('module\ufeff1', 'Module 1'), ('module\x851', 'module\x851'), ('\ufeffPolicies\ufeff', 'Policies')])
def test_category_whitespace_matches_browser_grouping(raw, expected):
    from postern.categories import category_name
    assert category_name(raw) == expected


@pytest.mark.parametrize('name', [
    'SOP Chapter 3: Professional Appearance and Grooming Standards',
    'ก' * 200,
])
def test_long_categories_can_be_created_and_renamed_without_truncation(client, name):
    admin = sign_in(client, ADMIN)
    post = create_post(client, admin, category=name)
    assert post['category'] == name
    assert rename(client, admin, name, 'Short name').status_code == 200
    assert rename(client, admin, 'Short name', name).json() == {'name': name, 'updated': 1}
    saved = client.get('/api/admin/posts', headers=admin).json()['posts']
    assert saved[0]['category'] == name


@pytest.mark.parametrize('category', ['x' * 201, 'module' + '1' * 194])
def test_create_rejects_category_above_limit(client, category):
    admin = sign_in(client, ADMIN)
    response = client.post('/api/admin/posts', headers=admin, json={
        'title': 'Document', 'category': category,
    })
    assert response.status_code == 422
    assert client.get('/api/admin/posts', headers=admin).json()['posts'] == []


def test_maximum_normalized_module_category_can_be_renamed(client):
    admin = sign_in(client, ADMIN)
    create_post(client, admin, category='module' + '1' * 193)
    # The browser sends the displayed group name, including the inserted space.
    assert rename(client, admin, 'Module ' + '1' * 193, 'Short name').json() == {
        'name': 'Short name', 'updated': 1,
    }
