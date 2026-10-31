"""Opt-in enrollment: pending -> active -> retired -> purged.

Only a sanitized, cloud-confirmed photograph unlocks reading. A random browser
identifier is a convenience binding, not hardware attestation or face matching.
"""
import asyncio
import contextlib
import hashlib
import json
import shutil
import time
import uuid

from .auth import Refusal, token_hash
from .db import event, rows
from .derive import DeriveCapacity, DeriveError, DeriveFault, SelfieFaceError
from .storage import StoreUnavailable

LIMIT = 1024 * 1024
LIFETIME = 30 * 86400
# Kept captures, and photos that could not be prepared, count against CAPTURES_PER_DAY. A face the check did
# not find is fixed by retaking, so it counts only against ATTEMPTS_PER_DAY, which still bounds the converter
# work one account can cause. A server fault (the detector, private storage or disk) counts against neither,
# up to RESERVATIONS_PER_DAY on everything reserved in a day: an outage never hands one account the converter
# without end.
CAPTURES_PER_DAY = 10
ATTEMPTS_PER_DAY = 30
RESERVATIONS_PER_DAY = 90
UNAVAILABLE = 'Your selfie could not be saved yet. Please try again. / ยังบันทึกเซลฟีของคุณไม่ได้ กรุณาลองใหม่อีกครั้ง'
# What to tell the person for each face-check outcome the converter reports (derive_child.FACE_FAILURES).
FACE_REFUSALS = {
    'face_required': (422, 'No clear face was found. Retake your selfie facing the camera in good light. / ไม่พบใบหน้าที่ชัดเจน กรุณาถ่ายใหม่โดยหันหน้าเข้ากล้องในที่สว่าง'),
    'multiple_faces': (422, 'More than one face was found. Retake your selfie with only you in the frame. / พบมากกว่าหนึ่งใบหน้า กรุณาถ่ายใหม่โดยให้มีเพียงคุณในภาพ'),
    'face_too_small': (422, 'Move closer so your face fills more of the frame, then retake your selfie. / กรุณาขยับเข้าใกล้กล้องให้เห็นใบหน้าชัดขึ้น แล้วถ่ายใหม่'),
    'face_out_of_frame': (422, 'Keep your whole face inside the frame, then retake your selfie. / กรุณาให้ใบหน้าทั้งหมดอยู่ในกรอบ แล้วถ่ายใหม่'),
    'face_check_unavailable': (503, 'The face check is temporarily unavailable. Please try again shortly. / ระบบตรวจจับใบหน้าไม่พร้อมใช้งานชั่วคราว กรุณาลองใหม่อีกครั้ง'),
}
# An outcome added to the converter before it gets its own message still asks for a retake, never a 500.
RETAKE = (422, 'The face check did not accept this photo. Retake your selfie facing the camera in good light. / ระบบตรวจใบหน้าไม่ผ่าน กรุณาถ่ายเซลฟีใหม่โดยหันหน้าเข้ากล้องในที่สว่าง')


def device_hash(value):
    try:
        return token_hash(str(uuid.UUID(value)))
    except (ValueError, TypeError, AttributeError):
        raise Refusal(422, 'browser_required', 'Please allow this browser to save its sign-in information, then try again.')


def browser(db, context):
    row = db.execute('SELECT device_hash FROM session_devices WHERE session_id=?', (context['id'],)).fetchone()
    return row[0] if row else ''


def enrollment(db, context, version):
    return db.execute("SELECT * FROM selfies WHERE email=? AND device_hash=? AND status='active' "
                      'AND expires_at>? AND consent_version=? ORDER BY captured_at DESC LIMIT 1',
                      (context['email'], browser(db, context), time.time(), version)).fetchone()


def require_enrollment(db, context, version):
    row = enrollment(db, context, version)
    if not row:
        raise Refusal(403, 'selfie_required', 'Please take a new enrollment selfie before continuing.')
    return row


