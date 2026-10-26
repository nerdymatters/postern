"""Enrollment privacy/access boundaries, using a public-domain test portrait and private FakeStore."""
import asyncio
import concurrent.futures
import io
import json
import re
import shutil
import threading
import time
import uuid

import pytest
from PIL import Image
from pathlib import Path

from postern import selfie
from postern.auth import parse_allowlist
from postern.storage import StoreUnavailable
from conftest import ADMIN, READER, create_post, events, open_view, ready_post

REAL_LOOP = selfie.loop


def test_enrollment_admission_keeps_a_burst_out_of_the_audit_queue(client, service, monkeypatch):
    """Only eight upload preflights may be waiting on the durable writer."""
    import httpx
    headers = login(client)
    async def exercise():
        saturated, overflow, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        active = peak = 0
        original = service.execute
        async def blocked(request, *args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 8:
                saturated.set()
            if active > 8:
                overflow.set()
            try:
                await release.wait()
                return await original(request, *args, **kwargs)
            finally:
                active -= 1
        async def captured(service, request, upload, version):
            await upload.close()
            return {'pending_id': 'synthetic'}
        monkeypatch.setattr(service, 'execute', blocked)
        monkeypatch.setattr(selfie, 'capture', captured)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url='http://test') as browser:
            pending = [asyncio.create_task(browser.post('/api/selfie', headers=headers,
                files={'file': ('camera.jpg', photo(), 'image/jpeg')}, data={'consent_version': 'selfie-1'})) for _ in range(12)]
            try:
                await asyncio.wait_for(saturated.wait(), 2)
                try:
                    await asyncio.wait_for(overflow.wait(), .2)
                except asyncio.TimeoutError:
                    pass
                assert not overflow.is_set()
            finally:
                release.set()
                responses = await asyncio.gather(*pending)
            assert [r.status_code for r in responses] == [202]*12
            assert peak == 8
    client.portal.call(exercise)


def test_queued_enrollment_rechecks_revocation_before_reading_upload(client, service):
    import httpx
    headers = login(client)
    async def exercise():
        for _ in range(8):
            await service.selfie_admissions.acquire()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url='http://test') as browser:
            request = asyncio.create_task(browser.post('/api/selfie', headers=headers,
                files={'file': ('camera.jpg', photo(), 'image/jpeg')}, data={'consent_version': 'selfie-1'}))
            try:
                await asyncio.sleep(.05)
                assert not request.done()
                await service.write(lambda db: db.execute('UPDATE sessions SET revoked=1 WHERE email=?', (READER,)))
            finally:
                for _ in range(8):
                    service.selfie_admissions.release()
            response = await asyncio.wait_for(request, 2)
            assert response.status_code == 403
    client.portal.call(exercise)
    assert events(service, 'selfie_capture_attempt') == []
    assert not list((service.config.data_dir/'selfies').iterdir())


@pytest.fixture(autouse=True)
def enrollment(config, monkeypatch):
    config.selfie_enabled = True
    async def paused(_service):
        await asyncio.Event().wait()
    monkeypatch.setattr(selfie, 'loop', paused)


