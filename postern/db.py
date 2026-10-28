"""Every write uses one bounded queue and a WAL/FULL transaction.

request -> writer checks deadline -> authorize + audit -> COMMIT -> response
                     failure -------------------------------> 503, no body
After body send: compare-and-set first_served_at + item_open in one transaction.
"""
from __future__ import annotations
import concurrent.futures
import queue
import sqlite3
import threading
import time
import json
import uuid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members(email TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, subject TEXT NOT NULL, email TEXT NOT NULL, name TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, consent_version TEXT, client TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_ip_agreements(session_id TEXT PRIMARY KEY REFERENCES sessions(id), version TEXT NOT NULL, accepted_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, created_by TEXT NOT NULL, published_at REAL, publish_op_id TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, post_id TEXT NOT NULL REFERENCES posts(id), filename TEXT NOT NULL, kind TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, pages INTEGER NOT NULL DEFAULT 0, drive_file_id TEXT, derive_status TEXT NOT NULL, derive_error TEXT, sort INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS views(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), email TEXT NOT NULL, item_id TEXT NOT NULL REFERENCES items(id), post_id TEXT NOT NULL, attempt_id TEXT NOT NULL, requested_at REAL NOT NULL, first_served_at REAL, last_served_at REAL, highest_page INTEGER, served_pages TEXT NOT NULL DEFAULT '[]', summarized_at REAL, UNIQUE(session_id,attempt_id));
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, ts REAL NOT NULL, type TEXT NOT NULL, email TEXT, post_id TEXT, item_id TEXT, view_id TEXT, summary INTEGER NOT NULL, payload TEXT NOT NULL, batch_id TEXT);
CREATE INDEX IF NOT EXISTS events_report ON events(post_id,type,email);
CREATE INDEX IF NOT EXISTS events_progress ON events(email,type,item_id);
CREATE TABLE IF NOT EXISTS video_metadata(item_id TEXT PRIMARY KEY REFERENCES items(id), duration_seconds REAL NOT NULL);
CREATE TABLE IF NOT EXISTS video_tickets(token_hash TEXT PRIMARY KEY, view_id TEXT UNIQUE NOT NULL REFERENCES views(id), expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS video_checkpoints(view_id TEXT PRIMARY KEY REFERENCES views(id), reported_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS video_coverage(view_id TEXT PRIMARY KEY REFERENCES views(id), ranges TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_devices(session_id TEXT PRIMARY KEY REFERENCES sessions(id), device_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS selfies(id TEXT PRIMARY KEY, email TEXT NOT NULL, device_hash TEXT NOT NULL, session_id TEXT NOT NULL, captured_at REAL NOT NULL, expires_at REAL NOT NULL, retired_at REAL, status TEXT NOT NULL, drive_id TEXT NOT NULL, sha256 TEXT NOT NULL, consent_version TEXT NOT NULL, context TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, error TEXT);
CREATE INDEX IF NOT EXISTS selfies_reader ON selfies(email,device_hash,status);
CREATE UNIQUE INDEX IF NOT EXISTS selfies_pending ON selfies(email,device_hash) WHERE status='pending';
CREATE UNIQUE INDEX IF NOT EXISTS selfies_active ON selfies(email,device_hash) WHERE status='active';
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, post_id TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, next_try REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS quiz_standing(email TEXT NOT NULL, chapter INTEGER NOT NULL, attempts INTEGER NOT NULL, best_score INTEGER NOT NULL, last_score INTEGER NOT NULL, last_ts REAL NOT NULL, last_seq INTEGER NOT NULL, passed_at REAL, PRIMARY KEY(email,chapter));
CREATE TABLE IF NOT EXISTS quiz_seen(email TEXT NOT NULL, chapter INTEGER NOT NULL, question TEXT NOT NULL, times INTEGER NOT NULL, last_ts REAL NOT NULL, last_seq INTEGER NOT NULL, PRIMARY KEY(email,chapter,question));
CREATE TABLE IF NOT EXISTS quiz_draws(email TEXT NOT NULL, chapter INTEGER NOT NULL, version TEXT NOT NULL, questions TEXT NOT NULL, drawn_at REAL NOT NULL, PRIMARY KEY(email,chapter));
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, drive_id TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, count INTEGER NOT NULL, created_at REAL NOT NULL, confirmed_at REAL, summary_confirmed INTEGER NOT NULL DEFAULT 0);
'''


class AuditUnavailable(Exception):
    pass


class Database:
    def __init__(self, path, capacity=128, deadline=1.5):
        self.path, self.deadline = path, deadline
        self.queue = queue.Queue(maxsize=capacity)
        self.ready = concurrent.futures.Future()
        self.failed = False
        self.thread = threading.Thread(target=self._worker, name='postern-writer', daemon=True)
        self.thread.start()
        self.settings = self.ready.result(timeout=10)

    def submit(self, operation):
        future = concurrent.futures.Future()
        try:
            self.queue.put_nowait((time.monotonic(), operation, future))
        except queue.Full:
            future.set_exception(AuditUnavailable('Audit queue is full'))
        return future

    def _worker(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=1)
            self.path.chmod(0o600)
            db.row_factory = sqlite3.Row
            for pragma in ['journal_mode=WAL', 'synchronous=FULL', 'fullfsync=ON', 'busy_timeout=1000', 'foreign_keys=ON']:
                db.execute('PRAGMA ' + pragma)
            db.executescript(SCHEMA)
            self.ready.set_result({p: db.execute('PRAGMA '+p).fetchone()[0] for p in ['journal_mode','synchronous','fullfsync','busy_timeout']})
            while True:
                job = self.queue.get()
                if job is None:
                    break
                queued, operation, future = job
                if not future.set_running_or_notify_cancel():
                    continue
                if time.monotonic() - queued >= self.deadline:
                    future.set_exception(AuditUnavailable('Audit queue deadline'))
                    continue
                try:
                    started = time.monotonic()
                    db.execute('BEGIN IMMEDIATE')
                    result = operation(db)
                    db.commit()
                    future.queue_ms = (started - queued) * 1000
                    future.commit_ms = (time.monotonic() - started) * 1000
                    if time.monotonic() - queued >= self.deadline:
                        raise AuditUnavailable('Audit response deadline')
                    self.failed = False
                    future.set_result(result)
                except BaseException as error:
                    db.rollback()
                    if isinstance(error, (sqlite3.Error, AuditUnavailable, OSError)):
                        self.failed = True
                        error = AuditUnavailable('Local audit storage is unavailable')
                    future.set_exception(error)
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(error)
            self.failed = True
        finally:
            if db:
                db.close()

    def read(self, operation):
        # A connection's `with` block ends its transaction but never closes it. Unclosed readers kept
        # their file descriptors while the writer stayed open, until the API hit its open-file limit.
        db = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True, timeout=1)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            return operation(db)
        finally:
            db.close()

    def close(self):
        self.queue.put(None, timeout=5)
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('Database writer did not stop')


def rows(db, sql, values=()):
    return [dict(r) for r in db.execute(sql, values).fetchall()]


def meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(db, key, value):
    db.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))


def event(db, kind, context=None, *, post_id=None, item_id=None, view_id=None, detail=None, summary=True, email=None, timestamp=None):
    context = context or {}
    detail = dict(detail or {})
    if context.get('selfie_id'):
        detail['selfie_id'] = context['selfie_id']
    identity = str(uuid.uuid4())
    ts = timestamp if timestamp is not None else time.time()
    payload = {'event_id': identity, 'ts': ts, 'event': kind, 'email': email or context.get('email'),
               'name': context.get('name'), 'post_id': post_id, 'item_id': item_id, 'view_id': view_id,
               'session_public_id': context.get('id'), 'client': context.get('client'),
               'ip': context.get('ip'), 'user_agent': context.get('user_agent'), 'detail': detail, 'summary': summary}
    db.execute('INSERT INTO events(id,ts,type,email,post_id,item_id,view_id,summary,payload) VALUES (?,?,?,?,?,?,?,?,?)',
               (identity, ts, kind, payload['email'], post_id, item_id, view_id, int(summary), json.dumps(payload)))
    return identity
