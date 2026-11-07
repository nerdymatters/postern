"""Compact inputs must preserve authoritative replay, old writers and recovery."""
import json
import sqlite3
import threading

import pytest

from postern import progress, progress_store
from postern.db import Database, SCHEMA, event
from postern.recover import import_events

ITEMS = {'pdf': {'kind': 'pdf', 'pages': 4},
         'video': {'kind': 'video', 'pages': 0, 'duration_seconds': 100}}


def connection(path=':memory:'):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def raw(db, identity, *, kind='page_served', payload='{"detail":{"page":1}}',
        seq=None, email='active', item='pdf', ts=1, replace=False):
    db.execute(f'INSERT {"OR REPLACE " if replace else ""}INTO events'
               '(seq,id,email,item_id,type,ts,payload,summary) VALUES (?,?,?,?,?,?,?,1)',
               (seq, identity, email, item, kind, ts, payload))


def finish(db):
    while not progress_store.rebuild_batch(db, limit=3):
        pass
    assert progress_store.ready(db)


def equivalent(monkeypatch, db, **kwargs):
    def outcome():
        try:
            return ('result', progress.history(db, ITEMS, **kwargs))
        except (ValueError, TypeError, AttributeError, OverflowError, RecursionError) as error:
            return ('error', type(error))
    projected = outcome()
    with monkeypatch.context() as patch:
        patch.setattr(progress_store, 'source', lambda db: 'events')
        original = outcome()
    assert projected == original
    return projected


@pytest.fixture(params=['python', 'rust'])
def backend(request, monkeypatch):
    if request.param == 'rust':
        pytest.importorskip('postern_progress_native')
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', request.param)


def test_compact_new_events_and_recovery_keep_selection_and_metadata(monkeypatch, backend):
    with connection() as db:
        db.executemany('INSERT INTO members VALUES (?,?,?)', [('active', 'A', 'reader'), ('second', 'B', 'reader')])
        finish(db)
        for email in ('active', 'second', 'removed'):
            for page in (0, 2, 2, -1, True, 99):
                event(db, 'page_served', email=email, item_id='pdf', detail={'page': page})
            event(db, 'video_progress', email=email, item_id='video',
                  detail={'ranges': [[0, 10], [5, 30], [-1, 100], [99, 100.5]]})
            event(db, 'item_open', email=email, item_id='pdf')
            event(db, 'view_requested', email=email, item_id='pdf')
        restored = [{'event_id': 'restored', 'email': 'active', 'event': 'item_summary',
                     'item_id': 'pdf', 'ts': 2, 'detail': {'pages': [1, 3]}}]
        assert import_events(db, restored) == 1
        assert import_events(db, [{**restored[0], 'detail': {'pages': [99]}}]) == 0
        assert progress_store.source(db) == 'progress_events'
        assert json.loads(db.execute("SELECT payload FROM progress_events WHERE id='restored'").fetchone()[0]) == {'detail': {'pages': [1, 3]}}
        assert db.execute('SELECT sum(length(payload)) FROM progress_events').fetchone()[0] < db.execute(
            'SELECT sum(length(payload)) FROM events').fetchone()[0]/3
        for email in (None, 'active', 'removed'):
            for coverage in (False, True):
                equivalent(monkeypatch, db, email=email, coverage=coverage)
        # No metadata-sensitive pre-reduction: edits still reinterpret history.
        with monkeypatch.context() as patch:
            patch.setitem(ITEMS, 'video', {**ITEMS['video'], 'duration_seconds': 20})
            patch.setitem(ITEMS, 'pdf', {**ITEMS['pdf'], 'pages': 1})
            equivalent(monkeypatch, db)


@pytest.mark.parametrize('payload', [
    b'{"detail":{"pages":[0]}}',
    '{', 'null', '[]', '{"detail":null}', '{"detail":{"pages":null}}',
    '{"detail":{"pages":[0,true,1.0,2]},"detail":{"pages":[3]}}',
    '{"detail":{"pages":[0]},"unused":NaN}',
    '{"detail":{"pages":[0]},"unused":"\\ud800"}',
    '{"detail":{"pages":[0]},"unused":' + '9'*5000 + '}',
    '{"detail":{"pages":[0]},"unused":' + '['*1100 + '0' + ']'*1100 + '}',
])
def test_rebuild_preserves_legacy_json_results_and_errors(monkeypatch, backend, payload):
    with connection() as db:
        db.execute("INSERT INTO members VALUES ('active','A','reader')")
        raw(db, 'legacy', kind='item_summary', payload=payload)
        finish(db)
        equivalent(monkeypatch, db)


