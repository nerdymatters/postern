"""ENG-D2: permissions are 60 seconds of monotonic time old at most, and what that costs.

These run against the HTTP surface with the injected clock, because the freshness window is only
real where it can refuse a page. The background refresh loop ticks every 30 real seconds, so each
test drives Service.refresh itself through the app's own event loop.
"""
from __future__ import annotations

import json
import uuid

from conftest import ADMIN, READER, events, open_view, ready_post, sign_in
from fastapi.testclient import TestClient

from postern.app import create_app
from postern.service import Service

PAUSED = 'Access temporarily paused while we verify permissions'


def refresh(client):
    """One allowlist refresh on the app's own loop. True when it validated and applied."""
    return client.portal.call(client.service.refresh)


def paused(response):
    assert response.status_code == 503, response.text
    body = response.json()
    assert body['error'] == 'authorization_source_unavailable'
    assert PAUSED in body['message']
    assert 'no-store' in response.headers['cache-control']


def members(service):
    return service.db.read(lambda db: {r[0]: r[1] for r in db.execute('SELECT email,role FROM members')})


def only_admin():
    return [['email', 'name', 'role'], [ADMIN, 'Demo administrator', 'admin']]


def started(config, store, clock):
    """A server run of its own, so a cold start and a restart are the real thing."""
    service = Service(config, store=store, clock=clock)
    client = TestClient(create_app(config, service=service))
    client.service = service
    return client


def test_an_invalid_snapshot_applies_nothing_and_does_not_extend_freshness(client, store, service, clock):
    reader = sign_in(client, READER)
    # Would remove the reader and promote a stranger, if it were ever applied.
    store.write_allowlist([['email', 'name', 'role'], [ADMIN, 'Demo administrator', 'admin'],
                           ['stranger', 'Typo', 'wizard']])
    clock.advance(59)
    assert refresh(client) is False
    assert members(service) == {ADMIN: 'admin', READER: 'reader'}
    assert client.get('/api/posts', headers=reader).status_code == 200
    assert events(service, 'allowlist_removed') == [] and events(service, 'role_changed') == []
    assert service.db.read(lambda db: db.execute('SELECT count(*) FROM sessions WHERE revoked=1').fetchone()[0]) == 0
    # The last good refresh was 60 s ago, so the old deadline still bites.
    clock.advance(1)
    paused(client.get('/api/posts', headers=reader))


def test_a_validation_error_reaches_admins_and_never_the_public_endpoint(client, store, service, allowlist):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    store.write_allowlist([['email', 'name', 'role'], [ADMIN, 'Demo administrator', 'admin'],
                           ['someone@example.invalid', 'S', 'wizard']])
    assert refresh(client) is False
    error = client.get('/api/admin/health', headers=admin).json()['allowlist_error']
    assert 'Row 3' in error and 'someone@example.invalid' not in error
    public = client.get('/health')
    assert 'someone@example.invalid' not in public.text
    assert set(public.json()) == {'process', 'permissions', 'audit', 'backup', 'storage'}
    assert public.json()['permissions'] == 'ready'  # repairable, and access holds until the deadline
    store.write_allowlist(allowlist)
    assert refresh(client) is True
    assert client.get('/api/admin/health', headers=admin).json()['allowlist_error'] is None


def test_access_pauses_at_sixty_seconds_and_a_later_refresh_clears_it(client, store, service, clock):
    reader = sign_in(client, READER)
    store.allowlist_available = False
    clock.advance(59.9)
    assert client.get('/api/posts', headers=reader).status_code == 200
    clock.advance(0.1)
    paused(client.get('/api/posts', headers=reader))
    paused(client.get('/api/session', headers=reader))
    assert client.get('/health').json()['permissions'] == 'paused'
    # The pause is not a revocation: the same bearer token works again once the source returns.
    assert refresh(client) is False
    clock.advance(600)
    store.allowlist_available = True
    assert refresh(client) is True
    assert client.get('/api/posts', headers=reader).status_code == 200
    assert client.get('/health').json()['permissions'] == 'ready'


def test_a_new_sign_in_is_refused_while_permissions_are_stale(client, store, clock):
    store.allowlist_available = False
    clock.advance(60)
    paused(client.post('/api/auth/demo', json={'email': READER, 'name': 'Demo reader'}))


def test_every_protected_route_pauses_on_the_same_guard(client, store, service, clock):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post, item_id = ready_post(client, admin, service)
    reader = sign_in(client, READER)
    view = open_view(client, reader, item_id)
    store.allowlist_available = False
    clock.advance(60)
    for url in ('/api/session', '/api/posts', f"/api/views/{view['id']}/page/0.jpg",
                f"/api/views/{view['id']}/photo.jpg", f"/api/posts/{post['id']}/thumb.jpg"):
        paused(client.get(url, headers=reader))
    paused(client.post(f'/api/items/{item_id}/views', json={'attempt_id': str(uuid.uuid4())}, headers=reader))
    for url in ('/api/admin/posts', f"/api/admin/posts/{post['id']}/report"):
        paused(client.get(url, headers=admin))
    paused(client.post('/api/admin/posts', json={'title': 'While paused', 'body': '', 'category': ''}, headers=admin))
    paused(client.post(f"/api/admin/posts/{post['id']}/archive", headers=admin))


