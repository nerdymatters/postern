"""Rebuildable progress inputs; the audit log remains the source of truth.

Triggers mirror old writers and arbitrary historical JSON without parsing it.
Compaction removes only metadata the progress reducers never inspect. The read
table becomes eligible only after every existing event has been visited.
"""
import json
import math
import sqlite3
from time import monotonic

KINDS = ('page_served', 'item_summary', 'video_progress', 'item_open', 'view_requested')
_KINDS_SQL = ','.join(repr(kind) for kind in KINDS)
READY = 'progress_inputs_v1'
CURSOR = 'progress_inputs_cursor_v1'
MAX_PAYLOAD_BYTES = 64*1024
BATCH_SECONDS = .025
_PAYLOAD = f"CASE WHEN length(CAST(payload AS BLOB))<={MAX_PAYLOAD_BYTES} THEN payload ELSE '' END"
_NEW_PAYLOAD = _PAYLOAD.replace('payload', 'NEW.payload')
_COLUMNS = f'seq,id,email,item_id,type,ts,{_PAYLOAD} AS payload'

SCHEMA = f'''
CREATE TABLE IF NOT EXISTS progress_events(
    seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, email TEXT, item_id TEXT,
    type TEXT NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL,
    portable INTEGER NOT NULL DEFAULT -1);
CREATE INDEX IF NOT EXISTS progress_events_reader ON progress_events(email,type,item_id);
CREATE INDEX IF NOT EXISTS progress_events_legacy ON progress_events(seq) WHERE portable<1;
CREATE INDEX IF NOT EXISTS progress_events_pending ON progress_events(seq) WHERE portable=-1;
CREATE TABLE IF NOT EXISTS progress_generation(id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL);
INSERT OR IGNORE INTO progress_generation VALUES(1,0);
CREATE TRIGGER IF NOT EXISTS progress_generation_delete AFTER DELETE ON progress_events BEGIN
    UPDATE progress_generation SET value=value+1 WHERE id=1;
END;
CREATE TRIGGER IF NOT EXISTS progress_generation_update
AFTER UPDATE OF seq,id,email,item_id,type,ts,payload,portable ON progress_events
WHEN OLD.portable>=0 BEGIN
    UPDATE progress_generation SET value=value+1 WHERE id=1;
END;
CREATE TRIGGER IF NOT EXISTS progress_generation_insert AFTER INSERT ON progress_events
WHEN EXISTS(SELECT 1 FROM progress_events WHERE seq>NEW.seq) BEGIN
    UPDATE progress_generation SET value=value+1 WHERE id=1;
END;
CREATE TRIGGER IF NOT EXISTS progress_event_insert AFTER INSERT ON events BEGIN
    DELETE FROM progress_events WHERE seq=NEW.seq OR id=NEW.id;
    INSERT INTO progress_events(seq,id,email,item_id,type,ts,payload) SELECT NEW.seq,NEW.id,NEW.email,NEW.item_id,NEW.type,NEW.ts,{_NEW_PAYLOAD}
        WHERE NEW.type IN ({_KINDS_SQL});
END;
CREATE TRIGGER IF NOT EXISTS progress_event_update
AFTER UPDATE OF seq,id,email,item_id,type,ts,payload ON events BEGIN
    DELETE FROM progress_events WHERE seq IN (OLD.seq,NEW.seq) OR id IN (OLD.id,NEW.id);
    INSERT INTO progress_events(seq,id,email,item_id,type,ts,payload) SELECT NEW.seq,NEW.id,NEW.email,NEW.item_id,NEW.type,NEW.ts,{_NEW_PAYLOAD}
        WHERE NEW.type IN ({_KINDS_SQL});
END;
-- UPDATE OF does not fire when a statement moves a row through rowid, oid or
-- _rowid_ without naming seq. Mirror every sequence change regardless of spelling.
CREATE TRIGGER IF NOT EXISTS progress_event_rowid AFTER UPDATE ON events
WHEN OLD.seq IS NOT NEW.seq BEGIN
    DELETE FROM progress_events WHERE seq IN (OLD.seq,NEW.seq) OR id IN (OLD.id,NEW.id);
    INSERT INTO progress_events(seq,id,email,item_id,type,ts,payload) SELECT NEW.seq,NEW.id,NEW.email,NEW.item_id,NEW.type,NEW.ts,{_NEW_PAYLOAD}
        WHERE NEW.type IN ({_KINDS_SQL});
END;
CREATE TRIGGER IF NOT EXISTS progress_event_delete AFTER DELETE ON events BEGIN
    DELETE FROM progress_events WHERE seq=OLD.seq OR id=OLD.id;
END;
'''
OBJECTS = frozenset({
    ('table', 'progress_events'), ('table', 'progress_generation'),
    ('trigger', 'progress_generation_delete'), ('trigger', 'progress_generation_update'),
    ('trigger', 'progress_generation_insert'), ('trigger', 'progress_event_insert'),
    ('trigger', 'progress_event_update'), ('trigger', 'progress_event_rowid'),
    ('trigger', 'progress_event_delete')})


def ordinary_json(value, depth=0):
    """A conservative subset with no parser limits or numeric fallback traps."""
    if depth > 32:
        return False
    if value is None or type(value) in (str, bool):
        return True
    if type(value) in (int, float):
        return -2**53 <= value <= 2**53 and math.isfinite(value)
    if type(value) is list:
        return all(ordinary_json(item, depth+1) for item in value)
    if type(value) is dict:
        return all(type(key) is str and ordinary_json(item, depth+1) for key, item in value.items())
    return False


