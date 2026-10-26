"""Running Postern as a service (ENG-D8) and the checked-in converter profile (ENG-D6).

Nothing here installs anything, needs sudo, or sends email: the launchd plists are rendered and read
back, the watchdog runs on a fake clock against a fake notifier, and the Seatbelt profile is exercised
against throwaway directories.
"""
from __future__ import annotations
import shutil
import dataclasses
import json
import plistlib
import re
import socket
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from conftest import Clock

from postern import derive, serve, watchdog
from postern.config import Config

ATOM = re.compile(r'\((?:literal|subpath) "([^"]+)"\)')
OWNER_CONFIG = Path('/Users/owner/.config/postern/config.json')


def write_config(tmp_path, mode=0o600, **extra):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700, exist_ok=True)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'data_dir': str(data), 'mode': 'demo', **extra}))
    path.chmod(mode)
    return path


def test_the_daemon_starts_at_boot_as_the_owner():
    jobs = serve.documents(OWNER_CONFIG, 'owner', Path('/Users/owner'), python='/opt/postern/.venv/bin/python')
    path, document = jobs[serve.LABEL]
    assert path == Path('/Library/LaunchDaemons/org.nerdymatters.postern.plist')
    assert document['UserName'] == 'owner' and document['RunAtLoad'] and document['KeepAlive']
    assert document['ProgramArguments'] == ['/opt/postern/.venv/bin/python', '-P', '-m', 'postern.serve',
                                            'run', '--config', str(OWNER_CONFIG)]
    assert document['EnvironmentVariables']['HOME'] == '/Users/owner'
    assert plistlib.loads(plistlib.dumps(document)) == document


def test_the_watchdog_is_a_second_job_with_its_own_log():
    jobs = serve.documents(OWNER_CONFIG, 'owner', Path('/Users/owner'))
    server, watcher = jobs[serve.LABEL][1], jobs[serve.WATCHDOG][1]
    assert watcher['ProgramArguments'][1:4] == ['-P', '-m', 'postern.watchdog']
    assert watcher['UserName'] == 'owner' and watcher['KeepAlive']
    assert watcher['StandardOutPath'] != server['StandardOutPath']


def test_the_user_agent_fallback_is_login_scoped(tmp_path):
    path, document = serve.documents(OWNER_CONFIG, 'owner', tmp_path, user_agent=True)[serve.LABEL]
    assert path == tmp_path / 'Library' / 'LaunchAgents' / 'org.nerdymatters.postern.plist'
    assert 'UserName' not in document  # a login-scoped agent already runs as the person who logged in
    assert serve.domain(user_agent=True).startswith('gui/') and serve.domain(user_agent=False) == 'system'


def test_install_refuses_to_clobber_a_plist_it_did_not_write(tmp_path):
    document = serve.documents(OWNER_CONFIG, 'owner', Path('/Users/owner'))[serve.LABEL][1]
    path = tmp_path / 'org.nerdymatters.postern.plist'
    assert serve.conflicting(path, document) is None
    path.write_bytes(plistlib.dumps({**document, 'ProgramArguments': ['/usr/bin/true'], 'KeepAlive': False}))
    conflict = serve.conflicting(path, document)
    assert 'ProgramArguments' in conflict and 'KeepAlive' in conflict
    path.write_bytes(plistlib.dumps(document))
    assert serve.conflicting(path, document) is None
    path.write_bytes(b'not a plist')
    assert 'not a readable plist' in serve.conflicting(path, document)