def status(db, context, config):
    if not config.selfie_enabled:
        return {'enabled': False, 'required': False}
    current = enrollment(db, context, config.selfie_notice_version)
    pending = db.execute("SELECT id,error FROM selfies WHERE email=? AND device_hash=? "
                         "AND status='pending' ORDER BY captured_at DESC LIMIT 1",
                         (context['email'], browser(db, context))).fetchone()
    return {'enabled': True, 'required': current is None,
            'id': current['id'] if current else None,
            'expires_at': current['expires_at'] if current else None,
            'pending_id': pending['id'] if pending else None,
            'error': pending['error'] if pending else None}


def enabled(service):
    if not service.config.selfie_enabled:
        raise Refusal(404, 'not_found', 'Selfie enrollment is not enabled.')


def settle_reservation(db, identity, outcome, reason=None):
    """Settle one reservation and its refund atomically; replay never refunds twice."""
    row = db.execute('SELECT * FROM selfie_reservations WHERE id=? AND outcome IS NULL', (identity,)).fetchone()
    if not row:
        return
    release_id = None
    if outcome in ('photo', 'server'):
        release_id = event(db, 'selfie_capture_released', json.loads(row['context']), summary=False,
                           detail={'reason': reason, 'fault': outcome}, timestamp=row['ts'])
    db.execute('UPDATE selfie_reservations SET outcome=?,release_id=? WHERE id=? AND outcome IS NULL',
               (outcome, release_id, identity))


def reconcile_reservations(db, email):
    """Caller holds this account's capture lock: an unfinished capture has stopped.

    A photo insert settles its reservation in the same transaction, even if its
    response times out. Anything still unfinished had an interrupted capture or
    settlement; classify that storage/request failure as a server fault.
    """
    for row in rows(db, 'SELECT id FROM selfie_reservations WHERE email=? AND outcome IS NULL', (email,)):
        settle_reservation(db, row['id'], 'server', 'interrupted')
    # Events remain the rolling-budget source, including legacy unpaired attempts.
    db.execute('DELETE FROM selfie_reservations WHERE email=? AND outcome IS NOT NULL AND ts<=?',
               (email, time.time()-86400))


async def finish_reservation(service, identity, outcome, reason=None):
    # Internal compensation uses the saved, authorized context, not a login that
    # might since have expired. Failure leaves a durable row for reconciliation.
    with contextlib.suppress(Exception):
        async with service.selfie_admissions:
            await service.write(lambda db: settle_reservation(db, identity, outcome, reason))