def login(client, email=READER, device=None, consent=True):
    response = client.post('/api/auth/demo', json={'email': email, 'name': 'Synthetic'},
                           headers={'X-Postern-Device': device or str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    headers = {'Authorization': 'Bearer '+response.json()['token']}
    if consent:
        assert client.post('/api/consent', headers=headers, json={'version': '1', 'ip_version': 'ip-1', 'monitoring_accepted': True, 'ip_accepted': True}).status_code == 200
    return headers


def photo():
    output = io.BytesIO()
    exif = Image.Exif()
    exif[270] = 'Private source metadata must disappear'
    with Image.open(Path(__file__).resolve().parents[1]/'fixtures/selfie-face.jpg') as image:
        image.save(output, 'JPEG', exif=exif)
    return output.getvalue()


def capture(client, headers, body=None, version='selfie-1'):
    return client.post('/api/selfie', headers=headers, files={'file': ('camera.jpg', photo() if body is None else body, 'image/jpeg')},
                       data={'consent_version': version})


def enroll(client, headers):
    response = capture(client, headers)
    assert response.status_code == 202, response.text
    client.portal.call(selfie.process, client.service)
    state = client.get('/api/selfie', headers=headers).json()
    assert not state['required'], state
    return state['id']


def read_photo(service, identity):
    return service.db.read(lambda db: dict(db.execute('SELECT * FROM selfies WHERE id=?', (identity,)).fetchone()))


def test_consent_cloud_confirmation_and_sanitizing(client, service, store):
    headers = login(client, consent=False)
    assert capture(client, headers).json()['error'] == 'consent_required'
    client.post('/api/consent', headers=headers, json={'version': '1', 'ip_version': 'ip-1', 'monitoring_accepted': True, 'ip_accepted': True})
    assert client.get('/api/posts', headers=headers).json()['error'] == 'selfie_required'
    notice = capture(client, headers, version='old')
    assert (notice.status_code, notice.json()['error']) == (409, 'notice_changed')
    assert ' / กรุณาอ่านประกาศการใช้เซลฟีฉบับปัจจุบัน' in notice.json()['message']
    result = capture(client, headers)
    assert result.status_code == 202, result.text
    identity = result.json()['pending_id']
    pending = capture(client, headers)
    assert (pending.status_code, pending.json()['error']) == (409, 'selfie_pending')
    assert ' / กำลังบันทึกเซลฟีก่อนหน้าของคุณ กรุณารอสักครู่' in pending.json()['message']
    assert client.get('/api/posts', headers=headers).status_code == 403
    store.available = False
    client.portal.call(selfie.process, service)
    state = client.get('/api/selfie', headers=headers).json()
    assert state['required'] and state['pending_id'] == identity and state['error']
    store.available = True
    service.db.submit(lambda db: db.execute('UPDATE selfies SET next_try=0')).result()
    client.portal.call(selfie.process, service)
    assert client.get('/api/posts', headers=headers).status_code == 200
    response = client.get(f'/api/selfies/{identity}.jpg', headers=headers)
    assert response.headers['cache-control'] == 'no-store'
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.size == (256, 256)
        assert not image.getexif()
    row = read_photo(service, identity)
    assert (store.root/'files'/row['drive_id']).read_bytes() == response.content
    assert list((service.config.data_dir/'staging').iterdir()) == []


def test_enrollment_worker_drains_ready_backlog_without_a_delay_per_person(client, service, monkeypatch):
    identities = [capture(client, login(client)).json()['pending_id'] for _ in range(3)]
    original_purge = selfie.purge
    cleanup_passes = []
    async def counted_purge(service):
        cleanup_passes.append(time.monotonic())
        await original_purge(service)
    monkeypatch.setattr(selfie, 'purge', counted_purge)
    async def drain():
        worker = asyncio.create_task(REAL_LOOP(service))
        try:
            async def confirmed():
                while True:
                    count = await asyncio.to_thread(service.db.read, lambda db: db.execute(
                        "SELECT count(*) FROM selfies WHERE status='active'").fetchone()[0])
                    if count == len(identities):
                        return
                    await asyncio.sleep(.01)
            await asyncio.wait_for(confirmed(), 1.5)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    client.portal.call(drain)
    assert len(cleanup_passes) == 1  # Cleanup does not amplify with each enrollment.


def test_idle_enrollment_worker_wakes_when_a_capture_is_committed(client, service, monkeypatch):
    import httpx
    headers = login(client)
    async def exercise():
        waiting = asyncio.Event()
        original = service.selfie_wakeup.wait
        async def idle():
            waiting.set()
            return await original()
        monkeypatch.setattr(service.selfie_wakeup, 'wait', idle)
        worker = asyncio.create_task(REAL_LOOP(service))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url='http://test') as browser:
                response = await browser.post('/api/selfie', headers=headers,
                    files={'file': ('camera.jpg', photo(), 'image/jpeg')}, data={'consent_version': 'selfie-1'})
                assert response.status_code == 202
                identity = response.json()['pending_id']
                async def confirmed():
                    while (await asyncio.to_thread(read_photo, service, identity))['status'] != 'active':
                        await asyncio.sleep(.01)
                await asyncio.wait_for(confirmed(), 1.5)
                state = await browser.get('/api/selfie', headers=headers)
                assert state.status_code == 200 and not state.json()['required']
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    client.portal.call(exercise)


def test_enrollment_retry_backoff_does_not_block_other_people(client, service, store, monkeypatch):
    identities = [capture(client, login(client)).json()['pending_id'] for _ in range(2)]
    failed_drive = read_photo(service, identities[0])['drive_id']
    original = store.put_file
    attempts = []
    def put(identity, *args, **kwargs):
        if identity == failed_drive:
            attempts.append(identity)
            raise StoreUnavailable('Simulated delayed cloud storage')
        return original(identity, *args, **kwargs)
    monkeypatch.setattr(store, 'put_file', put)
    async def drain():
        worker = asyncio.create_task(REAL_LOOP(service))
        try:
            async def confirmed():
                while (await asyncio.to_thread(read_photo, service, identities[1]))['status'] != 'active':
                    await asyncio.sleep(.01)
            await asyncio.wait_for(confirmed(), 1.5)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    client.portal.call(drain)
    assert len(attempts) == 1
    failed = read_photo(service, identities[0])
    assert failed['status'] == 'pending' and failed['next_try'] > time.time()


def test_browser_account_binding_and_expiry(client, service):
    device = str(uuid.uuid4())
    headers = login(client, device=device)
    identity = enroll(client, headers)
    same = login(client, device=device)
    assert client.get('/api/session', headers=same).json()['selfie']['id'] == identity
    different = login(client)
    assert client.get('/api/posts', headers=different).json()['error'] == 'selfie_required'
    other_account = login(client, ADMIN, device)
    assert client.get('/api/posts', headers=other_account).json()['error'] == 'selfie_required'
    service.db.submit(lambda db: db.execute('UPDATE selfies SET expires_at=? WHERE id=?', (time.time()-1, identity))).result()
    assert client.get('/api/posts', headers=same).json()['error'] == 'selfie_required'
    assert client.get(f'/api/selfies/{identity}.jpg', headers=headers).status_code == 403
    assert client.get('/api/session', headers=headers).status_code == 200
    assert client.post('/api/auth/signout', headers=headers).status_code == 200


@pytest.mark.parametrize('body', [b'', b'not an image', b'x'*(selfie.LIMIT+1)])
def test_rejects_untrusted_uploads_without_retaining_bytes(client, service, body):
    response = capture(client, login(client), body)
    assert response.status_code in (413, 422), response.text
    # Each refusal explains itself in both languages, Thai after the " / " the web app splits on.
    assert re.search(r' / [฀-๿]', response.json()['message']), response.text
    assert not list((service.config.data_dir/'selfies').iterdir())
    assert not list((service.config.data_dir/'staging').iterdir())


def test_pending_cancelled_after_signout(client, service, store):
    headers = login(client)
    identity = capture(client, headers).json()['pending_id']
    client.post('/api/auth/signout', headers=headers)
    client.portal.call(selfie.process, service)
    row = read_photo(service, identity)
    assert row['status'] == 'purged'
    assert not (store.root/'files'/row['drive_id']).exists()


def test_reauthorization_after_cloud_write(client, service, store, monkeypatch):
    headers = login(client)
    identity = capture(client, headers).json()['pending_id']
    put = store.put_file
    def revoke(*args, **kwargs):
        result = put(*args, **kwargs)
        service.db.submit(lambda db: db.execute('UPDATE sessions SET revoked=1')).result()
        return result
    monkeypatch.setattr(store, 'put_file', revoke)
    client.portal.call(selfie.process, service)
    assert read_photo(service, identity)['status'] == 'purged'
    assert not events(service, 'selfie_enrolled')


def test_retention_owner_admin_and_retrying_purge(client, service, store):
    owner = login(client)
    first = enroll(client, owner)
    second = enroll(client, owner)
    admin = login(client, ADMIN)
    enroll(client, admin)
    assert client.get(f'/api/selfies/{first}.jpg', headers=owner).status_code == 403
    assert client.get(f'/api/selfies/{second}.jpg', headers=owner).status_code == 200
    assert client.get(f'/api/selfies/{first}.jpg', headers=admin).status_code == 200
    assert client.get('/api/admin/selfies', headers=owner).status_code == 403
    row = read_photo(service, first)
    assert row['status'] == 'retired'
    service.db.submit(lambda db: db.execute('UPDATE selfies SET retired_at=? WHERE id=?', (time.time()-90*86400-1, first))).result()
    store.available = False
    client.portal.call(selfie.purge, service)
    assert service.selfie_cleanup_error
    assert client.get(f'/api/selfies/{first}.jpg', headers=admin).status_code == 404
    assert first not in [r['id'] for r in client.get('/api/admin/selfies', headers=admin).json()['selfies']]
    store.available = True
    service.db.submit(lambda db: db.execute('UPDATE selfies SET next_try=0 WHERE id=?', (first,))).result()
    client.portal.call(selfie.purge, service)
    assert read_photo(service, first)['status'] == 'purged'
    assert not (service.config.data_dir/'selfies'/(first+'.jpg')).exists()
    assert not (store.root/'files'/row['drive_id']).exists()
    assert events(service, 'selfie_purged')


def test_other_reader_cannot_view_photo(client, service):
    headers = login(client)
    identity = enroll(client, headers)
    values = [['email','name','role'], [READER,'Reader','reader'], ['other@example.invalid','Other','reader']]
    service.db.submit(lambda db: service.permissions.apply(db, parse_allowlist(values))).result()
    other = login(client, 'other@example.invalid')
    enroll(client, other)
    assert client.get(f'/api/selfies/{identity}.jpg', headers=other).status_code == 403


def test_offboarding_requires_fresh_allowlist(client, service, clock):
    headers = login(client)
    identity = enroll(client, headers)
    clock.advance(61)
    service.db.submit(lambda db: db.execute('DELETE FROM members')).result()
    client.portal.call(selfie.purge, service)
    assert read_photo(service, identity)['status'] == 'active'
    service.db.submit(lambda db: service.permissions.apply(db, {})).result()
    client.portal.call(selfie.purge, service)
    assert read_photo(service, identity)['status'] == 'retired'


def test_stamp_audit_and_recovery_metadata(client, service):
    admin = login(client, ADMIN)
    enroll(client, admin)
    post, item = ready_post(client, admin, service)
    reader = login(client)
    identity = enroll(client, reader)
    view = open_view(client, reader, item)
    response = client.get(f"/api/views/{view['id']}/page/0.jpg", headers=reader)
    assert response.status_code == 200, response.text
    assert json.loads(events(service, 'page_requested')[-1]['payload'])['detail']['selfie_id'] == identity
    with Image.open(io.BytesIO(response.content)) as image:
        # The teal synthetic portrait is present in the footer, outside content.
        assert any(g > r*2 and b > r*2 for r,g,b in image.crop((0,image.height-100,image.width,image.height)).get_flattened_data())
    first = read_photo(service, identity)
    service.db.submit(lambda db: db.execute('DELETE FROM selfies')).result()
    service.db.submit(selfie.restore_metadata).result()
    restored = read_photo(service, identity)
    assert restored['drive_id'] == first['drive_id'] and restored['status'] == 'active'
    assert restored['session_id'] == ''
    service.db.submit(selfie.restore_metadata).result()
    assert read_photo(service, identity)['status'] == 'active'


def test_safe_delete_wont_remove_document(store, tmp_path):
    document = tmp_path/'doc.pdf'
    document.write_bytes(b'document')
    import hashlib
    store.put_file('document', document, 'doc.pdf', hashlib.sha256(b'document').hexdigest(), {'kind':'original'})
    with pytest.raises(StoreUnavailable):
        store.delete_selfie('document')
    assert (store.root/'files'/'document').exists()


def test_existing_video_ticket_stops_at_selfie_expiry(client, service):
    admin = login(client, ADMIN)
    enroll(client, admin)
    post, item = ready_post(client, admin, service)
    directory = service.config.data_dir/'derived'/item
    (directory/'video.mp4').write_bytes(b'synthetic video bytes'*50)
    service.db.submit(lambda db: db.execute("UPDATE items SET kind='video' WHERE id=?", (item,))).result()
    reader = login(client)
    identity = enroll(client, reader)
    view = open_view(client, reader, item)
    prepared = client.post(f"/api/views/{view['id']}/video", headers=reader).json()
    assert prepared['selfie_id'] == identity
    assert client.get(prepared['path'], headers={'Range': 'bytes=0-31'}).status_code == 206
    service.db.submit(lambda db: db.execute('UPDATE selfies SET expires_at=? WHERE id=?', (time.time()-1, identity))).result()
    assert client.get(prepared['path'], headers={'Range': 'bytes=0-31'}).json()['error'] == 'selfie_required'


def test_reader_revoked_while_private_photo_is_loading(client, service, monkeypatch):
    reader = login(client)
    identity = enroll(client, reader)
    original = selfie.local_photo
    async def revoke(*args):
        path = await original(*args)
        await service.write(lambda db: db.execute('UPDATE sessions SET revoked=1'))
        return path
    monkeypatch.setattr(selfie, 'local_photo', revoke)
    assert client.get(f'/api/selfies/{identity}.jpg', headers=reader).status_code == 403


def test_purged_photo_cannot_be_restored_by_a_stale_reader(client, service, monkeypatch):
    headers = login(client)
    identity = enroll(client, headers)
    stale = read_photo(service, identity)
    service.db.submit(lambda db: db.execute("UPDATE selfies SET status='purged' WHERE id=?", (identity,))).result()
    (service.config.data_dir/'selfies'/(identity+'.jpg')).unlink()
    def unexpected(*args):
        raise AssertionError('A purged selfie must not be downloaded')
    monkeypatch.setattr(service.store, 'download', unexpected)
    from postern.auth import Refusal
    with pytest.raises(Refusal) as error:
        client.portal.call(selfie.local_photo, service, stale)
    assert error.value.status == 404


def test_cloud_id_allocation_failure_is_recoverable(client, service, monkeypatch):
    def offline():
        raise RuntimeError('unreachable storage')
    monkeypatch.setattr(service.store, 'new_id', offline)
    headers = login(client)
    for _ in range(selfie.CAPTURES_PER_DAY + 1):
        result = capture(client, headers)
        assert result.status_code == 503 and result.json()['error'] == 'selfie_unavailable'
    assert not list((service.config.data_dir/'selfies').iterdir())
    assert not service.db.read(lambda db: list(db.execute('SELECT * FROM selfies')))
    # Every attempt was given back as a server fault, so none counts toward either daily limit.
    assert [json.loads(row['payload'])['detail']['fault'] for row in events(service, 'selfie_capture_released')] == ['server'] * (selfie.CAPTURES_PER_DAY + 1)


def failing(code):
    """A converter that refuses every photo the way the face check or the host would."""
    from postern.derive import DeriveCapacity, SelfieFaceError
    def run(*args):
        if code == 'storage':
            raise OSError(5, 'Input/output error')
        raise DeriveCapacity('Not enough free disk space') if code == 'capacity' else SelfieFaceError(code)
    return run


def converted(source, destination, kind):
    """A converter that accepts every photo at once, as a real quarter-second selfie job nearly does."""
    Path(destination).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).resolve().parents[1]/'fixtures/selfie-face.jpg', Path(destination)/'page-0.jpg')
    return {'pages': 1, 'bytes': 1}


