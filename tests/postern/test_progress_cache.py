"""Exercise the real writer/read boundary, history repairs and snapshot ordering."""
import copy
import json
import sqlite3

import pytest

from postern import progress, progress_backend, progress_store
from postern.db import Database, event
from postern.progress_cache import HistoryCache, retained_size

pytest.importorskip('postern_progress_native')

ITEMS = {'pdf': {'kind': 'pdf', 'pages': 4},
         'video': {'kind': 'video', 'pages': 0, 'duration_seconds': 100}}
RAW = ("SELECT e.email,e.item_id,e.type,e.ts,e.payload FROM events e JOIN members m ON m.email=e.email "
       "WHERE e.type IN ('page_served','item_summary','video_progress','item_open','view_requested')")


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'rust')
    owner = Database(tmp_path/'history.sqlite3')
    write(owner, lambda db: db.execute("INSERT INTO members VALUES ('reader','Reader','reader')"))
    try:
        yield owner
    finally:
        owner.close()


def write(owner, operation):
    def apply(db):
        result = operation(db)
        while progress_store.pending(db):
            progress_store.rebuild_batch(db)
        return result
    return owner.submit(apply).result(timeout=5)


def raw(db, seq, identity, page, replace=False):
    db.execute(('INSERT OR REPLACE' if replace else 'INSERT')+
               ' INTO events(seq,id,ts,type,email,item_id,summary,payload) VALUES(?,?,10,?,?,?,?,?)',
               (seq, identity, 'page_served', 'reader', 'pdf', 0, json.dumps({'detail': {'page': page}})))


def read(owner, cache, items=ITEMS):
    def snapshot(db):
        db.execute('BEGIN')
        actual = cache.history(db, items)
        assert actual == progress.history(db, items) == progress._reduce_python(items, db.execute(RAW))
        return actual
    return owner.read(snapshot)


def test_append_and_old_writer_mutations_reinterpret_authoritative_history(owner):
    cache = HistoryCache(owner)
    write(owner, lambda db: (raw(db, 100, 'first', 0), raw(db, 200, 'second', 1)))
    assert read(owner, cache)['reader', 'pdf']['pages'] == {0, 1}
    read(owner, cache)
    assert list(cache.row_counts) == [2, 0]
    write(owner, lambda db: event(db, 'page_served', email='reader', item_id='pdf', detail={'page': 2}))
    assert read(owner, cache)['reader', 'pdf']['pages'] == {0, 1, 2}
    assert cache.row_counts[-1] == 1 and cache.full_replays == 1
    mutations = [
        (lambda db: db.execute("UPDATE events SET payload=? WHERE id='first'", ('{"detail":{"page":3}}',)), {1, 2, 3}),
        (lambda db: db.execute("DELETE FROM events WHERE id='second'"), {2, 3}),
        (lambda db: raw(db, 100, 'replacement', 1, True), {1, 2}),
        (lambda db: raw(db, 250, 'replacement', 0, True), {0, 2}),
        (lambda db: raw(db, 250, 'tail', 3, True), {2, 3}),
        (lambda db: raw(db, 50, 'early', 1), {1, 2, 3}),
        (lambda db: db.execute("UPDATE events SET email='removed' WHERE id='early'"), {2, 3}),
    ]
    for operation, expected in mutations:
        before = cache.full_replays
        write(owner, operation)
        assert read(owner, cache)['reader', 'pdf']['pages'] == expected
        assert cache.full_replays == before+1
    before = cache.full_replays
    write(owner, lambda db: db.execute("UPDATE events SET batch_id='export'"))
    read(owner, cache)
    assert cache.full_replays == before and cache.row_counts[-1] == 0


def test_roster_and_metadata_changes_rebuild_union_evidence(owner):
    cache, items = HistoryCache(owner), copy.deepcopy(ITEMS)
    write(owner, lambda db: (event(db, 'page_served', email='reader', item_id='pdf', detail={'page': 3}),
                            event(db, 'video_progress', email='reader', item_id='video', detail={'ranges': [[0, 10], [20, 30], [99, 100.5]]})))
    original = read(owner, cache, items)
    assert original['reader', 'video']['watched_seconds'] == 21
    items['pdf']['pages'], items['video']['duration_seconds'] = 3, 10
    smaller = read(owner, cache, items)
    assert smaller['reader', 'pdf']['pages'] == set()
    assert smaller['reader', 'video']['watched_seconds'] == 10
    assert read(owner, cache) == original
    write(owner, lambda db: db.execute("DELETE FROM members WHERE email='reader'"))
    assert read(owner, cache) == {}
    write(owner, lambda db: db.execute("INSERT INTO members VALUES ('reader','Returned','reader')"))
    assert read(owner, cache) == original


