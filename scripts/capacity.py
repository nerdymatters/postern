"""Loopback-only capacity rehearsal using synthetic data and the real HTTP application.

No production URL/config/credentials can be supplied. Each invocation creates a new
private directory. Byte fixtures exercise delivery, not browser decoding/buffering.
Run with: .venv/bin/python -m scripts.capacity --help
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import hashlib
from importlib.machinery import EXTENSION_SUFFIXES
import json
import math
import platform
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx
import psutil
import uvicorn

from postern.app import create_app
from postern.auth import token_hash
from postern.config import Config
from postern.db import event
from postern.service import Service
from postern.storage import FakeStore
from postern.video import CHUNK
from postern.selfie import device_hash
from postern import progress, progress_backend

ROOT = Path(__file__).resolve().parents[1]
VIDEO, PDF = 'capacity-video', 'capacity-pdf'
SEED_BATCH_ROWS = 128
HEALTH = {'process': 'up', 'permissions': 'ready', 'audit': 'ready',
          'backup': 'current', 'storage': 'ready'}


def health_payload(response):
    if response is None:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def progress_key(email, ranges):
    # The API normalizes 0.0 to 0 at the beginning of an interval. Compare
    # numeric endpoints, not their JSON spelling, while retaining exact values.
    return email, tuple((float(a), float(b)) for a, b in ranges)


async def verify_dashboard(service, port, token, stop):
    """After traffic stops, compare HTTP output with fresh raw Python evidence.

    This extra correctness request is outside measured workload CPU/latencies;
    it never counts as workload coverage. Resource guards remain active.
    """
    class RawHistory:
        def history(self, db, items):
            return progress._reduce_python(items, db.execute(
                "SELECT e.email,e.item_id,e.type,e.ts,e.payload FROM events e "
                "JOIN members m ON m.email=e.email WHERE e.type IN "
                "('page_served','item_summary','video_progress','item_open','view_requested')"))
    def reference(db):
        db.execute('BEGIN')
        # A guard racing verification can interrupt this disposable SQL replay.
        db.set_progress_handler(lambda: int(stop.is_set()), 1000)
        return progress.dashboard(db, service.permissions.quizzes, service.final_exam,
            default_group=service.config.branding.default_group,
            certificate_prefix=service.config.branding.certificate_prefix, history_cache=RawHistory())
    started = time.monotonic()
    failure = None
    if stop.is_set():
        return {'raw_python_dashboard_matches': False, 'elapsed_seconds': 0, 'skipped': 'resource guard'}
    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        try:
            response = await client.get(f'http://127.0.0.1:{port}/api/admin/progress',
                                        headers={'Authorization': 'Bearer '+token})
            if stop.is_set():
                return {'raw_python_dashboard_matches': False, 'elapsed_seconds': time.monotonic()-started,
                        'skipped': 'resource guard'}
            expected = await service.read_progress(reference)
            matches = response.status_code == 200 and response.json() == expected
        except Exception as error:
            # Verification faults fail the attempt without discarding the
            # completed workload's measurements. Keep only the exception class.
            failure = type(error).__name__
            matches = False
    return {'raw_python_dashboard_matches': matches, 'elapsed_seconds': time.monotonic()-started,
            'error': failure}


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {'count': 0}
    return {'count': len(ordered), 'min': round(ordered[0], 3),
            'mean': round(sum(ordered)/len(ordered), 3), **{
        name: round(ordered[max(0, math.ceil(len(ordered)*fraction)-1)], 3)
        for name, fraction in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]}}


def process_cpu_snapshot():
    """Diagnostic counters only: no arguments, environment or command lines."""
    result = {}
    for process in psutil.process_iter(['pid', 'name', 'create_time', 'cpu_times']):
        info = process.info
        if any(info.get(key) is None for key in ('pid', 'create_time', 'cpu_times')):
            continue  # Exited or inaccessible processes cannot be attributed.
        result[info['pid'], info['create_time']] = (
            info['name'], info['cpu_times'].user + info['cpu_times'].system)
    return result


def process_cpu_changes(before, after, seconds):
    """One-core percentages; only processes observed at both ends are comparable."""
    changes = [{'pid': pid, 'name': name, 'cpu_percent_one_core': round(100*(cpu-before[key][1])/seconds, 2)}
               for key, (name, cpu) in after.items() if key in before and cpu >= before[key][1]
               for pid, _created in [key]] if seconds > 0 else []
    return sorted(changes, key=lambda row: (-row['cpu_percent_one_core'], row['pid']))[:5]


def workload_coverage(args, statuses, acknowledged_progress):
    """Missing traffic is a failed rehearsal, even if every request made succeeded."""
    if args.scenario == 'signin':
        required = {'signin': ('200', args.users)}
    elif args.scenario == 'enrollment':
        required = {'selfie_capture': ('202', args.users), 'selfie_status': ('200', args.users),
                    'enrollment_ready': ('confirmed', args.users)}
        if args.with_reader:
            required.update(existing_reader_open=('200', 1), existing_reader_player=('200', 1),
                            existing_reader_range=('206', 1), existing_reader_progress=('200', 1))
    else:
        required = {'open_view': ('200', args.users*2), 'player': ('200', args.users),
                    'video_range': ('206', args.users), 'progress': ('200', args.users),
                    'catalog': ('200', args.users), 'page': ('200', math.ceil(args.users/5)),
                    'dashboard': ('200', 1)}
    counts = {label: {'status': status, 'minimum': minimum,
                      'observed': statuses.get(label, {}).get(status, 0)}
              for label, (status, minimum) in required.items()}
    missing = [label for label, count in counts.items() if count['observed'] < count['minimum']]
    expected_readers = ({f'reader{i}@example.invalid' for i in range(args.users)}
                        if args.scenario == 'mixed' else
                        {f'reader{args.users}@example.invalid'} if args.with_reader else set())
    actual_readers = {email for (email, _), count in acknowledged_progress.items() if count > 0}
    missing_readers = sorted(expected_readers - actual_readers)
    return {'passed': not missing and not missing_readers, 'families': counts,
            'missing': missing, 'readers_without_progress': missing_readers}


class Metrics:
    def __init__(self):
        self.latencies = defaultdict(list)
        self.statuses = defaultdict(Counter)
        self.bytes = 0
        self.audit = []
        self.queue = []
        self.commit = []
        self.lag = []
        self.rss = []
        self.host_idle = []
        self.queue_depth = []
        self.writer_operations = defaultdict(list)
        self.writer_queue = defaultdict(list)
        self.writer_commit = defaultdict(list)
        self.active = Counter()
        self.host_cpu_diagnostics = []
        self.transport_errors = defaultdict(Counter)

    async def request(self, client, label, method, path, **kwargs):
        started = time.monotonic()
        self.active[label] += 1
        try:
            response = await client.request(method, path, **kwargs)
            self.statuses[label][str(response.status_code)] += 1
            self.bytes += len(response.content)
            return response
        except httpx.HTTPError as error:
            self.statuses[label]['transport_error'] += 1
            self.transport_errors[label][type(error).__name__] += 1
            return None
        finally:
            self.active[label] -= 1
            self.latencies[label].append((time.monotonic()-started)*1000)

    def report(self):
        return {'requests': {label: {'latency_ms': distribution(values),
                                    'statuses': dict(self.statuses[label])}
                             for label, values in self.latencies.items()},
                'body_bytes': self.bytes, 'audit_ms': distribution(self.audit),
                'queue_ms': distribution(self.queue), 'commit_ms': distribution(self.commit),
                'event_loop_lag_ms': distribution(self.lag), 'process_rss_mib': distribution(self.rss),
                'whole_host_idle_percent': distribution(self.host_idle),
                'host_cpu_diagnostics': self.host_cpu_diagnostics,
                'transport_errors': {label: dict(errors) for label, errors in self.transport_errors.items()},
                'writer_queue_depth': distribution(self.queue_depth),
                'writer_by_caller': {owner: {'operation_cpu_ms': distribution(values),
                                             'queue_ms': distribution(self.writer_queue[owner]),
                                             'commit_ms': distribution(self.writer_commit[owner])}
                                     for owner, values in list(self.writer_operations.items())}}


def seed(service, roster, history_hours, existing_reader=None, diagnostics=None):
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(operations=0, queue_ms_max=0, commit_ms_max=0)

    def write(operation, phase, **position):
        future = service.db.submit(operation)
        try:
            return future.result(10)
        except BaseException as error:
            diagnostics['failure'] = {'phase': phase, **position, 'error': type(error).__name__,
                                      'queue_ms': getattr(future, 'queue_ms', None),
                                      'commit_ms': getattr(future, 'commit_ms', None)}
            raise
        finally:
            diagnostics['operations'] += 1
            for field in ('queue_ms', 'commit_ms'):
                diagnostics[field+'_max'] = max(diagnostics[field+'_max'], getattr(future, field, 0))

    """All identities/history/media are fictional, including pre-enrolled sessions."""
    fixture = ROOT/'tests/fixtures/page.jpg'
    for identity in (VIDEO, PDF):
        directory = service.config.data_dir/'derived'/identity
        directory.mkdir()
        (directory/'page-0.jpg').write_bytes(fixture.read_bytes())
        (directory/'thumb.jpg').write_bytes(fixture.read_bytes())
    # Deterministic opaque bytes: never label this as a playable MP4.
    (service.config.data_dir/'derived'/VIDEO/'video.mp4').write_bytes(bytes(range(256))*(CHUNK*2//256))
    now = time.time()
    tokens = [uuid.uuid4().hex for _ in range(roster)]
    emails = [f'reader{i}@example.invalid' for i in range(roster)]
    def initial(db):
        for i, (email, token) in enumerate(zip(emails, tokens)):
            db.execute('INSERT INTO sessions(id,token_hash,subject,email,name,created_at,expires_at,client,consent_version) VALUES (?,?,?,?,?,?,?, ?,?)',
                       (f'session{i}', token_hash(token), f'sub{i}', email, email, now, now+86400, 'web', service.config.notice_version))
            db.execute('INSERT INTO session_ip_agreements VALUES (?,?,?)', (f'session{i}', 'ip-1', now))
            db.execute('INSERT INTO session_devices VALUES (?,?)', (f'session{i}', device_hash(str(uuid.uuid4()))))
        for identity, kind in ((VIDEO, 'video'), (PDF, 'pdf')):
            db.execute("INSERT INTO posts(id,title,body,category,status,created_at,created_by,published_at) VALUES (?,?,?,'Synthetic','published',?,?,?)",
                       (identity, identity, 'Synthetic capacity fixture', now, emails[0], now))
            db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) VALUES (?,?,?,?,?,?,?,'ready',0)",
                       (identity, identity, identity, kind, hashlib.sha256(identity.encode()).hexdigest(), CHUNK*2 if kind == 'video' else fixture.stat().st_size, 0 if kind == 'video' else 1))
        db.execute('INSERT INTO video_metadata VALUES (?,?)', (VIDEO, 12*3600))
    write(initial, 'sessions_and_catalog')
    if existing_reader is not None:
        # A synthetic already-enrolled reader for coexistence tests. Newly
        # enrolling accounts still use the real sanitizer/confirmation worker.
        identity, drive_id = str(uuid.uuid4()), service.store.new_id()
        portrait = service.config.data_dir/'selfies'/(identity+'.jpg')
        portrait.write_bytes(fixture.read_bytes())
        portrait.chmod(0o600)
        digest = hashlib.sha256(portrait.read_bytes()).hexdigest()
        service.store.put_file(drive_id, portrait, portrait.name, digest, {'kind': 'selfie'})
        def existing(db):
            session = f'session{existing_reader}'
            binding = db.execute('SELECT device_hash FROM session_devices WHERE session_id=?', (session,)).fetchone()[0]
            db.execute('INSERT INTO selfies(id,email,device_hash,session_id,captured_at,expires_at,status,drive_id,sha256,consent_version,context) '
                       "VALUES (?,?,?,?,?,?,'active',?,?,?,'{}')",
                       (identity, emails[existing_reader], binding, session, now, now+86400,
                        drive_id, digest, service.config.selfie_notice_version))
        write(existing, 'existing_reader')
    # One synthetic 12-hour timeline. Nine hours corresponds to 2,160 events per reader.
    count = int(history_hours*240)
    if count:
        # Establish confirmed history before assigning events to it. Inserting
        # this last creates an orphan row for every event and then clears the
        # entire history in one bounded writer transaction. No workload is active
        # yet; this is synthetic prior-confirmation metadata, not recovery proof.
        write(lambda db: db.execute(
            'INSERT INTO batches(id,drive_id,sha256,bytes,count,created_at,confirmed_at,summary_confirmed) VALUES (?,?,?,?,?,?,?,1)',
            ('synthetic-seed', 'synthetic-seed', '0'*64, 0, roster*count, now-86400, now-1)), 'confirmed_history_batch')
    for reader, email in enumerate(emails):
        # Keep fixture transactions below the same admission deadline as live
        # writes. Chunking changes setup only, never the history or measured load.
        for start in range(0, count, SEED_BATCH_ROWS):
            end = min(start+SEED_BATCH_ROWS, count)
            def history(db):
                last_seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
                for n in range(start, end):
                    event(db, 'video_progress', {'email': email, 'id': f'session{reader}'},
                          item_id=VIDEO, post_id=VIDEO, detail={'ranges': [[n*15, n*15+15]]},
                          timestamp=now-86400+n*15)
                # Fake-cloud export measures only new traffic, not prior history.
                db.execute("UPDATE events SET batch_id='synthetic-seed' WHERE seq>?", (last_seq,))
            write(history, 'history', reader=reader, start=start, end=end)
    return tokens


async def run(args):
    folder = ROOT/'.context/capacity'/f'{time.strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:8]}'
    folder.mkdir(parents=True, mode=0o700)
    store = FakeStore(folder/'cloud')
    roster = max(args.roster, args.users)
    store.write_allowlist([['email', 'name', 'role']] +
                         [[f'reader{i}@example.invalid', f'Reader {i}', 'admin' if i == 0 else 'reader'] for i in range(roster)])
    original_put = store.put_file
    def delayed_put(*values, **kwargs):
        time.sleep(args.cloud_delay)
        return original_put(*values, **kwargs)
    store.put_file = delayed_put
    config = Config(data_dir=folder/'data', mode='demo', preview=True, selfie_enabled=args.scenario == 'enrollment')
    service = Service(config, store=store)
    setup = {'status': 'running'}
    try:
        tokens = await asyncio.to_thread(seed, service, roster, args.history_hours,
                                        args.users if args.with_reader else None, setup)
        setup['status'] = 'completed'
    except BaseException as error:
        setup.update(status='failed', error=type(error).__name__)
        await asyncio.to_thread(service.db.close)
        raise
    finally:
        (folder/'setup.json').write_text(json.dumps(setup, indent=2)+'\n')
    metrics = Metrics()
    recording = False
    original_submit = service.db.submit
    def measured_submit(operation):
        try:
            caller = asyncio.current_task()
        except RuntimeError:
            caller = None
        owner = (caller.get_coro().__qualname__ if caller in service.tasks else 'request_or_other')
        measure = recording
        def measured_operation(db):
            before = time.thread_time()
            try:
                return operation(db)
            finally:
                if measure:
                    metrics.writer_operations[owner].append((time.thread_time()-before)*1000)
        future = original_submit(measured_operation)
        def completed(done):
            if measure and hasattr(done, 'commit_ms'):
                metrics.writer_queue[owner].append(done.queue_ms)
                metrics.writer_commit[owner].append(done.commit_ms)
        future.add_done_callback(completed)
        return future
    service.db.submit = measured_submit
    expected_progress = Counter()
    bad_video_bodies = 0
    range_digest = hashlib.sha256(bytes(range(256))*(CHUNK//256)).digest()
    original_write = service.write
    async def measured_write(operation, timings=None):
        timing = timings if timings is not None else {}
        try:
            return await original_write(operation, timing)
        finally:
            if recording and 'audit_ms' in timing:
                metrics.audit.append(timing['audit_ms'])
                metrics.queue.append(timing['queue_ms'])
                metrics.commit.append(timing['commit_ms'])
    service.write = measured_write
    original_start = service.start
    lifecycle_task = None
    async def tracked_start():
        nonlocal lifecycle_task
        lifecycle_task = asyncio.current_task()
        await original_start()
    service.start = tracked_start
    app = create_app(config, service=service)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    task = sampler = None
    started = time.monotonic()
    aborted = None
    process = psutil.Process()
    rss_after_setup = process.memory_info().rss/1024**2
    metrics.rss.append(rss_after_setup)
    psutil.cpu_percent()
    stop = asyncio.Event()
    startup = None
    guard_phase = 'startup'
    failure = None
    async def serve():
        try:
            await server.serve(sockets=[sock])
        except SystemExit as error:
            # Uvicorn's startup refusal must not terminate the event loop before
            # the harness can preserve evidence and close its owned resources.
            raise RuntimeError(f'Isolated server exited during startup ({error.code})') from error
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2) as guard:
            try:
                live = await guard.get('http://127.0.0.1:8790/health')
                guard_live = live.status_code == 200 and live.json().get('process') == 'up'
            except (httpx.HTTPError, ValueError):
                guard_live = False
        async def sample():
            nonlocal aborted
            next_health = time.monotonic()+5
            busy_samples = 0
            previous_processes = None
            while not stop.is_set():
                before = time.monotonic()
                await asyncio.sleep(.5)
                metrics.lag.append(max(0, time.monotonic()-before-.5)*1000)
                rss = process.memory_info().rss/1024**2
                metrics.rss.append(rss)
                idle = 100-psutil.cpu_percent()
                metrics.host_idle.append(idle)
                busy_samples = busy_samples+1 if idle < 30 else 0
                if idle < 30:
                    observed_at = time.monotonic()
                    try:
                        current_processes = process_cpu_snapshot()
                        if previous_processes is not None:
                            previous_at, previous_snapshot = previous_processes
                            metrics.host_cpu_diagnostics.append({
                                'elapsed_seconds': round(observed_at-started, 3),
                                'phase': guard_phase,
                                'host_cpu_percent': round(100-idle, 2),
                                'interval_seconds': round(observed_at-previous_at, 3),
                                'active_requests': {k: v for k, v in metrics.active.items() if v},
                                'processes': process_cpu_changes(previous_snapshot, current_processes, observed_at-previous_at),
                                'snapshot_ms': round((time.monotonic()-observed_at)*1000, 3),
                            })
                        previous_processes = observed_at, current_processes
                    except psutil.Error as error:
                        metrics.host_cpu_diagnostics.append({'unavailable': type(error).__name__})
                        previous_processes = None
                    metrics.host_cpu_diagnostics[:] = metrics.host_cpu_diagnostics[-10:]
                else:
                    previous_processes = None
                metrics.queue_depth.append(service.db.queue.qsize())
                if rss > 1024 or psutil.virtual_memory().available < 1024**3:
                    aborted = 'memory safety threshold'
                    stop.set()
                if busy_samples >= 10:
                    aborted = 'whole-host CPU reserve below 30% for five seconds'
                    stop.set()
                if time.monotonic() >= next_health:
                    if server.started:
                        async with httpx.AsyncClient(trust_env=False, timeout=2) as isolated:
                            probe = await metrics.request(isolated, 'health', 'GET', f'http://127.0.0.1:{port}/health')
                            if probe is None or probe.status_code != 200 or health_payload(probe) != HEALTH:
                                aborted = 'isolated health check failed; rehearsal stopped'
                                stop.set()
                    next_health = time.monotonic()+5
                    if not guard_live:
                        continue
                    async with httpx.AsyncClient(trust_env=False, timeout=2) as guard:
                        try:
                            live = await guard.get('http://127.0.0.1:8790/health')
                            state = live.json()
                            healthy = live.status_code == 200 and all(state.get(key) == value for key, value in
                                {'process': 'up', 'permissions': 'ready', 'audit': 'ready', 'storage': 'ready'}.items())
                        except (httpx.HTTPError, ValueError):
                            healthy = False
                    if not healthy:
                        aborted = 'live local health check failed; rehearsal stopped'
                        stop.set()
        startup_started = started = time.monotonic()
        startup_cpu, startup_main_cpu = time.process_time(), time.thread_time()
        if rss_after_setup > 1024 or psutil.virtual_memory().available < 1024**3:
            aborted = 'memory safety threshold'
        if not aborted:
            task = asyncio.create_task(serve())
            sampler = asyncio.create_task(sample())
        # The same resource and live-health guards cover normal service startup,
        # including native checkpoint priming. No benchmark-only warm request.
        while time.monotonic()-startup_started < 10 and not server.started and not aborted:
            if task.done():
                await task
                raise RuntimeError('Isolated server stopped during startup')
            await asyncio.sleep(.05)
        metrics.rss.append(process.memory_info().rss/1024**2)
        startup = {'elapsed_seconds': time.monotonic()-startup_started,
                   'ready': server.started,
                   'process_cpu_seconds': time.process_time()-startup_cpu,
                   'main_thread_cpu_seconds': time.thread_time()-startup_main_cpu,
                   'peak_rss_mib': max(metrics.rss)}
        if not server.started or aborted or startup['elapsed_seconds'] > 10:
            raise RuntimeError(aborted or 'Isolated server startup timed out')
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=15,
                                     trust_env=False, limits=httpx.Limits(max_connections=args.users+5,
                                                                         keepalive_expiry=4)) as client:
            started = time.monotonic()
            cpu_started, main_cpu_started = time.process_time(), time.thread_time()
            recording = True
            guard_phase = 'workload'
            if args.scenario == 'signin':
                await asyncio.gather(*(metrics.request(client, 'signin', 'POST', '/api/auth/demo',
                    json={'email': f'reader{i}@example.invalid', 'name': f'Reader {i}'}) for i in range(args.users)))
            elif args.scenario == 'enrollment':
                viewer_stop = asyncio.Event()
                async def existing_reader():
                    nonlocal bad_video_bodies
                    headers = {'Authorization': 'Bearer '+tokens[args.users]}
                    opened = await metrics.request(client, 'existing_reader_open', 'POST', f'/api/items/{VIDEO}/views',
                        headers=headers, json={'attempt_id': str(uuid.uuid4())})
                    if opened is None or opened.status_code != 200:
                        return
                    view_id = opened.json()['id']
                    player = await metrics.request(client, 'existing_reader_player', 'POST', f'/api/views/{view_id}/video', headers=headers)
                    if player is None or player.status_code != 200:
                        return
                    began, elapsed = time.monotonic(), 0.0
                    while not viewer_stop.is_set() and not stop.is_set():
                        response = await metrics.request(client, 'existing_reader_range', 'GET', player.json()['path'],
                            headers={'Range': 'bytes=0-65535'})
                        if response is not None and response.status_code == 206 and response.content != bytes(range(256))*256:
                            bad_video_bodies += 1
                        del response
                        now = time.monotonic()-began
                        if now-elapsed >= 15:
                            ranges = [[elapsed, now]]
                            saved = await metrics.request(client, 'existing_reader_progress', 'POST', f'/api/views/{view_id}/video-progress',
                                headers=headers, json={'ranges': ranges})
                            if saved is not None and saved.status_code == 200:
                                expected_progress[progress_key(f'reader{args.users}@example.invalid', ranges)] += 1
                            elapsed = now
                        await asyncio.sleep(.5)
                async def enroll(i):
                    headers = {'Authorization': 'Bearer '+tokens[i]}
                    submitted = time.monotonic()
                    response = await metrics.request(client, 'selfie_capture', 'POST', '/api/selfie', headers=headers,
                        files={'file': ('test-portrait.jpg', (ROOT/'tests/fixtures/selfie-face.jpg').read_bytes(), 'image/jpeg')},
                        data={'consent_version': 'selfie-1'}, timeout=args.seconds)
                    if response is None or response.status_code != 202:
                        return
                    while time.monotonic()-started < args.seconds and not stop.is_set():
                        state = await metrics.request(client, 'selfie_status', 'GET', '/api/selfie', headers=headers)
                        if state is not None and state.status_code == 200 and not state.json()['required']:
                            metrics.latencies['enrollment_ready'].append((time.monotonic()-submitted)*1000)
                            metrics.statuses['enrollment_ready']['confirmed'] += 1
                            return
                        await asyncio.sleep(1)
                    metrics.statuses['enrollment_ready']['unfinished'] += 1
                    metrics.latencies['enrollment_ready'].append((time.monotonic()-submitted)*1000)
                viewer = asyncio.create_task(existing_reader()) if args.with_reader else None
                try:
                    await asyncio.gather(*(enroll(i) for i in range(args.users)))
                    # Keep the already-enrolled reader active for the declared
                    # observation window even when all enrollments finish early.
                    while viewer and time.monotonic()-started < args.seconds and not stop.is_set():
                        await asyncio.sleep(.1)
                finally:
                    viewer_stop.set()
                    if viewer:
                        await viewer
            else:
                async def reader(i):
                    nonlocal bad_video_bodies
                    headers = {'Authorization': 'Bearer '+tokens[i]}
                    await asyncio.sleep(i/args.users*min(args.ramp_seconds, args.seconds/2))
                    views = {}
                    for identity in (VIDEO, PDF):
                        response = await metrics.request(client, 'open_view', 'POST', f'/api/items/{identity}/views',
                                                         headers=headers, json={'attempt_id': str(uuid.uuid4())})
                        if response is None or response.status_code != 200:
                            return
                        views[identity] = response.json()['id']
                    response = await metrics.request(client, 'player', 'POST', f'/api/views/{views[VIDEO]}/video', headers=headers)
                    if response is None or response.status_code != 200:
                        return
                    path = response.json()['path']
                    elapsed = 0
                    next_range = next_progress = next_catalog = next_page = 0.0
                    begin = time.monotonic()
                    while time.monotonic()-started < args.seconds and not stop.is_set():
                        now = time.monotonic()-begin
                        if now >= next_range:
                            body = await metrics.request(client, 'video_range', 'GET', path, headers={'Range': 'bytes=0-'})
                            if body is not None and body.status_code == 206 and (
                                hashlib.sha256(body.content).digest() != range_digest or
                                body.headers.get('content-range') != f'bytes 0-{CHUNK-1}/{CHUNK*2}'):
                                bad_video_bodies += 1
                            # Idle synthetic clients must not retain the last 4 MiB
                            # body in the same process whose serving RSS we sample.
                            del body
                            next_range += CHUNK*8/(args.video_mbps*1_000_000)
                        if now >= next_progress and now >= 15:
                            end = min(now, elapsed+30)
                            ranges = [[elapsed, end]]
                            saved = await metrics.request(client, 'progress', 'POST', f'/api/views/{views[VIDEO]}/video-progress',
                                                          headers=headers, json={'ranges': ranges})
                            if saved is not None and saved.status_code == 200:
                                expected_progress[progress_key(f'reader{i}@example.invalid', ranges)] += 1
                            elapsed = end
                            next_progress = now+15
                        if now >= next_catalog:
                            await metrics.request(client, 'catalog', 'GET', '/api/posts', headers=headers)
                            next_catalog = now+30
                        if i % 5 == 0 and now >= next_page:
                            await metrics.request(client, 'page', 'GET', f'/api/views/{views[PDF]}/page/0.jpg', headers=headers)
                            next_page = now+20
                        if i == 0 and now >= getattr(reader, 'next_dashboard', 10):
                            await metrics.request(client, 'dashboard', 'GET', '/api/admin/progress', headers=headers)
                            reader.next_dashboard = now+30
                        await asyncio.sleep(.1)
                await asyncio.gather(*(reader(i) for i in range(args.users)))
            health = await metrics.request(client, 'health', 'GET', '/health', timeout=2)
            health_body = health_payload(health)
            if health is None or health.status_code != 200 or health_body != HEALTH:
                aborted = aborted or 'final isolated health check failed; rehearsal stopped'
        elapsed = time.monotonic()-started
        cpu_seconds, main_cpu_seconds = time.process_time()-cpu_started, time.thread_time()-main_cpu_started
        recording = False
        guard_phase = 'verification'
        if args.scenario != 'mixed':
            verification = {'raw_python_dashboard_matches': None, 'elapsed_seconds': 0}
        elif aborted:
            verification = {'raw_python_dashboard_matches': False, 'elapsed_seconds': 0,
                            'skipped': 'workload aborted'}
        else:
            verification = await verify_dashboard(service, port, tokens[0], stop)
        stop.set()
        await sampler  # Include any probe already in flight before deciding success.
        recorded_progress = await asyncio.to_thread(service.db.read, lambda db: Counter(
            progress_key(row['email'], json.loads(row['payload'])['detail']['ranges'])
            for row in db.execute("SELECT email,payload FROM events WHERE type='video_progress' AND seq>?",
                                  (roster*int(args.history_hours*240),))))
        integrity = {'video_body_mismatches': bad_video_bodies,
                     'raw_python_dashboard_matches': verification['raw_python_dashboard_matches'],
                     'progress_matches_acknowledged_requests': recorded_progress == expected_progress,
                     'acknowledged_progress_records': sum(expected_progress.values()),
                     'persisted_progress_records': sum(recorded_progress.values())}
        no_errors = all(status in {'200', '202', '206', 'confirmed'}
                        for statuses in metrics.statuses.values() for status in statuses)
        coverage = workload_coverage(args, metrics.statuses, expected_progress)
        if args.scenario == 'signin':
            complete = metrics.statuses['signin']['200'] == args.users
        elif args.scenario == 'enrollment':
            complete = metrics.statuses['enrollment_ready']['confirmed'] == args.users
            if args.with_reader:
                complete = complete and metrics.statuses['existing_reader_range']['206'] > 0
        else:
            complete = (metrics.statuses['open_view']['200'] == args.users*2 and
                        metrics.statuses['player']['200'] == args.users and
                        metrics.statuses['video_range']['206'] >= args.users)
        functional_checks = (not aborted and no_errors and complete and coverage['passed'] and not bad_video_bodies and
                             integrity['progress_matches_acknowledged_requests'] and
                             integrity['raw_python_dashboard_matches'] is not False and
                             health_body == HEALTH)
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True,
                                  capture_output=True, check=False).stdout.strip() or None
        backend = progress_backend.selected()
        native_hash = None
        if backend == 'rust':
            # Maturin's package __file__ is a Python shim, not the reducer binary.
            extension = sys.modules[progress_backend.native().reduce.__module__]
            binary = Path(extension.__file__)
            if not any(str(binary).endswith(suffix) for suffix in EXTENSION_SUFFIXES):
                raise RuntimeError('Native reducer provenance did not resolve to an extension binary')
            native_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
        runtime = {'backend': backend, 'revision': revision, 'python': platform.python_version(),
                   'platform': platform.platform(), 'machine': platform.machine(),
                   'logical_cpus': psutil.cpu_count(),
                   'native_sha256': native_hash}
        report = {'scenario': vars(args), 'elapsed_seconds': round(elapsed, 3),
                  'runtime': runtime, 'startup': startup, 'verification': verification,
                  'comparable_process_cpu_seconds': startup['process_cpu_seconds']+cpu_seconds,
                  'first_dashboard_ms': metrics.latencies['dashboard'][0] if metrics.latencies['dashboard'] else None,
                  'progress_cache': service.progress_history.diagnostics() if service.progress_history else None,
                  'process_cpu_seconds': cpu_seconds, 'main_thread_cpu_seconds': main_cpu_seconds,
                  'process_rss_after_setup_mib': round(rss_after_setup, 3),
                  'other_threads_cpu_seconds': max(0, cpu_seconds-main_cpu_seconds),
                  'roster': roster, 'seeded_history_events': roster*int(args.history_hours*240),
                  'aborted': aborted, 'health': health_body, 'integrity': integrity,
                  'workload_coverage': coverage,
                  'functional_checks_passed': functional_checks,
                  'live_health_guard_enabled': guard_live, **metrics.report(),
                  'limitations': ['Loopback HTTP; client and server share one process and event loop.',
                                  'Workload CPU and startup CPU are reported separately; comparable CPU adds both. Fixture seeding, post-workload correctness verification, shutdown and converter subprocesses are excluded. Sampled RSS covers startup, workload and verification and includes prior fixture allocation.',
                                  'Host CPU diagnostics retain the last ten busy intervals and five busiest visible processes per interval; short-lived/inaccessible processes may be missing. One-core percentages are not whole-host percentages.',
                                  'Writer operation CPU excludes SQLite commit; background caller labels identify task ownership, not exclusive job CPU.',
                                  'Synthetic sessions; selfies disabled for mixed workload, real sanitizer/face check with a public-domain test portrait in enrollment.',
                                  'Fake cloud with configured artificial delay; no actual Google authentication or quotas measured.',
                                  'Opaque byte fixture, not a playable video; no browser buffering measurement.',
                                  'No WAN/tunnel throughput or production capacity certification.']}
        (folder/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps({'report': str(folder/'report.json'), 'aborted': aborted,
                          'functional_checks_passed': functional_checks,
                          'workload_coverage': coverage,
                          'requests': report['requests'], 'audit_ms': report['audit_ms'],
                          'event_loop_lag_ms': report['event_loop_lag_ms']}, indent=2))
        return functional_checks
    except BaseException as error:
        failure = error
        if guard_phase == 'startup':
            (folder/'startup-failure.json').write_text(json.dumps({
                'functional_checks_passed': False, 'error': type(error).__name__,
                'aborted': aborted, 'startup': startup,
                'resources': metrics.report()}, indent=2)+'\n')
        raise
    finally:
        if sampler:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
        server.should_exit = True
        try:
            if not server.started and lifecycle_task is not None and not lifecycle_task.done():
                lifecycle_task.cancel()
                await asyncio.gather(lifecycle_task, return_exceptions=True)
            if task is not None:
                result, = await asyncio.gather(task, return_exceptions=True)
                if failure is None and isinstance(result, BaseException):
                    raise result
            else:
                await service.close()
        finally:
            sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=['mixed', 'signin', 'enrollment'], default='mixed')
    parser.add_argument('--users', type=int, default=25)
    parser.add_argument('--roster', type=int, default=228)
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--history-hours', type=float, default=0)
    parser.add_argument('--ramp-seconds', type=float, default=10)
    parser.add_argument('--video-mbps', type=float, default=1.5)
    parser.add_argument('--cloud-delay', type=float, default=0)
    parser.add_argument('--with-reader', action='store_true', help='Exercise one already-enrolled reader during enrollment (needs a spare roster account).')
    args = parser.parse_args()
    if args.with_reader and (args.scenario != 'enrollment' or args.roster <= args.users):
        parser.error('--with-reader requires enrollment and a roster larger than the enrolling group.')
    if not (1 <= args.users <= 228 and 1 <= args.roster <= 228 and 10 <= args.seconds <= 7200
            and 0 <= args.history_hours <= 9 and 0 <= args.ramp_seconds <= 300 and .1 <= args.video_mbps <= 5
            and 0 <= args.cloud_delay <= 2):
        parser.error('Use 1–228 users/roster, 10–7200 seconds, 0–9 history hours, 0–300 ramp seconds, .1–5 Mbps.')
    if not asyncio.run(run(args)):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
