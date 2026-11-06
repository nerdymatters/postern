import asyncio
import json
import sqlite3

import pytest

from scripts import progress_profile, progress_request_profile


def test_spans_measure_real_replay_and_restore_functions(tmp_path, monkeypatch):
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'python')
    path = tmp_path/'synthetic.sqlite3'
    progress_profile.seed(path, readers=1, events_per_reader=2)
    original = progress_request_profile.progress.history
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        expected = original(db, progress_profile.ITEMS)
        with progress_request_profile.capture_spans() as spans:
            assert progress_request_profile.progress.history(db, progress_profile.ITEMS) == expected
        assert [row['stage'] for row in spans] == ['roster_history']
        assert 'thread_cpu_ms' in spans[0]
    assert progress_request_profile.progress.history is original


def test_request_failure_restores_every_wrapper(monkeypatch):
    async def failed(*args, **kwargs):
        raise RuntimeError('injected request failure')
    module = progress_request_profile
    monkeypatch.setattr(module.service.Service, 'training_progress', failed)
    history, dashboard = module.progress.history, module.service.dashboard
    with pytest.raises(RuntimeError, match='injected request failure'):
        with module.capture_spans() as spans:
            asyncio.run(module.service.Service.training_progress())
    assert module.progress.history is history and module.service.dashboard is dashboard
    assert module.service.Service.training_progress is failed
    assert spans[0]['stage'] == 'service_request'
    assert 'thread_cpu_ms' not in spans[0]


@pytest.mark.parametrize('reports,cleanup_error', [(1, True), (0, False), (2, False), (1, False)])
def test_report_collection_includes_cleanup_failures(tmp_path, monkeypatch, reports, cleanup_error):
    module = progress_request_profile
    monkeypatch.setattr(module.capacity, 'ROOT', tmp_path)
    async def measured(args):
        for index in range(reports):
            folder = tmp_path/f'.context/capacity/run-{index}'
            folder.mkdir(parents=True)
            (folder/'report.json').write_text(json.dumps({'sample': index}))
        if cleanup_error:
            raise RuntimeError('original cleanup failure')
        return True
    monkeypatch.setattr(module.capacity, 'run', measured)
    output = tmp_path/'output'
    if cleanup_error or reports != 1:
        with pytest.raises(RuntimeError, match='original cleanup failure' if cleanup_error else 'fresh capacity report'):
            asyncio.run(module.run(output))
    else:
        assert asyncio.run(module.run(output)) is True
    record = json.loads((output/'report.json').read_text())
    assert record['status'] == ('completed' if reports == 1 and not cleanup_error else 'failed')
    assert record.get('capacity') == ({'sample': 0} if reports == 1 else None)


@pytest.mark.parametrize('capacity_passed,quiet_failed', [(True, False), (True, True), (False, False)])
def test_quiet_profile_uses_completed_fixture_and_preserves_capacity_failure(
        tmp_path, monkeypatch, capacity_passed, quiet_failed):
    module = progress_request_profile
    monkeypatch.setenv('POSTERN_PROGRESS_BACKEND', 'python')
    monkeypatch.setattr(module.capacity, 'ROOT', tmp_path)
    folder = tmp_path/'.context/capacity/run'
    cleaned_up = False
    calls = []
    async def measured(args):
        nonlocal cleaned_up
        folder.mkdir(parents=True)
        (folder/'report.json').write_text(json.dumps({'sample': 42}))
        cleaned_up = True
        return capacity_passed
    def quiet(output, mode, fixture, backend):
        assert cleaned_up
        assert (mode, fixture, backend) == ('capacity-profile', folder/'data/postern.sqlite3', 'python')
        calls.append(fixture)
        if quiet_failed:
            raise RuntimeError('injected quiet-profile failure')
        output.write_text(json.dumps({'digest': 'synthetic-result'}))
    monkeypatch.setattr(module.capacity, 'run', measured)
    monkeypatch.setattr(module.progress_profile, 'guarded', quiet)
    output = tmp_path/'output'
    if quiet_failed:
        with pytest.raises(RuntimeError, match='injected quiet-profile failure'):
            asyncio.run(module.run(output, quiet_profile=True))
    else:
        assert asyncio.run(module.run(output, quiet_profile=True)) is capacity_passed
    record = json.loads((output/'report.json').read_text())
    assert record['capacity'] == {'sample': 42} and 'spans' in record
    assert len(calls) == int(capacity_passed)
    assert record['status'] == ('completed' if capacity_passed and not quiet_failed else 'failed')
    assert record.get('quiet_replay') == ({'digest': 'synthetic-result'} if capacity_passed and not quiet_failed else None)