async def capture(service, request, upload, version):
    enabled(service)
    if version != service.config.selfie_notice_version:
        await upload.close()
        raise Refusal(409, 'notice_changed', 'Please review the current selfie notice. / กรุณาอ่านประกาศการใช้เซลฟีฉบับปัจจุบัน')
    # Bounded like the upload preflight: a burst's own writes enter the durable writer a few at a time, so
    # readers' audit writes keep their deadline.
    async with service.selfie_admissions:
        context = await service.execute(request, selfie=False, event_name=None)
    # One capture at a time per account, so simultaneous submits cannot both convert or both reserve (the writer
    # re-checks for a pending enrollment in this browser as it inserts). Other people's photos prepare in
    # parallel, bounded by the converter's slots, and readers restoring a portrait never wait behind someone's
    # face check.
    async with service.capture_locks[context['email']]:
        identity = str(uuid.uuid4())
        def check(db, ctx, reserve=False):
            binding = browser(db, ctx)
            if not binding:
                raise Refusal(401, 'sign_in_required', 'Please sign in again in this browser.')
            pending = db.execute("SELECT id FROM selfies WHERE email=? AND device_hash=? AND status='pending'",
                                 (ctx['email'], binding)).fetchone()
            if pending:
                raise Refusal(409, 'selfie_pending', 'Your previous selfie is still being saved. Please wait. / กำลังบันทึกเซลฟีก่อนหน้าของคุณ กรุณารอสักครู่')
            if reserve:
                reconcile_reservations(db, ctx['email'])
                attempts, released, faults = db.execute(
                    "SELECT coalesce(sum(type='selfie_capture_attempt'),0), coalesce(sum(type='selfie_capture_released'),0), "
                    "coalesce(sum(type='selfie_capture_released' AND CASE WHEN json_valid(payload) "
                    "THEN json_extract(payload,'$.detail.fault')='server' END),0) "
                    "FROM events WHERE email=? AND type IN ('selfie_capture_attempt','selfie_capture_released') AND ts>?",
                    (ctx['email'], time.time()-86400)).fetchone()
                if attempts - released >= CAPTURES_PER_DAY or attempts - faults >= ATTEMPTS_PER_DAY:
                    raise Refusal(429, 'rate_limited', 'Too many selfie attempts today. Please try again tomorrow. / '
                                  'วันนี้ถ่ายเซลฟีหลายครั้งแล้ว กรุณาลองใหม่พรุ่งนี้')
                if attempts >= RESERVATIONS_PER_DAY:
                    # The day's trouble was the service's, not the person's photos: the refusal must not blame them.
                    raise Refusal(429, 'rate_limited', 'The selfie service had repeated trouble today. Please try again tomorrow. / '
                                  'ระบบเซลฟีขัดข้องหลายครั้งในวันนี้ กรุณาลองใหม่พรุ่งนี้')
                attempt_id = event(db, 'selfie_capture_attempt', ctx, summary=False, timestamp=reserved_at)
                db.execute('INSERT INTO selfie_reservations(id,email,attempt_id,ts,context) VALUES (?,?,?,?,?)',
                           (identity, ctx['email'], attempt_id, reserved_at, json.dumps(ctx)))
            return binding
        reserved_at = time.time()
        try:
            async with service.selfie_admissions:
                binding = await service.execute(request, lambda db, ctx: check(db, ctx, True), selfie=False, event_name=None)
        except BaseException:
            # An already-running reservation can commit after this response. The
            # worker (or the next capture) reconciles it after this lock is free.
            service.selfie_wakeup.set()
            await upload.close()
            raise
        source = service.config.data_dir/'staging'/(identity+'.jpg')
        derived = service.config.data_dir/'staging'/('selfie-'+identity)
        target = service.config.data_dir/'selfies'/(identity+'.jpg')
        retain_target = False
        try:
            size = 0
            with source.open('xb') as output:
                source.chmod(0o600)
                while chunk := await upload.read(64 * 1024):
                    size += len(chunk)
                    if size > LIMIT:
                        raise Refusal(413, 'file_too_large', 'The selfie must be smaller than 1 MB. / ภาพเซลฟีต้องมีขนาดเล็กกว่า 1 MB')
                    await asyncio.to_thread(output.write, chunk)
            if not size:
                raise Refusal(422, 'invalid_selfie', 'Take a clear photo before continuing. / กรุณาถ่ายภาพให้ชัดเจนก่อนดำเนินการต่อ')
            await service.convert(source, derived, 'selfie')
            await asyncio.to_thread(shutil.copyfile, derived/'page-0.jpg', target)
            target.chmod(0o600)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            # A few at a time, waiting in the event loop: a slow Drive must not hold a worker thread per capture.
            try:
                async with service.cloud_allocations:
                    drive_id = await asyncio.to_thread(service.store.new_id)
            except Exception as error:
                raise StoreUnavailable('Private storage could not allocate a photo') from error
            now = time.time()
            def insert(db, ctx):
                check(db, ctx)
                db.execute('INSERT INTO selfies(id,email,device_hash,session_id,captured_at,expires_at,status,drive_id,sha256,consent_version,context) '
                           "VALUES (?,?,?,?,?,?,'pending',?,?,?,?)",
                           (identity, ctx['email'], binding, ctx['id'], now, now+LIFETIME,
                            drive_id, digest, version, json.dumps(context)))
                event(db, 'selfie_consent_accepted', ctx, detail={'version': version, 'selfie_id': identity,
                    'id': identity, 'device_hash': binding, 'captured_at': now, 'expires_at': now+LIFETIME,
                    'drive_id': drive_id, 'sha256': digest, 'consent_version': version})
                settle_reservation(db, identity, 'captured')
                return {'pending_id': identity}
            # A timed-out writer can still commit. Retain the prepared bytes
            # once dispatched; the worker reconciles any unreferenced file.
            retain_target = True
            try:
                async with service.selfie_admissions:
                    return await service.execute(request, insert, selfie=False, event_name=None)
            except Refusal as error:
                if error.code != 'audit_unavailable':
                    retain_target = False
                raise
        except SelfieFaceError as error:
            status, message = FACE_REFUSALS.get(error.code, RETAKE)
            await finish_reservation(service, identity, 'server' if status >= 500 else 'photo', error.code)
            raise Refusal(status, error.code, message)
        except DeriveFault as error:
            # The host or the converter failed, not the photo: given back, and never "take another photo".
            await finish_reservation(service, identity, 'server', 'capacity' if isinstance(error, DeriveCapacity) else 'converter')
            raise Refusal(503, 'selfie_unavailable', UNAVAILABLE)
        except DeriveError:
            await finish_reservation(service, identity, 'charged')
            raise Refusal(422, 'invalid_selfie', 'The photo could not be prepared safely. Take another clear photo. / '
                          'ไม่สามารถเตรียมรูปภาพนี้ได้อย่างปลอดภัย กรุณาถ่ายภาพใหม่ให้ชัดเจน')
        except (StoreUnavailable, OSError):
            await finish_reservation(service, identity, 'server', 'storage')
            raise Refusal(503, 'selfie_unavailable', UNAVAILABLE)
        except Refusal as error:
            if error.code != 'audit_unavailable':
                await finish_reservation(service, identity, 'charged')
            raise
        finally:
            service.selfie_wakeup.set()
            await upload.close()
            source.unlink(missing_ok=True)
            await asyncio.to_thread(shutil.rmtree, derived, True)
            if not retain_target:
                target.unlink(missing_ok=True)
            else:
                # Also wake after an ambiguous commit: the worker reconciles it.
                service.selfie_wakeup.set()


