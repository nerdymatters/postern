import json
import sqlite3

import pytest

from scripts import progress_profile


@pytest.mark.parametrize('backend', ['python', 'rust'])
def test_completed_capacity_catalog_drives_quiet_replay(tmp_path, monkeypatch, backend):
    if backend == 'rust':
        pytest.importorskip('postern_progress_native')
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', backend)
    path = tmp_path/'http-fixture.sqlite3'
    progress_profile.seed(path, readers=2, events_per_reader=12)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        for identity, status in [('published', 'published'), ('hidden', 'archived')]:
            db.execute("INSERT INTO posts(id,title,body,status,created_at,created_by) VALUES (?,?,'',?,1,'synthetic')",
                       (identity, identity, status))
        for identity, post in [('actual-video', 'published'), ('hidden-video', 'hidden')]:
            db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) "
                       "VALUES (?,?,?,'video','synthetic',1,0,'ready',0)", (identity, post, identity))
            db.execute('INSERT INTO video_metadata VALUES (?,30)', (identity,))
        db.execute("UPDATE events SET item_id='actual-video'")
        expected = progress_profile.digest(progress_profile.progress.history(db, {
            'actual-video': {'kind': 'video', 'pages': 0, 'duration_seconds': 30}}))
    result = progress_profile.profile(path, backend, catalog=True)
    assert result['items'] == 1 and result['rows'] == 24
    assert result['digest'] == expected


@pytest.mark.parametrize('backend', ['python', 'rust'])
def test_stage_profile_matches_streaming_progress(tmp_path, monkeypatch, backend):
    if backend == 'rust':
        pytest.importorskip('postern_progress_native')
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', backend)
    path = tmp_path/'synthetic.sqlite3'
    progress_profile.seed(path, readers=2, events_per_reader=12)
    result = progress_profile.profile(path, backend)
    assert result['rows'] == 24
    assert result['backend'] == backend
    assert len(result['digest']) == 64
    assert 'history' in result['profile']
    assert all(result[stage]['cpu_ms'] > 0 for stage in ('streaming', 'fetch_rows', 'reduce_materialized'))
    assert json.loads(json.dumps(result)) == result


@pytest.mark.parametrize('backend', ['python', 'rust'])
def test_stage_profile_materializes_the_selected_compact_source(tmp_path, monkeypatch, backend):
    if backend == 'rust':
        pytest.importorskip('postern_progress_native')
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', backend)
    path = tmp_path/'compact.sqlite3'
    progress_profile.seed(path, readers=2, events_per_reader=12)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute('UPDATE events SET ts=ts+1793990000')
        while not progress_profile.progress_store.rebuild_batch(db):
            pass
    result = progress_profile.profile(path, backend)
    assert result['source'] == 'progress_events'
    assert result['rows'] == 24


def test_cli_rejects_office_host_before_creating_output(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    output = tmp_path/'not-created'
    monkeypatch.setattr('sys.argv', ['progress_profile', '--output', str(output)])
    with pytest.raises(SystemExit, match='2'):
        progress_profile.main()
    assert not output.exists()


def test_materialized_mismatch_is_rejected(tmp_path, monkeypatch):
    path = tmp_path/'synthetic.sqlite3'
    progress_profile.seed(path, readers=1, events_per_reader=2)
    reference = progress_profile.progress._reduce_python
    calls = 0
    def wrong_on_second_call(*args):
        nonlocal calls
        calls += 1
        result = reference(*args)
        if calls == 2:
            next(iter(result.values()))['watched_seconds'] += 1
        return result
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'python')
    monkeypatch.setattr(progress_profile.progress, '_reduce_python', wrong_on_second_call)
    with pytest.raises(RuntimeError, match='Materialized and streaming progress differ'):
        progress_profile.profile(path, 'python')


def test_guard_retains_failed_worker_receipt(tmp_path):
    output = tmp_path/'failure.json'
    with pytest.raises(RuntimeError, match='Profiling worker exited'):
        progress_profile.guarded(output, 'profile', tmp_path/'missing.sqlite3', 'python')
    receipt = json.loads(output.with_suffix('.guard.json').read_text())
    assert receipt['status'] == 'stopped'
    assert receipt['exit_code'] != 0
    assert not output.exists()