def test_old_writer_mutations_replacement_and_export_keep_projection(monkeypatch, backend):
    with connection() as db:
        db.execute("INSERT INTO members VALUES ('active','A','reader')")
        finish(db)
        raw(db, 'first', seq=1, ts=0)
        raw(db, 'second', seq=2, ts=-5)
        equivalent(monkeypatch, db, email='active')
        equivalent(monkeypatch, db)

        # REPLACE deletes may not run DELETE triggers: clean both identities on insertion.
        raw(db, 'first', seq=2, replace=True, payload='{"detail":{"page":3}}')
        assert [tuple(row) for row in db.execute('SELECT seq,id FROM progress_events')] == [(2, 'first')]
        equivalent(monkeypatch, db)
        db.execute("UPDATE events SET seq=8,id='renamed',email='removed',item_id='video',type='video_progress',"
                   "payload='{\"detail\":{\"ranges\":[[0,20]]}}' WHERE seq=2")
        equivalent(monkeypatch, db, email='removed')
        db.execute("UPDATE events SET type='sign_in' WHERE seq=8")
        assert db.execute('SELECT count(*) FROM progress_events').fetchone()[0] == 0
        raw(db, 'renamed', seq=9, replace=True, kind='item_open')
        finish(db)
        before = db.execute('SELECT payload FROM progress_events').fetchone()[0]
        db.execute("UPDATE events SET batch_id='exported'")
        assert db.execute('SELECT payload FROM progress_events').fetchone()[0] == before
        db.commit()
        db.execute('DELETE FROM events')
        assert not db.execute('SELECT 1 FROM progress_events').fetchone()
        db.rollback()
        equivalent(monkeypatch, db)


def test_query_plan_reordering_keeps_legacy_clocks_on_the_original_path(monkeypatch, backend):
    with connection() as db:
        for member in range(4):
            email = str(member)
            db.execute('INSERT INTO members VALUES (?,?,?)', (email, email, 'reader'))
            raw(db, f'{member}-page', email=email, ts=0)
            raw(db, f'{member}-open', email=email, kind='item_open', ts=-2)
            raw(db, f'{member}-summary', email=email, kind='item_summary', ts=99,
                payload='{"detail":{"pages":[1]}}')
            for other in range(10):
                raw(db, f'{member}-other-{other}', email=email, kind='sign_in')
        finish(db)
        db.execute('ANALYZE')
        assert progress_store.source(db) == 'events'
        for email in (None, '0'):
            equivalent(monkeypatch, db, email=email)


def test_old_writer_uses_raw_until_idle_classification_then_compact_again(monkeypatch, backend):
    with connection() as db:
        db.execute("INSERT INTO members VALUES ('active','A','reader')")
        finish(db)
        raw(db, 'old-writer', payload='{"detail":{"page":2},"name":"unused audit metadata"}')
        assert progress_store.pending(db)
        assert progress_store.source(db) == 'events'
        finish(db)
        assert not progress_store.pending(db)
        assert progress_store.source(db) == 'progress_events'
        assert equivalent(monkeypatch, db)[1]['active', 'pdf']['pages'] == {2}


def test_input_mutation_after_encoding_cannot_change_recorded_evidence(monkeypatch, backend):
    detail = {'pages': [1]}
    original = progress_store.compact_created
    def interleaving(*args):
        detail['pages'].append(3)
        return original(*args)
    monkeypatch.setattr(progress_store, 'compact_created', interleaving)
    with connection() as db:
        db.execute("INSERT INTO members VALUES ('active','A','reader')")
        finish(db)
        event(db, 'item_summary', email='active', item_id='pdf', detail=detail)
        assert progress_store.source(db) == 'progress_events'
        assert equivalent(monkeypatch, db)[1]['active', 'pdf']['pages'] == {1}