def test_old_snapshot_cannot_poison_new_snapshot(owner):
    cache = HistoryCache(owner)
    write(owner, lambda db: raw(db, 1, 'old', 0))
    def old_snapshot(db):
        db.execute('BEGIN')
        before = progress.history(db, ITEMS)  # Pin the WAL snapshot.
        write(owner, lambda writer: raw(writer, 2, 'new', 1))
        assert read(owner, cache)['reader', 'pdf']['pages'] == {0, 1}
        assert cache.history(db, ITEMS) == before
    owner.read(old_snapshot)
    assert read(owner, cache)['reader', 'pdf']['pages'] == {0, 1}


def test_uncommitted_query_only_transaction_and_foreign_owner_never_retained(owner, tmp_path):
    cache = HistoryCache(owner)
    write(owner, lambda db: raw(db, 1, 'old', 0))
    read(owner, cache)
    def rolled_back(db):
        raw(db, 2, 'rolled-back', 1)
        while progress_store.pending(db):
            progress_store.rebuild_batch(db)
        db.execute('PRAGMA query_only=ON')
        try:
            assert cache.history(db, ITEMS)['reader', 'pdf']['pages'] == {0, 1}
            assert cache.stamp is None
        finally:
            db.execute('PRAGMA query_only=OFF')
        raise ValueError('rollback')
    with pytest.raises(ValueError, match='rollback'):
        owner.submit(rolled_back).result(5)
    write(owner, lambda db: raw(db, 2, 'committed', 2))
    assert read(owner, cache)['reader', 'pdf']['pages'] == {0, 2}
    other = Database(tmp_path/'other.sqlite3')
    try:
        write(other, lambda db: (db.execute("INSERT INTO members VALUES ('reader','Reader','reader')"), raw(db, 2, 'different', 3)))
        assert read(other, cache)['reader', 'pdf']['pages'] == {3}
        assert cache.stamp is None
    finally:
        other.close()
    assert read(owner, cache)['reader', 'pdf']['pages'] == {0, 2}
    def really_readonly(db):
        db.execute('PRAGMA query_only=OFF')
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            db.execute('DELETE FROM events')
    owner.read(really_readonly)


def test_legacy_failure_repair_budget_and_python_rollback(owner, monkeypatch):
    cache = HistoryCache(owner)
    write(owner, lambda db: raw(db, 1, 'old', 0))
    expected = read(owner, cache)
    write(owner, lambda db: db.execute("UPDATE events SET payload='{'"))
    with pytest.raises(ValueError):
        read(owner, cache)
    assert cache.stamp is None
    write(owner, lambda db: db.execute('UPDATE events SET payload=?', ('{"detail":{"page":0}}',)))
    assert read(owner, cache) == expected
    tiny = HistoryCache(owner, budget=1)
    for _ in range(2):
        assert read(owner, tiny) == expected
        assert tiny.stamp is None and tiny.retained_bytes == 0
    assert tiny.full_replays == 2
    for _ in range(40):
        assert read(owner, cache) == expected
    assert len(cache.row_counts) == 16
    assert cache.retained_bytes == retained_size((cache.checkpoint, cache.stamp)) > retained_size(cache.checkpoint)
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'python')
    assert read(owner, cache) == expected and cache.stamp is None


def test_owner_path_change_rebuilds_and_append_uses_primary_key_range(owner, tmp_path):
    cache = HistoryCache(owner)
    write(owner, lambda db: raw(db, 100, 'old', 0))
    assert read(owner, cache)['reader', 'pdf']['pages'] == {0}
    queries = []
    def incremental(db):
        db.execute('BEGIN')
        db.set_trace_callback(queries.append)
        cache.history(db, ITEMS)
        query = next(sql for sql in queries if 'SELECT e.email' in sql)
        plan = [row[3] for row in db.execute('EXPLAIN QUERY PLAN '+query)]
        assert any('SEARCH e USING INTEGER PRIMARY KEY (rowid>?)' in step for step in plan)
    owner.read(incremental)
    other = Database(tmp_path/'replacement.sqlite3')
    original_path = owner.path
    try:
        write(other, lambda db: (db.execute("INSERT INTO members VALUES ('reader','Reader','reader')"), raw(db, 100, 'different', 3)))
        owner.path = other.path
        assert read(owner, cache)['reader', 'pdf']['pages'] == {3}
        owner.path = original_path
        assert read(owner, cache)['reader', 'pdf']['pages'] == {0}
    finally:
        owner.path = original_path
        other.close()