def settled(arrivals, timeout=20, quiet_for=1):
    """Wait until arrivals stop changing for quiet_for seconds (or timeout), then require at least one."""
    deadline, seen, quiet = time.monotonic() + timeout, -1, time.monotonic()
    while time.monotonic() < deadline and (not arrivals or time.monotonic() - quiet < quiet_for):
        if len(arrivals) != seen:
            seen, quiet = len(arrivals), time.monotonic()
        # gstack-timing-ok: a poll with a deadline; the assertion is on completion within a bound, not on elapsed time
        time.sleep(.05)
    assert arrivals


def crowd(client, service, store, size=40, with_reader=True):
    """size synthetic staff signed in (more than any default worker pool has threads: Python caps it at 32),
    and an enrolled reader to ask on their behalf."""
    emails = [f'staff{n}@example.invalid' for n in range(size)]
    store.write_allowlist([['email', 'name', 'role'], [READER, 'Demo reader', 'reader'],
                           *[[email, 'Synthetic staff', 'reader'] for email in emails]])
    client.portal.call(service.refresh)
    reader = login(client) if with_reader else None
    if with_reader:
        enroll(client, reader)
    return [login(client, email) for email in emails], reader


# Value: protects=a person whose face the check keeps missing is not shut out for a day after ten retakes, while a good
#   photo then still goes through; fails_when=face rejections count against CAPTURES_PER_DAY again; why_new=every
#   rejection used to spend one of the ten daily attempts; seam=none
def test_face_rejections_do_not_use_up_the_daily_captures(client, service, monkeypatch):
    headers = login(client)
    real = service.deriver.run
    monkeypatch.setattr(service.deriver, 'run', failing('face_too_small'))
    for _ in range(selfie.CAPTURES_PER_DAY):
        assert capture(client, headers).json()['error'] == 'face_too_small'
    monkeypatch.setattr(service.deriver, 'run', real)
    assert capture(client, headers).status_code == 202
    assert len(events(service, 'selfie_capture_released')) == selfie.CAPTURES_PER_DAY


