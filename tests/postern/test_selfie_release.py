"""Release boundaries: concurrent enrollment, bounded pixels and Drive deletion."""
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import httplib2
import pytest
from googleapiclient.errors import HttpError
from PIL import Image

from conftest import events
from postern.config import Config
from postern.storage import GoogleStore, StoreUnavailable
from test_selfie import capture, enrollment, login


def test_concurrent_captures_create_only_one_pending_enrollment(client, service):
    headers = login(client)
    ready = threading.Barrier(2)

    def submit():
        ready.wait(timeout=10)
        return capture(client, headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: submit(), range(2)))

    assert sorted(response.status_code for response in responses) == [202, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()['error'] == 'selfie_pending'
    accepted = next(response for response in responses if response.status_code == 202)
    pending = service.db.read(lambda db: [dict(row) for row in db.execute('SELECT * FROM selfies')])
    assert len(pending) == 1
    assert pending[0]['id'] == accepted.json()['pending_id']
    assert pending[0]['status'] == 'pending'
    assert len(events(service, 'selfie_consent_accepted')) == 1
    assert len(events(service, 'selfie_capture_attempt')) == 1
    assert [path.name for path in (service.config.data_dir/'selfies').iterdir()] == [pending[0]['id']+'.jpg']
    assert not list((service.config.data_dir/'staging').iterdir())
    assert client.get('/api/posts', headers=headers).json()['error'] == 'selfie_required'


def test_selfie_pixel_limit_rejects_small_compressed_file_without_retaining_it(client, service):
    body = io.BytesIO()
    # Above the selfie-specific four-megapixel limit, while well below the upload byte limit.
    Image.new('RGB', (2001, 2000), '#157f78').save(body, 'JPEG')
    assert len(body.getvalue()) < 1024 * 1024
    result = capture(client, login(client), body.getvalue())
    assert result.status_code == 422
    assert result.json()['error'] == 'invalid_selfie'
    assert not list((service.config.data_dir/'selfies').iterdir())
    assert not list((service.config.data_dir/'staging').iterdir())
    assert service.db.read(lambda db: db.execute('SELECT COUNT(*) FROM selfies').fetchone()[0]) == 0
    assert not events(service, 'selfie_consent_accepted')


@pytest.fixture
def google_delete(tmp_path, monkeypatch):
    store = GoogleStore(Config(tmp_path, selfie_folder_id='private-selfies'))
    drive = Mock()
    files = drive.files.return_value
    files.get.return_value.execute.return_value = {
        'parents': ['private-selfies'], 'appProperties': {'kind': 'selfie'},
    }
    monkeypatch.setattr(store, '_service', lambda *_: drive)
    return store, files


def http_error(status):
    return HttpError(httplib2.Response({'status': str(status)}), b'{"error":{"message":"synthetic failure"}}')


def test_google_delete_removes_only_the_verified_selfie(google_delete):
    store, files = google_delete
    store.delete_selfie('synthetic-selfie')
    files.get.assert_called_once_with(fileId='synthetic-selfie', fields='parents,appProperties')
    files.delete.assert_called_once_with(fileId='synthetic-selfie')
    files.delete.return_value.execute.assert_called_once_with()


@pytest.mark.parametrize('folder,metadata', [
    ('', {'parents': ['private-selfies'], 'appProperties': {'kind': 'selfie'}}),
    ('private-selfies', {'parents': ['documents'], 'appProperties': {'kind': 'selfie'}}),
    ('private-selfies', {'appProperties': {'kind': 'selfie'}}),
    ('private-selfies', {'parents': ['private-selfies'], 'appProperties': {'kind': 'original'}}),
    ('private-selfies', {'parents': ['private-selfies']}),
])
def test_google_delete_refuses_missing_folder_wrong_parent_or_wrong_kind(google_delete, folder, metadata):
    store, files = google_delete
    store.config.selfie_folder_id = folder
    files.get.return_value.execute.return_value = metadata
    with pytest.raises(StoreUnavailable, match='non-selfie'):
        store.delete_selfie('synthetic-document')
    files.delete.assert_not_called()


@pytest.mark.parametrize('operation', ['get', 'delete'])
def test_google_delete_treats_404_as_already_deleted(google_delete, operation):
    store, files = google_delete
    getattr(files, operation).return_value.execute.side_effect = http_error(404)
    store.delete_selfie('already-gone')
    store.delete_selfie('already-gone')
    if operation == 'get':
        files.delete.assert_not_called()
    else:
        assert files.delete.return_value.execute.call_count == 2


@pytest.mark.parametrize('operation', ['get', 'delete'])
@pytest.mark.parametrize('status', [403, 500])
def test_google_delete_propagates_non_404_failure_and_can_retry(google_delete, operation, status):
    store, files = google_delete
    request = getattr(files, operation).return_value
    request.execute.side_effect = http_error(status)
    with pytest.raises(HttpError) as failure:
        store.delete_selfie('retryable-selfie')
    assert failure.value.resp.status == status
    request.execute.side_effect = None
    store.delete_selfie('retryable-selfie')
    assert files.delete.return_value.execute.called