def test_preflight_checks_every_prerequisite(tmp_path):
    checks = {name: (ok, detail) for name, ok, detail in serve.preflight(write_config(tmp_path))}
    assert set(checks) == {'python', 'pdftoppm', 'pdfinfo', 'ffmpeg', 'ffprobe', 'front proxy', 'pillow-heif', 'quiz bank', 'final exam and subtitles',
                           'sandbox-exec', 'selfie face detector', 'config file', 'data directory', 'sandbox profile', 'organization settings'}
    assert checks['quiz bank'] == (True, '9 chapter quiz(zes), 108 questions')
    # Caddy is an office-Mac prerequisite, not a test-runner one; where it is absent the check must say so.
    required = {name for name in checks if name != 'front proxy' or shutil.which('caddy')}
    assert all(checks[name][0] for name in required), checks
    assert shutil.which('caddy') or 'brew install caddy' in checks['front proxy'][1]
    assert '1.' in checks['pillow-heif'][1] and 'libheif' in checks['pillow-heif'][1]


def test_preflight_fails_a_config_anyone_can_read(tmp_path):
    checks = {name: (ok, detail) for name, ok, detail in serve.preflight(write_config(tmp_path, mode=0o644))}
    assert checks['config file'] == (False, f"{tmp_path / 'config.json'} mode 0644 — run chmod 600 on it")
    assert checks['sandbox profile'][0]


def test_preflight_fails_a_missing_data_directory(tmp_path):
    path = write_config(tmp_path)
    (tmp_path / 'data').rmdir()
    checks = {name: ok for name, ok, _ in serve.preflight(path)}
    assert checks['data directory'] is False and checks['config file'] is True


def test_run_refuses_a_port_that_is_already_listening(tmp_path, capsys):
    with socket.socket() as held:
        held.bind(('127.0.0.1', 0))
        held.listen()
        port = held.getsockname()[1]
        assert serve.main(['run', '--config', str(write_config(tmp_path, port=port))]) == 1
    printed = capsys.readouterr().out
    assert f'127.0.0.1:{port} is already listening' in printed and 'change "port"' in printed


def test_the_watchdog_alerts_after_three_failures_and_once_on_recovery():
    clock, sent = Clock(start=0.0), []
    answers = [True, False, False, False, False, True, True]
    guard = watchdog.Watchdog(lambda: (answers.pop(0), 'process up'), lambda subject, body: sent.append((subject, body)),
                              failures=3, interval=30, now=clock)

    guard.run(sleep=clock.advance, cycles=3)
    assert sent == []  # two consecutive failures are not yet an outage

    guard.run(sleep=clock.advance, cycles=4)
    assert [subject for subject, _ in sent] == ['Postern is not answering', 'Postern is answering again']
    assert '3 consecutive health checks failed, 30s apart' in sent[0][1]
    assert 'Recovered after 120s down and 4 failed checks' in sent[1][1]
    assert (guard.consecutive, guard.alerted, guard.since) == (0, False, None)


def test_alerts_fall_back_to_a_file_when_smtp_is_missing(tmp_path):
    where = watchdog.notifier(tmp_path / 'config.json', {'data_dir': str(tmp_path)})
    assert isinstance(where, watchdog.LogFile) and 'no smtp settings' in str(where)
    where('Postern is not answering', 'three checks failed')
    assert 'three checks failed' in (tmp_path / 'watchdog-alerts.log').read_text()


def test_smtp_settings_beside_the_config_are_used(tmp_path):
    (tmp_path / 'watchdog.json').write_text(json.dumps({'host': 'smtp.example', 'to': 'owner@example.invalid'}))
    where = watchdog.notifier(tmp_path / 'config.json', {'data_dir': str(tmp_path)})
    assert isinstance(where, watchdog.Email) and 'owner@example.invalid' in str(where)


def test_probing_a_dead_server_reports_instead_of_raising():
    answered, detail = watchdog.probe('http://127.0.0.1:1/health', timeout=1)
    assert not answered and detail


def substituted(arguments):
    """The checked-in profile with its -D parameters filled in, as sandbox-exec would see it."""
    text = derive.PROFILE.read_text()
    for pair in arguments:
        if '=' in pair:
            name, value = pair.split('=', 1)
            text = text.replace(f'(param "{name}")', f'"{value}"')
    return text