# Value: protects=face rejections still have a daily ceiling, so one account cannot run the converter without end;
#   fails_when=released attempts stop counting toward ATTEMPTS_PER_DAY; why_new=the release is new; seam=none
def test_face_rejections_are_still_bounded_per_day(client, service, monkeypatch):
    headers = login(client)
    monkeypatch.setattr(service.deriver, 'run', failing('face_required'))
    codes = [capture(client, headers).status_code for _ in range(selfie.ATTEMPTS_PER_DAY + 1)]
    assert codes == [422] * selfie.ATTEMPTS_PER_DAY + [429]


# Value: protects=every face refusal the converter can report gives the attempt back as a photo fault, with its own
#   code in the release; fails_when=a FACE_REFUSALS entry or the RETAKE fallback stops releasing, or releases as a
#   server fault; why_new=only face_too_small's release was asserted; seam=none
@pytest.mark.parametrize('code', [code for code, (status, _) in selfie.FACE_REFUSALS.items() if status == 422] + ['face_blurry'])
def test_each_face_refusal_gives_the_attempt_back_as_a_photo_fault(client, service, monkeypatch, code):
    headers = login(client)
    monkeypatch.setattr(service.deriver, 'run', failing(code))
    result = capture(client, headers)
    assert (result.status_code, result.json()['error']) == (422, code)
    released, = events(service, 'selfie_capture_released')
    assert json.loads(released['payload'])['detail'] == {'reason': code, 'fault': 'photo'}
    # The release carries the attempt's timestamp, so the pair leaves the daily window together.
    attempt, = events(service, 'selfie_capture_attempt')
    assert released['ts'] == attempt['ts']
    assert not list((service.config.data_dir/'selfies').iterdir())


# Value: protects=a release only cancels an attempt while that attempt is still in the window; fails_when=releases
#   are counted without the ts window (attempts - released goes negative and admits captures); why_new=the expiry
#   test ages attempts and releases together; seam=none
def test_a_release_outside_the_window_cancels_nothing(client, service, monkeypatch):
    headers = login(client)
    monkeypatch.setattr(service.deriver, 'run', failing('face_required'))
    for _ in range(selfie.CAPTURES_PER_DAY):
        assert capture(client, headers).status_code == 422
    service.db.submit(lambda db: db.execute("UPDATE events SET ts=ts-86401 WHERE type='selfie_capture_released'")).result()
    assert capture(client, headers).status_code == 429


