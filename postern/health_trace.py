"""Opt-in local health timings. No request data, SQL, results or error messages.

The request only queues bounded snapshots; a dedicated daemon writes at most two
1 MiB files. A slow/broken diagnostic sink drops records, never health checks.
"""
from __future__ import annotations

import asyncio
import contextvars
import itertools
import json
import os
from pathlib import Path
import queue
import stat
import threading
import time

CURRENT = contextvars.ContextVar('postern_health_trace', default=None)
STAGES = frozenset(('received', 'handler', 'submitted', 'worker_started',
                    'query_started', 'query_finished', 'worker_finished',
                    'loop_resumed', 'database_error', 'disk_started', 'disk_finished',
                    'response_started', 'response_finished', 'request_finished'))


class Recorder:
    MAX_BYTES = 1024 * 1024
    MAX_ACTIVE_TRACES = 16

    def __init__(self, directory: Path, backend: str):
        self.directory, self.backend = directory, backend
        self.pending = queue.Queue(maxsize=64)
        self.stopping = threading.Event()
        self.dropped = 0
        self.skipped_traces = 0
        self.trace_slots = threading.BoundedSemaphore(self.MAX_ACTIVE_TRACES)
        self.failed = False
        self.identities = itertools.count(1)
        self.thread = threading.Thread(target=self._run, name='postern-health-trace', daemon=True)
        self.thread.start()

    @classmethod
    def configured(cls, data_dir, backend):
        if os.environ.get('POSTERN_HEALTH_TRACE') != '1':
            return None
        try:
            return cls(data_dir / 'health-diagnostics', backend)
        except Exception:
            return None

    def acquire_trace(self):
        if self.stopping.is_set() or self.failed or not self.trace_slots.acquire(blocking=False):
            self.skipped_traces += 1
            return False
        return True

    def release_trace(self):
        self.trace_slots.release()

    def emit(self, record):
        try:
            if self.stopping.is_set() or self.failed:
                self.dropped += 1
                return
            self.pending.put_nowait({**record, 'dropped_records': self.dropped,
                                     'skipped_traces': self.skipped_traces})
        except Exception:
            self.dropped += 1

    def _append(self, record):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError('Unsafe diagnostic directory')
        self.directory.chmod(0o700)
        path = self.directory / 'health.jsonl'
        line = (json.dumps(record, separators=(',', ':'), allow_nan=False) + '\n').encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise OSError('Unsafe diagnostic file')
            os.fchmod(fd, 0o600)
            if info.st_size + len(line) > self.MAX_BYTES:
                os.close(fd)
                fd = None
                path.replace(self.directory / 'health.jsonl.1')
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            if os.write(fd, line) != len(line):
                raise OSError('Incomplete diagnostic write')
        finally:
            if fd is not None:
                os.close(fd)

    def _run(self):
        while not self.stopping.is_set() or not self.pending.empty():
            try:
                record = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if not self.failed:
                    self._append(record)
            except Exception:
                self.failed = True
            finally:
                self.pending.task_done()

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=1)


class Trace:
    def __init__(self, recorder):
        self.recorder = recorder
        self.started = time.monotonic()
        self.record = {'schema': 1, 'pid': os.getpid(), 'backend': recorder.backend,
                       'request': next(recorder.identities), 'started_at': time.time(),
                       'stages_ms': {}, 'loop_lag_max_ms': 0.0}
        self.lock = threading.Lock()
        self.loop = asyncio.get_running_loop()
        self.tick_due = self.loop.time() + 0.05
        self.timer = self.loop.call_at(self.tick_due, self._tick)
        self.mark('received')

    def _tick(self):
        with self.lock:
            self.record['loop_lag_max_ms'] = round(max(self.record['loop_lag_max_ms'],
                                                     (self.loop.time() - self.tick_due) * 1000), 3)
        self.tick_due = self.loop.time() + 0.05
        self.timer = self.loop.call_at(self.tick_due, self._tick)

    def mark(self, stage):
        if stage not in STAGES:
            return
        with self.lock:
            self.record['stages_ms'][stage] = round((time.monotonic() - self.started) * 1000, 3)
            snapshot = {**self.record, 'stage': stage, 'stages_ms': dict(self.record['stages_ms'])}
        # Recording cannot alter the handler's readiness, exception or cancellation.
        try:
            self.recorder.emit(snapshot)
        except Exception:
            pass

    def finish(self):
        self.timer.cancel()
        # Also account for a final blocked interval before the timer got to run.
        with self.lock:
            self.record['loop_lag_max_ms'] = round(max(self.record['loop_lag_max_ms'],
                                                     (self.loop.time() - self.tick_due) * 1000), 3)
        self.mark('request_finished')


def mark(stage):
    trace = CURRENT.get()
    if trace is not None:
        trace.mark(stage)


async def read(database, operation):
    if CURRENT.get() is None:
        return await asyncio.to_thread(database.read, operation)

    def query(db):
        mark('query_started')
        try:
            return operation(db)
        finally:
            mark('query_finished')

    def worker():
        mark('worker_started')
        try:
            return database.read(query)
        finally:
            mark('worker_finished')

    mark('submitted')
    try:
        return await asyncio.to_thread(worker)
    finally:
        mark('loop_resumed')


class HealthTiming:
    """Measure public GET /health through the final ASGI response send."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        service = getattr(getattr(scope.get('app'), 'state', None), 'service', None)
        recorder = getattr(service, 'health_recorder', None)
        if recorder is None or scope['type'] != 'http' or scope['method'] != 'GET' or scope['path'] != '/health':
            return await self.app(scope, receive, send)
        if not recorder.acquire_trace():
            return await self.app(scope, receive, send)
        try:
            trace = Trace(recorder)
        except Exception:
            recorder.release_trace()
            return await self.app(scope, receive, send)
        token = CURRENT.set(trace)

        async def measured_send(message):
            await send(message)
            if message['type'] == 'http.response.start':
                trace.mark('response_started')
            elif message['type'] == 'http.response.body' and not message.get('more_body', False):
                trace.mark('response_finished')

        try:
            await self.app(scope, receive, measured_send)
        finally:
            try:
                CURRENT.reset(token)
                trace.finish()
            finally:
                recorder.release_trace()
