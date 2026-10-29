"""ENG-D10: one guard in front of every protected route, one refusal shape per condition.

The route table is read from the running application rather than written down here, so a protected
route added later that skips `authorize_content_request` fails this file instead of shipping quietly.
"""
from __future__ import annotations
import re
import uuid

from fastapi import UploadFile
from fastapi.testclient import TestClient
from pydantic import BaseModel

from conftest import FIXTURE_PDF, READER, sign_in
from postern.app import create_app
from postern.config import Config
from postern.service import Service
from test_audit import JPEG, broken_audit, reading_room

PUBLIC = {('GET', '/health'), ('GET', '/api/config'), ('POST', '/api/auth/google'), ('POST', '/api/auth/demo')}

CONTRACT = PUBLIC | {
    ('GET', '/api/session'), ('POST', '/api/consent'), ('POST', '/api/auth/signout'),
    ('GET', '/api/posts'), ('POST', '/api/items/{item_id}/views'),
    ('GET', '/api/views/{view_id}/page/{page}.jpg'), ('GET', '/api/views/{view_id}/photo.jpg'),
    ('GET', '/api/posts/{post_id}/thumb.jpg'),
    ('GET', '/api/admin/posts'), ('POST', '/api/admin/posts'),
    ('POST', '/api/admin/categories/rename'), ('POST', '/api/admin/posts/{post_id}/rename'),
    ('POST', '/api/admin/posts/{post_id}/items'), ('POST', '/api/admin/posts/{post_id}/publish'),
    ('POST', '/api/admin/posts/{post_id}/archive'), ('GET', '/api/admin/posts/{post_id}/report'),
    ('GET', '/api/admin/operations/{operation_id}'), ('GET', '/api/admin/signin-denials'),
    ('GET', '/api/admin/health'), ('GET', '/api/admin/progress'), ('POST', '/api/admin/posts/{post_id}/videos'),
    ('POST', '/api/views/{view_id}/video'), ('POST', '/api/views/{view_id}/video-progress'),
    ('GET', '/api/video/{ticket}'),
    ('GET', '/api/selfie'), ('POST', '/api/selfie'),
    ('GET', '/api/selfies/{identity}.jpg'), ('GET', '/api/admin/selfies'),
    ('GET', '/api/quizzes/{chapter}'), ('POST', '/api/quizzes/{chapter}/attempts'),
    ('POST', '/api/certificate/name')}

FAMILIES = ('api', 'thumbnail', 'page')
# 'version' is the current notice version so /api/consent reaches the guard instead of 409ing first.
SAMPLES = {'version': '1', 'title': 'Probe', 'ranges': [], 'ip_version': 'ip-1', 'monitoring_accepted': True, 'ip_accepted': True,
           'answers': {}}


def inventory(app):
    """Every (method, path) the application actually registers."""
    return {(method, route.path) for route in app.routes if hasattr(route, 'dependant')
            for method in route.methods if method in ('GET', 'POST')}


def call(client, method, path, headers=None, values=None):
    """Drive a registered route with a body its own signature accepts, so the guard is what answers."""
    route = next(r for r in client.app.routes if r.path == path and method in r.methods)
    values = {'page': 0, 'chapter': 1, **(values or {})}

    def fill(parameter):
        value = values.get(parameter.group(1))
        return str(uuid.uuid4()) if value is None else str(value)

    url = re.sub(r'\{(\w+)[^}]*\}', fill, path)
    body = files = None
    data = {}
    for parameter in route.dependant.body_params:
        annotation = parameter.field_info.annotation
        if annotation is UploadFile:
            files = {parameter.name: ('probe.pdf', b'%PDF-1.4 probe', 'application/pdf')}
        elif annotation is str:
            data[parameter.name] = 'selfie-1'
        else:
            assert issubclass(annotation, BaseModel), f'unhandled body {annotation} on {path}'
            body = {field: SAMPLES.get(field, 'x') for field in annotation.model_fields}
            if 'attempt_id' in body:
                body['attempt_id'] = str(uuid.uuid4())
    return client.request(method, url, headers=headers, json=body, files=files, data=data or None)


