"""Real sandboxed MP4 preparation, range authorization, and durable watched coverage."""
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from conftest import ADMIN, READER, create_post, open_view, sign_in
from postern.db import set_meta
from postern.recover import import_events
from postern.video import CHUNK, byte_range


@pytest.fixture(scope='module')
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp('video')/'clip.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=12',
                    '-t', '4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], check=True)
    return path


def published_video(client, clip):
    admin = sign_in(client, ADMIN)
    post = create_post(client, admin)
    with clip.open('rb') as source:
        result = client.post(f"/api/admin/posts/{post['id']}/videos", headers=admin,
                             files={'file': ('clip.mp4', source, 'video/mp4')})
    assert result.status_code == 200, result.text
    item = result.json()['id']
    for _ in range(100):
        state = client.service.db.read(lambda db: dict(db.execute('SELECT * FROM items WHERE id=?', (item,)).fetchone()))
        if state['derive_status'] in ('ready', 'failed'):
            break
        time.sleep(.05)
    assert state['derive_status'] == 'ready', state
    assert client.post(f"/api/admin/posts/{post['id']}/publish", headers=admin).status_code == 202
    for _ in range(100):
        state = client.get('/api/posts', headers=admin).json()['posts']
        if state:
            break
        time.sleep(.05)
    assert state
    return admin, post, item


def playback(client, reader, item):
    view = open_view(client, reader, item)
    result = client.post(f"/api/views/{view['id']}/video", headers=reader)
    assert result.status_code == 200, result.text
    return view, result.json()['path']


def test_video_is_private_seekable_and_revoked_after_logout(client, clip):
    admin, post, item = published_video(client, clip)
    reader = sign_in(client)
    view, path = playback(client, reader, item)
    assert client.get(path, headers={'Range': 'bytes=0-31'}).status_code == 206
    response = client.get(path, headers={'Range': 'bytes=0-31'})
    assert len(response.content) == 32
    assert 'no-store' in response.headers['cache-control']
    assert response.headers['content-type'] == 'video/mp4'
    assert response.headers['content-range'].startswith('bytes 0-31/')
    assert client.get(path, headers={'Range': 'bytes=-12'}).content
    assert client.get(path, headers={'Range': 'bytes=99999999-'}).status_code == 416
    assert client.get(path).status_code == 416
    assert client.get('/api/video/' + 'x'*43, headers={'Range': 'bytes=0-'}).status_code == 401
    assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 404
    assert client.post(f"/api/views/{view['id']}/video", headers=admin).status_code == 403
    assert client.post('/api/auth/signout', headers=reader).status_code == 200
    assert client.get(path, headers={'Range': 'bytes=0-31'}).status_code == 403


def test_watched_ranges_union_are_personal_and_survive_recovery(client, clip):
    admin, post, item = published_video(client, clip)
    reader = sign_in(client)
    view, path = playback(client, reader, item)
    def coverage(headers=reader):
        return client.get('/api/posts', headers=headers).json()['posts'][0]['items'][0]['progress']
    assert coverage()['watched_seconds'] == 0
    client.get(path, headers={'Range': 'bytes=0-'})
    assert coverage()['watched_seconds'] == 0  # buffering isn't watching
    endpoint = f"/api/views/{view['id']}/video-progress"
    for ranges in ([[0, 1]], [[.5, 1.5]], [[3, 4]]):
        assert client.post(endpoint, headers=reader, json={'ranges': ranges}).status_code == 200
    assert coverage()['watched_seconds'] == 2.5  # seek gap remains unviewed
    assert coverage(admin)['watched_seconds'] == 0
    assert client.post(endpoint, headers=reader, json={'ranges': [[-1, 1]]}).status_code == 422
    assert client.post(endpoint, headers=reader, json={'ranges': [[1, 99]]}).status_code == 422
    assert client.post(endpoint, headers=reader, json={'ranges': [[2, 1]]}).status_code == 422
    assert client.post(endpoint, headers=admin, json={'ranges': []}).status_code == 403
    recovered = [{'event_id': 'restored-video', 'event': 'video_progress', 'ts': 1, 'email': READER,
                  'post_id': post['id'], 'item_id': item, 'detail': {'ranges': [[1, 2], [1, 2]]}}]
    client.service.db.submit(lambda db: import_events(db, recovered)).result()
    assert coverage()['watched_seconds'] == 3
    client.service.db.submit(lambda db: set_meta(db, 'history_incomplete', True)).result()
    assert coverage()['history_incomplete'] is True


