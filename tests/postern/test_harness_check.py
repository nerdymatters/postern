from conftest import ADMIN, READER, sign_in, ready_post, open_view, events


def test_harness_builds_a_published_post(client):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post, item_id = ready_post(client, admin, client.service)
    reader = sign_in(client, READER)
    wall = client.get('/api/posts', headers=reader).json()['posts']
    assert [p['id'] for p in wall] == [post['id']]
    view = open_view(client, reader, item_id)
    page = client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader)
    assert page.status_code == 200 and page.headers['cache-control'] == 'no-store'
    assert client.get(f"/api/posts/{post['id']}/thumb.jpg", headers=reader).status_code == 200
