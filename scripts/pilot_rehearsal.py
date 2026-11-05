"""Disposable loopback pilot: real HTTP, synthetic data, sequential Python/Rust rollback.

No host/config/credential arguments and no production jobs. Every run needs a new
directory. Child services bind an ephemeral loopback socket and hold an exclusive
lock on that directory. This is a functional rehearsal, not a capacity benchmark.
"""
from __future__ import annotations

if not __debug__:
    raise SystemExit('Pilot rehearsal requires assertions; run without -O or PYTHONOPTIMIZE.')

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, closing
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

import httpx
from PIL import Image
import uvicorn

from postern.app import create_app
from postern.config import Config, private_json
from postern.service import Service
from postern.storage import FakeStore
from postern import health_trace, progress_backend

SOURCE = Path(__file__).resolve().parents[1]
MARKER = {'format': 'postern-disposable-pilot-v1', 'synthetic_only': True}
READY = {'process': 'up', 'permissions': 'ready', 'audit': 'ready', 'backup': 'current', 'storage': 'ready'}
READER, ADMIN = 'reader@example.invalid', 'admin@example.invalid'


def initialize(root):
    root.mkdir(mode=0o700)  # Refuse reuse, including an existing production directory.
    private_json(root / 'pilot.json', MARKER)


def validate(root):
    if root.is_symlink() or json.loads((root / 'pilot.json').read_text()) != MARKER:
        raise ValueError('Not a disposable pilot directory')
    # None of the fixed paths may redirect a child service outside its fixture.
    for name in ('data', 'cloud', 'writer.lock'):
        if (root / name).is_symlink():
            raise ValueError('Pilot paths must not be symlinks')


def seed(service):
    fixture = SOURCE / 'tests/fixtures/page.jpg'
    for chapter in (1, 2):
        identity = f'pilot-chapter-{chapter}'
        directory = service.config.data_dir / 'derived' / identity
        directory.mkdir()
        for page in (0, 1):
            (directory / f'page-{page}.jpg').write_bytes(fixture.read_bytes())
        (directory / 'thumb.jpg').write_bytes(fixture.read_bytes())
        (directory / 'manifest.json').write_text(json.dumps({'pages': 2, 'bytes': fixture.stat().st_size * 2}))
        def insert(db):
            db.execute("INSERT INTO posts(id,title,body,category,status,created_at,created_by,published_at) "
                       "VALUES (?,?,'Synthetic fixture',?,'published',1,?,2)",
                       (identity, f'Pilot chapter {chapter}', f'SOP Chapter {chapter}: Synthetic', ADMIN))
            db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) "
                       "VALUES (?,?,'synthetic.pdf','pdf',?,1,2,'ready',0)",
                       (identity, identity, hashlib.sha256(fixture.read_bytes()).hexdigest()))
        service.db.submit(insert).result(5)


async def child(root, backend, ready, fault):
    validate(root)
    os.environ['POSTERN_PROGRESS_BACKEND'] = backend
    os.environ['POSTERN_HEALTH_TRACE'] = '1'
    with (root / 'writer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = FakeStore(root / 'cloud')
        config = Config(root / 'data', mode='demo', preview=True, origins=['http://127.0.0.1:5173'], converter_workers=1)
        service = Service(config, store=store)
        try:
            if not (root / 'seeded').exists():
                seed(service)
                (root / 'seeded').write_text('synthetic\n')
            if fault:
                # Faults exist only in this disposable harness, never in app configuration/routes.
                original = service.health
                fired = False
                if fault == 'queue':
                    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
                if fault == 'database':
                    read = service.db.read
                    def slow_read(operation):
                        if health_trace.CURRENT.get() is not None:
                            time.sleep(2.2)
                        return read(operation)
                    service.db.read = slow_read
                async def delayed(*args, **kwargs):
                    nonlocal fired
                    if not fired:
                        fired = True
                        if fault == 'queue':
                            asyncio.get_running_loop().run_in_executor(None, time.sleep, 2.2)
                        elif fault == 'loop':
                            asyncio.get_running_loop().call_soon(time.sleep, 2.2)
                    return await original(*args, **kwargs)
                service.health = delayed
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            if port == 8790:
                raise RuntimeError('Reserved production port')
            server = uvicorn.Server(uvicorn.Config(create_app(config, service=service), log_level='error', access_log=False))
            # Written before serve; the parent also waits for HEAD /health == 405.
            private_json(ready, {'pid': os.getpid(), 'port': port, 'backend': progress_backend.selected()})
            async def expire():
                await asyncio.sleep(120)
                server.should_exit = True
            expiry = asyncio.create_task(expire())
            try:
                await server.serve(sockets=[sock])
            finally:
                expiry.cancel()
                await asyncio.gather(expiry, return_exceptions=True)
                sock.close()
        finally:
            # Lifespan normally owns close; a pre-start failure still releases the writer.
            if service.db.thread.is_alive():
                await service.close()


@contextmanager
def serving(root, backend, label, fault=None):
    ready = root / f'{label}-ready.json'
    args = [sys.executable, '-m', 'scripts.pilot_rehearsal', '--child', str(root),
            '--backend', backend, '--ready', str(ready)]
    if fault:
        args += ['--fault', fault]
    with (root / f'{label}-server.log').open('w') as log:
        process = subprocess.Popen(args, cwd=SOURCE, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f'{label} service exited; inspect its local log')
                if ready.exists():
                    info = json.loads(ready.read_text())
                    assert info['pid'] == process.pid and info['backend'] == backend and info['port'] != 8790
                    url = f"http://127.0.0.1:{info['port']}"
                    client = httpx.Client(base_url=url, trust_env=False, timeout=2)
                    try:
                        try:
                            available = client.head('/health').status_code == 405
                        except httpx.ConnectError:
                            available = False
                        if available:
                            yield client, info
                            return
                    finally:
                        client.close()
                time.sleep(.05)
            raise RuntimeError('Pilot service startup deadline')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)
            assert process.poll() is not None  # no new process until the previous writer exits