def authorize_pending(service, db, photo):
    session = db.execute('SELECT token_hash FROM sessions WHERE id=?', (photo['session_id'],)).fetchone()
    if not session:
        raise Refusal(403, 'access_denied', 'The enrollment session ended. / เซสชันยืนยันตัวตนสิ้นสุดแล้ว')
    context = service.permissions.authorize_content_request(db, '', token_digest=session[0], selfie=False)
    if photo['expires_at'] <= time.time():
        raise Refusal(403, 'selfie_required', 'Take a new selfie to continue.')
    if photo['consent_version'] != service.config.selfie_notice_version:
        raise Refusal(403, 'consent_required', 'The selfie notice changed.')
    return {**json.loads(photo['context']), **context}


async def process(service, *, maintenance=True):
    if not service.config.selfie_enabled:
        return
    unfinished = await asyncio.to_thread(service.db.read, lambda db: rows(db,
        'SELECT DISTINCT email FROM selfie_reservations WHERE outcome IS NULL'))
    for row in unfinished:
        lock = service.capture_locks[row['email']]
        if lock.locked():
            continue  # Never refund a live conversion or wait behind its upload.
        async with lock, service.selfie_admissions:
            await service.write(lambda db: reconcile_reservations(db, row['email']))
    async with service.selfie_lock:
        pending = await asyncio.to_thread(service.db.read, lambda db: rows(db,
            "SELECT * FROM selfies WHERE status='pending' AND next_try<=? ORDER BY captured_at LIMIT 1", (time.time(),)))
        if pending:
            photo = pending[0]
            try:
                await service.write(lambda db: authorize_pending(service, db, photo))
                path = service.config.data_dir/'selfies'/(photo['id']+'.jpg')
                if not path.is_file():
                    raise Refusal(403, 'selfie_required', 'Take a new selfie to continue.')
                await asyncio.to_thread(service.store.put_file, photo['drive_id'], path, photo['id']+'.jpg',
                                        photo['sha256'], {'kind': 'selfie', 'selfie_id': photo['id']})
                def confirm(db):
                    context = authorize_pending(service, db, photo)
                    now = time.time()
                    db.execute("UPDATE selfies SET status='retired',retired_at=?,attempts=0,next_try=0,error=NULL WHERE email=? AND device_hash=? AND status='active'",
                               (now, photo['email'], photo['device_hash']))
                    db.execute("UPDATE selfies SET status='active',error=NULL WHERE id=? AND status='pending'", (photo['id'],))
                    detail = {key: photo[key] for key in ('id', 'device_hash', 'captured_at', 'expires_at', 'drive_id', 'sha256', 'consent_version')}
                    event(db, 'selfie_enrolled', context, detail=detail)
                await service.write(confirm)
            except Refusal as error:
                if error.status in (401, 403):
                    await service.write(lambda db: db.execute("UPDATE selfies SET status='cancelled',retired_at=?,attempts=0,next_try=0,error=? WHERE id=?",
                        (time.time(), 'Sign in and take a new selfie to continue.', photo['id'])))
                else:
                    await delayed(service, photo)
            except Exception:
                await delayed(service, photo)
        if not maintenance:
            return bool(pending)
        await purge(service)
        # The writer transaction above settles before this query. After a crash
        # or an ambiguous failed insert, remove only old, unreferenced portraits.
        known = await service.write(lambda db: {row[0] for row in db.execute('SELECT id FROM selfies')})
        for path in (service.config.data_dir/'selfies').glob('*.jpg'):
            if path.stem not in known and path.stat().st_mtime < time.time()-3600:
                path.unlink(missing_ok=True)
        return bool(pending)


