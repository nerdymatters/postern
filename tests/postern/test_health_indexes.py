"""Health and exporter read cost on retained history, using disposable SQLite data."""
import asyncio
import contextlib
import sqlite3

from postern.db import SCHEMA
from postern.service import Service


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
        assert any('SCAN e USING COVERING INDEX' in step for step in pending_plan), pending_plan
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
