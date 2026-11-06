import json

import pytest

from scripts import progress_profile


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
