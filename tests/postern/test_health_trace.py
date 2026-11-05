"""Controlled scheduling/SQL failures must be distinguishable without changing health."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from postern.app import create_app
from postern import health_trace
from postern.service import Service


class Capture:
    backend = 'python'

    def __init__(self):
        self.identities = itertools.count(1)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def acquire_trace(self):
        return True

    def release_trace(self):
        pass


def test_http_trace_is_opt_in_private_bounded_and_response_compatible(config, store, clock, monkeypatch):
    monkeypatch.delenv('POSTERN_HEALTH_TRACE', raising=False)
    service = Service(config, store, clock)
    with TestClient(create_app(config, service=service)) as client:
        expected = client.get('/health').json()
        assert service.health_recorder is None
    assert not (config.data_dir / 'health-diagnostics').exists()

    monkeypatch.setenv('POSTERN_HEALTH_TRACE', '1')
    service = Service(config, store, clock)
    with TestClient(create_app(config, service=service)) as client:
        response = client.get('/health?secret=never-record-this', headers={'Authorization': 'Bearer never-record-this'})
        assert response.status_code == 200 and response.json() == expected
        assert 'trace' not in str(response.headers).lower()
        assert client.head('/health').status_code == 405
        assert client.get('/api/config').status_code == 200
    path = config.data_dir / 'health-diagnostics/health.jsonl'
    text = path.read_text()
    assert 'never-record-this' not in text and 'Bearer' not in text
    records = [json.loads(line) for line in text.splitlines()]
    assert {row['request'] for row in records} == {1}  # only GET /health
    final = records[-1]
    assert final['stage'] == 'request_finished'
    assert set(final['stages_ms']) == health_trace.STAGES - {'database_error'}
    assert list(final['stages_ms'].values()) == sorted(final['stages_ms'].values())
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert set(final) == {'schema', 'pid', 'backend', 'request', 'started_at', 'stages_ms',
                          'loop_lag_max_ms', 'stage', 'dropped_records', 'skipped_traces'}
    assert not service.health_recorder.thread.is_alive()


# Value: protects=diagnostic construction failures preserve the public health response;
# fails_when=Recorder or Trace setup exceptions escape into service startup or GET /health;
# why_new=existing sink-failure coverage starts after diagnostic construction;
# seam=none; no_claim=catastrophic allocation failures or production availability.
@pytest.mark.parametrize('failure', ['recorder', 'trace'])
def test_diagnostic_construction_failure_preserves_http_health(config, store, clock, monkeypatch, failure):
    monkeypatch.delenv('POSTERN_HEALTH_TRACE', raising=False)
    with TestClient(create_app(config, service=Service(config, store, clock))) as client:
        expected = client.get('/health')
        assert expected.status_code == 200

    attempts = []
    def unavailable(*args, **kwargs):
        attempts.append(failure)
        raise OSError('Diagnostic setup unavailable')

    monkeypatch.setenv('POSTERN_HEALTH_TRACE', '1')
    if failure == 'recorder':
        monkeypatch.setattr(health_trace.Recorder, '__init__', unavailable)
    else:
        monkeypatch.setattr(health_trace, 'Trace', unavailable)
    service = Service(config, store, clock)
    with TestClient(create_app(config, service=service)) as client:
        response = client.get('/health')
        assert response.status_code == expected.status_code
        assert response.json() == expected.json()
        assert response.headers == expected.headers
    assert attempts == [failure]


# Value: protects=overlapping health requests retain independent partial and completed traces;
# fails_when=shared trace state or context propagation mixes one request's stages into another;
# why_new=existing timing tests exercise only one health request at a time;
# seam=none; no_claim=production concurrency capacity or diagnostic filesystem throughput.
def test_overlapping_http_health_requests_keep_independent_traces(config, store, clock, monkeypatch):
    service = Service(config, store, clock)
    capture = Capture()
    capture.close = lambda: None
    service.health_recorder = capture
    entered, release = threading.Event(), threading.Event()
    selection_lock = threading.Lock()
    selected = False
    original = service.db.read

    def block_first_health_read(operation):
        nonlocal selected
        block = False
        if health_trace.CURRENT.get() is not None:
            with selection_lock:
                if not selected:
                    selected = block = True
        if block:
            entered.set()
            assert release.wait(5)
        return original(operation)

    monkeypatch.setattr(service.db, 'read', block_first_health_read)
    expected_stages = health_trace.STAGES - {'database_error'}
    with TestClient(create_app(config, service=service)) as client:
        with ThreadPoolExecutor(max_workers=2) as requests:
            first = requests.submit(client.get, '/health')
            try:
                assert entered.wait(5)
                second_response = requests.submit(client.get, '/health').result(5)
                assert second_response.status_code == 200
                finished = [row for row in capture.records if row['stage'] == 'request_finished']
                assert len(finished) == 1
                assert set(finished[0]['stages_ms']) == expected_stages
                identities = {row['request'] for row in capture.records}
                assert len(identities) == 2
                blocked_identity, = identities - {finished[0]['request']}
                blocked = [row for row in capture.records if row['request'] == blocked_identity]
                assert blocked[-1]['stage'] == 'worker_started'
                assert 'query_started' not in blocked[-1]['stages_ms']
            finally:
                release.set()
            first_response = first.result(5)
            assert first_response.status_code == 200
            assert first_response.json() == second_response.json()
    finished = [row for row in capture.records if row['stage'] == 'request_finished']
    assert len(finished) == 2 and len({row['request'] for row in finished}) == 2
    for row in finished:
        assert set(row['stages_ms']) == expected_stages
        assert list(row['stages_ms'].values()) == sorted(row['stages_ms'].values())


def test_worker_queue_wait_is_visible_before_worker_runs(config, store, clock):
    service = Service(config, store, clock)
    capture = Capture()

    async def probe():
        pool = ThreadPoolExecutor(max_workers=1)
        asyncio.get_running_loop().set_default_executor(pool)
        occupied, release = threading.Event(), threading.Event()
        def block():
            occupied.set()
            assert release.wait(5)
        blocker = pool.submit(block)
        assert occupied.wait(5)
        trace = health_trace.Trace(capture)
        token = health_trace.CURRENT.set(trace)
        try:
            task = asyncio.create_task(service.health())
            for _ in range(100):
                if 'submitted' in trace.record['stages_ms']:
                    break
                await asyncio.sleep(0.001)
            assert 'submitted' in trace.record['stages_ms']
            assert 'worker_started' not in trace.record['stages_ms']
        finally:
            release.set()
            await task
            blocker.result(5)
            health_trace.CURRENT.reset(token)
            trace.finish()
            await service.close()
        assert 'worker_started' in trace.record['stages_ms']
    asyncio.run(probe())


@pytest.mark.parametrize('stage', ['connection', 'query', 'return_to_loop'])
def test_database_and_event_loop_stages_are_distinct(config, store, clock, monkeypatch, stage):
    service = Service(config, store, clock)
    capture = Capture()
    entered, release = threading.Event(), threading.Event()
    original = service.db.read

    async def probe():
        loop = asyncio.get_running_loop()
        def block():
            entered.set()
            assert release.wait(5)
        def instrumented(operation):
            if stage == 'connection':
                block()
            def query(db):
                if stage == 'query':
                    class DelayedConnection:
                        def execute(self, *args, **kwargs):
                            block()
                            return db.execute(*args, **kwargs)
                    return operation(DelayedConnection())
                return operation(db)
            result = original(query)
            if stage == 'return_to_loop':
                # Schedule an actual loop stall before delivering the worker result.
                loop.call_soon_threadsafe(time.sleep, 0.08)
            return result
        monkeypatch.setattr(service.db, 'read', instrumented)
        trace = health_trace.Trace(capture)
        token = health_trace.CURRENT.set(trace)
        task = asyncio.create_task(service.health())
        try:
            if stage != 'return_to_loop':
                for _ in range(1000):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.001)
                assert entered.is_set()
                marks = trace.record['stages_ms']
                assert 'worker_started' in marks and 'worker_finished' not in marks
                assert ('query_started' in marks) == (stage == 'query')
                assert 'query_finished' not in marks
        finally:
            release.set()
            await task
            health_trace.CURRENT.reset(token)
            trace.finish()
            await service.close()
        if stage == 'return_to_loop':
            marks = trace.record['stages_ms']
            # The deliberately injected 80 ms loop stall must be visible; no normal-speed assertion.
            assert marks['loop_resumed'] - marks['worker_finished'] >= 60
            assert trace.record['loop_lag_max_ms'] >= 20
    asyncio.run(probe())


@pytest.mark.parametrize('failure', ['database', 'sink', 'disk'])
def test_diagnostics_preserve_health_failures_and_success(config, store, clock, monkeypatch, failure):
    service = Service(config, store, clock)
    capture = Capture()
    service.health_recorder = capture
    capture.close = lambda: None
    with TestClient(create_app(config, service=service), raise_server_exceptions=False) as client:
        baseline = client.get('/health').json()
        if failure == 'database':
            def unavailable(_):
                raise OSError('private database error never recorded')
            monkeypatch.setattr(service.db, 'read', unavailable)
        elif failure == 'sink':
            def unavailable(_):
                raise OSError('private sink error never recorded')
            monkeypatch.setattr(capture, 'emit', unavailable)
        else:
            monkeypatch.setattr('postern.service.shutil.disk_usage', lambda _: (_ for _ in ()).throw(OSError('private disk error')))
        response = client.get('/health')
        if failure == 'disk':
            assert response.status_code == 500
        elif failure == 'database':
            assert response.json()['audit'] == 'unavailable'
            assert response.json()['backup'] == 'unknown'
            assert any(row['stage'] == 'database_error' for row in capture.records)
        else:
            assert response.json() == baseline
    assert 'private ' not in json.dumps(capture.records)


def test_sink_rotation_and_unsafe_paths(tmp_path):
    recorder = health_trace.Recorder(tmp_path / 'trace', 'python')
    recorder.MAX_BYTES = 256
    for i in range(30):
        recorder.emit({'sample': i})
        recorder.pending.join()
    recorder.close()
    assert {p.name for p in recorder.directory.iterdir()} == {'health.jsonl', 'health.jsonl.1'}
    assert all(p.stat().st_size <= 256 for p in recorder.directory.iterdir())
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in recorder.directory.iterdir())
    target = tmp_path / 'must-not-write'
    target.write_text('unchanged')
    (recorder.directory / 'health.jsonl').unlink()
    (recorder.directory / 'health.jsonl').symlink_to(target)
    recorder = health_trace.Recorder(recorder.directory, 'python')
    recorder.emit({'sample': 'blocked'})
    recorder.pending.join()
    recorder.close()
    assert recorder.failed and target.read_text() == 'unchanged'


def test_slow_sink_drops_records_and_close_is_bounded(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def blocked(*_):
        entered.set()
        release.wait(5)
    monkeypatch.setattr(health_trace.Recorder, '_append', blocked)
    recorder = health_trace.Recorder(tmp_path / 'trace', 'python')
    try:
        recorder.emit({'sample': 0})
        assert entered.wait(5)
        for i in range(200):
            recorder.emit({'sample': i})
        assert recorder.pending.qsize() == 64 and recorder.dropped > 0
        recorder.close()
        assert recorder.thread.is_alive()  # shutdown didn't wait for the blocked writer
    finally:
        release.set()
        recorder.thread.join(5)
    assert not recorder.thread.is_alive()


def test_cancelled_health_keeps_partial_trace_and_propagates(config, store, clock, monkeypatch):
    service = Service(config, store, clock)
    entered, release = threading.Event(), threading.Event()
    original = service.db.read
    def blocked(operation):
        entered.set()
        assert release.wait(5)
        return original(operation)
    monkeypatch.setattr(service.db, 'read', blocked)
    async def probe():
        capture = Capture()
        trace = health_trace.Trace(capture)
        token = health_trace.CURRENT.set(trace)
        task = asyncio.create_task(service.health())
        try:
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            assert entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert any(row['stage'] == 'worker_started' for row in capture.records)
            assert not any(row['stage'] == 'query_started' for row in capture.records)
        finally:
            release.set()
            health_trace.CURRENT.reset(token)
            trace.finish()
            await asyncio.to_thread(lambda: None)
            await service.close()
    asyncio.run(probe())


@pytest.mark.parametrize('ending', ['return', 'error', 'cancel'])
def test_trace_cap_preserves_health_and_recovers_permits(tmp_path, monkeypatch, ending):
    # Value: protects=16 active traces, unchanged excess requests, and permit recovery;
    # fails_when=admission is unbounded or any completion path leaks a permit;
    # why_new=existing checks trace one request at a time; seam=none;
    # no_claim=real server scheduling or capacity (controlled ASGI application).
    recorder = health_trace.Recorder(tmp_path / 'trace', 'python')
    service = SimpleNamespace(health_recorder=recorder)
    app_state = SimpleNamespace(state=SimpleNamespace(service=service))
    expected = [{'type': 'http.response.start', 'status': 200, 'headers': []},
                {'type': 'http.response.body', 'body': b'ready'}]

    async def probe():
        release = [asyncio.Event() for _ in range(16)]
        all_entered = asyncio.Event()
        traced = {}

        async def application(scope, receive, send):
            identity = scope['test_request']
            traced[identity] = health_trace.CURRENT.get() is not None
            if len(traced) == 16:
                all_entered.set()
            if identity < 16:
                await release[identity].wait()
            if identity == 0 and ending == 'error':
                raise OSError('controlled application failure')
            for message in expected:
                await send(message)

        middleware = health_trace.HealthTiming(application)

        async def request(identity):
            messages = []
            async def send(message):
                messages.append(message)
            scope = {'app': app_state, 'type': 'http', 'method': 'GET',
                     'path': '/health', 'test_request': identity}
            await middleware(scope, None, send)
            return messages

        tasks = [asyncio.create_task(request(i)) for i in range(16)]
        try:
            await asyncio.wait_for(all_entered.wait(), 5)
            assert all(traced.values())
            assert await request(16) == expected
            assert traced[16] is False and recorder.skipped_traces == 1
            if ending == 'cancel':
                tasks[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
            else:
                release[0].set()
                if ending == 'error':
                    with pytest.raises(OSError, match='controlled application failure'):
                        await tasks[0]
                else:
                    assert await tasks[0] == expected
            assert await request(17) == expected and traced[17] is True
            def unavailable_trace(_):
                raise OSError('controlled setup failure')
            with monkeypatch.context() as patch:
                patch.setattr(health_trace, 'Trace', unavailable_trace)
                assert await request(18) == expected and traced[18] is False
            assert await request(19) == expected and traced[19] is True
            assert recorder.skipped_traces == 1
        finally:
            for event in release:
                event.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)

    try:
        asyncio.run(probe())
    finally:
        recorder.close()
    records = [json.loads(line) for line in (recorder.directory / 'health.jsonl').read_text().splitlines()]
    assert max(row['skipped_traces'] for row in records) == 1
