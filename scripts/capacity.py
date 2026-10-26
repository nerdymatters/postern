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
import json
import math
from pathlib import Path
import socket
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

ROOT = Path(__file__).resolve().parents[1]
VIDEO, PDF = 'capacity-video', 'capacity-pdf'


def progress_key(email, ranges):
    # The API normalizes 0.0 to 0 at the beginning of an interval. Compare
    # numeric endpoints, not their JSON spelling, while retaining exact values.
    return email, tuple((float(a), float(b)) for a, b in ranges)


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {'count': 0}
    return {'count': len(ordered), 'min': round(ordered[0], 3),
            'mean': round(sum(ordered)/len(ordered), 3), **{
        name: round(ordered[max(0, math.ceil(len(ordered)*fraction)-1)], 3)
        for name, fraction in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]}}


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

    async def request(self, client, label, method, path, **kwargs):
        started = time.monotonic()
        try:
            response = await client.request(method, path, **kwargs)
            self.statuses[label][str(response.status_code)] += 1
            self.bytes += len(response.content)
            return response
        except httpx.HTTPError:
            self.statuses[label]['transport_error'] += 1
            return None
        finally:
            self.latencies[label].append((time.monotonic()-started)*1000)

    def report(self):
        return {'requests': {label: {'latency_ms': distribution(values),
                                    'statuses': dict(self.statuses[label])}
                             for label, values in self.latencies.items()},
                'body_bytes': self.bytes, 'audit_ms': distribution(self.audit),
                'queue_ms': distribution(self.queue), 'commit_ms': distribution(self.commit),
                'event_loop_lag_ms': distribution(self.lag), 'process_rss_mib': distribution(self.rss),
                'whole_host_idle_percent': distribution(self.host_idle),
                'writer_queue_depth': distribution(self.queue_depth)}


