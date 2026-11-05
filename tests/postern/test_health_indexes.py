"""Health and exporter read cost on retained history, using disposable SQLite data."""
import asyncio
import contextlib
import hashlib
import json
import sqlite3
import random

import pytest

from postern.db import SCHEMA, pending_health
from postern.service import Service
from postern.storage import StoreUnavailable


def assert_pending_matches_history(db):
    expected = db.execute('''SELECT e.seq,e.id,e.ts,e.batch_id FROM events e
        LEFT JOIN batches b ON b.id=e.batch_id WHERE e.batch_id IS NOT NULL AND b.id IS NULL ORDER BY e.seq''').fetchall()
    actual = db.execute('SELECT seq,id,ts,batch_id FROM health_orphan_events ORDER BY seq').fetchall()
    assert [tuple(r) for r in actual] == [tuple(r) for r in expected]
    reference = db.execute('SELECT MIN(e.ts),COUNT(*) FROM events e LEFT JOIN batches b ON b.id=e.batch_id WHERE b.confirmed_at IS NULL').fetchone()
    assert tuple(pending_health(db)) == tuple(reference)


def batch(db, identity, confirmed):
    db.execute('''INSERT OR REPLACE INTO batches(id,drive_id,sha256,bytes,count,created_at,confirmed_at)
        VALUES(?,'synthetic','synthetic',1,1,1,?)''', (identity, confirmed))


def test_pending_health_stays_exact_through_replacements_reversals_and_aborted_writes(config, store, clock):
    with running_service(config, store, clock) as service:
        def exercise(db):
            rng = random.Random(83271)
            for i in range(250):
                identity, target = f'e{rng.randrange(12)}', rng.choice([None, '', 'b0', 'b1', 'b2'])
                operation = i % 7
                if operation == 0:
                    db.execute("INSERT OR REPLACE INTO events(id,ts,type,summary,payload,batch_id) VALUES(?,?,'synthetic',1,'{}',?)", (identity, rng.randrange(100), target))
                elif operation == 1:
                    db.execute('UPDATE events SET ts=?,batch_id=? WHERE id=?', (rng.randrange(100), target, identity))
                elif operation == 2:
                    batch(db, target, rng.choice([None, 0, 300]))
                elif operation == 3:
                    db.execute('UPDATE batches SET confirmed_at=? WHERE id=?', (rng.choice([None, 0, 400]), target))
                elif operation == 4:
                    db.execute('DELETE FROM batches WHERE id=?', (target,))
                elif operation == 5:
                    db.execute('DELETE FROM events WHERE id=?', (identity,))
                else:
                    db.execute('UPDATE OR REPLACE batches SET id=? WHERE id=?', (target, 'b0'))
                assert_pending_matches_history(db)
            # Replace conflicting unique event identities/rowids without relying
            # on recursive DELETE triggers (legacy writers leave them off).
            db.execute("INSERT OR REPLACE INTO events(seq,id,ts,type,summary,payload) VALUES(900,'a',1,'synthetic',1,'{}'),(901,'b',2,'synthetic',1,'{}')")
            db.execute("UPDATE OR REPLACE events SET seq=901,id='b' WHERE seq=900")
            assert_pending_matches_history(db)
        service.db.submit(exercise).result(timeout=5)
        before = service.db.read(lambda db: [tuple(r) for r in db.execute('SELECT * FROM health_orphan_events ORDER BY seq')])
        def aborted(db):
            db.execute("DELETE FROM events")
            assert_pending_matches_history(db)
            raise ValueError('rollback this write')
        with pytest.raises(ValueError, match='rollback this write'):
            service.db.submit(aborted).result(timeout=5)
        assert service.db.read(lambda db: [tuple(r) for r in db.execute('SELECT * FROM health_orphan_events ORDER BY seq')]) == before


