"""Installation settings preserve consent, roster grouping and certificate identity."""
import json
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import ADMIN, READER, sign_in
from postern import branding, honors, training
from postern.config import Config
from postern.service import Service
from test_exam import restore
from test_quiz import publish


PRIVATE = {'organization_name': 'Example Academy', 'privacy_contact_email': 'privacy@example.invalid',
           'ip_notice': 'Example Academy owns these materials. Obtain permission before copying.',
           'default_group': 'Academy', 'certificate_prefix': 'PG', 'certificate_title': 'Academy Certificate'}


def write_branding(data, values):
    data.mkdir(parents=True, exist_ok=True)
    path = data / 'branding.json'
    path.write_text(json.dumps(values), encoding='utf-8')
    path.chmod(0o600)
    return path


@pytest.fixture
def config(tmp_path):
    data = tmp_path / 'data'
    write_branding(data, PRIVATE)
    return Config(data_dir=data, mode='demo', notice_version='1')


def test_missing_demo_branding_has_neutral_defaults_without_creating_files(tmp_path):
    result = branding.load(tmp_path / 'missing')
    assert result.default_group == 'Staff' and result.certificate_prefix == 'PT'
    assert result.public() == {'organization_name': '', 'privacy_contact_email': '',
                               'certificate_title': 'Training Certificate'}
    assert not (tmp_path / 'missing').exists()


def test_branding_accepts_home_paths_used_by_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    write_branding(tmp_path / 'data', PRIVATE)
    assert branding.load('~/data', required=True).certificate_prefix == 'PG'


@pytest.mark.parametrize('values', [[], {'unknown': 'value'}, {'organization_name': 12},
    {'organization_name': 'x' * 81}, {'ip_notice': ''}, {'default_group': ' Staff'},
    {'certificate_title': 'a\nb'}, {'organization_name': 'a\u200bb'},
    {'privacy_contact_email': 'person@example.invalid?subject=secret'},
    {'certificate_prefix': '../PG'}, {'certificate_prefix': 'pg'}, {'certificate_prefix': ''}])
def test_invalid_branding_refuses_startup(tmp_path, values):
    write_branding(tmp_path, values)
    with pytest.raises(ValueError):
        branding.load(tmp_path)


def test_branding_is_owner_only_and_cannot_follow_symlinks(tmp_path):
    path = write_branding(tmp_path, PRIVATE)
    path.chmod(0o644)
    with pytest.raises(ValueError, match='0600'):
        branding.load(tmp_path)
    path.chmod(0o600)
    target = path.with_suffix('.original')
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(ValueError, match='regular'):
        branding.load(tmp_path)


def test_google_requires_explicit_branding_before_store_or_database(tmp_path, monkeypatch):
    import postern.service as service_module
    shutil.copytree(training.SAMPLE, tmp_path / 'training')
    config = Config(data_dir=tmp_path, google_client_id='example', host_credentials='unused',
                    allowlist_sheet_id='example', drive_folder_id='example', database_sheet_id='example')
    def unexpected(*args, **kwargs):
        pytest.fail('Missing branding reached the store or database')
    monkeypatch.setattr(service_module, 'GoogleStore', unexpected)
    monkeypatch.setattr(service_module, 'Database', unexpected)
    with pytest.raises(ValueError, match='requires a private branding.json'):
        Service(config)
    assert not (tmp_path / 'postern.sqlite3').exists()


def test_public_projection_preserves_notice_and_existing_consent(client, service):
    response = client.get('/api/config').json()
    assert response['branding'] == {key: PRIVATE[key] for key in
                                    ('organization_name', 'privacy_contact_email', 'certificate_title')}
    assert response['ip_notice'] == PRIVATE['ip_notice']
    assert response['ip_notice_version'] == 'ip-1'
    assert 'certificate_prefix' not in response and 'data_dir' not in response
    headers = sign_in(client)
    assert client.get('/api/session', headers=headers).json()['consent_required'] is False
    # The branding settings are loaded without changing the agreement version.
    service.config.validate()
    assert client.get('/api/session', headers=headers).json()['consent_required'] is False


def test_private_default_group_reaches_permissions_and_admin_report(client, service):
    report = client.get('/api/admin/progress', headers=sign_in(client, ADMIN)).json()
    assert report['groups'] == ['Academy']
    assert all(person['groups'] == ['Academy'] for person in report['people'])


def test_configured_prefix_is_identical_for_restored_reader_admin_and_rename(client, service):
    publish(service, 'SOP Chapter 9: Practice')
    reader = sign_in(client)
    admin = sign_in(client, ADMIN)
    restore(service)
    restore(service, chapter=0, score=16, total=20, identity='final-pass')
    expected = 'PG-1969-CA6CC84B'  # ts50 is still Dec31 in Eastern; preserve the historical digest.
    before = client.get('/api/posts', headers=reader).json()['certificate']
    assert before['number'] == expected
    people = client.get('/api/admin/progress', headers=admin).json()['people']
    assert next(person for person in people if person['email'] == READER)['certificate']['number'] == expected
    renamed = client.post('/api/certificate/name', headers=reader, json={'name': 'Example Student'}).json()
    assert renamed['number'] == expected and renamed['name'] == 'Example Student'


def test_private_prefix_retains_eastern_year_boundaries():
    eve = datetime(2026, 12, 31, 20, 30, tzinfo=ZoneInfo('America/New_York')).timestamp()
    assert honors.number('final-pass', eve, prefix='PG') == 'PG-2026-CA6CC84B'
    assert honors.number('final-pass', eve + 4 * 3600, prefix='PG') == 'PG-2027-CA6CC84B'
