"""Sign-in at the edge: the Google credential check, the demo route's reach, and the proxy's client address.

verify_google_identity is the one call that talks to Google. It is driven here with the token verifier
replaced, so every refusal it can produce is exercised without a network, and the route above it is
driven in Google mode with the verifier replaced at the module boundary.
"""
from __future__ import annotations
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import postern.app
from postern import training
from conftest import READER, events
from postern.app import create_app
from postern.auth import Refusal, verify_google_identity
from postern.config import Config
from postern.service import Service

CLIENT_ID = 'postern-web.apps.googleusercontent.com'
CLAIMS = {'iss': 'https://accounts.google.com', 'aud': CLIENT_ID, 'sub': '1234567890',
          'email': 'Reader@Example.Invalid', 'email_verified': True, 'name': 'Demo reader'}


def verifier(monkeypatch, outcome, seen=None):
    """Replace Google's token verifier with one that returns the claims, or raises."""
    import google.oauth2.id_token as id_token

    def verify(credential, request):
        if seen is not None:
            seen.append(credential)
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)
    monkeypatch.setattr(id_token, 'verify_oauth2_token', verify)


def google_mode(tmp_path):
    # Google mode requires an installed private course; give auth tests a complete
    # synthetic one without bypassing startup validation or changing auth assertions.
    shutil.copytree(training.SAMPLE, tmp_path / 'google' / 'training')
    branding = tmp_path / 'google' / 'branding.json'
    branding.write_text('{}')
    branding.chmod(0o600)
    return Config(data_dir=tmp_path / 'google', mode='google', notice_version='1', google_client_id=CLIENT_ID,
                  host_credentials=str(tmp_path / 'host.json'), allowlist_sheet_id='sheet',
                  drive_folder_id='folder', database_sheet_id='db')


def demo_sign_in(client, headers=None):
    return client.post('/api/auth/demo', json={'email': READER, 'name': 'Demo reader'}, headers=headers or {})


@pytest.mark.parametrize('outcome,status,code', [
    (ValueError('Token expired'), 401, 'sign_in_required'),
    ({**CLAIMS, 'iss': 'https://evil.example'}, 401, 'sign_in_required'),
    ({**CLAIMS, 'aud': 'another-app.apps.googleusercontent.com'}, 401, 'sign_in_required'),
    ({**CLAIMS, 'email_verified': False}, 403, 'access_denied'),
    ({k: v for k, v in CLAIMS.items() if k != 'email'}, 403, 'access_denied'),
], ids=['rejected by google', 'wrong issuer', 'wrong audience', 'unverified email', 'no email'])
def test_a_google_credential_is_refused_unless_it_names_a_verified_account_for_this_app(monkeypatch, outcome, status, code):
    verifier(monkeypatch, outcome)
    with pytest.raises(Refusal) as refused:
        verify_google_identity('credential', [CLIENT_ID])
    assert (refused.value.status, refused.value.code) == (status, code)
    assert refused.value.message


def test_a_verified_credential_yields_the_identity_google_named(monkeypatch):
    seen = []
    verifier(monkeypatch, CLAIMS, seen)
    # The address is handed back as Google spelled it; canonicalization is the service's job at sign-in.
    assert verify_google_identity('credential', [CLIENT_ID]) == {
        'sub': '1234567890', 'email': 'Reader@Example.Invalid', 'name': 'Demo reader'}
    # One deployment may accept more than one client id (the web app and, later, the desktop client).
    assert verify_google_identity('credential', ['desktop-client', CLIENT_ID])['sub'] == '1234567890'
    # Nothing empty or oversized is ever sent to Google.
    for credential in ('', None, 'x' * 8193):
        with pytest.raises(Refusal) as refused:
            verify_google_identity(credential, [CLIENT_ID])
        assert (refused.value.status, refused.value.code) == (401, 'sign_in_required')
    assert seen == ['credential', 'credential']