async def delayed(service, photo):
    await service.write(lambda db: db.execute('UPDATE selfies SET attempts=attempts+1,next_try=?,error=? WHERE id=?',
        (time.time()+min(60, 2**min(photo['attempts']+1, 6)), 'Private backup is delayed. We will keep retrying; you can leave this page open.', photo['id'])))


def readable(photo, config, now=None):
    now = time.time() if now is None else now
    retired = photo['retired_at'] if photo['retired_at'] is not None else photo['expires_at']
    return photo['status'] in ('active', 'retired') and now < retired + config.selfie_retention_days * 86400


async def purge(service):
    now = time.time()
    def retire(db):
        db.execute('DELETE FROM selfie_reservations WHERE outcome IS NOT NULL AND ts<=?', (now-86400,))
        for photo in rows(db, "SELECT id,email,expires_at FROM selfies WHERE status='active' AND (expires_at<=? OR (? AND email NOT IN (SELECT email FROM members)))", (now, service.permissions.fresh(db))):
            when = min(now, photo['expires_at'])
            db.execute("UPDATE selfies SET status='retired',retired_at=?,attempts=0,next_try=0,error=NULL WHERE id=?", (when, photo['id']))
            event(db, 'selfie_retired', email=photo['email'], detail={'selfie_id': photo['id'], 'retired_at': when})
    await service.write(retire)
    expired = await asyncio.to_thread(service.db.read, lambda db: rows(db,
        "SELECT * FROM selfies WHERE ((status IN ('retired','quarantined') AND retired_at<=?) OR status='cancelled') "
        "AND next_try<=? ORDER BY next_try,captured_at LIMIT 10",
        (now-service.config.selfie_retention_days*86400, now)))
    for photo in expired:
        # Access already fails by deadline, even while a remote deletion retries.
        try:
            await asyncio.to_thread(service.store.delete_selfie, photo['drive_id'])
            (service.config.data_dir/'selfies'/(photo['id']+'.jpg')).unlink(missing_ok=True)
            def record(db):
                db.execute("UPDATE selfies SET status='purged',context='{}',error=NULL WHERE id=?", (photo['id'],))
                event(db, 'selfie_purged', email=photo['email'], detail={'selfie_id': photo['id']})
            await service.write(record)
        except Exception:
            # A failed batch must not monopolize the next retention pass.
            await service.write(lambda db: db.execute(
                'UPDATE selfies SET attempts=attempts+1,next_try=?,error=? WHERE id=?',
                (time.time()+min(3600, 30*2**min(photo['attempts'], 7)),
                 'Private storage deletion is delayed.', photo['id'])))
    overdue = await asyncio.to_thread(service.db.read, lambda db: db.execute(
        "SELECT count(*),MIN(CASE WHEN status='cancelled' THEN retired_at ELSE retired_at+? END) "
        "FROM selfies WHERE (status IN ('retired','quarantined') AND retired_at<=?) OR status='cancelled'",
        (service.config.selfie_retention_days*86400, now-service.config.selfie_retention_days*86400)).fetchone())
    service.selfie_cleanup_error = (
        f'{overdue[0]} expired selfie photo(s) are waiting for deletion from private storage; '
        f'oldest deadline {time.strftime("%b %-d, %Y, %-I:%M %p %Z", time.localtime(overdue[1]))}. '
        'Access is already blocked; deletion will retry.' if overdue[0] else None)


