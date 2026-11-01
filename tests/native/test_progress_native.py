"""Differential contracts for the actual optional Rust module; synthetic SQLite only."""
import json
import random
import sqlite3
from contextlib import contextmanager

import pytest

from postern import progress_backend
from postern.progress import history

pytest.importorskip('postern_progress_native')

ITEMS = {'pdf': {'kind': 'pdf', 'pages': 8}, 'photo': {'kind': 'photo', 'pages': 99},
         'video': {'kind': 'video', 'pages': 0, 'duration_seconds': 100}}


@contextmanager
def database(records):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('CREATE TABLE members(email TEXT PRIMARY KEY);'
                     'CREATE TABLE events(email TEXT,item_id TEXT,type TEXT,ts REAL,payload TEXT);'
                     "INSERT INTO members VALUES ('active'),('second');")
    db.executemany('INSERT INTO events VALUES (?,?,?,?,?)', records)
    db.commit()
    db.execute('PRAGMA query_only=ON')
    try:
        yield db
    finally:
        db.close()


def both(monkeypatch, db, items=ITEMS, email=None, coverage=False):
    results = []
    for backend in ('python', 'rust'):
        monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', backend)
        results.append(history(db, items, email, coverage))
    assert results[0] == results[1]
    return results[1]


def row(kind, item, detail, ts=1, email='active'):
    return email, item, kind, ts, json.dumps({'detail': detail})


def test_direct_replay_preserves_integer_timestamp_precision():
    from postern.progress import _evidence
    result = progress_backend.native().reduce(
        progress_backend.metadata(ITEMS),
        [row('item_open', 'pdf', {}, ts=2**53+1)], _evidence)
    assert result['active', 'pdf']['last_activity_at'] == 2**53+1


def test_evidence_union_refusals_roster_and_ignored_chunks(monkeypatch):
    records = [('active', 'outside', 'page_served', 1, 'invalid JSON')] * 600
    records += [row('page_served', 'pdf', {'page': n}, ts=i) for i, n in enumerate([0, 2, 2, True, -1, 8, 2.0])]
    records += [row('item_summary', 'pdf', {'pages': [1, 3, 99]}, ts=999),
                row('view_requested', 'pdf', {}, ts=1000),
                row('item_open', 'pdf', {}, ts=10),
                row('page_served', 'photo', {'page': 1}),
                row('page_served', 'photo', {'page': 0}),
                row('video_progress', 'video', {'ranges': [[0, 30], [20, 40], [99, 100.5],
                                                          [100, 100.6], [False, 90], [-1, 90]]}, ts=20),
                row('video_progress', 'video', {'ranges': [[40, 90], [50, 80]]}, ts=15),
                row('page_served', 'pdf', {'page': 7}, email='removed'),
                ('active', 'pdf', 'view_requested', 1001, 'invalid JSON')]
    with database(records) as db:
        result = both(monkeypatch, db)
        assert result['active', 'pdf']['pages'] == {0, 1, 2, 3}
        assert result['active', 'pdf']['attempts'] == 2
        assert result['active', 'pdf']['last_activity_at'] == 10
        assert result['active', 'pdf']['opened'] is True
        assert result['active', 'photo']['pages'] == {0}
        assert result['active', 'video']['watched_seconds'] == 91
        assert result['active', 'video']['last_activity_at'] == 20
        assert ('removed', 'pdf') not in result
        assert both(monkeypatch, db, email='removed')['removed', 'pdf']['pages'] == {7}
        covered = both(monkeypatch, db, email='active', coverage=True)
        assert covered['active', 'pdf']['attempts'] == 0
        assert not covered['active', 'pdf']['opened']
        assert both(monkeypatch, db, items={}) == {}