def test_google_mode_signs_in_through_the_verified_credential_and_has_no_demo_route(tmp_path, store, clock, monkeypatch):
    config = google_mode(tmp_path)
    seen = []

    def verify(credential, client_ids):
        seen.append((credential, list(client_ids)))
        return {'sub': '1234567890', 'email': 'Reader@Example.Invalid', 'name': 'Demo reader'}
    monkeypatch.setattr(postern.app, 'verify_google_identity', verify)

    service = Service(config, store=store, clock=clock)
    with TestClient(create_app(config, service=service)) as client:
        settings = client.get('/api/config').json()
        assert (settings['mode'], settings['google_client_id']) == ('google', CLIENT_ID)
        demo = demo_sign_in(client)
        assert demo.status_code == 404 and demo.json()['error'] == 'not_found', demo.text
        assert seen == [] and events(service, 'signin_ok') == []

        entered = client.post('/api/auth/google', json={'credential': 'id-token'}, headers={'X-Postern-Client': 'desktop'})
        assert entered.status_code == 200, entered.text
        assert seen == [('id-token', [CLIENT_ID])]
        body = entered.json()
        assert body['email'] == READER and body['role'] == 'reader' and body['consent_required'] is True and body['token']
        headers = {'Authorization': 'Bearer ' + body['token']}
        assert client.get('/api/session', headers=headers).json()['email'] == READER
        # The client kind is recorded from the known set only; anything else is the web client.
        other = client.post('/api/auth/google', json={'credential': 'id-token'}, headers={'X-Postern-Client': 'kiosk'})
        assert other.status_code == 200
        assert [json.loads(e['payload'])['client'] for e in events(service, 'signin_ok')] == ['desktop', 'web']


def test_slow_google_verification_does_not_block_other_requests(tmp_path, store, clock, monkeypatch):
    config = google_mode(tmp_path)
    entered, resume = threading.Event(), threading.Event()

    def verify(credential, client_ids):
        entered.set()
        assert resume.wait(10)
        return {'sub': CLAIMS['sub'], 'email': READER, 'name': 'Demo reader'}

    monkeypatch.setattr(postern.app, 'verify_google_identity', verify)
    service = Service(config, store=store, clock=clock)
    with TestClient(create_app(config, service=service)) as client, ThreadPoolExecutor(max_workers=2) as pool:
        signin = pool.submit(client.post, '/api/auth/google', json={'credential': 'id-token'})
        try:
            assert entered.wait(2)
            health = pool.submit(client.get, '/health')
            assert health.result(timeout=2).status_code == 200
            assert not signin.done()  # Health answered while verification was still waiting.
        finally:
            resume.set()
        assert signin.result(timeout=2).status_code == 200


def test_google_certificate_fetch_has_a_short_timeout_and_fails_closed(monkeypatch):
    import requests
    timeouts = []

    def unavailable(self, method, url, **kwargs):
        timeouts.append(kwargs['timeout'])
        raise requests.Timeout('Synthetic certificate fetch timeout')

    monkeypatch.setattr(requests.Session, 'request', unavailable)
    with pytest.raises(Refusal) as refused:
        verify_google_identity('credential', [CLIENT_ID])
    assert (refused.value.status, refused.value.code) == (401, 'sign_in_required')
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 5


def test_google_certificate_timeout_returns_a_refusal_without_creating_a_session(tmp_path, store, clock, monkeypatch):
    import requests

    def unavailable(self, method, url, **kwargs):
        raise requests.Timeout('Synthetic certificate fetch timeout')

    monkeypatch.setattr(requests.Session, 'request', unavailable)
    config = google_mode(tmp_path)
    service = Service(config, store=store, clock=clock)
    with TestClient(create_app(config, service=service)) as client:
        response = client.post('/api/auth/google', json={'credential': 'id-token'})
        assert response.status_code == 401
        assert response.json()['error'] == 'sign_in_required'
        assert response.json()['message'] == 'Google could not confirm that sign-in. Please try again.'
        assert response.headers['referrer-policy'] == 'no-referrer'
        assert 'token' not in response.json()
        assert events(service, 'signin_ok') == []
        assert service.db.read(lambda db: db.execute('SELECT count(*) FROM sessions').fetchone()[0]) == 0