def test_the_outage_and_its_recovery_are_logged_once_each(client, store, service, clock):
    unavailable = len(events(service, 'allowlist_unavailable'))
    recovered = len(events(service, 'allowlist_recovered'))
    store.allowlist_available = False
    assert refresh(client) is False
    assert len(events(service, 'allowlist_unavailable')) == unavailable  # still fresh, no outage yet
    clock.advance(60)
    for _ in range(3):
        assert refresh(client) is False
    assert len(events(service, 'allowlist_unavailable')) == unavailable + 1
    assert len(events(service, 'allowlist_recovered')) == recovered
    store.allowlist_available = True
    assert refresh(client) is True
    assert refresh(client) is True
    assert len(events(service, 'allowlist_recovered')) == recovered + 1
    assert len(events(service, 'allowlist_unavailable')) == unavailable + 1


def test_a_removed_reader_is_revoked_and_re_adding_does_not_revive_the_session(client, store, service, allowlist):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post, item_id = ready_post(client, admin, service)
    reader = sign_in(client, READER)
    view = open_view(client, reader, item_id)
    assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 200

    store.write_allowlist(only_admin())
    assert refresh(client) is True
    assert [e['email'] for e in events(service, 'allowlist_removed')] == [READER]
    for url in ('/api/posts', f"/api/views/{view['id']}/page/0.jpg", f"/api/posts/{post['id']}/thumb.jpg"):
        denied = client.get(url, headers=reader)
        assert denied.status_code == 403 and denied.json()['error'] == 'access_denied'

    store.write_allowlist(allowlist)
    assert refresh(client) is True
    assert [e['email'] for e in events(service, 'allowlist_added')][-1] == READER
    assert client.get('/api/posts', headers=reader).status_code == 403
    assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 403
    again = sign_in(client, READER)
    assert client.get('/api/posts', headers=again).status_code == 200


def test_a_demotion_takes_effect_on_the_next_request(client, store, service):
    headers = sign_in(client, ADMIN, 'Demo administrator')
    assert client.get('/api/admin/posts', headers=headers).status_code == 200
    store.write_allowlist([['email', 'name', 'role'], [ADMIN, 'Demo administrator', 'reader'],
                           [READER, 'Demo reader', 'reader']])
    assert refresh(client) is True
    denied = client.get('/api/admin/posts', headers=headers)
    assert denied.status_code == 403 and denied.json()['error'] == 'access_denied'
    assert client.get('/api/posts', headers=headers).status_code == 200
    assert client.get('/api/session', headers=headers).json()['role'] == 'reader'
    changed = events(service, 'role_changed')
    assert [e['email'] for e in changed] == [ADMIN]
    assert json.loads(changed[0]['payload'])['detail'] == {'role': 'reader'}


def test_a_promotion_is_logged_and_grants_the_admin_routes(client, store, service):
    headers = sign_in(client, READER)
    assert client.get('/api/admin/posts', headers=headers).status_code == 403
    store.write_allowlist([['email', 'name', 'role'], [ADMIN, 'Demo administrator', 'admin'],
                           [READER, 'Demo reader', 'admin']])
    assert refresh(client) is True
    assert client.get('/api/admin/posts', headers=headers).status_code == 200
    assert [e['email'] for e in events(service, 'role_changed')] == [READER]


def test_a_cold_start_with_an_unreadable_allowlist_authorizes_nobody(config, store, clock):
    store.allowlist_available = False
    with started(config, store, clock) as client:
        paused(client.post('/api/auth/demo', json={'email': ADMIN, 'name': 'Demo administrator'}))
        assert client.get('/health').json()['permissions'] == 'paused'
        store.allowlist_available = True
        assert refresh(client) is True
        assert client.post('/api/auth/demo', json={'email': ADMIN, 'name': 'Demo administrator'}).status_code == 200


def test_a_restart_never_authorizes_from_the_persisted_snapshot(config, store, clock):
    with started(config, store, clock) as first:
        headers = sign_in(first, READER)
        assert first.get('/api/posts', headers=headers).status_code == 200
    store.allowlist_available = False
    # Same database, same members table, same session, no successful read in this run.
    with started(config, store, clock) as second:
        paused(second.get('/api/posts', headers=headers))
        store.allowlist_available = True
        assert refresh(second) is True
        assert second.get('/api/posts', headers=headers).status_code == 200