# Value: protects=the daily selfie limits lift after 24 h, as the 429 promises; fails_when=the reservation query loses
#   or misapplies its ts window for attempts or releases; why_new=no test aged the budget events; seam=none
def test_the_daily_selfie_limits_lift_after_a_day(client, service, monkeypatch):
    headers = login(client)
    real = service.deriver.run
    monkeypatch.setattr(service.deriver, 'run', failing('face_required'))
    assert [capture(client, headers).status_code for _ in range(selfie.ATTEMPTS_PER_DAY + 1)][-1] == 429
    service.db.submit(lambda db: db.execute(
        "UPDATE events SET ts=ts-86401 WHERE type IN ('selfie_capture_attempt','selfie_capture_released')")).result()
    monkeypatch.setattr(service.deriver, 'run', real)
    assert capture(client, headers).status_code == 202


# Value: protects=a detector outage, a full disk or a storage fault never spends anyone's attempts; fails_when=server
#   faults are not released (the storage branch included), or a full disk is reported as a bad photo; why_new=an outage
#   used to burn everyone's budget; seam=none; no_claim=Drive allocation failures as such (a local I/O error stands in;
#   test_cloud_id_allocation_failure_is_recoverable covers their 503)
@pytest.mark.parametrize('fault, error', [('face_check_unavailable', 'face_check_unavailable'), ('capacity', 'selfie_unavailable'),
                                          ('storage', 'selfie_unavailable')])
def test_server_faults_never_use_up_selfie_attempts(client, service, monkeypatch, fault, error):
    headers = login(client)
    monkeypatch.setattr(service.deriver, 'run', failing(fault))
    for _ in range(selfie.ATTEMPTS_PER_DAY + 2):
        result = capture(client, headers)
        assert (result.status_code, result.json()['error']) == (503, error)


# Value: protects=an outage never hands one account the converter without end: everything reserved in a day meets a
#   ceiling whatever the outcome; fails_when=server-fault releases are refunded with no cap; why_new=the fault tests
#   stop at 32 attempts; seam=none
def test_server_faults_still_meet_a_hard_daily_ceiling(client, service, monkeypatch):
    headers = login(client)
    monkeypatch.setattr(service.deriver, 'run', failing('storage'))
    responses = [capture(client, headers) for _ in range(selfie.RESERVATIONS_PER_DAY + 1)]
    assert [response.status_code for response in responses] == [503] * selfie.RESERVATIONS_PER_DAY + [429]
    # The day's trouble was the service's: the refusal must not blame the person.
    assert responses[-1].json()['message'].startswith('The selfie service had repeated trouble today')


# Value: protects=the person still gets the face-check guidance when the release of their attempt cannot be recorded;
#   fails_when=release() lets a failed audit write escape, turning a retake prompt into an audit outage; why_new=every
#   release test records its event, none fails that write; seam=none; no_claim=a timed-out audit write (a refused one
#   is injected at the event write)
def test_a_face_refusal_is_still_shown_when_its_release_cannot_be_recorded(client, service, monkeypatch):
    from postern.db import AuditUnavailable
    headers = login(client)
    monkeypatch.setattr(service.deriver, 'run', failing('face_too_small'))
    original = selfie.event
    def unrecorded(db, kind, *args, **kwargs):
        if kind == 'selfie_capture_released':
            raise AuditUnavailable('Local audit storage is unavailable')
        return original(db, kind, *args, **kwargs)
    monkeypatch.setattr(selfie, 'event', unrecorded)
    result = capture(client, headers)
    assert (result.status_code, result.json()) == (422, {'error': 'face_too_small', 'message': selfie.FACE_REFUSALS['face_too_small'][1]})
    # Unrecorded, the attempt keeps counting.
    assert events(service, 'selfie_capture_released') == []


# Value: protects=two people's selfies prepare at the same time, up to the converter's slots; fails_when=capture holds
#   one lock for everyone again; why_new=a global lock serialized every face check during a burst; seam=none
def test_different_people_prepare_selfies_at_the_same_time(client, service, monkeypatch):
    first, second = login(client), login(client, ADMIN)
    together, real = threading.Barrier(2, timeout=5), service.deriver.run
    overlapped = []
    def meet(*args):
        try:
            together.wait()
            overlapped.append(True)
        except threading.BrokenBarrierError:
            overlapped.append(False)
        return real(*args)
    monkeypatch.setattr(service.deriver, 'run', meet)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda headers: capture(client, headers), [first, second]))
    assert [result.status_code for result in results] == [202, 202]
    assert overlapped == [True, True]


# Value: protects=one person can enroll from a second browser while the first stays active and readable; fails_when=
#   the second browser's confirmation retires or blocks the first (the retire is scoped by browser, the lock by
#   account); why_new=no test enrolled one account on two browsers; seam=none
def test_one_account_enrolls_on_two_browsers_and_both_stay_active(client, service):
    first, second = login(client), login(client)
    photos = enroll(client, first), enroll(client, second)
    assert photos[0] != photos[1]
    assert {read_photo(service, identity)['status'] for identity in photos} == {'active'}
    assert client.get('/api/posts', headers=first).status_code == 200
    assert client.get('/api/posts', headers=second).status_code == 200


# Value: protects=a capture burst enters the durable writer a few at a time, so readers' own audit writes keep
#   their deadline; fails_when=capture's authorization, reservation, insert or release writes run outside the
#   admission bound; why_new=the admission test covers only the upload preflight; seam=none; no_claim=real writer
#   latency (a 50 ms delay stands in for a busy fsync queue)
@pytest.mark.parametrize('outcome, code', [('refused', 422), ('accepted', 202)])
def test_capture_writes_stay_within_the_admission_bound(client, service, store, monkeypatch, outcome, code):
    people, _ = crowd(client, service, store, size=24, with_reader=False)
    monkeypatch.setattr(service.deriver, 'run', failing('face_required') if outcome == 'refused' else converted)
    active, peak, original = 0, 0, service.execute
    async def slow(request, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.05)  # a busy durable writer
            return await original(request, *args, **kwargs)
        finally:
            active -= 1
    monkeypatch.setattr(service, 'execute', slow)
    with concurrent.futures.ThreadPoolExecutor(len(people)) as pool:
        codes = {result.status_code for result in pool.map(lambda headers: capture(client, headers), people)}
    assert codes == {code}
    assert peak <= 8, f'{peak} capture writes waited on the writer together'