def test_every_conversion_job_runs_the_checked_in_profile(tmp_path):
    # Value: protects=the job command applies the checked-in profile with every parameter supplied, and the converter
    #   that parses untrusted uploads reads and writes only its runtime and job paths; fails_when=job_command drifts
    #   from derive.sb (an inline -p profile, a missing -D parameter) or derive.sb gains a wider rule such as
    #   (subpath "/Users"); why_new=the inline-profile comparison that pinned every path is gone; seam=none
    #   (test_sandbox_model_read_failure_is_reported_without_parser_details proves real jobs load this file)
    source, output = tmp_path / 'upload.pdf', tmp_path / 'job'
    command = derive.job_command(source, output, 'pdf', 1, 1000, 1000, '/usr/bin')
    assert command[:3] == ['/usr/bin/sandbox-exec', '-f', str(derive.PROFILE)] and '-p' not in command
    supplied = dict(pair.split('=', 1) for flag, pair in zip(command, command[1:]) if flag == '-D')
    rules = '\n'.join(line for line in derive.PROFILE.read_text().splitlines() if not line.lstrip().startswith(';'))
    assert set(supplied) == set(re.findall(r'\(param "([A-Z_]+)"\)', rules))
    profile = substituted(derive.sandbox_profile_args(source, output))
    writable = {atom for line in profile.splitlines() if 'file-write*' in line for atom in ATOM.findall(line)}
    assert writable == {str(output.resolve()), '/dev/null'}
    assert set(ATOM.findall(profile)) == {'/', '/dev/null', '/dev/urandom', '/dev/random', '/System/Library', '/usr/lib',
                                          '/usr/share', '/opt/homebrew/Cellar', '/opt/homebrew/opt', *supplied.values()}
    # The shape of every rule is pinned too: a path atom the regex above cannot see, a new operation or a new
    # Mach service would all pass the atom check alone.
    operations = {word for clause in re.findall(r'\(allow ([^()\n]+)', profile) for word in clause.split()}
    assert operations == {'process*', 'sysctl-read', 'mach-lookup', 'file-read-metadata', 'file-read*', 'file-write*'}
    assert set(re.findall(r'\(global-name "([^"]+)"\)', profile)) == {'com.apple.system.logger', 'com.apple.cfprefsd.daemon'}
    assert '(regex' not in profile and '(deny default)' in profile
    for rule in ('(deny default)', '(allow process*)', '(allow sysctl-read)', '(allow file-read-metadata)',
                 '(allow mach-lookup (global-name "com.apple.system.logger") (global-name "com.apple.cfprefsd.daemon"))'):
        assert rule in profile
    # Without this dyld aborts before Python starts; it is the rule most likely to be "tidied away".
    assert '(allow file-read* (literal "/"))' in profile


def test_the_checked_in_profile_compiles_and_still_denies_the_secrets_directory(tmp_path):
    compiles, detail = serve.profile_compiles()
    assert compiles, detail

    secrets = tmp_path / '.config' / 'postern'
    secrets.mkdir(parents=True)
    (secrets / 'config.json').write_text('{"host_credentials": "secret"}')
    source, output = tmp_path / 'upload.pdf', tmp_path / 'job'
    source.write_bytes(b'%PDF-1.4')
    output.mkdir()
    arguments = derive.sandbox_profile_args(source, output)
    read = subprocess.run(['/usr/bin/sandbox-exec', *arguments, '/bin/cat', str(secrets / 'config.json')],
                          capture_output=True, text=True)
    assert read.returncode != 0 and 'Operation not permitted' in read.stderr and 'secret' not in read.stdout

    escape = subprocess.run(['/usr/bin/sandbox-exec', *arguments, '/usr/bin/touch', str(tmp_path / 'escaped')],
                            capture_output=True, text=True)
    assert escape.returncode != 0 and not (tmp_path / 'escaped').exists()