def seed(service, roster, history_hours, existing_reader=None):
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
    service.db.submit(initial).result(10)
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
        service.db.submit(existing).result(10)
    # One synthetic 12-hour timeline. Nine hours corresponds to 2,160 events per reader.
    count = int(history_hours*240)
    for reader, email in enumerate(emails):
        def history(db):
            last_seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            for n in range(count):
                event(db, 'video_progress', {'email': email, 'id': f'session{reader}'},
                      item_id=VIDEO, post_id=VIDEO, detail={'ranges': [[n*15, n*15+15]]},
                      timestamp=now-86400+n*15)
            # Mark seeded history as already exported so fake-cloud export measures only new traffic.
            db.execute("UPDATE events SET batch_id='synthetic-seed' WHERE seq>?", (last_seq,))
        if count:
            service.db.submit(history).result(10)
    if count:
        # Health joins events to batches. A dangling seed batch would incorrectly
        # label all historical fixtures as an overdue backup, despite new exports
        # succeeding. This is synthetic prior-confirmation metadata, not a recovery test.
        service.db.submit(lambda db: db.execute(
            'INSERT INTO batches(id,drive_id,sha256,bytes,count,created_at,confirmed_at,summary_confirmed) VALUES (?,?,?,?,?,?,?,1)',
            ('synthetic-seed', 'synthetic-seed', '0'*64, 0, roster*count, now-86400, now-1))).result(10)
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
    try:
        tokens = await asyncio.to_thread(seed, service, roster, args.history_hours, args.users if args.with_reader else None)
    except BaseException:
        await asyncio.to_thread(service.db.close)
        raise
    metrics = Metrics()
    expected_progress = Counter()
    bad_video_bodies = 0
    range_digest = hashlib.sha256(bytes(range(256))*(CHUNK//256)).digest()
    original_write = service.write
    async def measured_write(operation, timings=None):
        timing = timings if timings is not None else {}
        try:
            return await original_write(operation, timing)
        finally:
            if 'audit_ms' in timing:
                metrics.audit.append(timing['audit_ms'])
                metrics.queue.append(timing['queue_ms'])
                metrics.commit.append(timing['commit_ms'])
    service.write = measured_write
    app = create_app(config, service=service)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    sampler = None
    started = time.monotonic()
    aborted = None
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError('Isolated server stopped during startup')
            await asyncio.sleep(.05)
        if not server.started:
            raise RuntimeError('Isolated server startup timed out')
        process = psutil.Process()
        psutil.cpu_percent()
        stop = asyncio.Event()
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
            while not stop.is_set():
                before = time.monotonic()
                await asyncio.sleep(.5)
                metrics.lag.append(max(0, time.monotonic()-before-.5)*1000)
                rss = process.memory_info().rss/1024**2
                metrics.rss.append(rss)
                idle = 100-psutil.cpu_percent()
                metrics.host_idle.append(idle)
                busy_samples = busy_samples+1 if idle < 30 else 0
                metrics.queue_depth.append(service.db.queue.qsize())
                if rss > 1024 or psutil.virtual_memory().available < 1024**3:
                    aborted = 'memory safety threshold'
                    stop.set()
                if busy_samples >= 10:
                    aborted = 'whole-host CPU reserve below 30% for five seconds'
                    stop.set()
                if guard_live and time.monotonic() >= next_health:
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
                    next_health = time.monotonic()+5
        sampler = asyncio.create_task(sample())
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=15,
                                     trust_env=False, limits=httpx.Limits(max_connections=args.users+5)) as client:
            started = time.monotonic()
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
            health = await client.get('/health')
            health_body = health.json()
        elapsed = time.monotonic()-started
        stop.set()
        recorded_progress = await asyncio.to_thread(service.db.read, lambda db: Counter(
            progress_key(row['email'], json.loads(row['payload'])['detail']['ranges'])
            for row in db.execute("SELECT email,payload FROM events WHERE type='video_progress' AND seq>?",
                                  (roster*int(args.history_hours*240),))))
        integrity = {'video_body_mismatches': bad_video_bodies,
                     'progress_matches_acknowledged_requests': recorded_progress == expected_progress,
                     'acknowledged_progress_records': sum(expected_progress.values()),
                     'persisted_progress_records': sum(recorded_progress.values())}
        no_errors = all(status in {'200', '202', '206', 'confirmed'}
                        for statuses in metrics.statuses.values() for status in statuses)
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
        functional_checks = (not aborted and no_errors and complete and not bad_video_bodies and
                             integrity['progress_matches_acknowledged_requests'] and
                             health_body == {'process': 'up', 'permissions': 'ready', 'audit': 'ready',
                                             'backup': 'current', 'storage': 'ready'})
        report = {'scenario': vars(args), 'elapsed_seconds': round(elapsed, 3),
                  'roster': roster, 'seeded_history_events': roster*int(args.history_hours*240),
                  'aborted': aborted, 'health': health_body, 'integrity': integrity,
                  'functional_checks_passed': functional_checks,
                  'live_health_guard_enabled': guard_live, **metrics.report(),
                  'limitations': ['Loopback HTTP; client and server share one process and event loop.',
                                  'Synthetic sessions; selfies disabled for mixed workload, real sanitizer/face check with a public-domain test portrait in enrollment.',
                                  'Fake cloud with configured artificial delay; no actual Google authentication or quotas measured.',
                                  'Opaque byte fixture, not a playable video; no browser buffering measurement.',
                                  'No WAN/tunnel throughput or production capacity certification.']}
        (folder/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps({'report': str(folder/'report.json'), 'aborted': aborted,
                          'functional_checks_passed': functional_checks,
                          'requests': report['requests'], 'audit_ms': report['audit_ms'],
                          'event_loop_lag_ms': report['event_loop_lag_ms']}, indent=2))
        return functional_checks
    finally:
        if sampler:
            sampler.cancel()
            await asyncio.gather(sampler, return_exceptions=True)
        server.should_exit = True
        await task
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
