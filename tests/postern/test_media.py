"""Media serving, per-reader stamping and composite isolation (ENG-D1, ENG-D15; tasks T1, T15)."""
from __future__ import annotations
from concurrent.futures import Future
import hashlib
import threading
import time

import pytest
from PIL import Image

from conftest import ADMIN, READER, attach, create_post, events, open_view, ready_post, sign_in
from postern.cache import CompositeCache, SessionInvalidated
from postern.stamp import label, stamp

SECOND = 'second@example.invalid'


@pytest.fixture
def allowlist():
    """A second reader, so two live sessions can ask for the same page."""
    return [['email', 'name', 'role'], [ADMIN, 'Demo administrator', 'admin'],
            [READER, 'Demo reader', 'reader'], [SECOND, 'Second reader', 'reader']]


def publish_now(client, admin, post_id):
    # Pump the publication queue instead of waiting for the three-second background pass.
    assert client.post(f'/api/admin/posts/{post_id}/publish', headers=admin).status_code == 202
    for _ in range(50):
        client.portal.call(client.service.process_operation)
        state = client.get('/api/admin/posts', headers=admin).json()['posts']
        if next(p['status'] for p in state if p['id'] == post_id) == 'published':
            return
        time.sleep(0.02)
    raise AssertionError('publication did not confirm')


def published(client, pages=2):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post, item_id = ready_post(client, admin, client.service, pages=pages, publish=False)
    publish_now(client, admin, post['id'])
    return admin, post, item_id


def routes(view_id, post_id):
    return [f'/api/views/{view_id}/page/0.jpg', f'/api/views/{view_id}/photo.jpg', f'/api/posts/{post_id}/thumb.jpg']


def directives(response):
    return {value.strip() for value in response.headers.get('cache-control', '').split(',')}