# Value: protects=a reader whose portrait must be restored is not kept waiting by someone else's capture; fails_when=
#   capture takes the selfie lock again; why_new=restoration queued behind every face check; seam=none
def test_restoring_a_portrait_does_not_wait_for_another_persons_capture(client, service, monkeypatch):
    identity = enroll(client, login(client))
    (service.config.data_dir/'selfies'/(identity+'.jpg')).unlink()
    inside, release, real = threading.Event(), threading.Event(), service.deriver.run
    def held(*args):
        inside.set()
        release.wait(10)
        return real(*args)
    monkeypatch.setattr(service.deriver, 'run', held)
    async def restore():
        return await asyncio.wait_for(selfie.local_photo(service, read_photo(service, identity)), 5)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        pending = pool.submit(capture, client, login(client, ADMIN))
        assert inside.wait(5)
        try:
            assert client.portal.call(restore).is_file()
        finally:
            release.set()
        assert pending.result(10).status_code == 202


# Value: protects=staff keep reading while a crowd enrolls at once and the converter is busy; fails_when=every capture
#   waiting for a converter slot holds one of the server's shared worker threads, starving readers; why_new=the capture
#   concurrency tests use one or two people, fewer than the worker pool; seam=none; no_claim=real converter timing (a
#   held fake converter stands in for slots taken by long document conversions)
def test_readers_do_not_wait_behind_a_crowd_of_enrollments(client, service, store, monkeypatch):
    from postern.derive import SelfieFaceError
    people, reader = crowd(client, service, store)
    waiting, release = [], threading.Event()
    def busy(*args):
        waiting.append(True)
        release.wait(30)
        raise SelfieFaceError('face_required')
    monkeypatch.setattr(service.deriver, 'run', busy)
    with concurrent.futures.ThreadPoolExecutor(len(people) + 1) as pool:
        captures = [pool.submit(capture, client, headers) for headers in people]
        try:
            # Let everyone who can reach the converter get there before the reader asks.
            settled(waiting)
            assert len(waiting) == service.config.converter_workers
            reading = pool.submit(client.get, '/api/posts', headers=reader)
            done, _ = concurrent.futures.wait([reading], timeout=5)
            assert done, f'the reader waited behind {len(waiting)} enrollments holding worker threads'
            assert reading.result().status_code == 200
        finally:
            release.set()
        assert [future.result(30).status_code for future in captures] == [422] * len(people)


# Value: protects=staff keep reading while many captures wait on private storage; fails_when=each capture's id
#   allocation holds a worker thread with no bound; why_new=the crowd test stops at the converter; seam=none;
#   no_claim=real Drive latency (a held fake allocation stands in for a slow generateIds)
def test_readers_do_not_wait_behind_slow_cloud_id_allocation(client, service, store, monkeypatch):
    people, reader = crowd(client, service, store)
    monkeypatch.setattr(service.deriver, 'run', converted)
    allocating, release, real = [], threading.Event(), store.new_id
    def stalled():
        allocating.append(True)
        release.wait(30)
        return real()
    monkeypatch.setattr(store, 'new_id', stalled)
    with concurrent.futures.ThreadPoolExecutor(len(people) + 1) as pool:
        captures = [pool.submit(capture, client, headers) for headers in people]
        try:
            settled(allocating)
            reading = pool.submit(client.get, '/api/posts', headers=reader)
            done, _ = concurrent.futures.wait([reading], timeout=5)
            assert done, f'the reader waited behind {len(allocating)} captures allocating cloud ids'
            assert reading.result().status_code == 200
        finally:
            release.set()
        assert {future.result(30).status_code for future in captures} == {202}


# Value: protects=staff keep reading while many attachments wait for the converter; fails_when=derive_item waits for a
#   converter slot inside a worker thread again; why_new=the crowd test drives only selfie captures; seam=none;
#   no_claim=real conversion timing (a held fake converter stands in for long document jobs)
def test_readers_do_not_wait_behind_queued_document_conversions(client, service, monkeypatch):
    from postern.derive import DeriveError
    admin, reader = login(client, ADMIN), login(client)
    enroll(client, reader)
    post = create_post(client, admin)
    waiting, release = [], threading.Event()
    def busy(*args):
        waiting.append(True)
        release.wait(30)
        raise DeriveError('held')
    monkeypatch.setattr(service.deriver, 'run', busy)
    # More attachments than any default worker pool has threads (Python caps it at 32).
    identities = [str(uuid.uuid4()) for _ in range(40)]
    for identity in identities:
        (service.config.data_dir/'staging'/(identity+'.pdf')).write_bytes(b'%PDF-1.4')
    service.db.submit(lambda db: db.executemany(
        "INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) VALUES (?,?,'policy.pdf','pdf','',8,0,'queued',0)",
        [(identity, post['id']) for identity in identities])).result()
    async def schedule():
        for identity in identities:
            service.schedule_derive(identity)
    async def settle():
        await asyncio.gather(*list(service.derive_tasks.values()), return_exceptions=True)
    client.portal.call(schedule)
    try:
        settled(waiting)
        assert len(waiting) == service.config.converter_workers
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            reading = pool.submit(client.get, '/api/posts', headers=reader)
            done, _ = concurrent.futures.wait([reading], timeout=5)
            assert done, f'the reader waited behind {len(waiting)} conversions holding worker threads'
            assert reading.result().status_code == 200
    finally:
        release.set()
        client.portal.call(settle)


