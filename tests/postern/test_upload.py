"""ENG-D12: upload, caption and sign-in limits at the HTTP boundary.

The real caps (100 MB per file, 1 GB free-disk floor) are too large to reach with real bytes, so the
configured value is lowered or `shutil.disk_usage` is replaced instead of writing a huge file. The
design's own numbers are asserted against `Config`'s defaults where they cannot be exercised.
"""
from __future__ import annotations
import collections
import io
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image
import pillow_heif

import postern.service
from starlette.datastructures import UploadFile
from conftest import ADMIN, READER, FIXTURE_PDF, create_post, sign_in
from postern.auth import Refusal
from postern.config import Config

Usage = collections.namedtuple('Usage', 'total used free')
UNSUPPORTED = ['minutes.docx', 'budget.xlsx', 'deck.pptx', 'briefing.webm', 'clip.mov', 'notes.txt', 'policy', 'policy.pdf.exe']


def image_bytes(fmt):
    buffer = io.BytesIO()
    Image.new('RGB', (16, 16), (200, 60, 60)).save(buffer, format=fmt)
    return buffer.getvalue()


def heic_bytes():
    pillow_heif.register_heif_opener()
    return image_bytes('HEIF')


SAMPLES = {'policy.pdf': FIXTURE_PDF.read_bytes, 'photo.jpg': lambda: image_bytes('JPEG'),
           'photo.jpeg': lambda: image_bytes('JPEG'), 'photo.png': lambda: image_bytes('PNG'),
           'photo.heic': heic_bytes}


def upload(client, headers, post_id, filename, payload):
    return client.post(f'/api/admin/posts/{post_id}/items', headers=headers,
                       files={'file': (filename, io.BytesIO(payload), 'application/octet-stream')})


def items(client, headers):
    return client.get('/api/admin/posts', headers=headers).json()['posts'][0]['items']


@pytest.fixture
def admin(client):
    return sign_in(client, ADMIN, 'Demo administrator')


@pytest.fixture
def draft(client, admin):
    return create_post(client, admin)


@pytest.fixture
def small_limit(config):
    """Lower the 100 MB cap before the service starts so the oversized case needs 64 KB, not 100 MB."""
    config.upload_limit = 64 * 1024
    return config.upload_limit


@pytest.mark.parametrize('filename', list(SAMPLES))
def test_accepts_pdf_jpeg_png_and_heic(client, admin, draft, filename):
    response = upload(client, admin, draft['id'], filename, SAMPLES[filename]())
    assert response.status_code == 200, response.text
    assert response.json()['derive_status'] == 'queued'
    assert [(i['filename'], i['kind']) for i in items(client, admin)] == [(filename, 'pdf' if filename.endswith('.pdf') else 'photo')]


@pytest.mark.parametrize('filename', UNSUPPORTED)
def test_rejects_every_other_type_naming_the_reason(client, admin, draft, filename):
    response = upload(client, admin, draft['id'], filename, b'%not a supported document%')
    assert response.status_code == 415, response.text
    message = response.json()['message']
    assert all(name in message for name in ('PDF', 'JPEG', 'PNG', 'HEIC')), message
    assert items(client, admin) == []


def test_rejects_a_file_over_the_upload_cap(small_limit, client, admin, draft, config, tmp_path):
    assert Config(data_dir=tmp_path / 'defaults').upload_limit == 100 * 1024 * 1024
    response = upload(client, admin, draft['id'], 'policy.pdf', b'%PDF-1.4\n' + b'0' * small_limit)
    assert response.status_code == 413, response.text
    assert 'MB' in response.json()['message']
    assert items(client, admin) == []
    assert list((config.data_dir / 'staging').iterdir()) == [], 'a refused upload must not leave bytes staged'


@pytest.mark.parametrize('caller,status,code', [('anonymous', 401, 'sign_in_required'),
    ('reader', 403, 'access_denied'), ('unconsented', 403, 'consent_required')])
def test_upload_authorization_happens_before_multipart_spooling(monkeypatch, client, admin, draft, caller, status, code):
    headers = {} if caller == 'anonymous' else sign_in(client, ADMIN if caller == 'unconsented' else READER,
                                                     consent=caller != 'unconsented')
    async def forbidden_write(*args):
        pytest.fail('Unauthorized bytes reached the multipart spool')
    monkeypatch.setattr(UploadFile, 'write', forbidden_write)
    response = upload(client, headers, draft['id'], 'policy.pdf', b'x' * (2 * 1024 * 1024))
    assert response.status_code == status
    assert response.json()['error'] == code
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('declared_length', [None, '1'])
def test_streamed_upload_is_bounded_without_trusting_content_length(monkeypatch, client, admin, draft, config, declared_length):
    import asyncio
    import json
    config.upload_limit = 1024 * 1024
    written, spools = [], []
    original = UploadFile.write
    async def observe(file, data):
        written.append(len(data));spools.append(file.file)
        await original(file, data)
    monkeypatch.setattr(UploadFile, 'write', observe)
    prefix = b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="large.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
    chunks = [prefix] + [b'x' * (128 * 1024)] * 12 + [b'\r\n--boundary--\r\n']
    headers = [(b'content-type', b'multipart/form-data; boundary=boundary'),
               (b'authorization', admin['Authorization'].encode())]
    if declared_length is not None:headers.append((b'content-length', declared_length.encode()))
    async def request():
        messages, consumed = [], 0
        async def receive():
            nonlocal consumed
            if consumed < len(chunks):
                body = chunks[consumed];consumed += 1
                return {'type':'http.request', 'body':body, 'more_body':consumed < len(chunks)}
            await asyncio.Event().wait()
        async def send(message):messages.append(message)
        await client.app({'type':'http', 'asgi':{'version':'3.0'}, 'http_version':'1.1', 'method':'POST',
                          'scheme':'http', 'path':f"/api/admin/posts/{draft['id']}/items", 'raw_path':b'',
                          'query_string':b'', 'root_path':'', 'headers':headers,
                          'client':('testclient',50000), 'server':('testserver',80)}, receive, send)
        return messages, consumed
    messages, consumed = client.portal.call(request)
    assert messages[0]['status'] == 413
    body = b''.join(message.get('body', b'') for message in messages)
    assert json.loads(body)['error'] == 'file_too_large'
    assert consumed < len(chunks), 'the oversized body was drained instead of refused'
    assert sum(written) <= config.upload_limit + 64 * 1024
    assert spools and all(spool.closed for spool in spools)
    assert items(client, admin) == []