def test_the_example_config_matches_the_real_config_surface():
    example = json.loads((serve.ROOT / 'ops' / 'postern' / 'config.example.json').read_text())
    fields = {field.name for field in dataclasses.fields(Config)}
    assert set(example) <= fields, set(example) - fields
    assert {'mode', 'data_dir', 'origins', 'google_client_id', 'host_credentials', 'allowlist_credentials',
            'allowlist_sheet_id', 'drive_folder_id', 'database_sheet_id', 'port', 'notice_version'} <= set(example)


def test_the_front_proxy_is_a_supervised_job_too():
    """The Funnel talks to Caddy, not to the server, so a reboot must bring the proxy back as well."""
    jobs = serve.documents(OWNER_CONFIG, 'owner', Path('/Users/owner'))
    assert set(jobs) == {serve.LABEL, serve.WATCHDOG, serve.PROXY}
    path, document = jobs[serve.PROXY]
    assert path == Path('/Library/LaunchDaemons/org.nerdymatters.postern.proxy.plist')
    assert document['ProgramArguments'][1:] == ['run', '--config', str(serve.CADDYFILE), '--adapter', 'caddyfile']
    assert document['UserName'] == 'owner' and document['KeepAlive'] is True
    assert serve.CADDYFILE.is_file()


def test_the_installer_runs_from_the_folder_above_a_checkout(tmp_path):
    """A checkout is a folder named "postern"; from its parent, that folder must not shadow the package."""
    root = Path(serve.__file__).resolve().parents[1]
    parent = tmp_path / 'home'
    parent.mkdir()
    (parent / 'postern').symlink_to(root)
    result = subprocess.run([sys.executable, '-P', '-m', 'postern.serve', '--help'], cwd=parent, capture_output=True, text=True)
    assert result.returncode == 0 and 'install' in result.stdout, result.stderr


def test_the_test_alert_says_plainly_when_no_email_is_configured(tmp_path, capsys):
    assert watchdog.main(['test', '--config', str(write_config(tmp_path))]) == 2
    assert 'FAIL no email is configured' in capsys.readouterr().out


def test_the_test_alert_goes_through_the_real_notifier(tmp_path, capsys, monkeypatch):
    sent = []
    monkeypatch.setattr(watchdog.Email, '__call__', lambda self, subject, body: sent.append((self.mail['to'], subject)))
    (tmp_path / 'watchdog.json').write_text(json.dumps({'host': 'smtp.example', 'to': 'owner@example.invalid'}))
    assert watchdog.main(['test', '--config', str(write_config(tmp_path))]) == 0
    assert sent == [('owner@example.invalid', 'Postern test alert')]
    assert 'ok   test alert sent' in capsys.readouterr().out


def test_a_refused_login_is_reported_not_raised(tmp_path, capsys, monkeypatch):
    def refuse(self, subject, body):
        raise OSError('Username and Password not accepted')
    monkeypatch.setattr(watchdog.Email, '__call__', refuse)
    (tmp_path / 'watchdog.json').write_text(json.dumps({'host': 'smtp.example', 'to': 'owner@example.invalid'}))
    assert watchdog.main(['test', '--config', str(write_config(tmp_path))]) == 2
    assert 'FAIL could not send' in capsys.readouterr().out


def test_preflight_refuses_a_quiz_bank_that_lost_its_dealing_rules(tmp_path, monkeypatch):
    # Value: protects=a bank edit that makes answers guessable by length stops the restart; fails_when=preflight ignores the rules tier hands() reports; why_new=preflight only checked that the bank loads; seam=none
    from postern import quiz
    real = quiz.load()
    weakened = {number: {**entry, 'tier': 'balanced' if number == 3 else entry['tier']} for number, entry in real.items()}
    monkeypatch.setattr(quiz, 'load', lambda *args, **kwargs: weakened)
    checks = {name: (ok, detail) for name, ok, detail in serve.preflight(write_config(tmp_path))}
    assert checks['quiz bank'] == (False, '9 chapter quiz(zes), 108 questions; chapters 3 cannot deal balanced, unguessable hands')