def test_existing_database_backfill_and_legacy_writer_rollback_keep_health_exact(config, store, clock):
    path = config.data_dir / 'postern.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA)
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'health_%'").fetchall():
            db.execute(f'DROP TRIGGER {name}')
        db.execute('DROP TABLE health_orphan_events')
        batch(db, 'confirmed', 300)
        db.executemany("INSERT INTO events(id,ts,type,summary,payload,batch_id) VALUES(?,?,'synthetic',1,'{}',?)",
                       [('pending', 10, None), ('done', 20, 'confirmed'), ('orphan', 30, 'missing')])
        db.commit()
        original = db.execute('SELECT * FROM events ORDER BY seq').fetchall()
    with running_service(config, store, clock) as service:
        service.db.read(assert_pending_matches_history)
        assert service.db.read(lambda db: [tuple(r) for r in db.execute('SELECT * FROM events ORDER BY seq')]) == original
    # Old code knows only the original event/batch tables. Its writes still fire
    # the retained triggers, including confirmation reversal and batch deletion.
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE batches SET confirmed_at=NULL WHERE id='confirmed'")
        db.execute("INSERT INTO events(id,ts,type,summary,payload) VALUES('old-writer',5,'synthetic',1,'{}')")
        db.commit()
        assert_pending_matches_history(db)
        db.execute('DELETE FROM batches')
        db.commit()
        assert_pending_matches_history(db)
    with running_service(config, store, clock) as service:
        assert asyncio.run(service.health(private=True))['pending_events'] == 4
        service.db.read(assert_pending_matches_history)


def test_health_work_does_not_grow_with_confirmed_event_history(config, store, clock, monkeypatch):
    with running_service(config, store, clock) as service:
        def seed(db):
            batch(db, 'done', 300)
            db.executemany("INSERT INTO events(id,ts,type,summary,payload,batch_id) VALUES(?,1,'synthetic',1,'{}','done')", ((f'old-{i}',) for i in range(3000)))
            db.execute("INSERT INTO events(id,ts,type,summary,payload,batch_id) VALUES('orphan',10,'synthetic',1,'{}','missing')")
        service.db.submit(seed).result(timeout=5)
        steps = [0]
        read = service.db.read
        def counted(operation):
            def query(db):
                def progress():
                    steps[0] += 100
                    return 0
                db.set_progress_handler(progress, 100)
                return operation(db)
            return read(query)
        monkeypatch.setattr(service.db, 'read', counted)
        health = asyncio.run(service.health(private=True))
        assert (health['pending_events'], health['oldest_unexported_at']) == (1, 10)
        # This is a SQLite-work bound, not a host-clock assertion. Scanning 3,000
        # confirmed events exceeds it even when those pages are already cached.
        assert steps[0] < 2000, steps[0]


@contextlib.contextmanager
def running_service(config, store, clock):
    # No background loop: confirmations are controlled by the test, not a fake
    # cloud upload racing the assertions. close() also drains the real writer.
    service = Service(config, store=store, clock=clock)
    try:
        yield service
    finally:
        asyncio.run(service.close())