def refusals(client, headers, urls):
    return {name: client.get(urls[name], headers=headers) for name in FAMILIES}


def assert_one_shape(responses, status, code):
    """The same condition must produce the same refusal in the API, thumbnail and page families."""
    for name, response in responses.items():
        body = response.json()
        assert response.status_code == status, (name, response.status_code, response.text)
        assert set(body) == {'error', 'message'}, (name, body)
        assert body['error'] == code, (name, body)
        assert isinstance(body['message'], str) and body['message'], (name, body)
        assert response.headers['content-type'].startswith('application/json'), name
        assert 'no-store' in response.headers['cache-control'], name
        assert JPEG not in response.content, name
    assert len({response.json()['message'] for response in responses.values()}) == 1, \
        {name: response.json()['message'] for name, response in responses.items()}


def test_the_registered_routes_are_exactly_the_declared_contract(client):
    # PDFs/photos remain protected pixels; video tickets are scoped to one view.
    assert inventory(client.app) == CONTRACT
    for _, path in inventory(client.app):
        assert not re.search(r'original|download|raw|source|media|token|mint|renew', path), path


def test_every_route_outside_the_public_four_refuses_an_unauthenticated_request(client):
    protected = inventory(client.app) - PUBLIC
    assert {('GET', '/api/posts'), ('GET', '/api/posts/{post_id}/thumb.jpg'),
            ('GET', '/api/views/{view_id}/page/{page}.jpg')} <= protected
    for method, path in sorted(protected):
        response = call(client, method, path)
        body = response.json()
        assert response.status_code == 401, (method, path, response.status_code, response.text)
        assert set(body) == {'error', 'message'}, (method, path, body)
        assert body['error'] == 'sign_in_required' and body['message'], (method, path, body)


def test_the_public_routes_are_the_only_ones_that_answer_without_credentials(client):
    assert PUBLIC <= inventory(client.app)
    assert client.get('/health').status_code == 200
    assert client.get('/api/config').status_code == 200
    assert client.post('/api/auth/demo', json={'email': READER, 'name': 'Demo reader'}).status_code == 200
    # /api/auth/google is public too. It is not driven here because verifying a credential calls
    # Google; the sign-in lane covers it.


def test_the_spa_fallback_is_the_only_extra_public_route_when_static_hosting_is_on(tmp_path, store, clock):
    root = tmp_path / 'web'
    (root / 'assets').mkdir(parents=True)
    (root / 'index.html').write_text('<!doctype html><title>Postern</title>')
    config = Config(data_dir=tmp_path / 'hosted', mode='demo', notice_version='1', static_dir=str(root))
    with TestClient(create_app(config, service=Service(config, store=store, clock=clock))) as client:
        assert inventory(client.app) - CONTRACT == {('GET', '/{path:path}')}
        assert any(getattr(route, 'path', None) == '/assets' for route in client.app.routes)
        for path in ('/', '/index.html', '/reading/room'):
            page = client.get(path)
            assert page.status_code == 200 and 'Postern' in page.text, path
        # The fallback neither shadows the guard nor reaches outside the web root.
        assert client.get('/api/posts').status_code == 401
        assert client.get('/api/admin/health').status_code == 401
        assert 'Postern' in client.get('/../postern.sqlite3').text


def test_missing_credentials_refuse_identically_in_every_family(client):
    room = reading_room(client)
    assert_one_shape(refusals(client, None, room['urls']), 401, 'sign_in_required')
    forged = {'Authorization': 'Bearer ' + 'x' * 43, 'X-Postern-Client': 'web'}
    assert_one_shape(refusals(client, forged, room['urls']), 401, 'sign_in_required')


