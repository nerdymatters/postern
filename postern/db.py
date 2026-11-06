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
import logging
import uuid
from . import progress_store

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
CREATE INDEX IF NOT EXISTS events_name_history ON events(email,ts DESC,id DESC) WHERE type='certificate_name';
-- Health reads only batch/clock metadata; export finds unbatched events in sequence order.
CREATE INDEX IF NOT EXISTS events_batch ON events(batch_id,seq,ts);
CREATE TABLE IF NOT EXISTS video_metadata(item_id TEXT PRIMARY KEY REFERENCES items(id), duration_seconds REAL NOT NULL);
CREATE TABLE IF NOT EXISTS video_tickets(token_hash TEXT PRIMARY KEY, view_id TEXT UNIQUE NOT NULL REFERENCES views(id), expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS video_checkpoints(view_id TEXT PRIMARY KEY REFERENCES views(id), reported_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS video_coverage(view_id TEXT PRIMARY KEY REFERENCES views(id), ranges TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_devices(session_id TEXT PRIMARY KEY REFERENCES sessions(id), device_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS selfies(id TEXT PRIMARY KEY, email TEXT NOT NULL, device_hash TEXT NOT NULL, session_id TEXT NOT NULL, captured_at REAL NOT NULL, expires_at REAL NOT NULL, retired_at REAL, status TEXT NOT NULL, drive_id TEXT NOT NULL, sha256 TEXT NOT NULL, consent_version TEXT NOT NULL, context TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, error TEXT);
CREATE INDEX IF NOT EXISTS selfies_reader ON selfies(email,device_hash,status);
CREATE UNIQUE INDEX IF NOT EXISTS selfies_pending ON selfies(email,device_hash) WHERE status='pending';
CREATE UNIQUE INDEX IF NOT EXISTS selfies_active ON selfies(email,device_hash) WHERE status='active';
CREATE TABLE IF NOT EXISTS selfie_reservations(id TEXT PRIMARY KEY, email TEXT NOT NULL, attempt_id TEXT UNIQUE NOT NULL, ts REAL NOT NULL, context TEXT NOT NULL, outcome TEXT, release_id TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS selfie_reservations_unfinished ON selfie_reservations(email) WHERE outcome IS NULL;
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, post_id TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, next_try REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS quiz_standing(email TEXT NOT NULL, chapter INTEGER NOT NULL, attempts INTEGER NOT NULL, best_score INTEGER NOT NULL, last_score INTEGER NOT NULL, last_ts REAL NOT NULL, last_seq INTEGER NOT NULL, passed_at REAL, PRIMARY KEY(email,chapter));
CREATE TABLE IF NOT EXISTS quiz_seen(email TEXT NOT NULL, chapter INTEGER NOT NULL, question TEXT NOT NULL, times INTEGER NOT NULL, last_ts REAL NOT NULL, last_seq INTEGER NOT NULL, PRIMARY KEY(email,chapter,question));
CREATE TABLE IF NOT EXISTS quiz_draws(email TEXT NOT NULL, chapter INTEGER NOT NULL, version TEXT NOT NULL, questions TEXT NOT NULL, drawn_at REAL NOT NULL, PRIMARY KEY(email,chapter));
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, drive_id TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, count INTEGER NOT NULL, created_at REAL NOT NULL, confirmed_at REAL, summary_confirmed INTEGER NOT NULL DEFAULT 0);
-- The exporter polls every three seconds; completed batches should cost no scan.
CREATE INDEX IF NOT EXISTS batches_pending ON batches(created_at) WHERE confirmed_at IS NULL OR summary_confirmed=0;
CREATE INDEX IF NOT EXISTS batches_confirmed ON batches(confirmed_at);
-- Pending batches and unbatched events already have indexed lookup paths.
-- Track only exceptional orphan events, so confirmation does not rewrite 500
-- derived rows. These triggers also protect health after rollback to old code.
CREATE INDEX IF NOT EXISTS batches_unconfirmed ON batches(id) WHERE confirmed_at IS NULL;
CREATE TABLE IF NOT EXISTS health_orphan_events(seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, ts REAL NOT NULL, batch_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS health_orphan_oldest ON health_orphan_events(ts);
CREATE INDEX IF NOT EXISTS health_orphan_batch ON health_orphan_events(batch_id);
CREATE TRIGGER IF NOT EXISTS health_event_insert AFTER INSERT ON events
WHEN EXISTS(SELECT 1 FROM health_orphan_events WHERE seq=NEW.seq OR id=NEW.id)
  OR (NEW.batch_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM batches WHERE id=NEW.batch_id)) BEGIN
  DELETE FROM health_orphan_events WHERE seq=NEW.seq OR id=NEW.id;
  INSERT INTO health_orphan_events SELECT NEW.seq,NEW.id,NEW.ts,NEW.batch_id
    WHERE NEW.batch_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM batches WHERE id=NEW.batch_id);
END;
CREATE TRIGGER IF NOT EXISTS health_event_update AFTER UPDATE OF seq,id,ts,batch_id ON events
WHEN (NEW.batch_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM batches WHERE id=NEW.batch_id))
  OR ((OLD.batch_id IS NOT NULL OR OLD.seq IS NOT NEW.seq OR OLD.id IS NOT NEW.id)
    AND EXISTS(SELECT 1 FROM health_orphan_events WHERE seq IN (OLD.seq,NEW.seq) OR id=NEW.id)) BEGIN
  DELETE FROM health_orphan_events WHERE seq IN (OLD.seq,NEW.seq) OR id=NEW.id;
  INSERT INTO health_orphan_events SELECT NEW.seq,NEW.id,NEW.ts,NEW.batch_id
    WHERE NEW.batch_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM batches WHERE id=NEW.batch_id);
END;
CREATE TRIGGER IF NOT EXISTS health_event_delete AFTER DELETE ON events BEGIN
  DELETE FROM health_orphan_events WHERE seq=OLD.seq;
END;
CREATE TRIGGER IF NOT EXISTS health_batch_insert AFTER INSERT ON batches BEGIN
  DELETE FROM health_orphan_events WHERE batch_id=NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS health_batch_update AFTER UPDATE OF id ON batches WHEN OLD.id IS NOT NEW.id BEGIN
  DELETE FROM health_orphan_events WHERE batch_id=NEW.id;
  INSERT OR REPLACE INTO health_orphan_events SELECT seq,id,ts,batch_id FROM events WHERE batch_id=OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS health_batch_delete AFTER DELETE ON batches BEGIN
  INSERT OR REPLACE INTO health_orphan_events SELECT seq,id,ts,batch_id FROM events WHERE batch_id=OLD.id;
END;
''' + progress_store.SCHEMA


class AuditUnavailable(Exception):
    pass


class Database:
    def __init__(self, path, capacity=128, deadline=1.5):
        self.path, self.deadline = path, deadline
        self.queue = queue.Queue(maxsize=capacity)
        self.ready = concurrent.futures.Future()
        self.failed = False
        self.progress_rebuild_error = None
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
            if not db.execute("SELECT 1 FROM meta WHERE key='health_orphans_v1'").fetchone():
                # Backfill once before readiness. A failed/interrupted transaction
                # leaves no marker and will be rebuilt on the next startup.
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM health_orphan_events')
                db.execute('''INSERT INTO health_orphan_events SELECT e.seq,e.id,e.ts,e.batch_id
                    FROM events e LEFT JOIN batches b ON b.id=e.batch_id WHERE e.batch_id IS NOT NULL AND b.id IS NULL''')
                db.execute("INSERT INTO meta(key,value) VALUES('health_orphans_v1','true')")
                db.commit()
            if not progress_store.ready(db) and not db.execute('SELECT 1 FROM events LIMIT 1').fetchone():
                db.execute('BEGIN IMMEDIATE')
                progress_store.rebuild_batch(db)
                db.commit()
            rebuild_pending = progress_store.pending(db)
            self.ready.set_result({p: db.execute('PRAGMA '+p).fetchone()[0] for p in ['journal_mode','synchronous','fullfsync','busy_timeout']})
            while True:
                try:
                    job = self.queue.get(timeout=.1 if rebuild_pending else None)
                except queue.Empty:
                    # Existing installations keep reading authoritative events
                    # until this bounded, resumable projection is complete. Only
                    # spend idle writer time; queued audit work always goes first.
                    try:
                        db.execute('BEGIN IMMEDIATE')
                        rebuild_pending = not progress_store.rebuild_batch(db)
                        db.commit()
                    except Exception as error:
                        db.rollback()
                        self.progress_rebuild_error = type(error).__name__
                        rebuild_pending = False  # retry on next startup, retain raw fallback
                        logging.getLogger(__name__).warning(
                            'Progress read table rebuild paused (%s); audit history remains active',
                            self.progress_rebuild_error)
                    continue
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
                    # Raw recovery/old-style writes may have introduced inputs
                    # needing classification. The partial index makes this cheap.
                    if self.progress_rebuild_error is None and not rebuild_pending:
                        rebuild_pending = progress_store.pending(db)
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


def pending_health(db):
    # Disjoint sets preserve missing batches, NULL batch IDs and confirmation
    # reversals without scanning confirmed history or serving a cached status.
    return db.execute("""SELECT MIN(oldest),SUM(n) FROM (
        SELECT MIN(ts) oldest,COUNT(*) n FROM events WHERE batch_id IS NULL
        UNION ALL
        SELECT MIN(e.ts),COUNT(*) FROM batches b INDEXED BY batches_unconfirmed
          CROSS JOIN events e ON e.batch_id=b.id WHERE b.confirmed_at IS NULL
        UNION ALL
        SELECT MIN(ts),COUNT(*) FROM health_orphan_events
    )""").fetchone()


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
    progress_store.compact_created(db, identity, kind)
    return identity