def test_explicit_installed_label_controls_every_supervised_job_and_status(tmp_path, monkeypatch):
    label = 'org.example.existing-reading-room'
    jobs = serve.documents(OWNER_CONFIG, 'owner', tmp_path, label=label)
    assert set(jobs) == {label, label + '.watchdog', label + '.proxy'}
    assert all(path.name == key + '.plist' and document['Label'] == key for key, (path, document) in jobs.items())
    calls = []
    monkeypatch.setattr(serve, 'launchctl', lambda *args: calls.append(args) or
                        SimpleNamespace(returncode=0, stdout='state = running\npid = 123\n', stderr=''))
    monkeypatch.setattr(serve, 'probe', lambda url: (True, 'ready'))
    assert serve.main(['status', '--label', label, '--config', str(tmp_path / 'config.json')]) == 0
    assert calls == [('print', 'system/' + key) for key in jobs]


@pytest.mark.parametrize('label', ['../outside', 'label/other', 'with spaces', '-option', ''])
def test_invalid_service_labels_are_refused_before_any_launchctl_call(monkeypatch, label):
    monkeypatch.setattr(serve, 'launchctl', lambda *args: pytest.fail('invalid label reached launchctl'))
    with pytest.raises(SystemExit):
        serve.main(['status', '--label=' + label])


def test_install_refuses_a_second_label_for_existing_configuration_even_with_force(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    root = tmp_path / 'jobs'
    root.mkdir()
    monkeypatch.setattr(serve, 'plist_path', lambda label, user_agent, home: root / ('agent-' if user_agent else '') / (label + '.plist'))
    old = serve.plist_path('org.example.existing', False, tmp_path)
    old.parent.mkdir(exist_ok=True)
    old.write_bytes(plistlib.dumps({'ProgramArguments': ['/python', '-m', 'postern.serve', 'run', '--config', str(config)]}))
    original = old.read_bytes()
    monkeypatch.setattr(serve, 'preflight', lambda path: [('fixture prerequisites', True, 'ok')])
    monkeypatch.setattr(serve, 'launchctl', lambda *args: pytest.fail('duplicate install changed launchd'))
    assert serve.install(config, 'owner', tmp_path, False, False, True) == 1
    assert 'already supervises' in capsys.readouterr().out
    assert old.read_bytes() == original
    assert not (tmp_path / 'Library').exists()


def test_install_refuses_an_unowned_listening_port(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    monkeypatch.setattr(serve, 'plist_path', lambda label, user_agent, home: tmp_path / 'none' / (label + '.plist'))
    monkeypatch.setattr(serve, 'preflight', lambda path: [('fixture prerequisites', True, 'ok')])
    monkeypatch.setattr(serve, 'listening', lambda port: True)
    calls = []
    monkeypatch.setattr(serve, 'launchctl', lambda *args: calls.append(args) or SimpleNamespace(returncode=1))
    assert serve.install(config, 'owner', tmp_path, False, False, True) == 1
    assert calls == [('print', 'system/' + serve.LABEL)]
    assert 'already in use' in capsys.readouterr().out
    assert not (tmp_path / 'Library').exists()


def test_uninstall_only_removes_the_explicit_label_family(tmp_path, monkeypatch):
    label = 'org.example.existing'
    monkeypatch.setattr(serve, 'plist_path', lambda job_label, user_agent, home: tmp_path / (job_label + '.plist'))
    for job_label in (*serve.service_labels(label), serve.LABEL):
        (tmp_path / (job_label + '.plist')).write_text('retained unless selected')
    calls = []
    monkeypatch.setattr(serve, 'launchctl', lambda *args: calls.append(args) or SimpleNamespace(returncode=0))
    assert serve.uninstall('owner', tmp_path, False, label=label) == 0
    assert calls == [('bootout', 'system/' + job_label) for job_label in serve.service_labels(label)]
    assert (tmp_path / (serve.LABEL + '.plist')).is_file()
    assert all(not (tmp_path / (job_label + '.plist')).exists() for job_label in serve.service_labels(label))
