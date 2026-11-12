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


@pytest.mark.parametrize('code,out,expected', [
    (0, 'active\n', 'operational'), (0, 'reloading\n', 'operational'),
    (3, 'inactive\n', 'outage'), (3, 'failed\n', 'outage'), (3, 'activating\n', 'outage'),
    (3, 'deactivating\n', 'outage'), (3, 'maintenance\n', 'outage'),
    (4, 'unknown\n', 'unknown'), (1, '', 'unknown'), (1, 'active\n', 'unknown'),
    (0, 'unexpected format\n', 'unknown'), (0, 'active\ninactive\n', 'unknown'),
])
def test_systemd_states_keep_the_worker_contract(code, out, expected):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=code, stdout=out, stderr='synthetic diagnostic')
    assert heartbeat.process_state('systemd/room-api.service', run) == expected
    assert calls == [(['systemctl', '--system', 'is-active', 'room-api.service'],
                      {'capture_output': True, 'text': True, 'timeout': 5})]


@pytest.mark.parametrize('error', [OSError('not installed'), subprocess.TimeoutExpired('systemctl', 5)])
def test_systemd_observation_errors_are_unknown(error):
    def run(*args, **kwargs):
        raise error
    assert heartbeat.process_state('systemd/room-api.service', run) == 'unknown'


@pytest.mark.parametrize('target', ['systemd/../other.service', 'systemd/--user', 'systemd/api.service other.service',
                                   'systemd/*.service', 'systemd/api.timer', 'systemd/api.service\n',
                                   'systemd/api;true.service', 'systemd/api%h.service', 'systemd/api\\x20.service'])
def test_systemd_targets_cannot_escape_the_allowlist(tmp_path, target):
    agents = {**heartbeat.AGENTS, 'api-process': target}
    with pytest.raises(ValueError, match='agent targets'):
        heartbeat.load_settings(settings_file(tmp_path, agents=agents), sending=False)
    assert heartbeat.process_state(target, lambda *args, **kwargs: pytest.fail('invalid unit reached systemctl')) == 'unknown'


def test_linux_heartbeat_check_uses_configured_units_and_sends_no_private_fields(tmp_path, monkeypatch, capsys):
    agents = {'api-process': 'systemd/org.example.room.service', 'proxy': 'systemd/org.example.room.proxy.service',
              'watchdog': 'systemd/org.example.room.watchdog.service', 'tunnel': 'systemd/tunnel@postern.service'}
    path = settings_file(tmp_path, agents=agents)
    calls = []
    def read(target, runner=None):
        calls.append(target)
        return 'outage' if target == agents['tunnel'] else 'operational'
    monkeypatch.setattr(heartbeat, 'systemd_state', read)
    monkeypatch.setattr(heartbeat.urllib.request, 'build_opener', lambda *args: pytest.fail('--check sent a request'))
    assert heartbeat.main(['--config', str(path), '--check']) == 0
    body = json.loads(capsys.readouterr().out)
    assert calls == list(agents.values())
    assert set(body) == {'sent_at', 'agents'} and type(body['sent_at']) is int
    assert body['agents'] == {key: 'outage' if key == 'tunnel' else 'operational' for key in agents}
    assert not any(value in json.dumps(body) for value in [str(path), 'org.example', 'test-only-token', 'systemd/'])


def test_linux_heartbeat_cannot_add_or_omit_state_ids(tmp_path):
    for agents in ({key: 'systemd/room.service' for key in heartbeat.AGENTS if key != 'tunnel'},
                   {**heartbeat.AGENTS, 'extra': 'systemd/extra.service'}):
        with pytest.raises(ValueError, match='agent targets'):
            heartbeat.load_settings(settings_file(tmp_path, agents=agents), sending=False)
