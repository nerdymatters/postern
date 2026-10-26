"""Refusals the guard lane does not reach: the per-route conditions behind a well-formed, authorized request.

Each one is asserted the way ENG-D10 asks: one status, one code, one message, no protected bytes, and no
row written for the request that was refused.
"""
from __future__ import annotations
import json
import sqlite3
import uuid

from conftest import ADMIN, READER, attach, create_post, events, open_view, ready_post, sign_in

STRANGER = 'stranger@example.invalid'


def refused(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {'error', 'message'} and body['error'] == code and body['message'], body
    assert 'no-store' in response.headers['cache-control']
    return body


def admin_post(client, headers, post_id):
    return next(p for p in client.get('/api/admin/posts', headers=headers).json()['posts'] if p['id'] == post_id)


def count(service, table):
    return service.db.read(lambda db: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0])


def test_consent_to_a_stale_notice_version_is_refused(client, service):
    headers = sign_in(client, READER, consent=False)
    refused(client.post('/api/consent', json={'version': '0'}, headers=headers), 409, 'notice_changed')
    assert events(service, 'consent_accepted') == []
    assert client.get('/api/posts', headers=headers).json()['error'] == 'consent_required'
    assert client.get('/api/session', headers=headers).json()['consent_required'] is True
    # The current version is the only one that opens the door.
    assert client.post('/api/consent', json={'version': '1', 'ip_version': 'ip-1', 'monitoring_accepted': True, 'ip_accepted': True}, headers=headers).status_code == 200
    assert client.get('/api/posts', headers=headers).status_code == 200
    assert len(events(service, 'consent_accepted')) == 1


def test_a_malformed_attempt_id_is_refused_before_anything_is_written(client, service):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post, item_id = ready_post(client, admin, service)
    reader = sign_in(client, READER)
    for attempt in ('not-a-uuid', '', '1234', str(uuid.uuid4())[:-1]):
        refused(client.post(f'/api/items/{item_id}/views', json={'attempt_id': attempt}, headers=reader), 422, 'invalid_attempt')
    assert events(service, 'view_requested') == []
    assert count(service, 'views') == 0
    assert open_view(client, reader, item_id)['item_id'] == item_id


def test_a_published_post_refuses_new_attachments(client, service):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post, item_id = ready_post(client, admin, service)
    staging = service.config.data_dir / 'staging'
    before = sorted(p.name for p in staging.iterdir())

    refused(attach(client, admin, post['id']), 409, 'post_locked')

    assert sorted(p.name for p in staging.iterdir()) == before, 'a refused upload must not leave bytes staged'
    assert [i['id'] for i in admin_post(client, admin, post['id'])['items']] == [item_id]
    assert events(service, 'admin_upload') == []


def test_an_archived_post_cannot_be_republished_and_only_real_operations_are_on_record(client, service):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post, item_id = ready_post(client, admin, service)
    assert client.post(f"/api/admin/posts/{post['id']}/archive", headers=admin).json() == {'status': 'archived'}

    body = refused(client.post(f"/api/admin/posts/{post['id']}/publish", headers=admin), 409, 'post_locked')
    assert 'archived' in body['message'].lower()
    archived = admin_post(client, admin, post['id'])
    assert archived['status'] == 'archived'
    assert len(events(service, 'admin_publish')) == 1  # the original publication, nothing since

    on_record = client.get(f"/api/admin/operations/{archived['publish_op_id']}", headers=admin)
    assert on_record.status_code == 200 and on_record.json()['kind'] == 'archive'
    refused(client.get(f'/api/admin/operations/{uuid.uuid4()}', headers=admin), 404, 'operation_not_found')
    assert client.get('/api/posts', headers=sign_in(client, READER)).json()['posts'] == []


def test_a_post_without_a_prepared_attachment_has_no_thumbnail(client, service):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post = create_post(client, admin)
    refused(client.get(f"/api/posts/{post['id']}/thumb.jpg", headers=admin), 404, 'no_thumbnail')

    def queued(db):
        db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,derive_status,sort)"
                   " VALUES (?,?,'policy.pdf','pdf',?,1,'queued',0)", (str(uuid.uuid4()), post['id'], 'x' * 64))
    service.db.submit(queued).result(timeout=5)
    # Queued is not prepared: a thumbnail exists only once the converter has produced one.
    refused(client.get(f"/api/posts/{post['id']}/thumb.jpg", headers=admin), 404, 'no_thumbnail')
    assert events(service, 'thumbnail_served') == []
    # A reader never learns that a draft exists at all.
    refused(client.get(f"/api/posts/{post['id']}/thumb.jpg", headers=sign_in(client, READER)), 403, 'access_denied')


def test_a_denied_sign_in_is_listed_for_administrators(client, service):
    body = refused(client.post('/api/auth/demo', json={'email': STRANGER, 'name': 'Someone'}), 403, 'access_denied')
    assert 'staff list' in body['message']
    assert count(service, 'sessions') == 0

    admin = sign_in(client, ADMIN, 'Demo administrator')
    denied = client.get('/api/admin/signin-denials', headers=admin).json()['denied']
    assert [d['email'] for d in denied] == [STRANGER]
    assert set(denied[0]) == {'ts', 'email', 'ip', 'user_agent'}
    assert denied[0]['ts'] and denied[0]['ip'] and denied[0]['user_agent']
    assert [e['email'] for e in events(service, 'signin_denied')] == [STRANGER]
    refused(client.get('/api/admin/signin-denials', headers=sign_in(client, READER)), 403, 'access_denied')


def test_an_unconfirmable_send_is_recorded_as_such_not_as_an_open(client, service):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post, item_id = ready_post(client, admin, service)
    reader = sign_in(client, READER)
    view = open_view(client, reader, item_id)
    page = f"/api/views/{view['id']}/page/0.jpg"
    original = service.db.submit

    def unwritable(db):
        raise sqlite3.OperationalError('attempt to write a readonly database')

    def submit(operation):
        # Only the post-send confirmation fails; the authorizing audit row before the body still commits.
        return original(unwritable if getattr(operation, '__name__', '') == 'confirm' else operation)

    service.db.submit = submit
    try:
        served = client.get(page, headers=reader)
    finally:
        service.db.submit = original

    assert served.status_code == 200 and served.content[:3] == b'\xff\xd8\xff'
    assert events(service, 'item_open') == [] and events(service, 'page_served') == []
    unconfirmed = events(service, 'confirm_unavailable')
    assert len(unconfirmed) == 1
    assert (unconfirmed[0]['view_id'], unconfirmed[0]['email'], unconfirmed[0]['summary']) == (view['id'], READER, 0)
    assert json.loads(unconfirmed[0]['payload'])['detail'] == {'page': 0}
    stored = service.db.read(lambda db: dict(db.execute('SELECT * FROM views WHERE id=?', (view['id'],)).fetchone()))
    assert stored['first_served_at'] is None and stored['served_pages'] == '[]'

    # The next send confirms normally, and that one is the open.
    assert client.get(page, headers=reader).status_code == 200
    assert [e['view_id'] for e in events(service, 'item_open')] == [view['id']]
    person = next(p for p in client.get(f"/api/admin/posts/{post['id']}/report", headers=admin).json()['people']
                  if p['email'] == READER)
    assert person['status'] == 'opened' and person['attempts'] == 1
