"""Progress uses actual served-page history, scoped to the signed-in reader."""
from conftest import ADMIN, READER, open_view, ready_post, sign_in
from postern.db import event, set_meta
from postern.recover import import_events


def progress(client, headers, post_id):
    response = client.get('/api/posts', headers=headers)
    assert response.status_code == 200
    return next(post for post in response.json()['posts'] if post['id'] == post_id)['items'][0]['progress']


def test_unique_pages_survive_sessions_and_derivative_rebuilds(client):
    admin = sign_in(client, ADMIN)
    post, item = ready_post(client, admin, client.service, pages=3)
    reader = sign_in(client)
    view = open_view(client, reader, item)
    assert client.get(f"/api/posts/{post['id']}/thumb.jpg", headers=reader).status_code == 200
    assert progress(client, reader, post['id'])['opened_pages'] == []
    for page in (0, 2, 0):
        assert client.get(f"/api/views/{view['id']}/page/{page}.jpg", headers=reader).status_code == 200
    assert progress(client, reader, post['id'])['opened_pages'] == [0, 2]
    assert progress(client, admin, post['id'])['opened_pages'] == []
    client.service.db.submit(lambda db: db.execute('UPDATE items SET revision=revision+1 WHERE id=?', (item,))).result()
    assert client.post('/api/auth/signout', headers=reader).status_code == 200
    assert progress(client, sign_in(client), post['id'])['opened_pages'] == [0, 2]
    assert client.get('/api/posts').status_code == 401


def test_recovered_summary_and_page_events_union_without_views(client):
    admin = sign_in(client, ADMIN)
    post, item = ready_post(client, admin, client.service, pages=4)
    reader = sign_in(client)
    recovered = [
        {'event_id': 'restored-summary', 'ts': 1, 'event': 'item_summary', 'email': READER,
         'post_id': post['id'], 'item_id': item, 'detail': {'pages': [0, 2, 2, 99, -1, True]}},
        {'event_id': 'restored-page', 'ts': 2, 'event': 'page_served', 'email': READER,
         'post_id': post['id'], 'item_id': item, 'detail': {'page': 1}},
    ]
    client.service.db.submit(lambda db: import_events(db, recovered)).result()
    client.service.db.submit(lambda db: set_meta(db, 'history_incomplete', True)).result()
    assert progress(client, reader, post['id']) == {'opened_pages': [0, 1, 2], 'history_incomplete': True}
    assert progress(client, admin, post['id']) == {'opened_pages': [], 'history_incomplete': True}


def test_photo_counts_once_and_drafts_are_excluded(client):
    admin = sign_in(client, ADMIN)
    post, item = ready_post(client, admin, client.service)
    hidden, _ = ready_post(client, admin, client.service, publish=False)
    reader = sign_in(client)
    def seed(db):
        db.execute("UPDATE items SET kind='photo',pages=1 WHERE id=?", (item,))
        event(db, 'page_served', email=READER, post_id=post['id'], item_id=item, detail={'page': 0}, summary=False)
    client.service.db.submit(seed).result()
    assert progress(client, reader, post['id'])['opened_pages'] == [0]
    assert hidden['id'] not in [p['id'] for p in client.get('/api/posts', headers=reader).json()['posts']]
