"""Video originals, progress and access policy survive the full durable path."""
import hashlib
import shutil
import sqlite3
import time

import pytest

from conftest import ADMIN, READER, create_post, sign_in
from postern import recover
from postern.config import Config
from postern.progress import annotate
from postern.storage import HashSink, StoreUnavailable
from test_video import clip, playback, published_video


def test_video_cloud_restore_rebuilds_duration_bytes_and_watched_coverage(client, clip, tmp_path, store):
    _, post, item = published_video(client, clip)
    reader = sign_in(client)
    view, _ = playback(client, reader, item)
    assert client.post(f"/api/views/{view['id']}/video-progress", headers=reader,
                       json={'ranges': [[0, 1], [2, 3]]}).status_code == 200
    for _ in range(3):
        client.portal.call(client.service.export)
    restored = Config(data_dir=tmp_path / 'restored', mode='demo').validate()
    report = recover.restore(restored, store, log=lambda _: None)
    assert report['publications']['rebuilt'] == 1
    assert report['publications']['unavailable'] == []
    assert report['missing_ranges'] == []
    video = restored.data_dir / 'derived' / item / 'video.mp4'
    assert video.is_file() and video.stat().st_size > 0
    assert b'ftyp' in video.read_bytes()[:32]
    with sqlite3.connect(restored.data_dir / 'postern.sqlite3') as db:
        db.row_factory = sqlite3.Row
        attachment = dict(db.execute('SELECT i.*,v.duration_seconds FROM items i JOIN video_metadata v ON v.item_id=i.id').fetchone())
        assert attachment['derive_status'] == 'ready'
        assert attachment['pages'] == 0
        assert attachment['duration_seconds'] == pytest.approx(4)
        restored_post = {'id': post['id'], 'items': [attachment]}
        assert annotate(db, [restored_post], READER)[0]['items'][0]['progress']['watched_seconds'] == 2
        assert annotate(db, [restored_post], ADMIN)[0]['items'][0]['progress']['watched_seconds'] == 0
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM video_tickets').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['member_removed', 'archived', 'consent_outdated'])
def test_an_existing_video_ticket_obeys_current_access_policy(client, clip, store, change):
    admin, post, item = published_video(client, clip)
    reader = sign_in(client)
    view, path = playback(client, reader, item)
    assert client.get(path, headers={'Range': 'bytes=0-31'}).status_code == 206
    if change == 'member_removed':
        store.write_allowlist([['email', 'name', 'role'], [ADMIN, 'Administrator', 'admin']])
        client.portal.call(client.service.refresh)
    elif change == 'archived':
        assert client.post(f"/api/admin/posts/{post['id']}/archive", headers=admin).status_code == 200
    else:
        client.service.db.submit(lambda db: db.execute("UPDATE sessions SET consent_version='' WHERE email=?", (READER,))).result()
    assert client.get(path, headers={'Range': 'bytes=0-31'}).status_code == 403
    assert client.post(f"/api/views/{view['id']}/video-progress", headers=reader, json={'ranges': []}).status_code == 403


def test_progress_budget_rejects_instant_completion_and_heartbeats_renew_only_the_ticket(client, clip):
    _, _, item = published_video(client, clip)
    reader = sign_in(client)
    view, path = playback(client, reader, item)
    endpoint = f"/api/views/{view['id']}/video-progress"
    client.service.db.submit(lambda db: db.execute('UPDATE video_checkpoints SET reported_at=? WHERE view_id=?', (time.time() + 10, view['id']))).result()
    assert client.post(endpoint, headers=reader, json={'ranges': [[0, 4]]}).status_code == 422
    client.service.db.submit(lambda db: db.execute('UPDATE video_tickets SET expires_at=? WHERE view_id=?', (time.time() + 1, view['id']))).result()
    assert client.post(endpoint, headers=reader, json={'ranges': []}).status_code == 200
    expiry = client.service.db.read(lambda db: db.execute('SELECT expires_at FROM video_tickets WHERE view_id=?', (view['id'],)).fetchone()[0])
    assert expiry > time.time() + 1000
    progress = client.get('/api/posts', headers=reader).json()['posts'][0]['items'][0]['progress']
    assert progress['watched_seconds'] == 0
    client.service.db.submit(lambda db: db.execute('UPDATE video_checkpoints SET reported_at=? WHERE view_id=?', (time.time() - 4, view['id']))).result()
    assert client.post(endpoint, headers=reader, json={'ranges': [[0, 4]]}).status_code == 200
    assert client.get('/api/posts', headers=reader).json()['posts'][0]['items'][0]['progress']['watched_seconds'] == 4


def test_malformed_mp4_fails_safely_without_publication(client):
    admin = sign_in(client, ADMIN)
    post = create_post(client, admin)
    result = client.post(f"/api/admin/posts/{post['id']}/videos", headers=admin,
                         files={'file': ('broken.mp4', b'private parser input, not an MP4', 'video/mp4')})
    assert result.status_code == 200, result.text
    identity = result.json()['id']
    for _ in range(100):
        item = client.service.db.read(lambda db: dict(db.execute('SELECT * FROM items WHERE id=?', (identity,)).fetchone()))
        if item['derive_status'] == 'failed':
            break
        time.sleep(.05)
    assert item['derive_status'] == 'failed'
    assert item['derive_error'] == 'The attachment could not be converted safely. Check its format, size and page count.'
    assert not (client.service.config.data_dir / 'derived' / identity).exists()
    assert client.post(f"/api/admin/posts/{post['id']}/publish", headers=admin).status_code == 202
    client.portal.call(client.service.process_operation)
    assert client.get('/api/posts', headers=sign_in(client)).json()['posts'] == []


def test_remote_video_hashing_is_streamed_and_bounded():
    sink = HashSink(8)
    assert sink.write(b'abcd') == 4
    assert sink.write(b'efgh') == 4
    assert sink.hasher.hexdigest() == hashlib.sha256(b'abcdefgh').hexdigest()
    with pytest.raises(StoreUnavailable, match='expected bound'):
        sink.write(b'x')


def test_revocation_during_a_video_read_prevents_bytes_leaving(client, clip, monkeypatch):
    _, _, item = published_video(client, clip)
    reader = sign_in(client)
    _, path = playback(client, reader, item)
    read_path = client.service.deriver.read_path

    def revoke_after_read(*args):
        result = read_path(*args)
        client.service.db.submit(lambda db: db.execute('UPDATE sessions SET revoked=1 WHERE email=?', (READER,))).result()
        return result

    monkeypatch.setattr(client.service.deriver, 'read_path', revoke_after_read)
    response = client.get(path, headers={'Range': 'bytes=0-31'})
    assert response.status_code == 403
    assert response.headers['content-type'].startswith('application/json')


def test_an_evicted_video_rebuilds_from_its_cloud_original(client, clip):
    _, _, item = published_video(client, clip)
    reader = sign_in(client)
    _, path = playback(client, reader, item)
    derived = client.service.config.data_dir / 'derived' / item
    shutil.rmtree(derived)
    response = client.get(path, headers={'Range': 'bytes=0-31'})
    assert response.status_code == 409
    assert response.json()['error'] == 'preparing'
    for _ in range(100):
        state = client.service.db.read(lambda db: db.execute('SELECT derive_status FROM items WHERE id=?', (item,)).fetchone()[0])
        if state == 'ready' and (derived / 'video.mp4').is_file():
            break
        time.sleep(.05)
    response = client.get(path, headers={'Range': 'bytes=0-31'})
    assert response.status_code == 206
    assert b'ftyp' in response.content
