"""One queued SQLite writer. Measurement code, not the production event schema.

request -> bounded queue -> check deadline -> WAL/FULL commit -> receipt -> body
                            expired/error --------------------------> no body
"""
from __future__ import annotations

import concurrent.futures
import queue
import sqlite3
import threading
import time
from pathlib import Path


class AuditUnavailable(Exception):
    pass


class AuditWriter:
    def __init__(self, path: Path, capacity=128, busy_ms=1000, deadline_s=1.5):
        self.path = path
        self.deadline_s = deadline_s
        self.busy_ms = busy_ms
        self.jobs = queue.Queue(maxsize=capacity)
        self.ready = concurrent.futures.Future()
        self.thread = threading.Thread(target=self._run, name='day1-audit', daemon=True)
        self.thread.start()
        self.settings = self.ready.result(timeout=5)

    def submit(self, run_id, reader, route):
        result = concurrent.futures.Future()
        try:
            self.jobs.put_nowait((time.monotonic(), run_id, reader, route, result))
        except queue.Full:
            result.set_exception(AuditUnavailable('audit queue full'))
        return result

    def _run(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=self.busy_ms / 1000)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA fullfsync=ON')
            db.execute(f'PRAGMA busy_timeout={int(self.busy_ms)}')
            db.execute('CREATE TABLE IF NOT EXISTS attempts ('
                       'id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, reader TEXT NOT NULL, '
                       'route TEXT NOT NULL, created_at REAL NOT NULL)')
            db.commit()
            self.ready.set_result({key: db.execute(f'PRAGMA {key}').fetchone()[0]
                                   for key in ('journal_mode', 'synchronous', 'fullfsync', 'busy_timeout')})
            while True:
                job = self.jobs.get()
                if job is None:
                    break
                queued, run_id, reader, route, result = job
                if not result.set_running_or_notify_cancel():
                    continue
                started = time.monotonic()
                if started - queued >= self.deadline_s:
                    result.set_exception(AuditUnavailable('audit queue deadline'))
                    continue
                try:
                    db.execute('INSERT INTO attempts(run_id,reader,route,created_at) VALUES (?,?,?,?)',
                               (run_id, reader, route, time.time()))
                    db.commit()
                    ended = time.monotonic()
                    if ended - queued >= self.deadline_s:
                        raise AuditUnavailable('audit commit missed response deadline')
                    result.set_result({'queue_ms': (started - queued) * 1000,
                                       'commit_ms': (ended - started) * 1000,
                                       'total_ms': (ended - queued) * 1000})
                except (sqlite3.Error, AuditUnavailable) as error:
                    db.rollback()
                    result.set_exception(AuditUnavailable(str(error)))
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(error)
            else:
                raise
        finally:
            if db is not None:
                db.close()

    def close(self):
        self.jobs.put(None, timeout=5)
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('audit worker did not stop')