def test_google_verification_concurrency_is_bounded(tmp_path, store, monkeypatch):
    import time
    saturated, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = peak = 0
    def verify(*_args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 4:
                saturated.set()
        try:
            assert release.wait(5)
            return {'sub': 'synthetic', 'email': READER, 'name': 'Reader'}
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(postern.app, 'verify_google_identity', verify)
    config = google_mode(tmp_path)
    service = Service(config, store=store)
    with TestClient(create_app(config, service=service)) as client:
        with ThreadPoolExecutor(max_workers=6) as pool:
            pending = [pool.submit(client.post, '/api/auth/google', json={'credential': 'synthetic'}) for _ in range(6)]
            try:
                assert saturated.wait(3)
                time.sleep(.1)
                assert peak == 4
            finally:
                release.set()
            assert [future.result(5).status_code for future in pending] == [200]*6
    assert peak == 4


def test_google_verifier_wait_expires_without_a_session_and_recovers(tmp_path, store, monkeypatch):
    saturated, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    verified = []

    def verify(credential, client_ids):
        with lock:
            verified.append(credential)
            if len(verified) == 4:
                saturated.set()
        assert release.wait(10)
        return {'sub': 'synthetic', 'email': READER, 'name': 'Reader'}

    monkeypatch.setattr(postern.app, 'verify_google_identity', verify)
    config = google_mode(tmp_path)
    service = Service(config, store=store)
    with TestClient(create_app(config, service=service)) as client, ThreadPoolExecutor(max_workers=5) as pool:
        active = [pool.submit(client.post, '/api/auth/google', json={'credential': f'active-{i}'}) for i in range(4)]
        try:
            assert saturated.wait(3)
            queued = pool.submit(client.post, '/api/auth/google', json={'credential': 'queued'})
            response = queued.result(timeout=4)
            assert response.status_code == 503
            assert response.json() == {'error': 'signin_busy', 'message': 'Sign-in is busy. Please try again shortly.'}
            assert response.headers['referrer-policy'] == 'no-referrer'
            assert 'queued' not in verified
            assert events(service, 'signin_ok') == []
            assert service.db.read(lambda db: db.execute('SELECT count(*) FROM sessions').fetchone()[0]) == 0
        finally:
            release.set()
        assert [future.result(timeout=3).status_code for future in active] == [200]*4
        retry = client.post('/api/auth/google', json={'credential': 'retry'})
        assert retry.status_code == 200
        assert len(verified) == 5 and verified[-1] == 'retry'
        assert service.db.read(lambda db: db.execute('SELECT count(*) FROM sessions').fetchone()[0]) == 5


def test_the_proxy_address_is_trusted_only_from_loopback_and_only_when_well_formed(tmp_path, store, clock):
    def host(name, address, **settings):
        """One server run per case: a lifespan closes its writer, so a Service is never reopened."""
        config = Config(data_dir=tmp_path / name, mode='demo', notice_version='1', **settings)
        service = Service(config, store=store, clock=clock)
        return service, TestClient(create_app(config, service=service), client=(address, 4321))

    service, client = host('proxied', '127.0.0.1', trust_loopback_proxy=True)
    with client:
        assert demo_sign_in(client, {'X-Forwarded-For': '203.0.113.7'}).status_code == 200
        assert json.loads(events(service, 'signin_ok')[-1]['payload'])['ip'] == '203.0.113.7'
        for headers in ({}, {'X-Forwarded-For': 'not an address'}, {'X-Forwarded-For': '203.0.113.7, 10.0.0.1'}):
            response = demo_sign_in(client, headers)
            assert response.status_code == 400 and response.json()['error'] == 'invalid_proxy', response.text
            assert 'no-store' in response.headers['cache-control']
        assert len(events(service, 'signin_ok')) == 1

    # A connection that did not come through the loopback proxy keeps its own address, header or not.
    service, client = host('remote', '198.51.100.9', trust_loopback_proxy=True)
    with client:
        assert demo_sign_in(client, {'X-Forwarded-For': '203.0.113.7'}).status_code == 200
        assert json.loads(events(service, 'signin_ok')[-1]['payload'])['ip'] == '198.51.100.9'

    # Without the flag the header is ignored even from loopback.
    service, client = host('direct', '127.0.0.1')
    with client:
        assert demo_sign_in(client, {'X-Forwarded-For': '203.0.113.7'}).status_code == 200
        assert json.loads(events(service, 'signin_ok')[-1]['payload'])['ip'] == '127.0.0.1'