def portable(value, kind, timestamp, email, item):
    # Query plans can visit this smaller table in a different order. Positive
    # finite activity clocks and non-throwing evidence have order-independent
    # reduction. Preserve the original SQL plan for every exceptional history.
    if (type(timestamp) not in (int, float) or not 0 < timestamp <= 2**53
            or type(email) is not str or type(item) is not str
            or type(value) is not dict or not ordinary_json(value)):
        return False
    detail = value.get('detail', {})
    if kind in ('view_requested', 'item_open'):
        return True
    if type(detail) is not dict:
        return False
    if kind == 'item_summary' and type(detail.get('pages', [])) is not list:
        return False
    if kind == 'video_progress' and type(detail.get('ranges', [])) is not list:
        return False
    return True


def compact(row):
    """Return payload and eligibility, retaining exceptional inputs verbatim."""
    payload = row['payload']
    if type(payload) is not str:
        # Python json.loads accepts bytes, whereas the native row boundary does
        # not. Turning a historical BLOB into TEXT would suppress that exception.
        return payload, 0
    try:
        value = json.loads(payload)
        if portable(value, row['type'], row['ts'], row['email'], row['item_id']):
            return json.dumps({'detail': value.get('detail', {})}, separators=(',', ':')), 1
    except (ValueError, TypeError, RecursionError, OverflowError):
        pass
    return payload, 0


def compact_created(db, identity, kind):
    # Derive from the bytes just inserted by the trigger, never from mutable
    # request objects which could change after the authoritative audit encoding.
    if kind in KINDS:
        cursor = db.execute(f'SELECT {_COLUMNS} FROM progress_events WHERE id=?', (identity,))
        cursor.row_factory = sqlite3.Row
        row = cursor.fetchone()
        db.execute('UPDATE progress_events SET payload=?,portable=? WHERE id=?', (*compact(row), identity))


def ready(db):
    return db.execute('SELECT 1 FROM meta WHERE key=? AND value=?', (READY, 'true')).fetchone() is not None


def complete(db):
    """Whether every projection table and trigger exists. Check before SCHEMA recreates them."""
    rows = db.execute("SELECT type,name FROM sqlite_schema WHERE type IN ('table','trigger') AND name LIKE 'progress%'")
    return OBJECTS <= {(kind, name) for kind, name in rows}


def started(db):
    return db.execute('SELECT 1 FROM meta WHERE key IN (?,?) LIMIT 1', (READY, CURSOR)).fetchone() is not None


def reset(db):
    """Forget a projection that may have missed writes and rebuild it from events.

    A removed table or trigger leaves the authoritative marker behind. Run inside one
    writer transaction after SCHEMA has recreated every object; reads use events
    until the rebuild completes again.
    """
    db.execute('DELETE FROM meta WHERE key IN (?,?)', (READY, CURSOR))
    db.execute('DELETE FROM progress_events')
    db.execute('UPDATE progress_generation SET value=value+1 WHERE id=1')


def pending(db):
    return not ready(db) or db.execute('SELECT 1 FROM progress_events WHERE portable=-1 LIMIT 1').fetchone() is not None


def rebuild_batch(db, limit=128):
    """One writer transaction, resumable after shutdown; returns whether complete."""
    started = monotonic()
    if ready(db):
        events = db.execute(f'SELECT {_COLUMNS} FROM progress_events WHERE portable=-1 ORDER BY seq LIMIT ?', (limit,)).fetchall()
        for event in events:
            db.execute('UPDATE progress_events SET payload=?,portable=? WHERE seq=?', (*compact(event), event['seq']))
            if monotonic()-started >= BATCH_SECONDS:
                break
        return not pending(db)
    row = db.execute('SELECT value FROM meta WHERE key=?', (CURSOR,)).fetchone()
    cursor = int(row[0]) if row else None
    query = f'SELECT {_COLUMNS} FROM events '
    query += 'WHERE seq>? ' if cursor is not None else ''
    events = db.execute(query + 'ORDER BY seq LIMIT ?',
                        (cursor, limit) if cursor is not None else (limit,)).fetchall()
    processed = 0
    for event in events:
        if event['type'] in KINDS:
            # The writer owns this snapshot. A concurrent application insert or
            # update cannot interleave; all later changes are covered by triggers.
            db.execute('INSERT OR REPLACE INTO progress_events VALUES (?,?,?,?,?,?,?,?)',
                       (*tuple(event)[:6], *compact(event)))
        processed += 1
        if monotonic()-started >= BATCH_SECONDS:
            break
    if processed:
        db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (CURSOR, str(events[processed-1]['seq'])))
    if processed == len(events) and len(events) < limit:
        db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (READY, 'true'))
        db.execute('DELETE FROM meta WHERE key=?', (CURSOR,))
        return not pending(db)
    return False


def diagnostics(db):
    """Which history the progress reads use, and why, for the owner. One statement reads the marker and both
    counts, so a writer commit between separate reads can never make them disagree; it scans only the partial
    index of rows below eligible. The source is decided as source() decides it."""
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='progress_events'").fetchone():
        return {'source': 'events', 'ready': False, 'pending_rows': None, 'original_query_rows': None}
    marked, below, pending = db.execute(
        'SELECT (SELECT 1 FROM meta WHERE key=? AND value=?) IS NOT NULL, COUNT(*), TOTAL(portable=-1) '
        'FROM progress_events WHERE portable<1', (READY, 'true')).fetchone()
    pending = int(pending)
    return {'source': 'progress_events' if marked and not below else 'events', 'ready': bool(marked),
            'pending_rows': pending, 'original_query_rows': below - pending}


def source(db):
    # Small standalone reducer fixtures and old read-only databases have no
    # projection. Do not catch read errors or treat them as empty progress.
    exists = db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='progress_events'").fetchone()
    usable = exists and ready(db) and not db.execute('SELECT 1 FROM progress_events WHERE portable<1 LIMIT 1').fetchone()
    return 'progress_events' if usable else 'events'