async def local_photo(service, photo):
    path = service.config.data_dir/'selfies'/(photo['id']+'.jpg')
    # Cached portraits need no lock: regular readers must not queue behind the
    # selfie worker's private-storage upload or cleanup (captures hold their own
    # per-account lock). Access is rechecked by the caller before bytes are
    # served; only restoration can recreate files.
    if path.is_file() and readable(photo, service.config):
        return path
    # Coordinate restoration with the deletion worker so a download cannot
    # resurrect an already-purged local photograph.
    async with service.selfie_lock:
        current = await asyncio.to_thread(service.db.read, lambda db: db.execute(
            'SELECT * FROM selfies WHERE id=?', (photo['id'],)).fetchone())
        if not current or not readable(current, service.config):
            raise Refusal(404, 'selfie_unavailable', 'This selfie is no longer retained. / รูปเซลฟีนี้ถูกลบตามระยะเวลาเก็บรักษาแล้ว')
        path = service.config.data_dir/'selfies'/(photo['id']+'.jpg')
        if not path.is_file():
            temporary = path.with_name(photo['id']+'.'+str(uuid.uuid4())+'.part')
            try:
                await asyncio.to_thread(service.store.download, photo['drive_id'], temporary, photo['sha256'], 512*1024)
                if temporary.stat().st_size > 512 * 1024:
                    raise Refusal(409, 'selfie_unavailable', 'This selfie is unavailable. / ไม่สามารถเข้าถึงรูปเซลฟีนี้ได้')
                temporary.replace(path)
            except Refusal:
                raise
            except Exception as error:
                raise Refusal(503, 'selfie_unavailable', 'Your enrollment photo could not be loaded from private storage. Please try again shortly. / ไม่สามารถโหลดรูปยืนยันตัวตนจากที่เก็บข้อมูลส่วนตัวได้ กรุณาลองใหม่ในอีกสักครู่') from error
            finally:
                temporary.unlink(missing_ok=True)
        return path


async def image(service, request, identity):
    def authorize(db, ctx):
        enabled(service)
        photo = db.execute('SELECT * FROM selfies WHERE id=?', (identity,)).fetchone()
        if not photo or not readable(photo, service.config):
            raise Refusal(404, 'selfie_unavailable', 'This selfie is no longer retained. / รูปเซลฟีนี้ถูกลบตามระยะเวลาเก็บรักษาแล้ว')
        if ctx['role'] != 'admin' and photo['id'] != ctx.get('selfie_id'):
            raise Refusal(403, 'access_denied', 'This selfie is not available. / ไม่สามารถเข้าถึงรูปเซลฟีนี้ได้')
        return dict(photo)
    def viewed(db, ctx):
        photo = authorize(db, ctx)
        event(db, 'selfie_viewed', ctx, detail={'viewed_selfie_id': identity})
        return photo
    photo = await service.execute(request, viewed, event_name=None)
    path = await local_photo(service, photo)
    try:
        body = await asyncio.to_thread(path.read_bytes)
    except OSError as error:
        raise Refusal(503, 'selfie_unavailable', 'Your enrollment photo could not be loaded. Please try again shortly. / ไม่สามารถโหลดรูปยืนยันตัวตนได้ กรุณาลองใหม่ในอีกสักครู่') from error
    await service.execute(request, authorize, event_name=None)
    return body