def test_a_revoked_reader_refuses_identically_in_every_family(client):
    room = reading_room(client)
    assert client.post('/api/auth/signout', headers=room['reader']).status_code == 200
    assert_one_shape(refusals(client, room['reader'], room['urls']), 403, 'access_denied')


def test_a_reader_who_has_not_accepted_the_notice_refuses_identically_in_every_family(client):
    room = reading_room(client)
    unaccepted = sign_in(client, READER, consent=False)
    assert_one_shape(refusals(client, unaccepted, room['urls']), 403, 'consent_required')


def test_a_stale_allowlist_refuses_identically_in_every_family(client, clock):
    room = reading_room(client)
    clock.advance(61)  # ENG-D2 keeps a validated snapshot fresh for 60 s of monotonic time
    assert_one_shape(refusals(client, room['reader'], room['urls']), 503, 'authorization_source_unavailable')


def test_a_failed_audit_write_refuses_identically_in_every_family(client):
    room = reading_room(client)
    with broken_audit(client.service):
        assert_one_shape(refusals(client, room['reader'], room['urls']), 503, 'audit_unavailable')


def test_another_signed_in_member_cannot_use_a_readers_view(client):
    room = reading_room(client)
    for url in (f"/api/views/{room['view']['id']}/page/0.jpg", f"/api/views/{room['view']['id']}/photo.jpg"):
        stolen = client.get(url, headers=room['admin'])
        assert stolen.status_code == 403 and stolen.json()['error'] == 'access_denied', (url, stolen.text)
        assert JPEG not in stolen.content, url


def test_a_reader_cannot_reach_any_admin_route(client):
    room = reading_room(client)
    admin_routes = {(method, path) for method, path in inventory(client.app) if path.startswith('/api/admin/')}
    assert len(admin_routes) == 14
    for method, path in sorted(admin_routes):
        response = call(client, method, path, headers=room['reader'],
                        values={'post_id': room['post']['id'], 'item_id': room['item_id']})
        body = response.json()
        assert response.status_code == 403, (method, path, response.status_code, response.text)
        assert set(body) == {'error', 'message'}, (method, path, body)
        assert body['error'] == 'access_denied' and body['message'] == 'Administrator access is required. / ต้องมีสิทธิ์ผู้ดูแลระบบ', (method, path, body)
    # The refusals were refusals: nothing the reader posted reached the post.
    state = client.get('/api/admin/posts', headers=room['admin']).json()['posts']
    assert [(p['id'], p['status']) for p in state] == [(room['post']['id'], 'published')]


def test_no_route_serves_an_original_files_bytes(client):
    room = reading_room(client)
    original = FIXTURE_PDF.read_bytes()
    staged = client.service.config.data_dir / 'staging' / f"{room['item_id']}.pdf"
    staged.write_bytes(original)  # the original an admin uploaded, still on the Mac
    middle = original[len(original) // 2:len(original) // 2 + 64]

    for name in FAMILIES[1:]:
        served = client.get(room['urls'][name], headers=room['reader'])
        assert served.status_code == 200 and served.content[:3] == JPEG, name

    published = client.get('/api/admin/posts', headers=room['admin']).json()['posts'][0]
    values = {'post_id': room['post']['id'], 'item_id': room['item_id'], 'view_id': room['view']['id'],
              'operation_id': published['publish_op_id']}
    # Sign-in routes are excluded: one of them would call Google. GET routes sort before POST, so
    # every read happens before the writes that archive the post or revoke a session.
    for method, path in sorted(inventory(client.app)):
        if path.startswith('/api/auth/'):
            continue
        if method == 'GET':
            assert staged.read_bytes() == original  # the read sweep never had to lose it to pass
        for headers in (room['reader'], room['admin']):
            response = call(client, method, path, headers=headers, values=values)
            assert b'%PDF' not in response.content, (method, path)
            assert original[:512] not in response.content, (method, path)
            assert middle not in response.content, (method, path)
