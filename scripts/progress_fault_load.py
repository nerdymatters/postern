"""Synthetic sustained disclosure, media/export failure and same-database rollback.

The default is a small diagnostic. --representative selects the registered
25-reader/228-roster/nine-hour/120-second workload on disposable hosted macOS.
This is correctness evidence, never a replacement for capacity acceptance.
"""
import argparse
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx
import psutil
import uvicorn

from postern import progress, progress_backend
from postern.app import create_app
from postern.config import Config
from postern.db import event
from postern.recover import import_events
from postern.service import Service
from postern.storage import FakeStore
from scripts import capacity


class Running:
    def __init__(self, config, store):
        self.service = self.socket = self.lifecycle_task = None
        self.loop = None
        self.error = None
        self.thread = None
        try:
            self.service = Service(config, store=store)
            original_start = self.service.start
            async def tracked_start():
                self.lifecycle_task = asyncio.current_task()
                await original_start()
            self.service.start = tracked_start
            self.socket = socket.socket()
            self.socket.bind(('127.0.0.1', 0))
            self.url = f'http://127.0.0.1:{self.socket.getsockname()[1]}'
            self.server = uvicorn.Server(uvicorn.Config(
                create_app(config, service=self.service), log_level='error', access_log=False))
        except BaseException:
            self.close()
            raise

    def start(self):
        def serve():
            async def run():
                self.loop = asyncio.get_running_loop()
                await self.server.serve(sockets=[self.socket])
            try:
                asyncio.run(run())
            except BaseException as error:
                self.error = type(error).__name__
        self.thread = threading.Thread(target=serve, name='fault-http', daemon=True)
        self.thread.start()
        deadline = time.monotonic()+10
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.025)
        if not self.server.started or time.monotonic() >= deadline:
            raise RuntimeError('Isolated application did not become ready within ten seconds')
        return self

    def call(self, coroutine, timeout=15):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def close(self):
        try:
            if self.thread is None:
                if self.service is not None:
                    asyncio.run(self.service.close())
            else:
                self.server.should_exit = True
                if not self.server.started and self.lifecycle_task is not None and not self.lifecycle_task.done():
                    self.loop.call_soon_threadsafe(self.lifecycle_task.cancel)
                self.thread.join(15)
                if self.thread.is_alive():
                    raise RuntimeError('Owned application did not stop')
        finally:
            if self.socket is not None:
                self.socket.close()
        if self.error:
            raise RuntimeError('Owned application task failed: '+self.error)


def durable(db, maximum=None):
    maximum = db.execute('SELECT MAX(seq) FROM events').fetchone()[0] if maximum is None else maximum
    digest = hashlib.sha256()
    count = 0
    for identity, payload in db.execute('SELECT id,payload FROM events WHERE seq<=? ORDER BY seq', (maximum,)):
        digest.update(json.dumps([identity, payload], separators=(',', ':')).encode())
        count += 1
    return {'maximum': maximum, 'count': count, 'sha256': digest.hexdigest()}


def standing(body):
    return {'locks': [(post['id'], post.get('locked_by')) for post in body['posts']],
            'quizzes': [(quiz['chapter'], quiz['passed']) for quiz in body['quizzes']],
            'certificate': body['certificate']}


def reader_state(body):
    return {**standing(body), 'progress': [
        (post['id'], item['id'], item['progress']) for post in body['posts'] for item in post['items']]}