# Value: protects=uploads the converter cannot prepare still spend one of the ten daily captures, as the runbook states;
#   fails_when=the invalid_selfie branch starts releasing attempts like a face refusal; why_new=only empty uploads were
#   limited, and those are refused before the converter runs; seam=none; no_claim=none beyond protects (real converter)
@pytest.mark.parametrize('body', [b'', b'not an image'])
def test_invalid_capture_attempts_are_durably_limited(client, service, body):
    headers = login(client)
    for _ in range(selfie.CAPTURES_PER_DAY):
        assert capture(client, headers, body).status_code == 422
    assert capture(client, headers, body).status_code == 429
    assert len(events(service, 'selfie_capture_attempt')) == selfie.CAPTURES_PER_DAY
    assert events(service, 'selfie_capture_released') == []


@pytest.mark.parametrize('confirmed_before_loss', [False, True])
def test_recovery_quarantines_ambiguous_cloud_photo_until_retention(client, service, store, confirmed_before_loss):
    headers = login(client)
    identity = capture(client, headers).json()['pending_id']
    row = read_photo(service, identity)
    path = service.config.data_dir/'selfies'/(identity+'.jpg')
    store.put_file(row['drive_id'], path, 'photo.jpg', row['sha256'], {'kind':'selfie'})
    if confirmed_before_loss:
        client.portal.call(selfie.process, service)
        assert read_photo(service, identity)['status'] == 'active'
        # The confirmation may be in a missing batch or an unexported tail.
        service.db.submit(lambda db: db.execute("DELETE FROM events WHERE type='selfie_enrolled'")).result()
    service.db.submit(lambda db: db.execute('DELETE FROM selfies')).result()
    service.db.submit(selfie.restore_metadata).result()
    restored = read_photo(service, identity)
    assert restored['status'] == 'quarantined'
    assert restored['retired_at'] == row['expires_at']
    assert not selfie.readable(restored, service.config)
    service.db.submit(selfie.restore_metadata).result()
    client.portal.call(selfie.process, service)
    assert read_photo(service, identity)['status'] == 'quarantined'
    assert (store.root/'files'/row['drive_id']).exists()
    assert client.get('/api/posts', headers=headers).json()['error'] == 'selfie_required'
    service.db.submit(lambda db: db.execute('UPDATE selfies SET retired_at=? WHERE id=?',
        (time.time()-service.config.selfie_retention_days*86400-1, identity))).result()
    client.portal.call(selfie.process, service)
    assert read_photo(service, identity)['status'] == 'purged'
    assert not (store.root/'files'/row['drive_id']).exists()


def test_failed_purge_batch_backs_off_without_blocking_newer_photos(client, service, store, monkeypatch):
    identity = enroll(client, login(client))
    original = read_photo(service, identity)
    now = time.time()
    service.db.submit(lambda db: db.execute("UPDATE selfies SET status='retired',retired_at=?,captured_at=? WHERE id=?",
        (now-91*86400, now, identity))).result()
    poisoned = []
    for index in range(10):
        bad_id, drive_id = str(uuid.uuid4()), str(uuid.uuid4())
        poisoned.append(drive_id)
        store.put_file(drive_id, service.config.data_dir/'selfies'/(identity+'.jpg'), 'photo.jpg', original['sha256'], {'kind':'selfie'})
        service.db.submit(lambda db, i=bad_id, d=drive_id, n=index: db.execute(
            'INSERT INTO selfies(id,email,device_hash,session_id,captured_at,expires_at,retired_at,status,drive_id,sha256,consent_version,context) '
            "SELECT ?,email,device_hash,session_id,?,expires_at,retired_at,'retired',?,sha256,consent_version,context FROM selfies WHERE id=?",
            (i, now-100+n, d, identity))).result()
    remove = store.delete_selfie
    attempts = []
    def fail_poisoned(drive_id):
        attempts.append(drive_id)
        if drive_id in poisoned:
            raise StoreUnavailable('Remote deletion unavailable')
        remove(drive_id)
    monkeypatch.setattr(store, 'delete_selfie', fail_poisoned)
    client.portal.call(selfie.purge, service)
    assert len(attempts) == 10
    retries = service.db.read(lambda db: list(db.execute("SELECT next_try,attempts FROM selfies WHERE id!=?", (identity,))))
    assert all(r['next_try'] > now and r['attempts'] == 1 for r in retries)
    client.portal.call(selfie.purge, service)
    assert read_photo(service, identity)['status'] == 'purged'
    assert not (store.root/'files'/original['drive_id']).exists()
    assert len(attempts) == 11
    client.portal.call(selfie.purge, service)
    assert len(attempts) == 11
    assert '10 expired' in service.selfie_cleanup_error and 'oldest deadline' in service.selfie_cleanup_error
    # American dates, as everywhere else people read them (e.g. "Oct 24, 2026, 2:05 PM").
    zone = re.escape(time.strftime('%Z', time.localtime(now - 86400)))  # e.g. EDT or Bangkok's +07
    assert re.search(r'oldest deadline [A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d{2} [AP]M ' + zone + r'\.', service.selfie_cleanup_error)
    monkeypatch.setattr(store, 'delete_selfie', remove)
    service.db.submit(lambda db: db.execute('UPDATE selfies SET next_try=0')).result()
    client.portal.call(selfie.purge, service)
    assert all(not (store.root/'files'/drive_id).exists() for drive_id in poisoned)
    assert service.selfie_cleanup_error is None


def test_upgrade_requests_fresh_signin_before_camera(client, service):
    headers = login(client)
    # A production session created by 0.3.1 has no browser binding.
    service.db.submit(lambda db: db.execute('DELETE FROM session_devices')).result()
    response = client.get('/api/session', headers=headers)
    assert response.status_code == 401 and response.json()['error'] == 'sign_in_required'
    response = capture(client, headers)
    assert response.status_code == 401 and response.json()['error'] == 'sign_in_required'
    replacement = login(client)
    assert client.get('/api/session', headers=replacement).json()['selfie']['required'] is True
    enroll(client, replacement)
    assert client.get('/api/posts', headers=replacement).status_code == 200


