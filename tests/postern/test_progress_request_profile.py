import asyncio
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