def test_queued_audit_jobs_precede_idle_rebuild_and_shutdown(tmp_path, monkeypatch):
    path = tmp_path/'busy.sqlite3'
    # Actual pre-upgrade shape: no read table or triggers exist yet.
    db = sqlite3.connect(path)
    db.executescript(SCHEMA.removesuffix(progress_store.SCHEMA))
    raw(db, 'old')
    db.commit()
    db.close()
    entered, release, rebuilt = threading.Event(), threading.Event(), threading.Event()
    order = []
    original = progress_store.rebuild_batch
    def observe(db, limit=128):
        order.append('rebuild')
        result = original(db, limit)
        rebuilt.set()
        return result
    monkeypatch.setattr(progress_store, 'rebuild_batch', observe)
    database = Database(path)
    def first(db):
        order.append('first')
        entered.set()
        assert release.wait(3)
        return event(db, 'item_open', email='active', item_id='pdf')
    try:
        first_job = database.submit(first)
        assert entered.wait(3)
        second_job = database.submit(lambda db: order.append('second'))
        assert not rebuilt.is_set()
        release.set()
        first_job.result(3)
        second_job.result(3)
        assert rebuilt.wait(3)
        assert order[:3] == ['first', 'second', 'rebuild']
        database.submit(lambda db: None).result(3)
        assert database.read(progress_store.source) == 'progress_events'
    finally:
        release.set()
        database.close()
    assert not database.thread.is_alive()


