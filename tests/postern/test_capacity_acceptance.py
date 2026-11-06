"""Acceptance cannot be inferred from an incomplete synthetic HTTP workload."""
import argparse
import asyncio
import json
import shutil

import pytest

from scripts import capacity


def test_short_mixed_workload_cannot_pass_without_progress_and_dashboard(tmp_path, monkeypatch):
    # Regression: CAP-001, found by /qa 2026-11-06.
    # Report: .context/rust-representative-20261106/qa/qa-report-capacity-2026-11-06.md
    # Value: protects=incomplete workload cannot qualify; fails_when=only opens/ranges are counted;
    # why_new=capacity tests cover service behavior, not CLI acceptance; seam=none;
    # no_claim=representative load performance (one synthetic reader, real loopback HTTP).
    fixtures = tmp_path / 'tests/fixtures'
    fixtures.mkdir(parents=True)
    shutil.copyfile(capacity.ROOT / 'tests/fixtures/page.jpg', fixtures / 'page.jpg')
    monkeypatch.setattr(capacity, 'ROOT', tmp_path)
    args = argparse.Namespace(scenario='mixed', users=1, roster=1, seconds=10,
                              history_hours=0, ramp_seconds=0, video_mbps=1.5,
                              cloud_delay=0, with_reader=False)
    assert asyncio.run(capacity.run(args)) is False
    report = json.loads(next(tmp_path.glob('.context/capacity/*/report.json')).read_text())
    assert report['aborted'] is None
    assert report['integrity']['progress_matches_acknowledged_requests'] is True
    assert report['requests']['video_range']['statuses'] == {'206': 1}
    assert report['workload_coverage']['passed'] is False
    assert 'progress' in report['workload_coverage']['missing']
    assert 'dashboard' in report['workload_coverage']['missing']


@pytest.mark.parametrize('scenario,with_reader,counts,readers', [
    ('signin', False, {'signin': ('200', 2)}, []),
    ('enrollment', False, {'selfie_capture': ('202', 2), 'selfie_status': ('200', 2),
                           'enrollment_ready': ('confirmed', 2)}, []),
    ('enrollment', True, {'selfie_capture': ('202', 2), 'selfie_status': ('200', 2),
                          'enrollment_ready': ('confirmed', 2), 'existing_reader_open': ('200', 1),
                          'existing_reader_player': ('200', 1), 'existing_reader_range': ('206', 1),
                          'existing_reader_progress': ('200', 1)}, [2]),
    ('mixed', False, {'open_view': ('200', 4), 'player': ('200', 2), 'video_range': ('206', 2),
                      'progress': ('200', 2), 'catalog': ('200', 2), 'page': ('200', 1),
                      'dashboard': ('200', 1)}, [0, 1]),
])
def test_missing_family_or_reader_never_qualifies(scenario, with_reader, counts, readers):
    args = argparse.Namespace(scenario=scenario, users=2, with_reader=with_reader)
    statuses = {label: {status: count} for label, (status, count) in counts.items()}
    progress = {(f'reader{i}@example.invalid', ((0., 15.),)): 1 for i in readers}
    assert capacity.workload_coverage(args, statuses, progress)['passed']
    for label in statuses:
        incomplete = {key: value for key, value in statuses.items() if key != label}
        result = capacity.workload_coverage(args, incomplete, progress)
        assert not result['passed'] and label in result['missing']
    if readers:
        # A repeated acknowledgement from one reader cannot stand in for another.
        missing = dict(progress)
        missing.pop(next(iter(missing)))
        assert not capacity.workload_coverage(args, statuses, missing)['passed']
