"""Attempts, opens and view idempotency (ENG-D3, ENG-D11; tasks T3, T11)."""
from __future__ import annotations
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from conftest import ADMIN, READER, events, open_view, ready_post, sign_in


def publish_now(client, admin, post_id):
    # Pump the publication queue instead of waiting for the three-second background pass.
    assert client.post(f'/api/admin/posts/{post_id}/publish', headers=admin).status_code == 202
    for _ in range(50):
        client.portal.call(client.service.process_operation)
        state = client.get('/api/admin/posts', headers=admin).json()['posts']
        if next(p['status'] for p in state if p['id'] == post_id) == 'published':
            return
        time.sleep(0.02)
    raise AssertionError('publication did not confirm')


def published(client, admin=None, pages=2):
    admin = admin or sign_in(client, ADMIN, 'Demo administrator')
    post, item_id = ready_post(client, admin, client.service, pages=pages, publish=False)
    publish_now(client, admin, post['id'])
    return admin, post, item_id


def count(service, table):
    return service.db.read(lambda db: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0])


def test_creating_a_view_never_marks_an_open(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    views = [open_view(client, reader, item_id) for _ in range(3)]
    assert len({view['id'] for view in views}) == 3
    assert all(view['first_served_at'] is None for view in views)
    assert len(events(client.service, 'view_requested')) == 3
    assert events(client.service, 'item_open') == []


def test_a_thumbnail_never_marks_an_open(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    open_view(client, reader, item_id)
    for _ in range(3):
        assert client.get(f"/api/posts/{post['id']}/thumb.jpg", headers=reader).status_code == 200
    assert events(client.service, 'item_open') == []
    served = events(client.service, 'thumbnail_served')
    assert len(served) == 3 and all(row['summary'] == 0 for row in served)


def test_a_completed_page_send_marks_exactly_one_open(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    for page in (0, 1, 0):
        assert client.get(f"/api/views/{view['id']}/page/{page}.jpg", headers=reader).status_code == 200
    assert client.get(f"/api/views/{view['id']}/photo.jpg", headers=reader).status_code == 200

    marks = events(client.service, 'item_open')
    assert len(marks) == 1
    assert (marks[0]['view_id'], marks[0]['item_id'], marks[0]['post_id']) == (view['id'], item_id, post['id'])
    assert marks[0]['email'] == READER and marks[0]['summary'] == 1
    served = events(client.service, 'page_served')
    assert [json.loads(row['payload'])['detail']['page'] for row in served] == [0, 1, 0, 0]
    assert all(row['summary'] == 0 and row['view_id'] == view['id'] for row in served)
    stored = client.service.db.read(lambda db: dict(db.execute('SELECT * FROM views WHERE id=?', (view['id'],)).fetchone()))
    assert json.loads(stored['served_pages']) == [0, 1] and stored['highest_page'] == 1
    assert stored['first_served_at'] and stored['last_served_at'] >= stored['first_served_at']


def test_each_view_marks_its_own_open(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    first, second = open_view(client, reader, item_id), open_view(client, reader, item_id)
    for view in (first, second):
        assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 200
    marks = events(client.service, 'item_open')
    assert {row['view_id'] for row in marks} == {first['id'], second['id']} and len(marks) == 2


def test_concurrent_page_sends_mark_one_open(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)

    start = threading.Barrier(4)

    def fetch(page):
        start.wait(timeout=5)  # all four bodies are in flight before any of them completes
        return client.get(f"/api/views/{view['id']}/page/{page}.jpg", headers=reader)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(fetch, [0, 1, 0, 1]))
    assert [response.status_code for response in responses] == [200] * 4
    assert len(events(client.service, 'item_open')) == 1
    assert len(events(client.service, 'page_served')) == 4


def test_a_head_request_never_marks_an_open(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    response = client.head(f"/api/views/{view['id']}/page/0.jpg", headers=reader)
    assert response.status_code in (200, 405)  # the family is GET-only today; adding HEAD still may not open

    assert events(client.service, 'item_open') == []


def test_a_refused_page_never_marks_an_open(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    page = f"/api/views/{view['id']}/page/0.jpg"
    assert client.get(f"/api/views/{view['id']}/page/7.jpg", headers=reader).status_code == 404
    assert client.get(page).status_code == 401
    assert client.get(page, headers=admin).status_code == 403
    assert events(client.service, 'item_open') == []
    assert events(client.service, 'page_served') == []


def test_the_same_attempt_id_returns_one_view(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    attempt = str(uuid.uuid4())
    first = open_view(client, reader, item_id, attempt)
    assert open_view(client, reader, item_id, attempt)['id'] == first['id']
    assert len(events(client.service, 'view_requested')) == 1
    assert count(client.service, 'views') == 1

    other = open_view(client, reader, item_id, str(uuid.uuid4()))
    assert other['id'] != first['id']
    assert len(events(client.service, 'view_requested')) == 2
    assert count(client.service, 'views') == 2


def test_an_attempt_id_replayed_on_another_item_is_refused(client):
    admin, post, item_id = published(client)
    admin, other_post, other_item = published(client, admin)
    reader = sign_in(client)
    attempt = str(uuid.uuid4())
    view = open_view(client, reader, item_id, attempt)

    response = client.post(f'/api/items/{other_item}/views', json={'attempt_id': attempt}, headers=reader)
    assert response.status_code == 409
    assert set(response.json()) == {'error', 'message'}
    assert len(events(client.service, 'view_requested')) == 1
    assert count(client.service, 'views') == 1
    assert open_view(client, reader, item_id, attempt)['id'] == view['id']


def test_an_attempt_id_is_scoped_to_one_session(client):
    admin, post, item_id = published(client)
    attempt = str(uuid.uuid4())
    one, two = sign_in(client), sign_in(client)
    assert open_view(client, one, item_id, attempt)['id'] != open_view(client, two, item_id, attempt)['id']
    assert len(events(client.service, 'view_requested')) == 2


def test_an_unprepared_attachment_cannot_be_viewed(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    client.service.db.submit(
        lambda db: db.execute("UPDATE items SET derive_status='converting' WHERE id=?", (item_id,))).result(timeout=5)
    response = client.post(f'/api/items/{item_id}/views', json={'attempt_id': str(uuid.uuid4())}, headers=reader)
    assert response.status_code == 409 and response.json()['error'] == 'preparing'
    assert events(client.service, 'view_requested') == []
    assert count(client.service, 'views') == 0