def check(response, status=200):
    if response.status_code != status:
        raise AssertionError(f'Expected HTTP {status}, got {response.status_code}')
    return response


def signin(client, email):
    token = check(client.post('/api/auth/demo', json={'email': email})).json()['token']
    headers = {'Authorization': 'Bearer ' + token, 'X-Postern-Client': 'web'}
    check(client.post('/api/consent', headers=headers, json={
        'version': '2', 'ip_version': 'ip-1', 'monitoring_accepted': True, 'ip_accepted': True}))
    return headers


def page(client, headers, chapter, number):
    view = check(client.post(f'/api/items/pilot-chapter-{chapter}/views', headers=headers,
                             json={'attempt_id': str(uuid.uuid4())})).json()
    response = check(client.get(f"/api/views/{view['id']}/page/{number}.jpg", headers=headers))
    assert response.headers['content-type'] == 'image/jpeg'
    with Image.open(io.BytesIO(response.content)) as image:
        image.verify()


def state(client, headers):
    result = check(client.get('/api/posts', headers=headers)).json()
    return {'progress': {p['id']: p['items'][0]['progress'] for p in result['posts']},
            'quizzes': result['quizzes'], 'certificate': result['certificate']}


def snapshot(root, upper=None):
    with closing(sqlite3.connect((root / 'data/postern.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        upper = upper if upper is not None else db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        rows = db.execute('SELECT seq,id,ts,type,email,post_id,item_id,view_id,summary,payload FROM events WHERE seq<=? ORDER BY seq', (upper,)).fetchall()
        return {'upper': upper, 'count': len(rows), 'sha256': hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
                'schema': db.execute('PRAGMA schema_version').fetchone()[0],
                'pages': sum(row[3] == 'page_served' for row in rows),
                'quiz_attempts': sum(row[3] == 'quiz_attempt' for row in rows)}


def rehearsal(root):
    initialize(root)
    report = {'status': 'running', 'synthetic_only': True, 'phases': [], 'source': str(SOURCE)}
    previous = saved = None
    try:
        for phase, backend in enumerate(('python', 'rust', 'python')):
            label = f'{phase}-{backend}'
            with serving(root, backend, label) as (client, info):
                if previous:
                    assert snapshot(root, previous['upper']) == previous
                assert check(client.get('/health')).json() == READY
                check(client.get('/api/posts'), 401)
                if phase == 0:
                    reader, admin = signin(client, READER), signin(client, ADMIN)
                check(client.get('/api/admin/progress', headers=reader), 403)
                if phase < 2:
                    assert check(client.get('/api/quizzes/1', headers=reader)).json()['ready'] is False
                    check(client.get('/api/quizzes/2', headers=reader), 423)
                    page(client, reader, 1, phase)
                if phase == 0:
                    assert check(client.get('/api/quizzes/1', headers=reader)).json()['ready'] is False
                elif phase == 1:
                    dealt = check(client.get('/api/quizzes/1', headers=reader)).json()
                    assert dealt['ready'] is True
                    bank = json.loads((SOURCE / 'postern/sample_training/quizzes.json').read_text())['chapters'][0]
                    answers = {q['id']: q['answer'] for q in bank['questions']}
                    attempt = check(client.post('/api/quizzes/1/attempts', headers=reader, json={
                        'version': dealt['version'], 'answers': {q['id']: answers[q['id']] for q in dealt['questions']}})).json()
                    assert attempt['passed'] is True and attempt['score'] == attempt['total']
                    assert attempt['next_chapter']['unlocked'] is True
                    page(client, reader, 2, 0)
                current = state(client, reader)
                if phase == 2:
                    assert current == saved
                    assert check(client.get('/api/quizzes/2', headers=reader)).json()['ready'] is False
                elif phase == 1:
                    saved = current
                staff = check(client.get('/api/admin/progress', headers=admin)).json()
                assert len(staff['people']) == 2
                assert check(client.get('/health')).json() == READY
                report['phases'].append({'phase': label, 'pid': info['pid'], 'backend': backend,
                                         'progress_and_locks_verified': True, 'staff_report_verified': True})
            previous = snapshot(root)
        assert previous['pages'] == 3 and previous['quiz_attempts'] == 1
        assert len({p['pid'] for p in report['phases']}) == 3
        report.update(status='passed', durable=previous, rollback_output_parity=True, all_children_stopped=True)
    except BaseException as error:
        report.update(status='failed', error_type=type(error).__name__)
        raise
    finally:
        report['scope'] = 'Loopback HTTP with synthetic pre-derived images and sample quizzes; no converter, Google, selfie or production capacity claim.'
        private_json(root / 'report.json', report)
    return report


def fault_probes(root):
    """Prove tracing sensitivity, not the cause of any production incident."""
    initialize(root)
    report = {'status': 'running', 'synthetic_only': True, 'probes': [],
              'scope': 'Forced 2.2-second stalls; client health deadline stays two seconds.'}
    try:
        for fault in ('queue', 'database', 'loop'):
            directory = root / fault
            initialize(directory)
            with serving(directory, 'python', fault, fault=fault) as (client, info):
                def request(method):
                    started = time.monotonic()
                    try:
                        response = client.request(method, '/health')
                        return {'status': response.status_code, 'elapsed_ms': (time.monotonic() - started) * 1000}
                    except httpx.TimeoutException:
                        return {'timeout': True, 'elapsed_ms': (time.monotonic() - started) * 1000}
                path = directory / 'data/health-diagnostics/health.jsonl'
                with ThreadPoolExecutor(max_workers=2) as pool:
                    health = pool.submit(request, 'GET')
                    deadline = time.monotonic() + 1
                    while not path.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    assert path.exists(), 'No partial health trace before timeout'
                    control = pool.submit(request, 'HEAD')
                    observed, lightweight = health.result(5), control.result(5)
                assert observed.get('timeout'), 'Injected stall did not exceed the unchanged client deadline'
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    records = [json.loads(line) for line in path.read_text().splitlines() if line.endswith('}')]
                    completed = [r for r in records if r['stage'] == 'request_finished']
                    if completed:
                        break
                    time.sleep(.02)
                assert completed, 'No completed trace after the injected stall ended'
                final = completed[-1]
                stages = final['stages_ms']
                durations = {'queue': stages['worker_started'] - stages['submitted'],
                             'database': stages['query_started'] - stages['worker_started'],
                             'loop': stages['loop_resumed'] - stages['worker_finished']}
                assert durations[fault] >= 2000, 'Trace did not identify the injected stage'
                if fault != 'loop':
                    assert lightweight.get('status') == 405
                else:
                    assert final['loop_lag_max_ms'] >= 2000
                report['probes'].append({'fault': fault, 'health': observed, 'control': lightweight,
                                         'stages_ms': stages, 'loop_lag_max_ms': final['loop_lag_max_ms'],
                                         'identified_stage': max(durations, key=durations.get)})
        report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', error_type=type(error).__name__)
        raise
    finally:
        private_json(root / 'report.json', report)
    return report


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output', type=Path, help='New disposable evidence directory')
    group.add_argument('--fault-probes', type=Path, help='New directory for controlled health-delay probes')
    group.add_argument('--child', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--backend', choices=('python', 'rust'), default='python', help=argparse.SUPPRESS)
    parser.add_argument('--ready', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--fault', choices=('queue', 'database', 'loop'), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        root = args.child.absolute()
        if args.ready is None or args.ready.absolute().parent != root:
            parser.error('Child readiness output must be in its disposable directory')
        asyncio.run(child(root, args.backend, args.ready.absolute(), args.fault))
    elif args.fault_probes:
        print(json.dumps(fault_probes(args.fault_probes.absolute()), indent=2))
    else:
        print(json.dumps(rehearsal(args.output.absolute()), indent=2))


if __name__ == '__main__':
    main()
