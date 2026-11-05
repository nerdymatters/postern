"""The rehearsal cannot accept a live endpoint/config or reuse existing data."""
import importlib.util
import json

import pytest

from scripts import pilot_rehearsal as pilot


def test_refuse_existing_output_and_unmarked_or_redirected_child_paths(tmp_path):
    with pytest.raises(FileExistsError):
        pilot.initialize(tmp_path)
    with pytest.raises(FileNotFoundError):
        pilot.validate(tmp_path)
    root = tmp_path / 'new'
    pilot.initialize(root)
    (root / 'data').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='symlinks'):
        pilot.validate(root)
    assert not (tmp_path / 'postern.sqlite3').exists()


@pytest.mark.skipif(importlib.util.find_spec('postern_progress_native') is None, reason='optional native wheel not installed')
def test_real_process_rehearsal_preserves_pages_quiz_unlock_and_stops_children(tmp_path):
    report = pilot.rehearsal(tmp_path / 'rehearsal')
    assert report['status'] == 'passed'
    assert report['rollback_output_parity'] and report['all_children_stopped']
    assert [phase['backend'] for phase in report['phases']] == ['python', 'rust', 'python']
    assert report['durable']['pages'] == 3 and report['durable']['quiz_attempts'] == 1
    for path in (tmp_path / 'rehearsal/data/health-diagnostics').glob('health.jsonl*'):
        for line in path.read_text().splitlines():
            record = json.loads(line)
            assert record['backend'] in ('python', 'rust')
            assert '@example.invalid' not in line and 'Bearer' not in line
