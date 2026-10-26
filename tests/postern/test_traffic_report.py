import gzip
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('traffic_report', Path(__file__).resolve().parents[2] / 'ops/postern/traffic-report.py')
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def test_rotation_utc_totals_and_truncated_record(tmp_path):
    previous = tmp_path / 'access-old.log.gz'
    with gzip.open(previous, 'wt') as handle:
        handle.write(json.dumps({'ts': 1791417599, 'size': 100, 'bytes_read': 20, 'status': 206, 'duration': 0.5}) + '\n')
    active = tmp_path / 'access.log'
    active.write_text(json.dumps({'ts': 1791417600, 'size': 250, 'status': 503, 'duration': 1.5}) + '\n{"partial":')
    result = REPORT.summarize([previous, active])
    assert list(result['days']) == ['2026-10-07', '2026-10-08']
    assert result['days']['2026-10-07']['response_bytes'] == 100
    assert result['days']['2026-10-07']['request_bytes'] == 20
    assert result['days']['2026-10-08']['requests'] == 1
    assert result['days']['2026-10-08']['server_errors'] == 1
    assert result['malformed_lines'] == 1
    assert result['first_observation'] < result['last_observation']


def test_no_samples_are_not_reported_as_observed_zero(tmp_path):
    result = REPORT.summarize([tmp_path / 'rotated-away.log'])
    assert result['days'] == {}
    assert result['first_observation'] is None
    assert result['last_observation'] is None