async def history(service, request):
    def listing(db, ctx):
        enabled(service)
        return {'cleanup_error': service.selfie_cleanup_error,
                'selfies': [{k: r[k] for k in ('id', 'email', 'captured_at', 'expires_at', 'retired_at', 'status')}
                           for r in rows(db, "SELECT * FROM selfies WHERE status IN ('active','retired') ORDER BY captured_at DESC LIMIT 100")
                           if readable(r, service.config)]}
    return await service.execute(request, listing, admin=True, event_name='selfie_history_viewed')


async def loop(service):
    next_maintenance = 0
    while True:
        service.selfie_wakeup.clear()
        worked = False
        maintenance = time.monotonic() >= next_maintenance
        with contextlib.suppress(Exception):
            try:
                worked = await process(service, maintenance=maintenance)
            finally:
                if maintenance:
                    # Retain the prior cleanup cadence even while enrollment
                    # drains quickly; failed cleanup also waits before retrying.
                    next_maintenance = time.monotonic() + 3
        if worked:
            # Drain ready work, yielding to requests between people. Failed jobs
            # retain next_try backoff, so a storage outage cannot cause a hot loop.
            await asyncio.sleep(0)
        else:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(service.selfie_wakeup.wait(), 3)


def restore_metadata(db):
    """Only confirmed metadata unlocks reading; ambiguous captures keep retention.

    A missing enrollment event is not evidence that enrollment failed: even a
    recovery with no detected batch gaps can lack the last unexported events.
    Keep consent-only portraits inaccessible until expiry plus retention.
    """
    for row in db.execute("SELECT email,type,ts,payload FROM events WHERE type IN ('selfie_consent_accepted','selfie_enrolled','selfie_retired','selfie_purged') ORDER BY ts,seq").fetchall():
        detail = json.loads(row['payload']).get('detail', {})
        if row['type'] in ('selfie_consent_accepted', 'selfie_enrolled'):
            if 'drive_id' not in detail:  # Early preview consent records had no recovery metadata.
                continue
            identity = str(uuid.UUID(detail['id']))
            confirmed = row['type'] == 'selfie_enrolled'
            if confirmed:
                db.execute("UPDATE selfies SET status='retired',retired_at=? WHERE email=? AND device_hash=? AND status='active' AND id!=?",
                           (row['ts'], row['email'], detail['device_hash'], identity))
            db.execute('INSERT INTO selfies(id,email,device_hash,session_id,captured_at,expires_at,status,drive_id,sha256,consent_version,context,retired_at) '
                       "VALUES (?,?,?,'',?,?,?,?,?,?,'{}',?) ON CONFLICT(id) DO NOTHING",
                       (identity, row['email'], detail['device_hash'], detail['captured_at'], detail['expires_at'],
                        'active' if confirmed else 'quarantined', detail['drive_id'], detail['sha256'], detail['consent_version'],
                        None if confirmed else detail['expires_at']))
            if confirmed:
                db.execute("UPDATE selfies SET status='active',retired_at=NULL WHERE id=?", (identity,))
        elif row['type'] == 'selfie_retired':
            db.execute("UPDATE selfies SET status='retired',retired_at=? WHERE id=? AND status!='purged'",
                       (detail['retired_at'], detail['selfie_id']))
        else:
            db.execute("UPDATE selfies SET status='purged' WHERE id=?", (detail['selfie_id'],))


def relocate_restored_metadata(db, relocations):
    """Translate verified storage locations, preserving the sealed event history."""
    for photo in db.execute('SELECT id,drive_id,sha256 FROM selfies').fetchall():
        target = relocations.get(photo['drive_id'])
        if not target:
            continue
        if target['sha256'] != photo['sha256']:
            raise StoreUnavailable('Migrated portrait checksum differs from its enrollment record')
        db.execute('UPDATE selfies SET drive_id=? WHERE id=?', (target['id'], photo['id']))