def test_oversized_history_never_enters_projection_parser(monkeypatch, backend):
    payload = json.dumps({'detail': {'page': 2}, 'unused': 'x'*(128*1024)})
    with connection() as db:
        db.execute("INSERT INTO members VALUES ('active','A','reader')")
        raw(db, 'oversized', payload=payload)
        original = json.loads
        lengths = []
        def inspect(value, *args, **kwargs):
            lengths.append(len(value))
            return original(value, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(progress_store.json, 'loads', inspect)
            progress_store.compact_created(db, 'oversized', 'page_served')
            db.execute('DELETE FROM progress_events')
            finish(db)
        assert all(length <= 64*1024 for length in lengths)
        assert db.execute('SELECT length(CAST(payload AS BLOB)) FROM progress_events').fetchone()[0] <= 64*1024
        assert progress_store.source(db) == 'events'
        assert equivalent(monkeypatch, db)[1]['active', 'pdf']['pages'] == {2}


@pytest.mark.parametrize('already_built', [False, True])
def test_rebuild_yields_between_rows_at_its_time_budget(monkeypatch, already_built):
    with connection() as db:
        if already_built:
            finish(db)
        for number in range(5):
            raw(db, str(number))
        if not already_built:
            db.execute('DELETE FROM progress_events')
        ticks = iter([0, .03, .06, .09, .12, .15])
        with monkeypatch.context() as patch:
            patch.setattr(progress_store, 'monotonic', lambda: next(ticks), raising=False)
            assert not progress_store.rebuild_batch(db)
        assert db.execute('SELECT count(*) FROM progress_events WHERE portable!=-1').fetchone()[0] == 1
        finish(db)
        assert db.execute('SELECT count(*) FROM progress_events WHERE portable=1').fetchone()[0] == 5


def test_rebuild_is_resumable_bounded_and_handles_negative_sequences():
    with connection() as db:
        raw(db, 'minimum', seq=-(2**63))
        for number in range(8):
            raw(db, str(number), seq=number)
        db.execute('DELETE FROM progress_events')
        assert progress_store.source(db) == 'events'
        assert not progress_store.rebuild_batch(db, limit=3)
        assert db.execute('SELECT count(*) FROM progress_events').fetchone()[0] == 3
        db.commit()
        assert not progress_store.rebuild_batch(db, limit=3)
        db.rollback()
        assert db.execute('SELECT count(*) FROM progress_events').fetchone()[0] == 3
        # Old-writer changes below the persisted cursor must still be mirrored.
        raw(db, 'late', seq=-3)
        db.execute("UPDATE events SET payload='{\"detail\":{\"page\":3}}' WHERE id='minimum'")
        finish(db)
        assert db.execute('SELECT count(*) FROM progress_events').fetchone()[0] == 10
        assert db.execute("SELECT payload FROM progress_events WHERE id='minimum'").fetchone()[0] == '{"detail":{"page":3}}'


def test_projection_and_ready_marker_share_the_read_snapshot(tmp_path, monkeypatch, backend):
    path = tmp_path/'snapshot.sqlite3'
    with connection(path) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("INSERT INTO members VALUES ('active','A','reader')")
        raw(writer, 'old')
        writer.commit()
        reader = sqlite3.connect(path)
        reader.row_factory = sqlite3.Row
        try:
            reader.execute('BEGIN')
            assert progress_store.source(reader) == 'events'
            finish(writer)
            raw(writer, 'new', payload='{"detail":{"page":3}}')
            finish(writer)
            writer.commit()
            assert progress_store.source(reader) == 'events'
            assert equivalent(monkeypatch, reader)[1]['active', 'pdf']['pages'] == {1}
            reader.rollback()
            reader.execute('BEGIN')
            assert progress_store.source(reader) == 'progress_events'
            assert equivalent(monkeypatch, reader)[1]['active', 'pdf']['pages'] == {1, 3}
        finally:
            reader.close()


def test_idle_rebuild_failure_rolls_back_and_restarts_without_stopping_audit(tmp_path, monkeypatch):
    path = tmp_path/'existing.sqlite3'
    with connection(path) as db:
        raw(db, 'old')
        db.execute('DELETE FROM progress_events')
    attempted = threading.Event()
    original = progress_store.rebuild_batch
    def broken(db, limit=128):
        original(db, limit)
        attempted.set()
        raise sqlite3.OperationalError('injected rebuild failure')
    with monkeypatch.context() as patch:
        patch.setattr(progress_store, 'rebuild_batch', broken)
        database = Database(path)
        try:
            assert attempted.wait(3)
            # Submitted after failure; completion proves rollback and continued service.
            assert database.submit(lambda db: event(db, 'item_open', email='active', item_id='pdf')).result(3)
            assert database.progress_rebuild_error == 'OperationalError'
            assert database.read(progress_store.source) == 'events'
            assert database.read(lambda db: db.execute("SELECT 1 FROM progress_events WHERE id='old'").fetchone()) is None
        finally:
            database.close()
    completed = threading.Event()
    def observed(db, limit=128):
        result = original(db, limit)
        if result:
            completed.set()
        return result
    monkeypatch.setattr(progress_store, 'rebuild_batch', observed)
    database = Database(path)
    try:
        assert completed.wait(3)
        database.submit(lambda db: None).result(3)  # Wait for the rebuild's commit.
        assert database.read(progress_store.source) == 'progress_events'
        assert database.progress_rebuild_error is None
        assert database.read(lambda db: db.execute('SELECT count(*) FROM progress_events').fetchone()[0]) == 2
    finally:
        database.close()


def test_rowid_alias_moves_and_deletes_keep_projection(monkeypatch, backend):
    # Value: protects=a sequence change spelled through rowid/oid/_rowid_ is mirrored, and a
    # later delete cannot leave stale evidence; fails_when=only UPDATE OF seq is observed;
    # seam=raw SQL as an old writer or operator would issue it; no_claim=cache timing.
    with connection() as db:
        db.execute("INSERT INTO members VALUES ('active','A','reader')")
        finish(db)
        raw(db, 'moved', seq=1, payload='{"detail":{"page":0}}')
        raw(db, 'kept', seq=2, payload='{"detail":{"page":1}}')
        finish(db)
        generation = db.execute('SELECT value FROM progress_generation').fetchone()[0]
        for alias, target in (('rowid', 50), ('oid', 60), ('_rowid_', 70)):
            db.execute(f"UPDATE events SET {alias}=? WHERE id='moved'", (target,))
            finish(db)
            assert ([tuple(row) for row in db.execute('SELECT seq,id FROM progress_events ORDER BY seq')]
                    == [tuple(row) for row in db.execute("SELECT seq,id FROM events ORDER BY seq")])
            assert db.execute('SELECT value FROM progress_generation').fetchone()[0] > generation
            generation = db.execute('SELECT value FROM progress_generation').fetchone()[0]
            assert progress_store.source(db) == 'progress_events'
            equivalent(monkeypatch, db)
        db.execute("DELETE FROM events WHERE id='moved'")
        finish(db)
        assert [tuple(row) for row in db.execute('SELECT seq,id FROM progress_events')] == [(2, 'kept')]
        assert progress_store.source(db) == 'progress_events'
        assert sorted(equivalent(monkeypatch, db, email='active')[1][('active', 'pdf')]['pages']) == [1]


@pytest.mark.parametrize('removal', ['read_table', 'generation_table', 'event_trigger'])
def test_missing_projection_objects_rebuild_from_events(tmp_path, monkeypatch, removal):
    # Value: protects=a removed rebuildable table or trigger cannot leave the authoritative
    # ready marker trusting an empty or incomplete projection; fails_when=startup recreates
    # the objects but keeps the marker; seam=pre-startup SQL surgery on a stopped database;
    # no_claim=live schema changes while a service is running.
    path = tmp_path/'surgery.sqlite3'
    with connection(path) as db:
        db.execute("INSERT INTO members VALUES ('active','A','reader')")
        raw(db, 'first', seq=1, payload='{"detail":{"page":0}}')
        raw(db, 'second', seq=2, payload='{"detail":{"page":2}}')
        finish(db)
        if removal == 'read_table':
            db.execute('DROP TABLE progress_events')
        elif removal == 'generation_table':
            db.execute('DROP TABLE progress_generation')
        else:
            db.execute('DROP TRIGGER progress_event_insert')
            raw(db, 'unmirrored', seq=3, payload='{"detail":{"page":3}}')
        db.commit()
    gate, entered, rebuilt = threading.Event(), threading.Event(), threading.Event()
    original = progress_store.rebuild_batch
    def held(db, limit=128):
        entered.set()
        assert gate.wait(5)
        complete = original(db, limit)
        if complete:
            rebuilt.set()
        return complete
    monkeypatch.setattr(progress_store, 'rebuild_batch', held)
    database = Database(path)
    try:
        assert entered.wait(5)
        # Before the rebuild finishes, reads use authoritative events, not a stale marker.
        assert database.read(progress_store.source) == 'events'
        gate.set()
        assert rebuilt.wait(5), 'Projection was not rebuilt'
        database.submit(lambda db: None).result(3)  # Wait for the rebuild's commit.
        assert database.read(progress_store.source) == 'progress_events'
        projected = database.read(lambda db: progress.history(db, ITEMS, email='active'))
        with monkeypatch.context() as patch:
            patch.setattr(progress_store, 'source', lambda db: 'events')
            authoritative = database.read(lambda db: progress.history(db, ITEMS, email='active'))
        assert projected == authoritative
        expected = [0, 2, 3] if removal == 'event_trigger' else [0, 2]
        assert sorted(projected[('active', 'pdf')]['pages']) == expected
        assert database.read(progress_store.complete)
    finally:
        gate.set()
        database.close()


def test_fresh_database_projection_is_ready_before_first_request(tmp_path):
    # Value: protects=new installs read the compact table from the first request;
    # fails_when=the synchronous empty-history ready marker before readiness is removed or
    # gated wrongly; why_new=other tests start from existing events and wait for idle rebuild;
    # seam=none; no_claim=does not cover installs that already have events.
    database = Database(tmp_path/'fresh.sqlite3')
    try:
        assert database.read(progress_store.source) == 'progress_events'
        assert database.read(progress_store.ready)
        assert database.progress_rebuild_error is None
    finally:
        database.close()


def test_read_connection_drops_progress_owner_when_file_is_replaced(tmp_path, monkeypatch):
    # Value: protects=evidence from a connection opened during a file replacement is never
    # retained by the owner's cache; fails_when=Database.read ignores the before/after inode
    # comparison; why_new=cache tests construct ReadConnection owners directly; seam=none;
    # no_claim=does not prove an actual concurrent rename race.
    import pathlib
    database = Database(tmp_path/'owned.sqlite3')
    try:
        seen = lambda db: (db.progress_owner, db.progress_file)
        owner, stamp = database.read(seen)
        assert owner is database and stamp[0] == str((tmp_path/'owned.sqlite3').resolve())
        original, calls = pathlib.Path.stat, []
        def moved(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            calls.append(1)
            if len(calls) > 1:  # The file is replaced after the read's first identity check.
                return type('Stat', (), {'st_dev': result.st_dev, 'st_ino': result.st_ino + 1})()
            return result
        monkeypatch.setattr(pathlib.Path, 'stat', moved)
        owner, _ = database.read(seen)
        assert owner is None
    finally:
        monkeypatch.undo()
        database.close()
