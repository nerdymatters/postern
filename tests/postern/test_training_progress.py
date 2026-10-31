"""Admin progress uses the same durable evidence as reader progress, without hidden content."""
import asyncio
import uuid

import pytest

from conftest import ADMIN, READER, events, sign_in
from postern.db import event, set_meta
from postern.progress import dashboard
from postern.recover import import_events
from postern.service import Service


@pytest.fixture(autouse=True)
def pause_worker(monkeypatch):
    async def paused(_service):
        await asyncio.Event().wait()
    monkeypatch.setattr(Service, 'background_loop', paused)


def seed_item(service, kind='pdf', status='published', pages=4, duration=100):
    post, item = str(uuid.uuid4()), str(uuid.uuid4())
    def write(db):
        db.execute("INSERT INTO posts(id,title,body,category,status,created_at,created_by,published_at) VALUES (?,?,'','Module 1',?,1,?,2)",
                   (post, kind + ' training', status, ADMIN))
        db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) VALUES (?,?,?,?,?,1,?,'ready',0)",
                   (item, post, kind + '.file', kind, 'x' * 64, 0 if kind == 'video' else pages))
        if kind == 'video' and duration is not None:
            db.execute('INSERT INTO video_metadata VALUES (?,?)', (item, duration))
    service.db.submit(write).result()
    return post, item


def fetch(client, admin):
    response = client.get('/api/admin/progress', headers=admin)
    assert response.status_code == 200, response.text
    return response.json()


def test_shared_page_and_video_evidence_roster_and_published_scope(client, service):
    admin = sign_in(client, ADMIN)
    reader = sign_in(client)
    pdf, photo, video = [seed_item(service, kind) for kind in ('pdf', 'photo', 'video')]
    hidden = [seed_item(service, status=status) for status in ('draft', 'archived', 'publishing')]
    def history(db):
        for page in (0, 1, 1, 99, -1, True):
            event(db, 'page_served', email=READER, post_id=pdf[0], item_id=pdf[1], detail={'page': page}, timestamp=10)
        event(db, 'page_served', email=READER, post_id=photo[0], item_id=photo[1], detail={'page': 0}, timestamp=20)
        for ranges in ([[0, 10]], [[5, 20], [40, 45]], [[40, 45]]):
            event(db, 'video_progress', email=READER, post_id=video[0], item_id=video[1], detail={'ranges': ranges}, timestamp=30)
        event(db, 'video_bytes_requested', email=ADMIN, post_id=video[0], item_id=video[1], timestamp=99)
        event(db, 'view_requested', email=ADMIN, post_id=video[0], item_id=video[1], timestamp=100)
        event(db, 'thumbnail_served', email=ADMIN, post_id=pdf[0], item_id=pdf[1], timestamp=101)
        event(db, 'page_served', email='removed@example.invalid', post_id=pdf[0], item_id=pdf[1], detail={'page': 0})
        for post, item in hidden:
            event(db, 'page_served', email=ADMIN, post_id=post, item_id=item, detail={'page': 0})
    service.db.submit(history).result()
    report = fetch(client, admin)
    assert report['total_items'] == 3
    people = {person['email']: person for person in report['people']}
    assert set(people) == {ADMIN, READER}
    person = people[READER]
    assert (person['percent'], person['completed_items'], person['opened_items'], person['status']) == (58, 1, 3, 'in_progress')
    assert person['last_activity_at'] == 30
    video_item = next(item for item in person['items'] if item['kind'] == 'video')
    assert video_item['watched_seconds'] == 25 and video_item['fraction'] == .25
    assert people[ADMIN]['percent'] == 0 and people[ADMIN]['status'] == 'not_started'
    assert people[ADMIN]['last_activity_at'] is None
    assert sum(item['attempts'] for item in people[ADMIN]['items']) == 1
    personal = client.get('/api/posts', headers=reader).json()['posts']
    assert next(p for p in personal if p['id'] == video[0])['items'][0]['progress']['watched_seconds'] == 25
    assert next(p for p in personal if p['id'] == pdf[0])['items'][0]['progress']['opened_pages'] == [0, 1]


def test_recovered_history_unknown_and_completed_evidence(client, service):
    admin = sign_in(client, ADMIN)
    post, item = seed_item(service, pages=2)
    def restore(db):
        import_events(db, [{'event_id': 'restored', 'event': 'item_summary', 'ts': 10, 'email': READER,
                            'post_id': post, 'item_id': item, 'detail': {'pages': [0, 0, 1, 10]}}])
        set_meta(db, 'history_incomplete', True)
    service.db.submit(restore).result()
    report = fetch(client, admin)
    assert report['history_incomplete']
    people = {p['email']: p for p in report['people']}
    assert people[READER]['percent'] == 100 and people[READER]['status'] == 'complete'
    assert people[READER]['last_activity_at'] is None  # summary export time is not a page-open time
    assert people[ADMIN]['status'] == 'unknown'