def pin(client, clock):
    """Freeze the overlay minute so a difference in bytes can only come from the reader."""
    client.service.cache.clock = clock
    return int(clock() // 60)


def write(service, sql, values):
    service.db.submit(lambda db: db.execute(sql, values)).result(timeout=5)


def test_image_routes_refuse_a_caller_without_a_credential(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    for route in routes(view['id'], post['id']):
        for headers in ({}, {'Authorization': 'Bearer ' + 'k' * 43}):
            response = client.get(route, headers=headers)
            assert response.status_code == 401, route
            assert response.json()['error'] == 'sign_in_required'
            assert 'no-store' in directives(response)
            assert not response.headers['content-type'].startswith('image/')


def test_a_view_serves_only_the_session_that_created_it(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    other = sign_in(client, SECOND, 'Second reader')
    view = open_view(client, reader, item_id)
    for route in routes(view['id'], post['id'])[:2]:
        response = client.get(route, headers=other)
        assert response.status_code == 403, route
        assert response.json()['error'] == 'access_denied'
        assert 'no-store' in directives(response)
    assert client.get(routes(view['id'], post['id'])[0], headers=reader).status_code == 200


def test_an_expired_session_cannot_fetch_pixels(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 200
    write(client.service, 'UPDATE sessions SET expires_at=? WHERE email=?', (time.time() - 1, READER))
    for route in routes(view['id'], post['id']):
        response = client.get(route, headers=reader)
        assert response.status_code == 401, route
        assert response.json()['error'] == 'sign_in_required'


def test_a_signed_out_session_cannot_fetch_pixels(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 200
    assert client.post('/api/auth/signout', headers=reader).status_code == 200
    for route in routes(view['id'], post['id']):
        response = client.get(route, headers=reader)
        assert response.status_code == 403, route
        assert response.json()['error'] == 'access_denied'


def test_an_archived_post_serves_no_more_pixels(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    assert client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader).status_code == 200
    assert client.post(f"/api/admin/posts/{post['id']}/archive", headers=admin).status_code == 200
    for route in routes(view['id'], post['id']):
        response = client.get(route, headers=reader)
        assert response.status_code == 403, route
        assert response.json()['error'] == 'access_denied'


@pytest.mark.parametrize('change', ['archive', 'demote_draft_admin', 'recovery'])
@pytest.mark.parametrize('thumbnail', [False, True])
def test_changes_during_render_are_checked_before_sending_pixels(monkeypatch, client, change, thumbnail):
    from concurrent.futures import ThreadPoolExecutor
    from postern.db import set_meta
    admin, post, item_id = published(client)
    if change == 'demote_draft_admin':
        write(client.service, "UPDATE posts SET status='draft' WHERE id=?", (post['id'],))
        reader = admin
    else:
        reader = sign_in(client)
    view = open_view(client, reader, item_id)
    entered, resume = threading.Event(), threading.Event()
    original = client.service.composite
    def paused(*args, **kwargs):
        entered.set()
        assert resume.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(client.service, 'composite', paused)
    route = f"/api/posts/{post['id']}/thumb.jpg" if thumbnail else f"/api/views/{view['id']}/page/0.jpg"
    with ThreadPoolExecutor() as pool:
        response = pool.submit(client.get, route, headers=reader)
        try:
            assert entered.wait(10)
            if change == 'archive':
                assert client.post(f"/api/admin/posts/{post['id']}/archive", headers=admin).status_code == 200
            elif change == 'demote_draft_admin':
                write(client.service, "UPDATE members SET role='reader' WHERE email=?", (ADMIN,))
            else:
                client.service.db.submit(lambda db: set_meta(db, 'recovery_mode', True)).result(timeout=5)
        finally:
            resume.set()
        refused = response.result(timeout=10)
    assert refused.status_code == 403
    assert refused.json()['error'] == 'access_denied'
    assert 'image/' not in refused.headers['content-type']
    assert events(client.service, 'item_open') == []


def test_a_page_outside_the_document_is_refused(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    for number in (2, 97, -1):
        response = client.get(f"/api/views/{view['id']}/page/{number}.jpg", headers=reader)
        assert response.status_code == 404, number
        assert not response.headers['content-type'].startswith('image/')
        assert 'no-store' in directives(response)
    assert events(client.service, 'item_open') == []


def test_an_unprepared_attachment_is_refused_with_preparing(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    write(client.service, "UPDATE items SET derive_status='converting' WHERE id=?", (item_id,))
    for route in routes(view['id'], post['id'])[:2]:
        response = client.get(route, headers=reader)
        assert response.status_code == 409, route
        assert response.json()['error'] == 'preparing'
        assert 'no-store' in directives(response)
    assert events(client.service, 'item_open') == []


def test_every_derivative_response_forbids_caching(client):
    admin, post, item_id = published(client)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    for route in routes(view['id'], post['id']):
        response = client.get(route, headers=reader)
        assert response.status_code == 200, route
        assert response.headers['content-type'] == 'image/jpeg'
        assert response.content[:2] == b'\xff\xd8'
        # ENG-D15 fixes the directive, not the whole header; `private` may accompany it.
        assert 'no-store' in directives(response)


def test_stamped_bytes_differ_by_reader_and_by_minute(tmp_path):
    page = tmp_path / 'page-0.jpg'
    Image.new('RGB', (900, 1200), (244, 243, 238)).save(page, quality=85)
    minute = 29_000_000
    mine = stamp(page, READER, minute=minute)
    assert stamp(page, READER, minute=minute) == mine
    assert stamp(page, SECOND, minute=minute) != mine
    assert stamp(page, READER, minute=minute + 1) != mine
    assert stamp(page, READER, minute=minute, thumbnail=True) != mine
    assert READER in label(READER, minute)
    assert label(READER, minute) != label(READER, minute + 1) != label(SECOND, minute + 1)


def test_the_composite_cache_is_keyed_by_session_and_minute(tmp_path, clock):
    page = tmp_path / 'page-0.jpg'
    Image.new('RGB', (900, 1200), (244, 243, 238)).save(page, quality=85)
    cache = CompositeCache(tmp_path / 'composites', free_floor=0, clock=clock)
    asset = ['item-1', 1, 'page-0.jpg']
    render = lambda reader: (lambda minute: stamp(page, reader, minute=minute))
    minute = int(clock() // 60)

    mine, state = cache.get('session-a', asset, render(READER))
    assert state == 'miss' and mine == stamp(page, READER, minute=minute)
    assert cache.get('session-a', asset, render(READER)) == (mine, 'hit')
    theirs, state = cache.get('session-b', asset, render(SECOND))
    assert state == 'miss' and theirs == stamp(page, SECOND, minute=minute) and theirs != mine
    assert cache.get('session-a', asset, render(READER)) == (mine, 'hit')

    clock.advance(60)
    rolled, state = cache.get('session-a', asset, render(READER))
    assert state == 'miss'
    assert rolled == stamp(page, READER, minute=minute + 1) != mine

    cache.invalidate_session('session-a')
    with pytest.raises(SessionInvalidated):
        cache.get('session-a', asset, render(READER))
    assert cache.get('session-b', asset, render(SECOND))[1] == 'miss'
    cache.close()
    assert not list((tmp_path / 'composites').glob('*.jpg'))


def test_two_readers_receive_different_bytes_for_the_same_page(client, clock):
    admin, post, item_id = published(client)
    pin(client, clock)
    sessions = [sign_in(client), sign_in(client, SECOND, 'Second reader')]
    pages, thumbs = [], []
    for headers in sessions:
        view = open_view(client, headers, item_id)
        response = client.get(f"/api/views/{view['id']}/page/0.jpg", headers=headers)
        assert response.status_code == 200
        pages.append(response.content)
        thumbs.append(client.get(f"/api/posts/{post['id']}/thumb.jpg", headers=headers).content)
    assert pages[0] != pages[1]
    assert thumbs[0] != thumbs[1]


def test_signing_out_drops_that_session_composites(client, clock):
    admin, post, item_id = published(client)
    pin(client, clock)
    identity = client.post('/api/auth/demo', json={'email': READER, 'name': 'Demo reader'}).json()
    reader = {'Authorization': 'Bearer ' + identity['token'], 'X-Postern-Client': 'web'}
    assert client.post('/api/consent', json={'version': '1', 'ip_version': 'ip-1', 'monitoring_accepted': True, 'ip_accepted': True}, headers=reader).status_code == 200
    view = open_view(client, reader, item_id)
    page = f"/api/views/{view['id']}/page/0.jpg"
    composites = client.service.config.data_dir / 'composites'
    assert client.get(page, headers=reader).status_code == 200
    assert len(list(composites.glob('*.jpg'))) == 1
    assert client.service.cache.snapshot()['entries'] == 1

    assert client.post('/api/auth/signout', headers=reader).status_code == 200
    assert list(composites.glob('*.jpg')) == []
    assert client.service.cache.snapshot()['entries'] == 0
    with pytest.raises(SessionInvalidated):
        client.service.cache.get(identity['id'], ['item', 1, 'page-0.jpg'], lambda minute: b'never served')
    assert client.get(page, headers=reader).status_code == 403


def test_revoking_a_reader_drops_their_composites(client, clock, store, allowlist):
    admin, post, item_id = published(client)
    pin(client, clock)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    page = f"/api/views/{view['id']}/page/0.jpg"
    composites = client.service.config.data_dir / 'composites'
    assert client.get(page, headers=reader).status_code == 200
    assert client.service.cache.snapshot()['entries'] == 1

    store.write_allowlist([row for row in allowlist if row[0] != READER])
    client.portal.call(client.service.refresh)
    assert list(composites.glob('*.jpg')) == []
    assert client.service.cache.snapshot()['entries'] == 0
    response = client.get(page, headers=reader)
    assert response.status_code == 403 and response.json()['error'] == 'access_denied'


def test_compositing_does_not_block_the_event_loop(client, monkeypatch):
    admin, post, item_id = published(client, pages=1)
    # Keep the real thirty-megapixel render after proving that its worker is off the event loop.
    source = client.service.config.data_dir / 'derived' / item_id / 'page-0.jpg'
    with Image.open(source) as page:
        page.convert('RGB').resize((4800, 6200)).save(source, quality=80)
    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    entered, release = threading.Event(), threading.Event()
    composite = client.service.composite

    def held_composite(*args, **kwargs):
        entered.set()
        assert release.wait(10), 'the test did not release the composite worker'
        return composite(*args, **kwargs)
    monkeypatch.setattr(client.service, 'composite', held_composite)

    def fetch(result, path):
        try:
            result.set_result(client.get(path, headers=reader))
        except BaseException as error:
            result.set_exception(error)

    media, session = Future(), Future()
    worker = threading.Thread(target=fetch, args=(media, f"/api/views/{view['id']}/page/0.jpg"), daemon=True)
    probe = threading.Thread(target=fetch, args=(session, '/api/session'), daemon=True)
    worker.start()
    try:
        # These timeouts bound deadlocks; responsiveness is the ordering, not a latency ratio.
        assert entered.wait(10), 'the media request did not enter the composite worker'
        probe.start()
        try:
            response = session.result(timeout=5)
        except TimeoutError as error:
            raise AssertionError('authenticated session did not finish while the composite was held') from error
        assert response.status_code == 200
        assert not media.done(), 'the media request finished before the composite was released'
    finally:
        release.set()
        for thread in (worker, probe):
            if thread.ident is not None:
                thread.join(timeout=10)
        assert not any(thread.is_alive() for thread in (worker, probe)), 'request thread did not finish after releasing the composite'

    response = media.result(timeout=0)
    assert response.status_code == 200 and len(response.content) > 100_000


def test_a_reader_never_receives_a_stored_original(client, tmp_path):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post = create_post(client, admin)
    original = tmp_path / 'annual-review.jpg'
    Image.new('RGB', (900, 1200), (206, 88, 42)).save(original, quality=92)
    assert attach(client, admin, post['id'], source=original, filename='annual-review.jpg').status_code == 200
    for _ in range(150):
        items = next(p for p in client.get('/api/admin/posts', headers=admin).json()['posts'] if p['id'] == post['id'])['items']
        assert items[0]['derive_status'] != 'failed', items
        if items[0]['derive_status'] == 'ready':
            break
        time.sleep(0.02)
    else:
        raise AssertionError('conversion did not finish')
    item_id = items[0]['id']
    publish_now(client, admin, post['id'])

    stored = {hashlib.sha256(original.read_bytes()).hexdigest()}
    for directory in (client.service.config.data_dir / 'staging', client.service.store.root / 'files'):
        stored |= {hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir() if path.is_file()}
    recorded = client.service.db.read(lambda db: db.execute('SELECT sha256 FROM items WHERE id=?', (item_id,)).fetchone()[0])
    assert recorded in stored  # the upload the server kept really is among the digests compared against

    reader = sign_in(client)
    view = open_view(client, reader, item_id)
    derived = (client.service.config.data_dir / 'derived' / item_id / 'page-0.jpg').read_bytes()
    for route in routes(view['id'], post['id']):
        response = client.get(route, headers=reader)
        assert response.status_code == 200, route
        assert response.headers['referrer-policy'] == 'no-referrer', route
        assert hashlib.sha256(response.content).hexdigest() not in stored, route
        assert response.content != derived, route