def test_late_audit_commit_keeps_pending_photo(client, service, monkeypatch):
    headers = login(client)
    original = selfie.event
    def slow_event(db, kind, *args, **kwargs):
        result = original(db, kind, *args, **kwargs)
        if kind == 'selfie_consent_accepted':
            time.sleep(1.7)
        return result
    monkeypatch.setattr(selfie, 'event', slow_event)
    result = capture(client, headers)
    assert result.status_code == 503 and result.json()['error'] == 'audit_unavailable'
    # A queued writer query is a barrier after the late commit.
    row = service.db.submit(lambda db: dict(db.execute("SELECT * FROM selfies WHERE status='pending'").fetchone())).result(timeout=5)
    assert (service.config.data_dir/'selfies'/(row['id']+'.jpg')).is_file()
    client.portal.call(selfie.process, service)
    assert read_photo(service, row['id'])['status'] == 'active'
    assert client.get('/api/posts', headers=headers).status_code == 200


def test_lost_pending_file_allows_retaking_and_unreferenced_files_expire(client, service):
    headers = login(client)
    identity = capture(client, headers).json()['pending_id']
    (service.config.data_dir/'selfies'/(identity+'.jpg')).unlink()
    orphan = service.config.data_dir/'selfies'/(str(uuid.uuid4())+'.jpg')
    orphan.write_bytes(photo())
    import os
    os.utime(orphan, (time.time()-7200, time.time()-7200))
    client.portal.call(selfie.process, service)
    assert read_photo(service, identity)['status'] == 'purged'
    assert not orphan.exists()
    enroll(client, headers)


def test_cached_portrait_does_not_wait_for_another_cloud_upload(client, service):
    headers = login(client)
    identity = enroll(client, headers)
    row = read_photo(service, identity)
    async def read_while_upload_locked():
        async with service.selfie_lock:
            return await asyncio.wait_for(selfie.local_photo(service, row), timeout=.5)
    assert client.portal.call(read_while_upload_locked).is_file()


@pytest.mark.parametrize('failure', [StoreUnavailable('checksum mismatch'), OSError('disk failure'), RuntimeError('cloud transport')])
def test_private_photo_restore_failure_is_typed_and_leaves_no_partial(client, service, store, monkeypatch, failure):
    headers = login(client)
    identity = enroll(client, headers)
    (service.config.data_dir/'selfies'/(identity+'.jpg')).unlink()
    def unavailable(_identity, target, *_args):
        target.write_bytes(b'partial')
        raise failure
    monkeypatch.setattr(store, 'download', unavailable)
    response = client.get(f'/api/selfies/{identity}.jpg', headers=headers)
    assert response.status_code == 503
    assert response.json()['error'] == 'selfie_unavailable'
    assert not list((service.config.data_dir/'selfies').iterdir())


def test_pending_capture_is_visible_after_same_browser_signin(client, service):
    device = str(uuid.uuid4())
    original = login(client, device=device)
    identity = capture(client, original).json()['pending_id']
    replacement = login(client, device=device)
    assert client.get('/api/session', headers=replacement).json()['selfie']['pending_id'] == identity
    assert client.get('/api/selfie', headers=login(client)).json()['pending_id'] is None
    client.post('/api/auth/signout', headers=original)
    client.portal.call(selfie.process, service)
    assert client.get('/api/selfie', headers=replacement).json()['pending_id'] is None
    enroll(client, replacement)


def test_admin_can_manage_without_selfie_but_cannot_read_training(client, service):
    admin = login(client, ADMIN)
    post, item = ready_post(client, admin, service)
    assert client.get('/api/session', headers=admin).json()['selfie']['required']
    for path in ('/api/admin/posts', '/api/admin/progress', f"/api/admin/posts/{post['id']}/report"):
        assert client.get(path, headers=admin).status_code == 200
    for method, path, body in (
        ('GET', '/api/posts', None),
        ('POST', f'/api/items/{item}/views', {'attempt_id': str(uuid.uuid4())}),
        ('GET', f"/api/posts/{post['id']}/thumb.jpg", None),
        ('GET', '/api/views/missing/page/0.jpg', None),
        ('POST', '/api/views/missing/video', None),
    ):
        response = client.request(method, path, headers=admin, json=body)
        assert response.status_code == 403 and response.json()['error'] == 'selfie_required', (path, response.text)
    assert not events(service, 'selfie_enrolled')
    assert not events(service, 'item_open')
    reader = login(client)
    assert client.get('/api/admin/posts', headers=reader).json()['error'] == 'access_denied'
    unagreed = login(client, ADMIN, consent=False)
    assert client.get('/api/admin/posts', headers=unagreed).json()['error'] == 'consent_required'
    service.db.submit(lambda db: db.execute("UPDATE members SET role='reader' WHERE email=?", (ADMIN,))).result()
    assert client.get('/api/admin/progress', headers=admin).json()['error'] == 'access_denied'


def test_migrated_portrait_recovery_preserves_sealed_events(client, service):
    identity = enroll(client, login(client))
    before = read_photo(service, identity)
    sealed_payloads = service.db.read(lambda db: [r[0] for r in db.execute('SELECT payload FROM events ORDER BY seq')])
    service.db.submit(lambda db: db.execute('DELETE FROM selfies')).result()
    def restore(db):
        selfie.restore_metadata(db)
        selfie.relocate_restored_metadata(db, {before['drive_id']: {'id': 'new-private-drive-object', 'sha256': before['sha256']}})
    service.db.submit(restore).result()
    assert read_photo(service, identity)['drive_id'] == 'new-private-drive-object'
    assert read_photo(service, identity)['status'] == 'active'
    assert service.db.read(lambda db: [r[0] for r in db.execute('SELECT payload FROM events ORDER BY seq')]) == sealed_payloads


def test_migrated_portrait_recovery_rejects_different_bytes(client, service):
    identity = enroll(client, login(client))
    before = read_photo(service, identity)
    with pytest.raises(StoreUnavailable, match='checksum differs'):
        service.db.submit(lambda db: selfie.relocate_restored_metadata(db,
            {before['drive_id']: {'id': 'different-object', 'sha256': '0' * 64}})).result()
    assert read_photo(service, identity)['drive_id'] == before['drive_id']