def test_missing_video_metadata_and_empty_content_are_not_zero_completion(client, service):
    admin = sign_in(client, ADMIN)
    empty = fetch(client, admin)
    assert empty['total_items'] == 0
    assert all(p['percent'] is None and p['status'] == 'no_content' for p in empty['people'])
    seed_item(service, kind='video', duration=None)
    assert all(p['percent'] is None and p['status'] == 'unavailable' for p in fetch(client, admin)['people'])


def test_admin_authorization_consent_freshness_and_recovery(client, service, clock):
    assert client.get('/api/admin/progress').status_code == 401
    assert client.get('/api/admin/progress', headers=sign_in(client)).status_code == 403
    admin = sign_in(client, ADMIN, consent=False)
    assert client.get('/api/admin/progress', headers=admin).json()['error'] == 'consent_required'
    admin = sign_in(client, ADMIN)
    service.db.submit(lambda db: set_meta(db, 'recovery_mode', True)).result()
    assert client.get('/api/admin/progress', headers=admin).status_code == 409
    service.db.submit(lambda db: set_meta(db, 'recovery_mode', False)).result()
    clock.advance(61)
    assert client.get('/api/admin/progress', headers=admin).status_code == 503


def test_current_roster_uses_one_event_scan_and_reports_post_item_kinds(client, service):
    admin = sign_in(client, ADMIN)
    post, _ = seed_item(service, kind='video')
    statements = []
    def inspect(db):
        db.set_trace_callback(statements.append)
        return dashboard(db)
    service.db.read(inspect)
    assert len([s for s in statements if 'FROM events' in s]) == 1
    report = client.get(f'/api/admin/posts/{post}/report', headers=admin).json()
    assert report['post']['items'][0]['kind'] == 'video'
    assert 'video playback was recorded' in report['meaning']


def test_recovered_video_intervals_and_open_only_evidence_keep_partial_history(client, service):
    admin = sign_in(client, ADMIN)
    post, item = seed_item(service, kind='video')
    def restore(db):
        import_events(db, [
            {'event_id': 'recovered-video', 'event': 'video_progress', 'ts': 10, 'email': READER,
             'post_id': post, 'item_id': item, 'detail': {'ranges': [[0, 20], [10, 30], [90, 100.2], [120, 150], [50, 40], [True, 20], ['0', 20], [1], None]}},
            {'event_id': 'recovered-open', 'event': 'item_open', 'ts': 20, 'email': ADMIN,
             'post_id': post, 'item_id': item, 'detail': {}},
        ])
        set_meta(db, 'history_incomplete', True)
    service.db.submit(restore).result()
    people = {person['email']: person for person in fetch(client, admin)['people']}
    assert (people[READER]['percent'], people[READER]['status'], people[READER]['last_activity_at']) == (40, 'in_progress', 10)
    assert people[READER]['items'][0]['watched_seconds'] == 40
    assert (people[ADMIN]['percent'], people[ADMIN]['status'], people[ADMIN]['opened_items']) == (0, 'in_progress', 1)
    assert people[ADMIN]['last_activity_at'] == 20


def test_report_scan_leaves_writer_free_and_rechecks_admin_before_disclosure(client, service, monkeypatch):
    import concurrent.futures
    import threading
    import postern.service as service_module
    admin = sign_in(client, ADMIN)
    fetch(client, admin)
    assert len(events(service, 'training_progress_viewed')) == 1
    started, release = threading.Event(), threading.Event()
    original = service_module.dashboard
    def slow(db, *args, **kwargs):
        assert db.execute('PRAGMA query_only').fetchone()[0] == 1
        assert db.in_transaction
        started.set()
        assert release.wait(5)
        return original(db, *args, **kwargs)
    monkeypatch.setattr(service_module, 'dashboard', slow)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(client.get, '/api/admin/progress', headers=admin)
        assert started.wait(3)
        try:
            # This would time out if the history scan held the single writer.
            service.db.submit(lambda db: db.execute("UPDATE members SET role='reader' WHERE email=?", (ADMIN,))).result(timeout=1)
        finally:
            release.set()
        response = pending.result(timeout=5)
    assert response.status_code == 403 and response.json()['error'] == 'access_denied'
    assert len(events(service, 'training_progress_viewed')) == 1
