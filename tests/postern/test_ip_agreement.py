"""The IP checkbox is an explicit, versioned server-side condition of entry."""
import json

import pytest

from conftest import events, sign_in
from postern.config import IP_NOTICE

ACCEPT = {'version':'1', 'ip_version':'ip-1', 'monitoring_accepted':True, 'ip_accepted':True}


@pytest.mark.parametrize('changes', [
    {'monitoring_accepted':False}, {'ip_accepted':False},
    {'ip_accepted':'yes'}, {'ip_version':'old'},
])
def test_api_cannot_bypass_either_agreement(client, service, changes):
    headers = sign_in(client, consent=False)
    response = client.post('/api/consent', headers=headers, json={**ACCEPT, **changes})
    assert response.status_code in (409, 422)
    assert client.get('/api/session', headers=headers).json()['consent_required'] is True
    assert client.get('/api/posts', headers=headers).json()['error'] == 'consent_required'
    assert not events(service, 'consent_accepted')


def test_missing_agreements_are_not_inferred_from_notice_version(client):
    headers = sign_in(client, consent=False)
    assert client.post('/api/consent', headers=headers, json={'version':'1'}).status_code == 409
    assert client.get('/api/session', headers=headers).json()['consent_required'] is True


def test_acceptance_records_both_versions_and_unlocks_content(client, service):
    headers = sign_in(client, consent=False)
    settings = client.get('/api/config').json()
    assert settings['ip_notice'] == IP_NOTICE and settings['ip_notice_version'] == 'ip-1'
    assert client.post('/api/consent', headers=headers, json=ACCEPT).status_code == 200
    assert client.get('/api/session', headers=headers).json()['consent_required'] is False
    assert client.get('/api/posts', headers=headers).status_code == 200
    detail = json.loads(events(service, 'consent_accepted')[-1]['payload'])['detail']
    assert detail == ACCEPT
    agreement = service.db.read(lambda db: dict(db.execute('SELECT * FROM session_ip_agreements').fetchone()))
    assert agreement['version'] == 'ip-1' and agreement['accepted_at'] > 0


def test_existing_session_without_ip_acceptance_must_review_again(client, service):
    headers = sign_in(client)
    service.db.submit(lambda db: db.execute('DELETE FROM session_ip_agreements')).result()
    assert client.get('/api/session', headers=headers).json()['consent_required'] is True
    assert client.get('/api/posts', headers=headers).json()['error'] == 'consent_required'
    assert client.post('/api/consent', headers=headers, json=ACCEPT).status_code == 200
    assert client.get('/api/posts', headers=headers).status_code == 200


def test_updated_ip_version_requires_new_agreement(client, service):
    headers = sign_in(client)
    service.config.ip_notice_version = service.permissions.ip_notice_version = 'ip-2'
    assert client.get('/api/session', headers=headers).json()['consent_required'] is True
    assert client.get('/api/posts', headers=headers).json()['error'] == 'consent_required'
    assert client.post('/api/consent', headers=headers, json=ACCEPT).status_code == 409
    assert client.post('/api/consent', headers=headers, json={**ACCEPT,'ip_version':'ip-2'}).status_code == 200
