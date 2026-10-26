import json
import io
import pytest

from scripts.day1 import remote


def test_remote_retains_failure_and_runs_audit_without_exporting_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    credential = 'synthetic-test-credential-never-export'
    for key, value in {
        'POSTERN_PROBE_URL': 'https://example.invalid',
        'POSTERN_PROBE_CREDENTIALS': json.dumps({'operator': credential, 'readers': {credential: 'fake'}}),
        'RUNNER_ENVIRONMENT': 'github-hosted', 'GITHUB_RUN_ID': '123', 'GITHUB_SHA': 'test-sha',
    }.items():
        monkeypatch.setenv(key, value)

    class FakeClient:
        def __init__(self, _base):
            pass

        def get(self, _path):
            return 200, {}, b'{"observed_ip":"192.0.2.1"}', 1

        def close(self):
            pass

    calls = []

    def load(_base, credentials, mode, readers, duration, warmup):
        assert credentials['operator'] == credential
        calls.append((mode, readers, duration, warmup))
        if mode == 'pages':
            raise RuntimeError('simulated connection failure')
        return {'mode': mode, 'requests': 10, 'errors': 0, 'page_ms': {}, 'audit_ms': {}, 'mbps': 1}

    monkeypatch.setattr(remote, 'Client', FakeClient)
    monkeypatch.setattr(remote, 'verify_public_ip', lambda *_: {'independent_ip_match': True})
    monkeypatch.setattr(remote, 'run_load', load)
    assert remote.main() == 1
    assert calls == [('pages', 20, 300, 15), ('audit', 25, 300, 15)]
    artifacts = list((tmp_path / 'reports/day1').glob('*.json'))
    assert {p.name for p in artifacts} == {'runner.json', 'pages-error.json', 'audit.json'}
    assert all(credential not in p.read_text() for p in artifacts)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in artifacts)


def test_ip_check_rejects_a_spoof_even_with_correct_initial_ip(monkeypatch):
    monkeypatch.setattr(remote, 'urlopen', lambda *_args, **_kwargs: io.BytesIO(b'192.0.2.1'))

    class FakeClient:
        def get(self, _path, extra):
            return 200, {}, b'{"observed_ip":"192.0.2.123"}', 1

    with pytest.raises(ValueError, match='spoof'):
        remote.verify_public_ip(FakeClient(), {'observed_ip': '192.0.2.1', 'forwarding_headers_trusted': True})
