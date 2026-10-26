import importlib.util
import json
import os
import plistlib
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('heartbeat', Path(__file__).parents[2] / 'ops/postern/status-heartbeat.py')
heartbeat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(heartbeat)


def result(code=0, out='', err=''):
    return lambda *a, **kw: SimpleNamespace(returncode=code, stdout=out, stderr=err)


def test_process_state_distinguishes_down_from_unobservable():
    assert heartbeat.process_state('x', result(out='\tstate = running\n')) == 'operational'
    assert heartbeat.process_state('x', result(out='\tstate = waiting\n')) == 'outage'
    assert heartbeat.process_state('x', result(1, err='Could not find service "x"')) == 'outage'
    assert heartbeat.process_state('x', result(1, err='Operation not permitted')) == 'unknown'
    assert heartbeat.process_state('x', result(out='unexpected format')) == 'unknown'


def test_process_timeout_is_unknown():
    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired('launchctl', 5)
    assert heartbeat.process_state('x', timeout) == 'unknown'


def test_heartbeat_contains_only_postern_states(monkeypatch):
    monkeypatch.setattr(heartbeat, 'process_state', lambda target: 'operational')
    body = heartbeat.collect()
    assert set(body) == {'sent_at', 'agents'}
    assert set(body['agents']) == {'api-process', 'proxy', 'watchdog', 'tunnel'}
    assert all('postern' in target for target in heartbeat.AGENTS.values())


def test_redirect_never_receives_credential():
    assert heartbeat.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid') is None


def settings_file(tmp_path, **values):
    path = tmp_path / 'heartbeat.json'
    path.write_text(json.dumps({'token': 'test-only-token-' * 3, 'url': 'https://status.example.invalid/api/heartbeat', **values}))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize('url', [None, '', 'http://status.example.invalid', 'https://user:secret@status.example.invalid',
                                 'https://status.example.invalid/#fragment', 'https://status.example.invalid:bad',
                                 'https://status.example.invalid/\n'])
def test_heartbeat_refuses_missing_or_unsafe_endpoint_before_sending(tmp_path, monkeypatch, capsys, url):
    path = settings_file(tmp_path, url=url)
    monkeypatch.setattr(heartbeat.urllib.request, 'build_opener', lambda *args: pytest.fail('invalid endpoint reached network'))
    assert heartbeat.main(['--config', str(path)]) == 1
    assert 'secret' not in capsys.readouterr().out


def test_configured_agent_labels_are_used_without_exposing_them_in_the_payload(tmp_path, monkeypatch, capsys):
    agents = {'api-process': 'system/org.example.existing', 'proxy': 'system/org.example.existing.proxy',
              'watchdog': 'system/org.example.existing.watchdog', 'tunnel': f'gui/{os.getuid()}/org.example.tunnel'}
    path = settings_file(tmp_path, agents=agents)
    seen = []
    monkeypatch.setattr(heartbeat, 'process_state', lambda target: seen.append(target) or 'operational')
    assert heartbeat.main(['--config', str(path), '--check']) == 0
    body = json.loads(capsys.readouterr().out)
    assert seen == list(agents.values())
    assert body['agents'] == {key: 'operational' for key in agents}
    assert 'org.example' not in json.dumps(body)


def test_invalid_agent_targets_and_readable_config_are_refused(tmp_path):
    agents = {**heartbeat.AGENTS, 'tunnel': 'system/../../not-a-label'}
    path = settings_file(tmp_path, agents=agents)
    with pytest.raises(ValueError, match='agent targets'):
        heartbeat.load_settings(path)
    path = settings_file(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match='0600'):
        heartbeat.load_settings(path)


def test_heartbeat_sends_to_the_configured_endpoint_only(tmp_path, monkeypatch, capsys):
    path = settings_file(tmp_path)
    monkeypatch.setattr(heartbeat, 'process_state', lambda target: 'operational')
    sent = []

    class Reply:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b'{"accepted":true}'

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            return Reply()

    monkeypatch.setattr(heartbeat.urllib.request, 'build_opener', lambda *args: Opener())
    assert heartbeat.main(['--config', str(path)]) == 0
    assert len(sent) == 1 and sent[0].full_url == 'https://status.example.invalid/api/heartbeat'
    assert set(json.loads(sent[0].data)) == {'sent_at', 'agents'}
    assert 'token' not in capsys.readouterr().out


def test_heartbeat_installer_refuses_a_duplicate_job_without_replacing_the_sender(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('heartbeat_installer', Path(__file__).parents[2] / 'ops/postern/install-status-heartbeat.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    monkeypatch.setattr(installer.Path, 'home', lambda: tmp_path)
    config = settings_file(tmp_path)
    old_job = tmp_path / 'Library/LaunchAgents/org.example.existing-heartbeat.plist'
    old_job.parent.mkdir(parents=True)
    old_job.write_bytes(plistlib.dumps({'ProgramArguments': ['/python', '/private/install/status-heartbeat.py', '--config', str(config)]}))
    old_script = tmp_path / 'Library/Application Support/PosternStatus/status-heartbeat.py'
    old_script.parent.mkdir(parents=True)
    old_script.write_text('retain this installed sender')
    monkeypatch.setattr(installer.subprocess, 'run', lambda *args, **kwargs: pytest.fail('duplicate job reached launchctl'))
    with pytest.raises(SystemExit, match='already installed'):
        installer.main(['--config', str(config)])
    assert old_script.read_text() == 'retain this installed sender'
    assert sorted(path.name for path in old_job.parent.iterdir()) == [old_job.name]