def test_video_ticket_expires_and_permission_freshness_is_checked(client, clip, clock):
    admin, post, item = published_video(client, clip)
    reader = sign_in(client)
    view, path = playback(client, reader, item)
    client.service.db.submit(lambda db: db.execute('UPDATE video_tickets SET expires_at=0')).result()
    assert client.get(path, headers={'Range': 'bytes=0-'}).status_code == 401
    view, path = playback(client, reader, item)
    clock.advance(61)
    assert client.get(path, headers={'Range': 'bytes=0-'}).status_code == 503
    assert client.post(f"/api/views/{view['id']}/video-progress", headers=reader, json={'ranges': []}).status_code == 503


def test_range_is_bounded_even_for_open_ended_requests():
    assert byte_range('bytes=0-', CHUNK * 10) == (0, CHUNK - 1)
    assert byte_range('bytes=20-29', 100) == (20, 29)
    assert byte_range('bytes=-5', 100) == (95, 99)
    from postern.auth import Refusal
    for value in ('bytes=0-1,2-3', 'bytes=-0', 'bytes=20-10', 'bytes=-', ''):
        with pytest.raises(Refusal):
            byte_range(value, 100)


def test_resending_recorded_playback_costs_nothing_but_new_playback_is_still_bounded(client, clip):
    admin, post, item = published_video(client, clip)
    reader = sign_in(client)
    view, path = playback(client, reader, item)
    endpoint = f"/api/views/{view['id']}/video-progress"
    assert client.post(endpoint, headers=reader, json={'ranges': [[0, 2]]}).status_code == 200
    # The reply to [0, 2] was lost: the player re-sends it with two new seconds. Only the new seconds count.
    assert client.post(endpoint, headers=reader, json={'ranges': [[0, 2], [2, 4]]}).status_code == 200
    assert client.post(endpoint, headers=reader, json={'ranges': [[0, 4]]}).status_code == 200
    # Fresh playback beyond the elapsed-time allowance is still refused.
    assert client.post(endpoint, headers=reader, json={'ranges': [[4, 8]]}).status_code == 422
    progress = client.get('/api/posts', headers=reader).json()['posts'][0]['items'][0]['progress']
    assert progress['watched_seconds'] == 4


def test_subtitles_follow_video_hash_and_the_authorized_view(client, clip):
    admin, post, item = published_video(client, clip)
    service = client.service
    digest = service.db.read(lambda db: db.execute('SELECT sha256 FROM items WHERE id=?', (item,)).fetchone()[0])
    captions = 'WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nWelcome to the lesson.\n'
    service.subtitles = {digest: captions}
    reader = sign_in(client)
    view = open_view(client, reader, item)
    endpoint = f"/api/views/{view['id']}/video"
    response = client.post(endpoint, headers=reader)
    assert response.status_code == 200
    assert response.json()['subtitles'] == captions
    assert 'no-store' in response.headers['cache-control']
    assert client.post(endpoint).status_code == 401
    assert client.post(endpoint, headers=admin).status_code == 403
    service.subtitles = {'0' * 64: captions}
    assert 'subtitles' not in client.post(endpoint, headers=reader).json()
    progress = client.get('/api/posts', headers=reader).json()['posts'][0]['items'][0]['progress']
    assert progress['watched_seconds'] == 0