@pytest.mark.parametrize('sequence,append_before_read', [(0, False), (-1, True), (-(2**63), True)])
def test_empty_checkpoint_accepts_nonpositive_history(owner, sequence, append_before_read):
    cache = HistoryCache(owner)
    assert read(owner, cache) == {}
    write(owner, lambda db: raw(db, sequence, 'first', 0))
    if append_before_read:
        write(owner, lambda db: raw(db, 1, 'positive', 1))
    expected = {0, 1} if append_before_read else {0}
    assert read(owner, cache)['reader', 'pdf']['pages'] == expected
    write(owner, lambda db: raw(db, 2, 'later', 2))
    assert read(owner, cache)['reader', 'pdf']['pages'] == expected | {2}


def checks(tmp_path):
    return json.loads((tmp_path / progress_backend.CHECK).read_text())


def test_a_drifted_checkpoint_is_caught_served_from_python_and_reported(owner, tmp_path):
    # Value: protects=a report extending a wrong checkpoint is replaced by the full Python replay and recorded; fails_when=the comparison is skipped or its result is served anyway; why_new=parity runs in another process; seam=none; no_claim=watchdog reaction (test_progress_backend_selection)
    progress_backend.health.configure(tmp_path)
    cache = HistoryCache(owner)
    write(owner, lambda db: raw(db, 100, 'first', 0))
    read(owner, cache)
    cache.checkpoint = []  # drift: the evidence for page 0 is lost while stamp and watermark still match
    write(owner, lambda db: raw(db, 200, 'second', 1))
    assert read(owner, cache)['reader', 'pdf']['pages'] == {0, 1}  # read() also checks it equals Python
    assert cache.stamp is None and cache.delta_replays == 1
    record = checks(tmp_path)
    assert (record['checks'], record['mismatches'], record['last_check'], record['native_errors']) == (1, 1, 'mismatch', 0)
    read(owner, cache)  # the next report rebuilds from the full history
    assert cache.full_replays == 2 and checks(tmp_path)['checks'] == 1


def test_comparisons_run_on_extended_checkpoints_at_most_every_15_minutes(owner, tmp_path):
    # Value: protects=the Python comparison runs on the first extended report, then at most every 15 min; fails_when=it runs on every report or never again; why_new=no test pins the cadence; seam=none; no_claim=real request latency
    progress_backend.health.configure(tmp_path)
    now = [1000.0]
    cache = HistoryCache(owner, clock=lambda: now[0])
    write(owner, lambda db: raw(db, 1, 'first', 0))
    read(owner, cache)  # a full replay: nothing to compare
    assert checks(tmp_path)['checks'] == 0
    for seq, expected in ((2, 1), (3, 1)):
        write(owner, lambda db: raw(db, seq, f'e{seq}', 1))
        read(owner, cache)
        assert checks(tmp_path)['checks'] == expected
    now[0] += 900
    write(owner, lambda db: raw(db, 4, 'e4', 2))
    read(owner, cache)
    record = checks(tmp_path)
    assert (record['checks'], record['mismatches'], record['last_check']) == (2, 0, 'match')


def test_a_failed_python_replay_is_not_a_mismatch(owner, tmp_path, monkeypatch):
    # Value: protects=a comparison that cannot run serves the native result and records no mismatch; fails_when=the request fails or the watchdog is told of drift that was never measured; why_new=new path; seam=none; no_claim=the cause of a Python failure
    progress_backend.health.configure(tmp_path)
    cache = HistoryCache(owner)
    write(owner, lambda db: raw(db, 1, 'first', 0))
    read(owner, cache)
    write(owner, lambda db: raw(db, 2, 'second', 1))
    def broken(items, cursor):
        raise MemoryError('synthetic')
    monkeypatch.setattr(progress, '_reduce_python', broken)
    def snapshot(db):
        db.execute('BEGIN')
        return cache.history(db, ITEMS)
    assert owner.read(snapshot)['reader', 'pdf']['pages'] == {0, 1}
    record = checks(tmp_path)
    assert (record['checks'], record['mismatches'], record['check_errors'], record['last_check']) == (0, 0, 1, 'error: MemoryError')
    assert cache.stamp is not None  # the checkpoint itself was not in doubt