def test_existing_history_gets_metadata_only_health_reads_without_changing_backup_status(config, store, clock, monkeypatch):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(config.data_dir / 'postern.sqlite3')) as db:
        # An existing database with the same tables but no optional read indexes.
        db.executescript('\n'.join(line for line in SCHEMA.splitlines() if not line.startswith('CREATE INDEX')))
        db.executemany("INSERT INTO batches(id,drive_id,sha256,bytes,count,created_at,confirmed_at,summary_confirmed) VALUES(?,'synthetic-drive','synthetic-sha',1,1,?,?,?)",
                       [('waiting', 1, None, 0), ('done', 2, 500, 1), ('needs-summary', 3, 600, 0)])
        db.executemany('INSERT INTO events(id,ts,type,summary,payload,batch_id) VALUES(?,?,?,?,?,?)',
                       [('unbatched', 10, 'synthetic', 1, '{}', None),
                        ('pending', 20, 'synthetic', 1, '{}', 'waiting'),
                        ('orphan', 30, 'synthetic', 1, '{}', 'missing'),
                        ('confirmed', 40, 'synthetic', 1, '{}', 'done'),
                        ('unsummarized', 50, 'synthetic', 1, '{}', 'needs-summary')])
        db.commit()
        original_events = db.execute('SELECT * FROM events ORDER BY seq').fetchall()

    with running_service(config, store, clock) as service:
        statements = []
        original_read = service.db.read
        def traced(operation):
            def query(db):
                db.set_trace_callback(statements.append)
                return operation(db)
            return original_read(query)
        monkeypatch.setattr(service.db, 'read', traced)
        health = asyncio.run(service.health(private=True))
        assert (health['pending_events'], health['oldest_unexported_at'], health['last_export_at']) == (3, 10, 600)
        assert health['backup'] == 'delayed'
        assert original_read(lambda db: [tuple(r) for r in db.execute('SELECT * FROM events ORDER BY seq')]) == original_events

        # These are the actual health statements, not a second implementation.
        # Covering reads use only the small metadata index, avoiding historical
        # payload pages. Neither assertion depends on an index's name or timing.
        pending = next(sql for sql in statements if 'MIN(e.ts)' in sql)
        latest = next(sql for sql in statements if 'MAX(confirmed_at)' in sql)
        pending_plan = original_read(lambda db: [r[3] for r in db.execute('EXPLAIN QUERY PLAN ' + pending)])
        latest_plan = original_read(lambda db: [r[3] for r in db.execute('EXPLAIN QUERY PLAN ' + latest)])
        event_reads = [step for step in pending_plan if step.startswith(('SCAN e ', 'SEARCH e ', 'SCAN events ', 'SEARCH events '))]
        # Both the unbatched lookup and the pending-batch join must stay covered.
        assert len(event_reads) == 2 and all('USING COVERING INDEX' in step for step in event_reads), pending_plan
        assert any('USING COVERING INDEX' in step for step in latest_plan), latest_plan

        def confirm(db):
            db.execute("UPDATE batches SET confirmed_at=700 WHERE id='waiting'")
            db.execute("INSERT INTO batches(id,drive_id,sha256,bytes,count,created_at,confirmed_at,summary_confirmed) VALUES('missing','synthetic-drive','synthetic-sha',1,1,4,800,1)")
        service.db.submit(confirm).result(timeout=5)
        health = asyncio.run(service.health(private=True))
        assert (health['pending_events'], health['oldest_unexported_at']) == (1, 10)
        service.db.submit(lambda db: db.execute("UPDATE events SET batch_id='done' WHERE id='unbatched'")).result(timeout=5)
        health = asyncio.run(service.health(private=True))
        assert health['pending_events'] == 0 and health['oldest_unexported_at'] is None
        assert health['last_export_at'] == 800 and health['backup'] == 'current'
        # A missing batch must still make its retained events pending again.
        service.db.submit(lambda db: db.execute("DELETE FROM batches WHERE id='missing'")).result(timeout=5)
        assert asyncio.run(service.health(private=True))['pending_events'] == 1

    # Normal reopen is idempotent and keeps the same activity and health meaning.
    with running_service(config, store, clock) as service:
        assert asyncio.run(service.health(private=True))['pending_events'] == 1
        assert service.db.read(lambda db: db.execute('SELECT count(*) FROM events').fetchone()[0]) == 5


def test_idle_export_work_stays_bounded_as_confirmed_history_grows(config, store, clock, monkeypatch):
    with running_service(config, store, clock) as service:
        def seed(db):
            db.executemany("INSERT INTO batches(id,drive_id,sha256,bytes,count,created_at,confirmed_at,summary_confirmed) VALUES(?,'synthetic-drive','synthetic-sha',1,1,?,?,1)",
                           ((f'batch-{i}', i, i + 1) for i in range(2000)))
            db.executemany('INSERT INTO events(id,ts,type,summary,payload,batch_id) VALUES(?,?,?,1,?,?)',
                           ((f'event-{i}', i + 1, 'synthetic', '{}', f'batch-{i % 2000}') for i in range(12000)))
        service.db.submit(seed).result(timeout=5)
        original_read = service.db.read
        counts = []
        def bounded(operation):
            def query(db):
                steps = 0
                def budget():
                    nonlocal steps
                    steps += 100
                    return int(steps > 2000)
                db.set_progress_handler(budget, 100)
                try:
                    return operation(db)
                finally:
                    db.set_progress_handler(None, 0)
                    counts.append(steps)
            return original_read(query)
        monkeypatch.setattr(service.db, 'read', bounded)
        # Real export() returns after finding no pending batch and no unbatched
        # event. Scanning either history table exhausts this instruction budget.
        asyncio.run(service.export())
        assert len(counts) == 2 and max(counts) <= 2000


