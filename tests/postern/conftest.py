"""Shared fixtures for the application tests.

Everything runs against FakeStore, a fake allowlist and a controllable clock, so no test needs
Google credentials or touches real data. Do not add test cases here; add them in the lane files.
"""
from __future__ import annotations
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from postern.app import create_app
from postern.config import Config
from postern.service import Service
from postern.storage import FakeStore

FIXTURE_PDF = Path(__file__).resolve().parents[1] / 'fixtures/synthetic.pdf'
FIXTURE_PHOTO = Path(__file__).resolve().parents[1] / 'fixtures/photo.jpg'
FIXTURE_PAGE = Path(__file__).resolve().parents[1] / 'fixtures/page.jpg'
ADMIN = 'admin@example.invalid'
READER = 'reader@example.invalid'


class Clock:
    """Stands in for time.monotonic so freshness windows can be moved deliberately."""

    def __init__(self, start=10_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def allowlist():
    return [['email', 'name', 'role'], [ADMIN, 'Demo administrator', 'admin'], [READER, 'Demo reader', 'reader']]


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / 'data', mode='demo', notice_version='1')


@pytest.fixture
def store(tmp_path, allowlist):
    store = FakeStore(tmp_path / 'cloud')
    store.write_allowlist(allowlist)
    return store


@pytest.fixture
def service(config, store, clock):
    service = Service(config, store=store, clock=clock)
    yield service


@pytest.fixture
def client(config, service):
    with TestClient(create_app(config, service=service)) as client:
        client.service = service
        yield client


def sign_in(client, email=READER, name='Demo reader', consent=True):
    """Return the bearer headers for a signed-in member of the fake allowlist."""
    response = client.post('/api/auth/demo', json={'email': email, 'name': name})
    assert response.status_code == 200, response.text
    headers = {'Authorization': 'Bearer ' + response.json()['token'], 'X-Postern-Client': 'web'}
    if consent:
        assert client.post('/api/consent', json={'version': '1', 'ip_version': 'ip-1', 'monitoring_accepted': True, 'ip_accepted': True}, headers=headers).status_code == 200
    return headers


def create_post(client, headers, title='Tip pooling policy', body='Effective Oct 6.', category='Policies'):
    response = client.post('/api/admin/posts', json={'title': title, 'body': body, 'category': category}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def attach(client, headers, post_id, source=FIXTURE_PDF, filename='policy.pdf'):
    with open(source, 'rb') as handle:
        response = client.post(f'/api/admin/posts/{post_id}/items', headers=headers,
                               files={'file': (filename, handle, 'application/pdf')})
    return response


def seed_derived(service, item_id, pages=2):
    """Publish derived page images without paying for a real conversion."""
    directory = service.config.data_dir / 'derived' / item_id
    directory.mkdir(parents=True, exist_ok=True)
    sample = FIXTURE_PAGE
    for page in range(pages):
        shutil.copyfile(sample, directory / f'page-{page}.jpg')
    shutil.copyfile(sample, directory / 'thumb.jpg')
    (directory / 'manifest.json').write_text(json.dumps({'pages': pages, 'bytes': 1}))
    return directory


def ready_post(client, headers, service, pages=2, publish=True):
    """A published post with one ready attachment, without waiting on the converter."""
    post = create_post(client, headers)
    item_id = str(uuid.uuid4())
    seed_derived(service, item_id, pages=pages)
    # The staged original has to exist: publication refuses to commit an attachment the store never acknowledged.
    original = b'%PDF-1.4 postern fixture original\n' + bytes(range(256))
    (service.config.data_dir / 'staging' / (item_id + '.pdf')).write_bytes(original)

    def insert(db):
        db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort)"
                   " VALUES (?,?,?,?,?,?,?,'ready',0)",
                   (item_id, post['id'], 'policy.pdf', 'pdf', hashlib.sha256(original).hexdigest(), len(original), pages))
    service.db.submit(insert).result(timeout=5)
    if publish:
        assert client.post(f"/api/admin/posts/{post['id']}/publish", headers=headers).status_code == 202
        for _ in range(200):
            state = client.get('/api/admin/posts', headers=headers).json()['posts']
            if next(p['status'] for p in state if p['id'] == post['id']) == 'published':
                break
            time.sleep(0.05)
        else:
            raise AssertionError('publication did not confirm')
    return post, item_id


def open_view(client, headers, item_id, attempt=None):
    response = client.post(f'/api/items/{item_id}/views', json={'attempt_id': attempt or str(uuid.uuid4())}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def events(service, kind=None):
    query = 'SELECT * FROM events' + (' WHERE type=?' if kind else '') + ' ORDER BY seq'
    return service.db.read(lambda db: [dict(r) for r in db.execute(query, (kind,) if kind else ())])