def run(output, representative=False):
    output.mkdir(parents=True, exist_ok=False)
    users, seconds, history, page_every, catalog_every = ((25, 120, 9, 20, 30)
        if representative else (2, 15, 0, .5, 1))
    record = {'status': 'running', 'representative': representative,
              'settings': dict(users=users, seconds=seconds, history_hours=history, roster=228),
              'phases': [], 'failures': [], 'limits': [
                  'Synthetic identities and fake cloud; no real authentication, WAN or provider quotas.',
                  'Document/catalog coexistence; video streaming capacity is measured by the separate campaign.',
                  'Global recovery/audit failure and full cloud-history restoration remain separate checks.',
                  'Rollback reuses the current database; no older backup is restored over acknowledged work.']}
    store = FakeStore(output/'cloud')
    allowlist = [['email', 'name', 'role']]+[[f'reader{i}@example.invalid', 'Synthetic',
        'admin' if i == 0 else 'reader'] for i in range(228)]
    store.write_allowlist(allowlist)
    config = Config(data_dir=output/'data', mode='demo', preview=True, selfie_enabled=False)
    app = service = None
    stop = threading.Event()
    phases_done = threading.Event()
    guard_done = threading.Event()
    guard_thread = None
    load = None
    client = None
    lock = threading.Lock()
    health_lock = threading.Lock()
    serving = threading.Event()
    acknowledgments, counts = Counter(), Counter()
    page_times = {i: [] for i in range(1, users+1)}
    catalog_times = {i: [] for i in range(1, users+1)}
    load_statuses = Counter()
    started = None
    load_pages, load_catalogs = Counter(), Counter()
    windows = []
    checks = []
    original_backend = os.environ.get('POSTERN_PROGRESS_BACKEND')
    try:
        record['phase'] = 'runtime'
        app = Running(config, store)
        service = app.service
        backend = progress_backend.selected()
        native_hash = None
        if backend == 'rust':
            binary = Path(sys.modules[progress_backend.native().reduce.__module__].__file__)
            native_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
        record['runtime'] = {'backend': backend, 'native_sha256': native_hash,
            'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=capacity.ROOT, text=True).strip(),
            'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'uncommitted_inputs': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=capacity.ROOT, text=True).strip())}
        process = psutil.Process()
        psutil.cpu_percent()

        guarded_started = time.monotonic()
        def guard():
            busy = 0
            next_health = time.monotonic()+5
            with httpx.Client(trust_env=False, timeout=2) as health:
                while not guard_done.wait(.5):
                    cpu = psutil.cpu_percent()
                    busy = busy+1 if cpu > 70 else 0
                    rss = process.memory_info().rss/1024**2
                    sample = {'elapsed_seconds': time.monotonic()-guarded_started,
                              'phase': record.get('phase'), 'host_cpu_percent': cpu, 'rss_mib': rss}
                    reason = None
                    if time.monotonic()-guarded_started > (600 if representative else 120):
                        reason = 'whole diagnostic deadline'
                    if busy >= 10:
                        reason = 'host CPU exceeded70% for five seconds'
                    if rss > 1024 or psutil.virtual_memory().available < 1024**3:
                        reason = 'memory guard'
                    with health_lock:
                        if serving.is_set() and time.monotonic() >= next_health:
                            began = time.monotonic()
                            try:
                                response = health.get(app.url+'/health')
                                sample['health_ms'] = (time.monotonic()-began)*1000
                                if (response.status_code != 200 or response.json() != capacity.HEALTH
                                        or sample['health_ms'] > 2000):
                                    reason = 'health contract'
                            except Exception as error:
                                reason = 'health '+type(error).__name__
                            next_health = time.monotonic()+5
                    checks.append(sample)
                    if reason:
                        record['failures'].append(reason)
                        stop.set()
                        return
        def guarded_target():
            try:
                guard()
            except BaseException as error:
                record['failures'].append('guard '+type(error).__name__)
                stop.set()
        guard_thread = threading.Thread(target=guarded_target, name='fault-guard', daemon=True)
        guard_thread.start()
        original_submit = service.db.submit
        def guarded_submit(operation):
            if stop.is_set():
                raise RuntimeError('Resource guard stopped fixture creation')
            return original_submit(operation)
        service.db.submit = guarded_submit
        try:
            record['phase'] = 'seed'
            record['setup'] = {}
            tokens = capacity.seed(service, 228, history, diagnostics=record['setup'])
        finally:
            service.db.submit = original_submit
        assert not stop.is_set()
        def prepare(db):
            db.execute("UPDATE posts SET category='SOP Chapter 1' WHERE id=?", (capacity.PDF,))
            db.execute("UPDATE posts SET category='SOP Chapter 2' WHERE id=?", (capacity.VIDEO,))
            import_events(db, [
                {'event_id': 'fault-chapter-pass', 'event': 'quiz_attempt', 'ts': 100,
                 'email': 'reader1@example.invalid', 'detail': {'chapter': 1, 'score': 5, 'total': 5, 'passed': True}},
                {'event_id': 'fault-final-pass', 'event': 'quiz_attempt', 'ts': 101,
                 'email': 'reader1@example.invalid', 'detail': {'chapter': 0, 'score': 20, 'total': 20, 'passed': True}},
                {'event_id': 'fault-name', 'event': 'certificate_name', 'ts': 102,
                 'email': 'reader1@example.invalid', 'detail': {'name': 'Synthetic Reader'}},
            ])
        service.db.submit(prepare).result(10)
        record['phase'] = 'startup'
        app.start()
        serving.set()
        assert not stop.is_set()
        client = httpx.Client(base_url=app.url, trust_env=False, timeout=10,
                             limits=httpx.Limits(max_connections=users+20, keepalive_expiry=4))
        headers = [{'Authorization': 'Bearer '+token} for token in tokens]
        views = []
        for i in range(1, users+1):
            response = client.post(f'/api/items/{capacity.PDF}/views', headers=headers[i],
                                   json={'attempt_id': str(uuid.uuid4())})
            response.raise_for_status()
            views.append(response.json()['id'])
        baseline = client.get('/api/posts', headers=headers[1])
        baseline.raise_for_status()
        before_standing = standing(baseline.json())
        assert before_standing['certificate']['name'] == 'Synthetic Reader'
        assert dict(before_standing['locks'])[capacity.VIDEO] is None

        def page(i, workload=False):
            began = time.monotonic()
            response = client.get(f'/api/views/{views[i-1]}/page/0.jpg', headers=headers[i])
            ended = time.monotonic()
            with lock:
                counts['page_attempts'] += 1
                if workload:
                    page_times[i].append(ended-started)
                    load_statuses[str(response.status_code)] += 1
                if response.status_code == 200:
                    assert response.headers['content-type'] == 'image/jpeg' and response.content.startswith(b'\xff\xd8')
                    acknowledgments[f'reader{i}@example.invalid'] += 1
                    if workload:
                        load_pages[i] += 1
                else:
                    allowed = any(start <= ended and (end is None or began <= end) for start, end in windows)
                    assert allowed and response.status_code == 409 and response.json()['error'] == 'preparing'
                    counts['expected_media_refusals'] += 1

        started = time.monotonic()
        record['phase'] = 'workload'
        def reader(i):
            next_page = next_catalog = 0
            try:
                while (time.monotonic()-started < seconds or not phases_done.is_set()) and not stop.is_set():
                    now = time.monotonic()-started
                    if now >= next_page:
                        page(i, workload=True)
                        next_page = now+page_every
                    if now >= next_catalog:
                        response = client.get('/api/posts', headers=headers[i])
                        assert response.status_code == 200
                        with lock:
                            counts['catalog'] += 1
                            load_catalogs[i] += 1
                            catalog_times[i].append(time.monotonic()-started)
                        next_catalog = now+catalog_every
                    stop.wait(.05)
            except Exception as error:
                with lock:
                    record['failures'].append('reader '+type(error).__name__)
                stop.set()
                raise
        load = ThreadPoolExecutor(max_workers=users, thread_name_prefix='fault-reader')
        readers = [load.submit(reader, i) for i in range(1, users+1)]

        def report_count():
            return service.db.read(lambda db: db.execute(
                "SELECT count(*) FROM events WHERE type='training_progress_viewed'").fetchone()[0])

        def disclosure(kind):
            record['phase'] = kind
            assert not stop.is_set() and not phases_done.is_set()
            boundary_deadline = 60 if representative else 10
            original = service.read_progress
            arrived = threading.Event()
            state = {}
            async def install():
                state['release'] = asyncio.Event()
                state['count'] = 0
                async def hold(operation):
                    result = await original(operation)
                    if operation.__qualname__ == 'Service.training_progress.<locals>.query':
                        state['count'] += 1
                        if state['count'] == 8:
                            arrived.set()
                        await asyncio.wait_for(state['release'].wait(), boundary_deadline+5)
                    return result
                service.read_progress = hold
            app.call(install())
            before = report_count()
            with ThreadPoolExecutor(max_workers=8) as pool:
                pending = [pool.submit(client.get, '/api/admin/progress', headers=headers[0],
                                       timeout=boundary_deadline+5) for _ in range(8)]
                try:
                    wait_until = time.monotonic()+boundary_deadline
                    while not arrived.wait(.1):
                        assert not stop.is_set() and time.monotonic() < wait_until, 'Eight snapshots did not reach the boundary'
                    assert not any(f.done() for f in pending)
                    page(1)
                    if kind == 'role':
                        changed = [row[:] for row in allowlist]
                        changed[1][2] = 'reader'
                        store.write_allowlist(changed)
                        service.db.submit(lambda db: db.execute("UPDATE members SET role='reader' WHERE email='reader0@example.invalid'")).result(5)
                    elif kind == 'revoke':
                        service.db.submit(lambda db: db.execute("UPDATE sessions SET revoked=1 WHERE id='session0'")).result(5)
                    elif kind == 'consent':
                        service.db.submit(lambda db: db.execute("UPDATE sessions SET consent_version=NULL WHERE id='session0'")).result(5)
                    app.loop.call_soon_threadsafe(state['release'].set)
                    responses = [future.result(10) for future in pending]
                    expected = 200 if kind == 'normal' else 403
                    assert all(response.status_code == expected for response in responses)
                    if kind != 'normal':
                        assert all('people' not in response.json() and 'items' not in response.json() for response in responses)
                    assert report_count()-before == (8 if kind == 'normal' else 0)
                    record['phases'].append({'phase': kind, 'responses': len(responses), 'status': expected,
                                             'report_audit_events': report_count()-before})
                finally:
                    app.loop.call_soon_threadsafe(state['release'].set)
                    service.read_progress = original
                    store.write_allowlist(allowlist)
                    service.db.submit(lambda db: (
                        db.execute("UPDATE members SET role='admin' WHERE email='reader0@example.invalid'"),
                        db.execute("UPDATE sessions SET revoked=0,consent_version=? WHERE id='session0'", (config.notice_version,)))).result(5)

        for kind in ('normal', 'role', 'revoke', 'consent'):
            disclosure(kind)
        record['phase'] = 'quiz'
        second = client.get('/api/posts', headers=headers[2])
        second.raise_for_status()
        assert dict(standing(second.json())['locks'])[capacity.VIDEO] == 1
        assert second.json()['certificate'] is None
        restored = [
            {'event_id': 'load-chapter-pass', 'event': 'quiz_attempt', 'ts': 200,
             'email': 'reader2@example.invalid', 'detail': {'chapter': 1, 'score': 5, 'total': 5, 'passed': True}},
            {'event_id': 'load-final-pass', 'event': 'quiz_attempt', 'ts': 201,
             'email': 'reader2@example.invalid', 'detail': {'chapter': 0, 'score': 20, 'total': 20, 'passed': True}},
            {'event_id': 'load-name', 'event': 'certificate_name', 'ts': 202,
             'email': 'reader2@example.invalid', 'detail': {'name': 'Restored Reader'}},
        ]
        assert service.db.submit(lambda db: import_events(db, restored)).result(5) == 3
        assert service.db.submit(lambda db: import_events(db, restored)).result(5) == 0
        second = client.get('/api/posts', headers=headers[2])
        second.raise_for_status()
        second_standing = standing(second.json())
        assert dict(second_standing['locks'])[capacity.VIDEO] is None
        assert second_standing['certificate']['name'] == 'Restored Reader'
        record['phases'].append({'phase': 'quiz', 'imported': 3, 'duplicate_imported': 0,
                                 'locks_and_certificate_updated': True})
        record['phase'] = 'media'
        composite, derive = service.composite, service.schedule_derive
        with lock:
            windows.append([time.monotonic(), None])
        def missing(*args, **kwargs):
            raise FileNotFoundError('synthetic missing page')
        service.composite = missing
        service.schedule_derive = lambda _item: None
        try:
            page(1)
        finally:
            service.composite, service.schedule_derive = composite, derive
            service.db.submit(lambda db: db.execute("UPDATE items SET derive_status='ready' WHERE id=?", (capacity.PDF,))).result(5)
            with lock:
                windows[-1][1] = time.monotonic()
        assert counts['expected_media_refusals'] >= 1
        record['phases'].append({'phase': 'media', 'expected_refusals': counts['expected_media_refusals']})

        async def pause_export_loop():
            tasks = [task for task in service.tasks if 'background_loop' in task.get_coro().__qualname__]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        record['phase'] = 'export'
        app.call(pause_export_loop())
        # Leave an unexported ordinary event so this phase cannot be a no-op.
        service.db.submit(lambda db: event(db, 'fault_probe', detail={'synthetic': True})).result(5)
        before_export = service.db.read(durable)
        put = store.put_file
        def unavailable(*args, **kwargs):
            raise OSError('synthetic export unavailable')
        store.put_file = unavailable
        try:
            try:
                app.call(service.export())
            except OSError:
                pass
            else:
                raise AssertionError('Export failure did not occur')
        finally:
            store.put_file = put
        assert service.db.read(lambda db: durable(db, before_export['maximum'])) == before_export
        assert service.db.read(lambda db: db.execute('SELECT count(*) FROM batches WHERE confirmed_at IS NULL').fetchone()[0]) >= 1
        app.call(service.export())
        assert service.db.read(lambda db: db.execute('SELECT count(*) FROM batches WHERE confirmed_at IS NULL OR summary_confirmed=0').fetchone()[0]) == 0
        async def resume_export_loop():
            service.tasks.append(asyncio.create_task(service.background_loop()))
        app.call(resume_export_loop())
        record['phases'].append({'phase': 'export', 'durable_before': before_export['count'], 'retry_confirmed': True})
        phases_done.set()
        for future in readers:
            future.result(seconds+20)
        record['phase'] = 'coverage'
        assert not record['failures'] and not stop.is_set()
        elapsed = time.monotonic()-started
        assert elapsed >= seconds
        # This probe tests sustained correctness, not ideal request throughput.
        # Planned media refusals are activity, but never successful/durable pages.
        def covered(times):
            return {min(2, int(t/(elapsed/3))) for t in times if 0 <= t <= elapsed} == {0, 1, 2}
        assert all(load_pages[i] >= 2 and covered(page_times[i]) for i in range(1, users+1))
        assert all(covered(catalog_times[i]) for i in range(1, users+1))
        assert [phase['phase'] for phase in record['phases']] == ['normal', 'role', 'revoke', 'consent', 'quiz', 'media', 'export']
        persisted = service.db.read(lambda db: Counter({row[0]: row[1] for row in db.execute(
            "SELECT email,count(*) FROM events WHERE type='page_served' GROUP BY email")}))
        assert persisted == acknowledgments
        record['phase'] = 'raw-parity'
        response = client.get('/api/admin/progress', headers=headers[0])
        response.raise_for_status()
        # Keep the raw query independent of the derived table/cache selector.
        class RawHistory:
            def history(self, db, items):
                return progress._reduce_python(items, db.execute(
                    "SELECT e.email,e.item_id,e.type,e.ts,e.payload FROM events e JOIN members m ON m.email=e.email "
                    "WHERE e.type IN ('page_served','item_summary','video_progress','item_open','view_requested')"))
        def reference(db, active_service):
            db.execute('BEGIN')
            return progress.dashboard(db, active_service.permissions.quizzes, active_service.final_exam,
                default_group=config.branding.default_group, certificate_prefix=config.branding.certificate_prefix,
                history_cache=RawHistory())
        assert response.json() == service.db.read(lambda db: reference(db, service))
        before_body = client.get('/api/posts', headers=headers[1])
        before_body.raise_for_status()
        assert standing(before_body.json()) == before_standing
        before_progress = reader_state(before_body.json())
        before_second = client.get('/api/posts', headers=headers[2])
        before_second.raise_for_status()
        before_second_progress = reader_state(before_second.json())
        assert not record['failures']
        record['phase'] = 'rollback-start'
        with health_lock:
            serving.clear()  # Resource guards continue during the deliberate stop/start.
        client.close()
        client = None
        app.close()
        before = service.db.read(durable)  # Include every write through completed shutdown.
        app = None
        os.environ['POSTERN_PROGRESS_BACKEND'] = 'python'
        app = Running(config, store)
        app.start()
        assert not stop.is_set()
        serving.set()
        record['phase'] = 'rollback-verify'
        assert app.service.db.read(lambda db: durable(db, before['maximum'])) == before
        with httpx.Client(base_url=app.url, trust_env=False, timeout=10) as rollback:
            after = rollback.get('/api/posts', headers=headers[1])
            after.raise_for_status()
            assert standing(after.json()) == before_standing
            assert reader_state(after.json()) == before_progress
            after_second = rollback.get('/api/posts', headers=headers[2])
            after_second.raise_for_status()
            assert standing(after_second.json()) == second_standing
            assert reader_state(after_second.json()) == before_second_progress
            report = rollback.get('/api/admin/progress', headers=headers[0])
            report.raise_for_status()
            assert report.json() == response.json() == app.service.db.read(lambda db: reference(db, app.service))
            began = time.monotonic()
            health = rollback.get('/health', timeout=2)
            record['rollback_health_ms'] = (time.monotonic()-began)*1000
            assert health.status_code == 200 and health.json() == capacity.HEALTH and record['rollback_health_ms'] <= 2000
            assert app.service.db.read(lambda db: Counter({row[0]: row[1] for row in db.execute(
                "SELECT email,count(*) FROM events WHERE type='page_served' GROUP BY email")})) == acknowledgments
        record.update(status='completed', elapsed_seconds=elapsed, requests=dict(counts),
                      acknowledged_pages=sum(acknowledgments.values()), persisted_pages=sum(persisted.values()),
                      workload_pages_per_reader=[load_pages[i] for i in range(1, users+1)],
                      workload_catalogs_per_reader=[load_catalogs[i] for i in range(1, users+1)],
                      readers_with_pages=len(acknowledgments), raw_dashboard_matches=True,
                      rollback_preserved=before, standing_preserved=True)
        return record
    except BaseException as error:
        record.update(status='failed', error=type(error).__name__)
        raise
    finally:
        stop.set()
        cleanup_errors = []
        def clean(operation):
            try:
                operation()
            except BaseException as error:
                cleanup_errors.append(type(error).__name__)
        if load is not None:
            clean(lambda: load.shutdown(wait=True, cancel_futures=True))
        if client:
            clean(client.close)
        with health_lock:
            serving.clear()
        if app:
            clean(app.close)
        guard_done.set()
        if guard_thread:
            clean(lambda: guard_thread.join(3))
            if guard_thread.is_alive():
                cleanup_errors.append('GuardThreadStillRunning')
        if original_backend is None:
            os.environ.pop('POSTERN_PROGRESS_BACKEND', None)
        else:
            os.environ['POSTERN_PROGRESS_BACKEND'] = original_backend
        if record['status'] == 'completed' and (record['failures'] or
                sum('health_ms' in sample for sample in checks) < (19 if representative else 2)):
            cleanup_errors.append('GuardValidationFailure')
        if cleanup_errors:
            record.update(status='failed', cleanup_errors=cleanup_errors)
        record.update(requests=dict(counts), acknowledged_pages=sum(acknowledgments.values()),
                      workload_pages_per_reader=[load_pages[i] for i in range(1, users+1)],
                      workload_catalogs_per_reader=[load_catalogs[i] for i in range(1, users+1)],
                      workload_page_statuses=dict(load_statuses),
                      workload_page_times=[page_times[i] for i in range(1, users+1)],
                      workload_catalog_times=[catalog_times[i] for i in range(1, users+1)],
                      observed_elapsed_seconds=time.monotonic()-started if started is not None else None)
        record['guard_samples'] = checks
        (output/'report.json').write_text(json.dumps(record, indent=2)+'\n')
        if cleanup_errors and sys.exc_info()[0] is None:
            raise RuntimeError('Owned fault-probe cleanup failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--representative', action='store_true')
    args = parser.parse_args()
    if args.representative and not (os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted' and sys.platform == 'darwin'):
        parser.error('Representative fault load requires a disposable GitHub-hosted Mac')
    run(args.output.resolve(), args.representative)
    print('Sustained fault diagnostic completed; inspect report.json for scope and limits.')


if __name__ == '__main__':
    main()
