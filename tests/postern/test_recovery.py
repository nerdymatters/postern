"""ENG-D5: exported history is verified on the way back in, and a fresh Mac restores from the cloud alone.

The live data directory is only ever the thing being thrown away: every assertion about the restored host
reads the new directory, and the only inputs are the confirmed catalog, the sealed batches, the summary log
and the original files in the store. The local derivative is seeded deliberately wrong (two pages for an
eight-page original) so a restore that trusted it would be visible.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from postern import recover
from postern.app import create_app
from postern.config import Config
from postern.db import set_meta
from postern.service import Service
from postern.storage import FakeStore, StoreUnavailable

from conftest import ADMIN, FIXTURE_PDF, READER, create_post, events, open_view, seed_derived, sign_in

FIXTURE_PHOTO = Path(__file__).resolve().parents[1] / 'fixtures/photo.jpg'


@pytest.fixture
def store(tmp_path, allowlist):
    """In demo mode the cloud lives under the live data directory, which is where recover.main looks."""
    store = FakeStore(tmp_path / 'data' / 'fake-cloud')
    store.write_allowlist(allowlist)
    return store


async def _stop_background(service):
    loops = [t for t in service.tasks if 'background_loop' in t.get_coro().__qualname__]
    for task in loops:
        task.cancel()
    await asyncio.gather(*loops, return_exceptions=True)


@pytest.fixture
def steps(client):
    """Publication and export are stepped by hand; the three-second loop would otherwise set the timing."""
    client.portal.call(_stop_background, client.service)

    def step(coroutine, times=1):
        for _ in range(times):
            client.portal.call(coroutine)
    return step


def published(client, service, steps, admin, source=FIXTURE_PDF):
    """A published post whose original reached the store and whose local pages disagree with it."""
    post = create_post(client, admin)
    item_id, suffix = str(uuid.uuid4()), Path(source).suffix.lower()
    data = Path(source).read_bytes()
    (service.config.data_dir / 'staging' / (item_id + suffix)).write_bytes(data)
    seed_derived(service, item_id, pages=2)

    def insert(db):
        db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort)"
                   " VALUES (?,?,?,?,?,?,2,'ready',0)",
                   (item_id, post['id'], 'policy' + suffix, 'pdf' if suffix == '.pdf' else 'photo',
                    hashlib.sha256(data).hexdigest(), len(data)))
    service.db.submit(insert).result(timeout=5)
    assert client.post(f"/api/admin/posts/{post['id']}/publish", headers=admin).status_code == 202
    steps(service.process_operation)
    assert client.get('/api/admin/posts', headers=admin).json()['posts'][0]['status'] == 'published'
    return post, item_id


def history(client, service, steps, source=FIXTURE_PDF):
    """A published post, a reader who opened page 0, and every event sealed into an exported batch."""
    admin = sign_in(client, ADMIN, 'Demo administrator')
    reader = sign_in(client, READER)
    post, item_id = published(client, service, steps, admin, source)
    view = open_view(client, reader, item_id)
    assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 200
    steps(service.export, 3)
    assert service.db.read(lambda db: db.execute('SELECT count(*) FROM events WHERE batch_id IS NULL').fetchone()[0]) == 0
    return {'admin': admin, 'reader': reader, 'post': post, 'item_id': item_id}


def restore_into(tmp_path, store, name='restored'):
    config = Config(data_dir=tmp_path / name, mode='demo', notice_version='1').validate()
    return config, recover.restore(config, store, log=lambda text: None)


def read(config, sql, values=()):
    db = sqlite3.connect(f"file:{config.data_dir / 'postern.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, values)]
    finally:
        db.close()


def setting(config, key):
    rows = read(config, 'SELECT value FROM meta WHERE key=?', (key,))
    return json.loads(rows[0]['value']) if rows else None


def only_batch(store):
    return next(b for b in store.batches())


@pytest.mark.parametrize('stage', ['catalog', 'batches', 'derivatives'])
def test_interrupted_restore_stays_gated_after_normal_startup(client, service, store, steps, tmp_path, monkeypatch, stage):
    admin = sign_in(client, ADMIN)
    post, _ = published(client, service, steps, admin)
    config = Config(data_dir=tmp_path / 'interrupted', mode='demo', notice_version='1', recovery_mode=True).validate()

    def interrupted(*args, **kwargs):
        raise StoreUnavailable('Synthetic interruption during restoration')

    if stage == 'derivatives':
        monkeypatch.setattr(recover, 'rebuild_derivatives', interrupted)
    else:
        monkeypatch.setattr(store, stage, interrupted)
    with pytest.raises(StoreUnavailable):
        recover.restore(config, store, log=lambda _: None)
    assert setting(config, 'recovery_mode') is True
    assert setting(config, 'history_incomplete') is True
    assert len(read(config, 'SELECT * FROM posts')) == (0 if stage == 'catalog' else 1)

    # The temporary CLI flag is gone. The durable gate must still protect the restored host.
    config.recovery_mode = False
    restored = Service(config, store=store)
    with TestClient(create_app(config, service=restored)) as host:
        reader = sign_in(host)
        assert host.get('/api/posts', headers=reader).json() == {'posts': [], 'quizzes': [], 'quiz_exempt': False, 'final_exam': None, 'certificate': None}
        response = host.get(f"/api/posts/{post['id']}/thumb.jpg", headers=reader)
        assert response.status_code == 403 and response.json()['error'] == 'access_denied'
        assert setting(config, 'recovery_mode') is True
        assert setting(config, 'history_incomplete') is True


def test_a_round_trip_reproduces_the_catalog_and_its_events(client, service, store, steps, tmp_path):
    room = history(client, service, steps)
    live = {e['id']: e for e in events(service)}

    config, report = restore_into(tmp_path, store)

    assert [p['id'] for p in read(config, 'SELECT * FROM posts')] == [room['post']['id']]
    assert [p['status'] for p in read(config, 'SELECT * FROM posts')] == ['published']
    item = read(config, 'SELECT * FROM items')[0]
    assert item['id'] == room['item_id'] and item['derive_status'] == 'ready' and item['derive_error'] is None
    # Eight, because the original was converted again; the dead Mac's directory claimed two.
    assert item['pages'] == 8
    assert sorted(p.name for p in (config.data_dir / 'derived' / item['id']).iterdir()) == \
        ['manifest.json'] + [f'page-{n}.jpg' for n in range(8)] + ['thumb.jpg']

    restored = {e['id']: e for e in read(config, 'SELECT * FROM events')}
    assert set(restored) == set(live)
    assert all(restored[k]['ts'] == live[k]['ts'] and restored[k]['type'] == live[k]['type'] for k in live)
    assert all(json.loads(restored[k]['payload']) == json.loads(live[k]['payload']) for k in live)
    assert {e['type'] for e in restored.values()} >= {'signin_ok', 'consent_accepted', 'admin_publish',
                                                      'view_requested', 'item_open', 'api_access', 'page_served'}

    summaries = sum(e['summary'] for e in live.values())
    assert report['batches'] == {'imported': 1, 'total': 1}
    assert report['events'] == {'imported': len(live), 'duplicate': summaries, 'from_summary': 0}
    assert report['publications']['posts'] == 1 and report['publications']['rebuilt'] == 1
    assert report['missing_ranges'] == [] and report['history_incomplete'] is False
    assert setting(config, 'recovery_mode') is True
    assert setting(config, 'history_incomplete') is False
    assert json.loads((config.data_dir / 'recovery-report.json').read_text())['events'] == report['events']

    batch = read(config, 'SELECT * FROM batches')[0]
    assert batch['id'] == only_batch(store)['batch_id'] and batch['confirmed_at'] and batch['summary_confirmed'] == 1
    assert read(config, 'SELECT count(*) c FROM events WHERE batch_id IS NULL')[0]['c'] == 0


def test_an_occupied_target_is_refused(client, service, store, steps, tmp_path, capsys):
    history(client, service, steps)
    settings = tmp_path / 'config.json'
    settings.write_text(json.dumps({'data_dir': str(service.config.data_dir), 'mode': 'demo', 'notice_version': '1'}))
    occupied = tmp_path / 'occupied'
    occupied.mkdir()
    (occupied / 'notes.txt').write_text('someone else lives here')

    with pytest.raises(SystemExit) as refused:
        recover.main(['--config', str(settings), '--into', str(occupied)])
    assert 'empty directory' in str(refused.value)
    assert [p.name for p in occupied.iterdir()] == ['notes.txt']

    for target in (service.config.data_dir, service.config.data_dir / 'restore', service.config.data_dir.parent):
        with pytest.raises(SystemExit) as live:
            recover.main(['--config', str(settings), '--into', str(target)])
        assert 'live data directory' in str(live.value)
    assert not (service.config.data_dir / 'restore').exists()

    capsys.readouterr()
    assert recover.main(['--config', str(settings), '--into', str(tmp_path / 'scratch')]) == 0
    printed = capsys.readouterr().out
    assert 'batches            1 of 1 imported' in printed
    assert 'sessions           0 restored' in printed
    assert 'recovery mode      on' in printed and 'history            complete' in printed
    assert (tmp_path / 'scratch' / 'postern.sqlite3').exists()


def flip_a_byte(store, batch):
    path = store.root / 'files' / batch['id']
    data = path.read_bytes()
    path.write_bytes(data[:10] + bytes([data[10] ^ 0x20]) + data[11:])


def truncate(store, batch):
    path = store.root / 'files' / batch['id']
    path.write_bytes(path.read_bytes()[:-20])


def claim_a_future_schema(store, batch):
    path = store.root / 'files' / (batch['id'] + '.meta')
    path.write_text(json.dumps({**json.loads(path.read_text()), 'schema': '9'}))


@pytest.mark.parametrize('damage,reason', [(flip_a_byte, 'SHA-256 does not match'),
                                           (truncate, 'bytes, not the recorded'),
                                           (claim_a_future_schema, 'schema version is 9')])
def test_a_corrupt_batch_becomes_unknown_history_not_a_silent_drop(client, service, store, steps, tmp_path, damage, reason):
    history(client, service, steps)
    batch = only_batch(store)
    damage(store, batch)

    config, report = restore_into(tmp_path, store)

    assert report['batches'] == {'imported': 0, 'total': 1}
    assert report['missing_ranges'] == [{'batch': batch['batch_id'], 'events': batch['count'], 'reason': report['missing_ranges'][0]['reason']}]
    assert reason in report['missing_ranges'][0]['reason']
    assert report['history_incomplete'] is True
    # The admin report reads this key and renders those readers as unknown rather than as not opened.
    assert setting(config, 'history_incomplete') is True
    assert "not 'not opened'" in recover.describe(report)

    restored = read(config, 'SELECT * FROM events')
    assert read(config, 'SELECT * FROM batches') == []
    assert all(e['batch_id'] is None for e in restored)
    assert [e['type'] for e in restored if e['type'] in ('api_access', 'page_served')] == []
    # The summary tier is a second copy, so the loss is bounded to the detail events in that batch.
    assert {e['type'] for e in restored} >= {'signin_ok', 'admin_publish', 'item_open'}
    assert report['events'] == {'imported': len(restored), 'duplicate': 0, 'from_summary': len(restored)}
    assert report['publications']['rebuilt'] == 1


def test_a_repeated_import_changes_nothing(client, service, store, steps, tmp_path):
    history(client, service, steps, source=FIXTURE_PHOTO)
    config, first = restore_into(tmp_path, store)
    tables = {name: read(config, f'SELECT * FROM {name} ORDER BY rowid') for name in ('posts', 'items', 'events', 'batches', 'meta')}

    second = recover.restore(config, store, log=lambda text: None)

    assert first['events']['imported'] > 0 and second['events']['imported'] == 0
    assert second['events']['duplicate'] == first['events']['imported'] + first['events']['duplicate']
    assert second['batches'] == first['batches'] and second['missing_ranges'] == []
    assert {name: read(config, f'SELECT * FROM {name} ORDER BY rowid') for name in tables} == tables


def test_an_original_the_store_no_longer_holds_is_named_not_guessed(client, service, store, steps, tmp_path, capsys):
    room = history(client, service, steps)
    original = service.db.read(lambda db: db.execute('SELECT drive_file_id FROM items WHERE id=?', (room['item_id'],)).fetchone()[0])
    (store.root / 'files' / original).unlink()
    settings = tmp_path / 'config.json'
    settings.write_text(json.dumps({'data_dir': str(service.config.data_dir), 'mode': 'demo', 'notice_version': '1'}))

    capsys.readouterr()
    assert recover.main(['--config', str(settings), '--into', str(tmp_path / 'scratch')]) == 2
    assert 'missing original   policy.pdf' in capsys.readouterr().out

    config = Config(data_dir=tmp_path / 'scratch', mode='demo', notice_version='1')
    item = read(config, 'SELECT * FROM items')[0]
    assert item['derive_status'] == 'failed' and 'could not be read' in item['derive_error']
    assert not (config.data_dir / 'derived' / item['id']).exists()
    assert len(read(config, 'SELECT * FROM posts')) == 1


@pytest.fixture
def recovered(client, service, store, steps, tmp_path):
    """The restored host, started the way an operator would start it on the replacement Mac."""
    room = history(client, service, steps)
    config, report = restore_into(tmp_path, store)
    # The operator's ordinary configuration: staying hidden has to come from the restored database itself.
    config.recovery_mode = False
    restored = Service(config, store=store)
    with TestClient(create_app(config, service=restored)) as host:
        host.service = restored
        yield {**room, 'config': config, 'report': report, 'host': host}


def test_no_session_survives_recovery(recovered):
    config, host = recovered['config'], recovered['host']
    assert read(config, 'SELECT * FROM sessions') == []
    assert host.get('/api/session', headers=recovered['reader']).status_code == 401
    assert host.get('/api/posts', headers=recovered['reader']).json()['error'] == 'sign_in_required'
    assert recovered['report']['sessions'] == 0

    # A fresh allowlist read restores membership; the reader restores nothing, they sign in again.
    assert len(read(config, 'SELECT * FROM members')) == 2
    again = sign_in(host, READER)
    assert again['Authorization'] != recovered['reader']['Authorization']
    assert len(read(config, 'SELECT * FROM sessions WHERE revoked=0')) == 1


def test_an_unverified_catalog_is_not_reader_visible(recovered):
    config, host, post, item_id = recovered['config'], recovered['host'], recovered['post'], recovered['item_id']
    reader = sign_in(host, READER)
    admin = sign_in(host, ADMIN, 'Demo administrator')
    assert host.get('/api/posts', headers=reader).json() == {'posts': [], 'quizzes': [], 'quiz_exempt': False, 'final_exam': None, 'certificate': None}
    assert host.get('/api/admin/posts', headers=admin).json() == {'posts': []}
    assert host.get(f"/api/posts/{post['id']}/thumb.jpg", headers=reader).json()['error'] == 'access_denied'
    denied = host.post(f'/api/items/{item_id}/views', json={'attempt_id': str(uuid.uuid4())}, headers=reader)
    assert denied.status_code == 403 and denied.json()['error'] == 'access_denied'
    assert host.get('/api/admin/health', headers=admin).json()['recovery_mode'] is True

    # The operator verifies the catalog and lifts recovery mode; only then is the restored post readable.
    host.portal.call(host.service.write, lambda db: set_meta(db, 'recovery_mode', False))
    assert [p['id'] for p in host.get('/api/posts', headers=reader).json()['posts']] == [post['id']]
    view = open_view(host, reader, item_id)
    assert host.get(f"/api/views/{view['id']}/page/7.jpg", headers=reader).content[:3] == b'\xff\xd8\xff'
    assert host.get(f"/api/views/{view['id']}/page/8.jpg", headers=reader).status_code == 404
    assert host.get(f"/api/posts/{post['id']}/thumb.jpg", headers=reader).status_code == 200


def test_a_quiz_pass_survives_a_lost_batch_through_the_summary_log(client, service, store, steps, tmp_path):
    from postern import quiz
    from test_quiz import answers, bank_file, read_chapter
    service.permissions.quizzes = quiz.load(bank_file(tmp_path, chapters=(1,)))
    admin = sign_in(client, ADMIN, 'Demo administrator')
    reader = sign_in(client, READER)
    post, _ = published(client, service, steps, admin)
    service.db.submit(lambda db: db.execute("UPDATE posts SET category='SOP Chapter 1: Introduction' WHERE id=?", (post['id'],))).result(timeout=5)
    read_chapter(service, READER, 1)
    version = client.get('/api/quizzes/1', headers=reader).json()['version']
    passed = client.post('/api/quizzes/1/attempts', headers=reader, json={'version': version, 'answers': answers(1, 5)})
    assert passed.json()['passed'] is True
    steps(service.export, 3)
    assert 'quiz_attempt' in {e['event'] for e in json.loads((store.root / 'summary.json').read_text())}
    flip_a_byte(store, only_batch(store))

    config, report = restore_into(tmp_path, store)

    assert report['history_incomplete'] is True
    db = sqlite3.connect(f"file:{config.data_dir / 'postern.sqlite3'}?mode=ro", uri=True)
    try:
        assert quiz.passes(db, READER) == {1}
    finally:
        db.close()