def test_export_seals_only_selected_ids_and_retries_identical_bytes(config, store, clock, monkeypatch):
    with running_service(config, store, clock) as service:
        def insert(db, identity, batch_id=None):
            db.execute("INSERT INTO events(id,ts,type,summary,payload,batch_id) VALUES(?,1,'synthetic',1,?,?)",
                       (identity, json.dumps({'event_id': identity}), batch_id))
        def seed(db):
            batch(db, 'already-exported', 300)
            db.execute("UPDATE batches SET summary_confirmed=1")
            for i in range(501):
                insert(db, f'pending-{i}')
                if i == 250:
                    insert(db, 'interleaved-confirmed', 'already-exported')
        service.db.submit(seed).result(timeout=5)
        new_id, put_file = store.new_id, store.put_file
        def allocate():
            service.db.submit(lambda db: insert(db, 'late-arrival')).result(timeout=5)
            return new_id()
        attempts = []
        def upload(identity, path, name, digest, properties):
            attempts.append((identity, path.read_bytes(), digest, properties.copy()))
            if len(attempts) == 1:
                raise StoreUnavailable('Synthetic upload interruption')
            return put_file(identity, path, name, digest, properties)
        monkeypatch.setattr(store, 'new_id', allocate)
        monkeypatch.setattr(store, 'put_file', upload)
        with pytest.raises(StoreUnavailable, match='Synthetic upload interruption'):
            asyncio.run(service.export())
        service.db.read(assert_pending_matches_history)
        assert asyncio.run(service.health(private=True))['pending_events'] == 502
        sealed = service.db.read(lambda db: dict(db.execute("SELECT * FROM batches WHERE id!='already-exported'").fetchone()))
        assert sealed['count'] == 500 and sealed['confirmed_at'] is None
        assigned = service.db.read(lambda db: {r[0]: r[1] for r in db.execute('SELECT id,batch_id FROM events')})
        expected = {f'pending-{i}' for i in range(500)}
        assert {identity for identity, target in assigned.items() if target == sealed['id']} == expected
        assert assigned['interleaved-confirmed'] == 'already-exported'
        assert assigned['late-arrival'] is None and assigned['pending-500'] is None
        content = attempts[0][1]
        assert {json.loads(line)['event_id'] for line in content.splitlines()} == expected
        assert len(content.splitlines()) == 500
        assert hashlib.sha256(content).hexdigest() == sealed['sha256']
        asyncio.run(service.export())
        assert attempts[0] == attempts[1]
        service.db.read(assert_pending_matches_history)
        assert asyncio.run(service.health(private=True))['pending_events'] == 2


def test_audit_insert_work_stays_bounded_with_existing_orphans(config, store, clock):
    with running_service(config, store, clock) as service:
        def exercise(db):
            db.executemany("INSERT INTO events(id,ts,type,summary,payload,batch_id) VALUES(?,1,'synthetic',0,'{}','missing')",
                           ((f'orphan-{i}',) for i in range(3000)))
            steps = [0]
            def progress():
                steps[0] += 100
                return 0
            db.set_progress_handler(progress, 100)
            try:
                db.execute("INSERT INTO events(id,ts,type,summary,payload) VALUES('new-audit',1,'synthetic',0,'{}')")
            finally:
                db.set_progress_handler(None, 0)
            assert steps[0] < 1000, steps[0]
            assert db.execute("SELECT COUNT(*) FROM events WHERE id='new-audit'").fetchone()[0] == 1
            assert_pending_matches_history(db)
        service.db.submit(exercise).result(timeout=5)