def test_rejects_an_empty_file(client, admin, draft, config):
    response = upload(client, admin, draft['id'], 'policy.pdf', b'')
    assert response.status_code == 422, response.text
    assert 'empty' in response.json()['message'].lower()
    assert items(client, admin) == []
    assert list((config.data_dir / 'staging').iterdir()) == []


def test_rejects_an_eleventh_attachment(client, admin, draft, service):
    def fill(db):
        for sort in range(9):
            db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort)"
                       " VALUES (?,?,?,'photo',?,1,1,'ready',?)", (str(uuid.uuid4()), draft['id'], f'photo-{sort}.jpg', 'x' * 64, sort))
    service.db.submit(fill).result(timeout=5)
    assert upload(client, admin, draft['id'], 'photo-9.jpg', image_bytes('JPEG')).status_code == 200
    response = upload(client, admin, draft['id'], 'photo-10.jpg', image_bytes('JPEG'))
    assert response.status_code == 422, response.text
    assert 'ten' in response.json()['message'].lower()
    assert len(items(client, admin)) == 10


@pytest.mark.parametrize('length,status', [(280, 200), (281, 422)])
def test_the_caption_stops_at_280_characters(client, admin, length, status):
    response = client.post('/api/admin/posts', headers=admin,
                           json={'title': 'Tip pooling policy', 'body': 'x' * length, 'category': 'Policies'})
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()['body'] == 'x' * length
    else:
        assert '280' in response.json()['message']


@pytest.mark.parametrize('title,status', [('T' * 120, 200), ('T' * 121, 422), ('', 422), ('   ', 422)])
def test_the_title_cap_is_enforced(client, admin, title, status):
    response = client.post('/api/admin/posts', headers=admin, json={'title': title, 'body': '', 'category': ''})
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()['title'] == title


def test_uploads_stop_before_the_free_disk_floor_is_crossed(monkeypatch, client, admin, draft, config):
    assert Config(data_dir=config.data_dir).free_floor == 1024 * 1024 * 1024
    free = SimpleNamespace(value=config.free_floor + config.upload_limit)
    monkeypatch.setattr(postern.service, 'shutil',
                        SimpleNamespace(disk_usage=lambda path: Usage(10 ** 12, 10 ** 12 - free.value, free.value)))
    assert client.get('/health').json()['storage'] == 'ready'
    free.value -= 1
    assert client.get('/health').json()['storage'] == 'low'
    response = upload(client, admin, draft['id'], 'policy.pdf', FIXTURE_PDF.read_bytes())
    assert response.status_code >= 400, response.text
    assert 'space' in response.json()['message'].lower()
    assert items(client, admin) == []
    assert list((config.data_dir / 'staging').iterdir()) == []


def attempt_signin(client):
    return client.post('/api/auth/demo', json={'email': READER, 'name': 'Demo reader'})


class ShiftedMonotonic:
    """Only `time.monotonic` moves; everything else in postern.service keeps the real clock."""

    def __init__(self, module):
        self.module, self.offset = module, 0.0

    def __getattr__(self, name):
        return getattr(self.module, name)

    def monotonic(self):
        return self.module.monotonic() + self.offset


def test_sign_in_rate_limit_refuses_past_ten_a_minute_and_recovers(monkeypatch, client):
    clock = ShiftedMonotonic(postern.service.time)
    monkeypatch.setattr(postern.service, 'time', clock)
    assert [attempt_signin(client).status_code for _ in range(10)] == [200] * 10
    refused = attempt_signin(client)
    assert refused.status_code == 429, refused.text
    assert 'too many' in refused.json()['message'].lower()
    clock.offset = 59
    assert attempt_signin(client).status_code == 429, 'the window is a full minute'
    clock.offset = 61
    assert attempt_signin(client).status_code == 200


def test_shared_ip_budget_is_separate_from_the_account_budget(client, service):
    for _ in range(10):
        assert attempt_signin(client).status_code == 200
    assert attempt_signin(client).status_code == 429
    for _ in range(300):
        service.rate_limit('ip:198.51.100.7')
    with pytest.raises(Refusal) as refused:
        service.rate_limit('ip:198.51.100.7')
    assert refused.value.status == 429