@pytest.mark.parametrize('payload', [
    '{"detail":{"page":-0}}', '{"detail":{"page":-0.0}}',
    '{"detail":{"pages":[0,1,true,2.0,999999999999999999999999999999999]}}',
    '{"detail":{"ranges":[[0,10],[NaN,50],[1,Infinity],[10,20]]}}',
    '{"detail":{"ranges":[[0,10]]},"unused":"\\ud800"}',
    '{"detail":{"ranges":[[0,10]],"unused":1e999}}',
    '{"detail":{"ranges":[[100.1,100.2]]}}',
    '{"detail":{"ranges":{"not":"a list"},"pages":"123"}}',
])
@pytest.mark.parametrize('kind,item', [('page_served', 'pdf'), ('item_summary', 'pdf'), ('video_progress', 'video'), ('item_open', 'video')])
def test_historical_json_semantics(monkeypatch, payload, kind, item):
    with database([('active', item, kind, 0, payload), row('item_open', item, {}, ts=-5)]) as db:
        both(monkeypatch, db)


@pytest.mark.parametrize('payload', ['{', 'null', '[]', '{"detail":null}',
                                    '{"detail":{"ranges":null}}', '{"detail":{"pages":null}}',
                                    '{"detail":{"ranges":[[0,' + '9' * 400 + ']]}}'])
@pytest.mark.parametrize('kind,item', [('item_summary', 'pdf'), ('video_progress', 'video')])
def test_errors_never_become_partial_success(monkeypatch, payload, kind, item):
    with database([row(kind, item, {}), ('active', item, kind, 1, payload)]) as db:
        outcomes = []
        for backend in ('python', 'rust'):
            monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', backend)
            try:
                outcomes.append(('value', history(db, ITEMS)))
            except (ValueError, TypeError, AttributeError, OverflowError) as error:
                outcomes.append(('error', type(error)))
        assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize('seed', range(20))
def test_randomized_replay_and_selection(monkeypatch, seed):
    rng = random.Random(seed)
    records = []
    for _ in range(700):
        kind = rng.choice(['page_served', 'item_summary', 'video_progress', 'item_open', 'view_requested'])
        detail = {'page': rng.choice([True, 0, 1, 2.0, 7, 8, -1]),
                  'pages': rng.choices([False, 0, 1, 2, 2.0, 7, 99], k=5),
                  'ranges': [[rng.uniform(-1, 101), rng.uniform(-1, 101)] for _ in range(5)]}
        records.append(row(kind, rng.choice([*ITEMS, 'hidden']), detail,
                           ts=rng.choice([0, -1, rng.uniform(0, 100)]),
                           email=rng.choice(['active', 'second', 'removed'])))
    with database(records) as db:
        for email in (None, 'active', 'removed'):
            for coverage in (False, True):
                both(monkeypatch, db, email=email, coverage=coverage)


def test_cursor_errors_propagate_and_nonstandard_metadata_stays_in_python(monkeypatch):
    class BrokenCursor:
        def __iter__(self):
            yield ('active', 'pdf', 'page_served', 1, '{"detail":{"page":0}}')
            raise sqlite3.OperationalError('synthetic read failure')
    from postern.progress import _evidence
    with pytest.raises(sqlite3.OperationalError):
        progress_backend.native().reduce(progress_backend.metadata(ITEMS), BrokenCursor(), _evidence)
    with database([row('video_progress', 'video', {'ranges': [[0, 10]]})]) as db:
        both(monkeypatch, db, items={'video': {'kind': 'video', 'pages': 0, 'duration_seconds': 2**54}})


def test_recovered_timestamps_are_only_compared_for_actual_evidence(monkeypatch):
    records = [row('view_requested', 'pdf', {}, ts='2026-11-01T12:00:00Z'),
               row('item_summary', 'pdf', {'pages': [0]}, ts='not an activity timestamp'),
               row('video_progress', 'video', {'ranges': [[-1, 90]]}, ts='ignored'),
               row('page_served', 'pdf', {'page': 1}, ts='2026-11-01'),
               row('page_served', 'pdf', {'page': 2}, ts='2026-10-31')]
    with database(records) as db:
        result = both(monkeypatch, db)
        assert result['active', 'pdf']['attempts'] == 1
        assert result['active', 'pdf']['last_activity_at'] == '2026-11-01'
        assert result['active', 'pdf']['pages'] == {0, 1, 2}
        assert result['active', 'video']['last_activity_at'] is None
    with database([row('page_served', 'pdf', {'page': 0}), records[-1]]) as db:
        for backend in ('python', 'rust'):
            monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', backend)
            with pytest.raises(TypeError):
                history(db, ITEMS)
