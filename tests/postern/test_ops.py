"""Running Postern as a service (ENG-D8) and the checked-in converter profile (ENG-D6).

Nothing here installs anything, needs sudo, or sends email: the launchd plists are rendered and read
back, the watchdog runs on a fake clock against a fake notifier, and the Seatbelt profile is exercised
against throwaway directories.
"""
from __future__ import annotations
import shutil
import dataclasses
import json
import os
import plistlib
import re
import shlex
import socket
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from conftest import Clock

from postern import derive, progress_backend, serve, watchdog
from postern.config import Config

SEATBELT = pytest.mark.skipif(derive.LINUX, reason='macOS Seatbelt; test_sandbox.py covers bubblewrap')
LAUNCHD = pytest.mark.skipif(sys.platform == 'linux', reason='launchd jobs; test_linux_host.py covers systemd')
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


@SEATBELT
def test_preflight_checks_every_prerequisite(tmp_path):
    checks = {name: (ok, detail) for name, ok, detail in serve.preflight(write_config(tmp_path))}
    assert set(checks) == {'python', 'pdftoppm', 'pdfinfo', 'ffmpeg', 'ffprobe', 'front proxy', 'pillow-heif', 'quiz bank', 'final exam and subtitles',
                           'sandbox-exec', 'selfie face detector', 'config file', 'data directory', 'sandbox profile', 'organization settings',
                           'rust progress'}
    assert checks['quiz bank'] == (True, '9 chapter quiz(zes), 108 questions')
    # Caddy is an office-Mac prerequisite, not a test-runner one; where it is absent the check must say so.
    required = {name for name in checks if name != 'front proxy' or shutil.which('caddy')}
    assert all(checks[name][0] for name in required), checks
    assert shutil.which('caddy') or checks['front proxy'][1] == (
        "caddy not found (install caddy 2.8 or later from the Caddy project's Debian repository; Debian's own "
        'package is too old)' if derive.LINUX else 'caddy not found (brew install caddy)')
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


@SEATBELT
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


@SEATBELT
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


@LAUNCHD
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


@LAUNCHD
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


@LAUNCHD
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


@LAUNCHD
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


@pytest.mark.parametrize('linux,restricted,diagnostic,hinted', [
    (True, True, 'bwrap: No permissions to create new namespace', True),
    (True, True, 'bwrap: Creating new namespace failed', True),
    (True, True, "bwrap: Can't find source path /missing", False),
    (True, False, 'bwrap: No permissions to create new namespace', False),
    (False, True, 'bwrap: No permissions to create new namespace', False),
])
def test_namespace_refusal_hint_names_the_restriction_and_rule(tmp_path, monkeypatch, linux, restricted, diagnostic, hinted):
    profile = tmp_path / 'derive.profile'
    profile.write_text('synthetic profile')
    monkeypatch.setattr(derive, 'PROFILE', profile)
    monkeypatch.setattr(derive, 'LINUX', linux)
    monkeypatch.setattr(derive, 'sandbox_command', lambda source, output, program: ['synthetic-sandbox', *program])
    monkeypatch.setattr(serve, 'userns_restricted', lambda: restricted)
    monkeypatch.setattr(serve.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout='', stderr=diagnostic))
    ok, detail = serve.profile_compiles()
    assert not ok and detail.startswith(diagnostic)
    if hinted:
        assert "Ubuntu's user-namespace restriction blocked the sandbox" in detail
        assert 'load the checked-in AppArmor rule ' + str(profile.with_name('postern-bwrap.apparmor')) in detail
    else:
        assert detail == diagnostic


@pytest.fixture
def native_module(monkeypatch):
    """Fake importing the Rust wheel: 'ok', 'missing', 'old' (another API version), 'broken' (an installed extension
    that cannot load, which CPython raises as ImportError), 'partial' (the package without its extension module),
    'oserror' (any other load fault) or 'panic' (a Rust panic, which PyO3 raises as a BaseException)."""
    def use(kind):
        def import_module(name):
            assert name == 'postern_progress_native'
            if kind == 'missing':
                raise ModuleNotFoundError(f"No module named '{name}'", name=name)
            if kind == 'broken':
                raise ImportError('dlopen(postern_progress_native.abi3.so): incompatible architecture')
            if kind == 'partial':
                raise ModuleNotFoundError('no extension module', name=name + '.' + name)
            if kind == 'oserror':
                raise OSError('dlopen failed')
            if kind == 'panic':
                raise type('PanicException', (BaseException,), {})('panicked while loading')  # PyO3's Rust panic
            return SimpleNamespace(API_VERSION=1 if kind == 'ok' else 2, CHECKPOINT_VERSION=1, reduce_incremental=lambda *args: None)
        monkeypatch.setattr(progress_backend, 'importlib', SimpleNamespace(import_module=import_module))
        progress_backend.native.cache_clear()
    yield use
    progress_backend.native.cache_clear()


NATIVE_DETAIL = {'missing': 'native module not installed', 'old': 'Rust progress module has an incompatible API version',
                 'broken': 'native module failed to load (ImportError)',
                 'partial': 'native module failed to load (ModuleNotFoundError)',
                 'oserror': 'native module failed to load (OSError)', 'panic': 'native module failed to load (PanicException)'}


@pytest.mark.parametrize('kind', ['ok', 'missing', 'old', 'broken', 'partial', 'oserror', 'panic'])
def test_preflight_says_which_progress_backend_install_selects_and_never_fails(tmp_path, native_module, kind):
    native_module(kind)
    name, ok, detail = serve.rust_progress_check(tmp_path / 'config.json')
    assert name == 'rust progress' and ok is True
    if kind == 'ok':
        assert detail == 'verified native module; ' + (
            'select it with postern.serve backend rust' if derive.LINUX else 'a fresh install selects rust')
    else:
        assert detail == NATIVE_DETAIL[kind] + '; Python serves'


REFUSED = ('progress-backend.json is not a private regular file of the API account naming python or rust; the API '
           'will refuse to start (run postern.serve backend python or rust as that account)')


@pytest.mark.parametrize('existing,kind,ok,detail', [
    ('rust', 'ok', True, 'progress-backend.json selects rust, which install keeps; verified native module'),
    ('rust', 'missing', False, 'progress-backend.json selects rust but native module not installed; the API will refuse to '
                               'start (reinstall the wheel or run postern.serve backend python)'),
    ('python', 'missing', True, 'progress-backend.json selects python, which install keeps; native module not installed'),
    ('unreadable', 'ok', False, REFUSED),
    ('symlink', 'ok', False, REFUSED),
    ('shared', 'ok', False, REFUSED),  # group-writable: the API refuses it
    ('fifo', 'ok', False, REFUSED),  # never opened, so the preflight cannot hang on it
])
def test_preflight_fails_a_selection_the_next_api_start_would_refuse(tmp_path, native_module, existing, kind, ok, detail):
    # Value: protects=install --check-only, the rollout's pre-restart gate, fails when a rust selection cannot start;
    #   fails_when=the row ignores the existing file and says ok, Python serves; why_new=the other rows have no file;
    #   seam=none; no_claim=the rollout helper's own handling of a FAIL row
    native_module(kind)
    config = tmp_path / 'config.json'
    selection = progress_backend.selection_path(config)
    if existing == 'symlink':
        selection.symlink_to(tmp_path / 'elsewhere.json')
    elif existing == 'fifo':
        os.mkfifo(selection, 0o600)
    else:
        selection.write_text('{not json' if existing == 'unreadable' else json.dumps({'backend': existing}))
        selection.chmod(0o664 if existing == 'shared' else 0o600)
    assert serve.rust_progress_check(config) == ('rust progress', ok, detail)
    if existing in ('rust', 'python', 'shared'):  # the file checks agree with the API's own
        try:
            accepted = progress_backend.read_selection(config) is not None
        except RuntimeError:
            accepted = False
        assert accepted is (existing != 'shared')


def launchd_install(tmp_path, monkeypatch, fail=None, guarded=True):
    """A macOS install against throwaway plist paths. Each launchctl call records whether a selection existed then;
    `guarded` is whether the running watchdog's heartbeat names the new selection."""
    config = write_config(tmp_path)
    monkeypatch.setattr(serve, 'watchdog_saw', lambda config_path, written: guarded)
    monkeypatch.setattr(serve, 'plist_path', lambda label, user_agent, home: tmp_path / 'jobs' / (label + '.plist'))
    monkeypatch.setattr(serve, 'preflight', lambda path: [('fixture prerequisites', True, 'ok')])
    monkeypatch.setattr(serve, 'listening', lambda port: False)
    monkeypatch.setattr(serve, 'tool_version', lambda path: None)
    selection = progress_backend.selection_path(config)
    calls = []

    def launchctl(*args):
        calls.append((args, selection.exists()))
        failed = args[0] == 'bootstrap' and fail and args[2].endswith(fail + '.plist')
        return SimpleNamespace(returncode=int(bool(failed)), stdout='', stderr='bootstrap failed' if failed else '')
    monkeypatch.setattr(serve, 'launchctl', launchctl)
    return config, selection, calls


def started(tmp_path, where='system'):
    """launchctl calls for bootstrapping every job in the launchd domain `where`, before any selection exists."""
    return [call for label in serve.service_labels(serve.LABEL) for call in (
        (('bootstrap', where, str(tmp_path / 'jobs' / (label + '.plist'))), False),
        (('kickstart', '-k', f'{where}/{label}'), False))]


@LAUNCHD
@pytest.mark.parametrize('user_agent', [False, True])
def test_a_fresh_install_with_the_verified_module_selects_rust_after_its_watchdog_starts(tmp_path, monkeypatch, capsys, native_module,
                                                                                         user_agent):
    # Generated by Claude Code (ship test coverage audit, pass 1): the user_agent=True row.
    # Value: protects=the login-scoped fallback restarts its own gui/<uid> API job onto rust, after both of its jobs start;
    #   fails_when=the restart after the write names system/<label> or another fixed domain, leaving Python under an ok line;
    #   why_new=every other install test runs in the system domain, so a hard-coded domain passed them all; seam=none;
    #   no_claim=real launchd domains, that a real kickstart succeeds
    native_module('ok')
    config, selection, calls = launchd_install(tmp_path, monkeypatch)
    assert serve.install(config, 'owner', tmp_path, user_agent, False, False) == 0
    where = f'gui/{os.getuid()}' if user_agent else 'system'
    # Every job, the watchdog included, started on Python; only then was rust written and the API restarted onto it.
    assert calls == started(tmp_path, where) + [(('kickstart', '-k', f'{where}/{serve.LABEL}'), True)]
    data = json.loads(selection.read_text())
    assert data['backend'] == 'rust' and data['set_by'] == 'install'
    info = selection.stat()
    assert info.st_mode & 0o777 == 0o600 and info.st_uid == os.getuid()
    assert progress_backend.read_selection(config) == data  # the API accepts it
    assert ('ok   progress-backend.json: rust, checked by the running watchdog before the API restarted onto it\n'
            '     (postern.serve backend python keeps Python)\n' in capsys.readouterr().out)


@LAUNCHD
@pytest.mark.parametrize('kind', ['missing', 'old', 'broken', 'partial', 'oserror', 'panic'])
def test_install_leaves_python_when_the_native_module_is_not_verified(tmp_path, monkeypatch, capsys, native_module, kind):
    native_module(kind)
    config, selection, calls = launchd_install(tmp_path, monkeypatch)
    assert serve.install(config, 'owner', tmp_path, False, False, False) == 0
    assert calls == started(tmp_path) and not selection.exists()
    assert f'note progress-backend.json: not set; Python serves ({NATIVE_DETAIL[kind]})' in capsys.readouterr().out


@LAUNCHD
def test_a_watchdog_that_fails_to_start_leaves_the_api_on_python(tmp_path, monkeypatch, capsys, native_module):
    native_module('ok')
    config, selection, calls = launchd_install(tmp_path, monkeypatch, fail=serve.WATCHDOG)
    assert serve.install(config, 'owner', tmp_path, False, False, False) == 1
    assert calls == started(tmp_path)[:3] and not selection.exists()
    assert 'FAIL launchctl bootstrap ' + serve.WATCHDOG in capsys.readouterr().out


@LAUNCHD
@pytest.mark.parametrize('existing', ['python', 'rust', 'unreadable', 'symlink'])
def test_install_never_replaces_an_existing_selection_even_with_force(tmp_path, monkeypatch, capsys, native_module, existing):
    native_module('ok')
    config, selection, calls = launchd_install(tmp_path, monkeypatch)
    if existing == 'symlink':
        selection.symlink_to(tmp_path / 'elsewhere.json')
    else:
        selection.write_text('{not json' if existing == 'unreadable' else json.dumps({'backend': existing, 'set_by': 'owner'}))
        selection.chmod(0o600)
    before = os.readlink(selection) if existing == 'symlink' else selection.read_bytes()
    assert serve.install(config, 'owner', tmp_path, False, False, True) == 0
    assert (os.readlink(selection) if existing == 'symlink' else selection.read_bytes()) == before
    assert [args for args, _ in calls] == [args for args, _ in started(tmp_path)]
    assert 'note progress-backend.json: already present; left unchanged' in capsys.readouterr().out


@LAUNCHD
def test_install_check_only_writes_no_selection(tmp_path, monkeypatch, native_module):
    native_module('ok')
    config, selection, calls = launchd_install(tmp_path, monkeypatch)
    assert serve.install(config, 'owner', tmp_path, False, True, False) == 0
    assert calls == [] and not selection.exists()


@LAUNCHD
@pytest.mark.parametrize('chown_fails', [False, True])
def test_the_root_rerun_writes_a_selection_the_service_account_owns(tmp_path, monkeypatch, capsys, native_module, chown_fails):
    # Extended by Claude Code (ship test coverage audit, pass 1): runs the real install call site, not the helper alone.
    # Value: protects=the sudo rerun of install hands the rust selection to the --user account before the rename, then
    #   restarts the API; fails_when=install passes select_piloted_backend another account (root, SUDO_USER, home);
    #   why_new=this test called select_piloted_backend directly and the fresh-install test runs unprivileged; seam=none;
    #   no_claim=a real sudo rerun, a real chown or launchd on this live host
    native_module('ok')
    config, selection, calls = launchd_install(tmp_path, monkeypatch)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)  # the second phase of a macOS install runs under sudo
    accounts = {'owner': SimpleNamespace(pw_uid=os.getuid(), pw_gid=20)}  # any other name is a KeyError, as for an unknown user
    monkeypatch.setattr(serve, 'pwd', SimpleNamespace(getpwnam=lambda user: accounts[user]))
    owners = []

    def chown(path, uid, gid):
        if Path(path).parent != config.parent:
            return  # the plists (root) and the logs (the owner) change owner as before
        owners.append((Path(path).parent, Path(path).name.startswith('.progress-backend-'), uid, gid, selection.exists()))
        if chown_fails:
            raise PermissionError('chown refused')
    monkeypatch.setattr(os, 'chown', chown)
    # A selection failure is a note, never a failed install: the jobs are already running on Python.
    assert serve.install(config, 'owner', tmp_path, False, False, False) == 0
    # The temporary file changed owner before the rename: the final path never held a root-owned selection.
    assert owners == [(config.parent, True, os.getuid(), 20, False)]
    output = capsys.readouterr().out
    if chown_fails:
        assert not selection.exists() and calls == started(tmp_path)
        assert not list(config.parent.glob('.progress-backend-*'))
        assert 'note progress-backend.json: not written (PermissionError); Python serves' in output
    else:
        assert json.loads(selection.read_text())['backend'] == 'rust'
        assert calls == started(tmp_path) + [(('kickstart', '-k', 'system/' + serve.LABEL), True)]


@LAUNCHD
@pytest.mark.parametrize('folder', ['foreign', 'read-only'])
def test_the_root_rerun_selects_nothing_in_a_folder_the_service_account_cannot_write(tmp_path, monkeypatch, capsys,
                                                                                      native_module, folder):
    # Value: protects=as root, install never writes a rust selection that the owner's watchdog could not replace with
    #   python; fails_when=the folder owner or mode check is dropped; why_new=root writes succeed anywhere; seam=none;
    #   no_claim=a real root-owned folder
    native_module('ok')
    config, selection, calls = launchd_install(tmp_path, monkeypatch)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    uid = os.getuid() + (1 if folder == 'foreign' else 0)
    monkeypatch.setattr(serve, 'pwd', SimpleNamespace(getpwnam=lambda user: SimpleNamespace(pw_uid=uid, pw_gid=20)))
    monkeypatch.setattr(os, 'chown', lambda *args: None)
    if folder == 'read-only':
        config.parent.chmod(0o500)
    try:
        assert serve.select_piloted_backend(config, 'owner', 'system', serve.LABEL) is None
    finally:
        config.parent.chmod(0o700)
    assert not selection.exists() and calls == []
    assert (f'note progress-backend.json: not set; {config.parent} is not writable by owner, so the watchdog could not '
            'return the API to Python; Python serves') in capsys.readouterr().out


@LAUNCHD
@pytest.mark.skipif(os.geteuid() == 0, reason='root can write the unwritable plist directory')
def test_the_unprivileged_phase_prints_the_sudo_rerun_and_selects_nothing(tmp_path, monkeypatch, capsys, native_module):
    # Generated by Claude Code (ship test coverage audit, pass 1).
    # Value: protects=the owner's unprivileged LaunchDaemon phase starts nothing, selects nothing and prints the exact sudo
    #   rerun; fails_when=rust is written where the plists cannot be (e.g. moved into the PermissionError branch to skip the
    #   chown); why_new=no test reached that branch; seam=none; no_claim=the real /Library/LaunchDaemons permissions or sudo
    native_module('ok')
    config, selection, calls = launchd_install(tmp_path, monkeypatch)
    jobs = tmp_path / 'jobs'
    jobs.mkdir(mode=0o500)  # stands in for /Library/LaunchDaemons, which the owner cannot write
    try:
        assert serve.install(config, 'owner', tmp_path, False, False, False) == 1
    finally:
        jobs.chmod(0o700)
    output = capsys.readouterr().out
    assert calls == [] and not selection.exists() and not selection.is_symlink()
    assert not list(config.parent.glob('.progress-backend-*')) and 'progress-backend.json' not in output
    rerun = shlex.join(['sudo', sys.executable, '-P', '-m', 'postern.serve', 'install', '--config', str(config),
                        '--user', 'owner', '--label', serve.LABEL])
    assert f'{jobs} needs root. Run exactly this:\n\n  {rerun}\n' in output


def test_an_interrupt_while_loading_the_native_module_is_not_swallowed(native_module, monkeypatch):
    # Value: protects=only load faults mean "not verified"; an interrupt still stops the install;
    #   fails_when=native_available catches every BaseException; why_new=the panic row widened the catch; seam=none;
    #   no_claim=a real Ctrl-C during a real import
    def interrupted(name):
        raise KeyboardInterrupt
    native_module('ok')
    monkeypatch.setattr(progress_backend, 'importlib', SimpleNamespace(import_module=interrupted))
    with pytest.raises(KeyboardInterrupt):
        progress_backend.native_available()


@LAUNCHD
def test_a_selection_renamed_into_place_but_not_synced_still_starts_under_its_watchdog(tmp_path, monkeypatch, capsys,
                                                                                       native_module):
    # Value: protects=the install's message and restart match the file on disk when only the folder sync failed;
    #   fails_when=a sync error after the rename prints "not written" and skips the restart, so Rust starts later unannounced;
    #   why_new=the chown row fails before the rename; seam=none; no_claim=a real fsync failure
    def unsynced(path):
        raise OSError('folder sync failed')
    native_module('ok')
    config, selection, calls = launchd_install(tmp_path, monkeypatch)
    monkeypatch.setattr(progress_backend, 'sync_directory', unsynced)
    assert serve.install(config, 'owner', tmp_path, False, False, False) == 0
    assert json.loads(selection.read_text())['backend'] == 'rust'
    assert calls == started(tmp_path) + [(('kickstart', '-k', 'system/' + serve.LABEL), True)]
    assert 'ok   progress-backend.json: rust' in capsys.readouterr().out


@LAUNCHD
def test_without_a_watchdog_heartbeat_install_takes_its_selection_back_and_python_serves(tmp_path, monkeypatch, capsys,
                                                                                         native_module):
    # Value: protects=Rust never starts unless the running watchdog has checked this exact selection;
    #   fails_when=install restarts the API onto rust after a bootstrap alone; why_new=a watchdog can bootstrap and crash;
    #   seam=none; no_claim=a real watchdog process (its heartbeat is faked)
    native_module('ok')
    config, selection, calls = launchd_install(tmp_path, monkeypatch, guarded=False)
    assert serve.install(config, 'owner', tmp_path, False, False, False) == 0
    assert calls == started(tmp_path) and not selection.exists()  # the API was never restarted onto it
    assert ('note progress-backend.json: not set; no running watchdog checked it within 75 s, so Python serves'
            in capsys.readouterr().out)


@LAUNCHD
def test_install_never_removes_a_selection_the_owner_made_while_it_waited(tmp_path, monkeypatch, native_module):
    native_module('ok')
    config, selection, calls = launchd_install(tmp_path, monkeypatch)

    def owner_selects_python(config_path, written):
        progress_backend.write_selection(config_path, 'python', set_by='owner')
        return False
    monkeypatch.setattr(serve, 'watchdog_saw', owner_selects_python)
    assert serve.install(config, 'owner', tmp_path, False, False, False) == 0
    assert json.loads(selection.read_text())['set_by'] == 'owner' and calls == started(tmp_path)


@LAUNCHD
def test_install_never_overwrites_a_selection_the_owner_made_while_it_checked(tmp_path, monkeypatch, capsys):
    # Value: protects=an owner's concurrent postern.serve backend python survives the install;
    #   fails_when=the install publishes with a replacing rename; why_new=the existence check alone leaves a window;
    #   seam=none; no_claim=a real second process
    config, selection, calls = launchd_install(tmp_path, monkeypatch)

    def import_module(name):  # the owner selects Python between the install's check and its write
        progress_backend.write_selection(config, 'python', set_by='owner')
        return SimpleNamespace(API_VERSION=1, CHECKPOINT_VERSION=1, reduce_incremental=lambda *args: None)
    monkeypatch.setattr(progress_backend, 'importlib', SimpleNamespace(import_module=import_module))
    progress_backend.native.cache_clear()
    try:
        assert serve.install(config, 'owner', tmp_path, False, False, False) == 0
    finally:
        progress_backend.native.cache_clear()
    assert json.loads(selection.read_text())['set_by'] == 'owner' and calls == started(tmp_path)
    assert not list(config.parent.glob('.progress-backend-*'))
    assert 'note progress-backend.json: already present; left unchanged' in capsys.readouterr().out


@pytest.mark.parametrize('heartbeat', ['this selection', 'another selection', 'none'])
def test_watchdog_saw_waits_for_a_heartbeat_naming_this_selection(tmp_path, monkeypatch, heartbeat):
    config = write_config(tmp_path)
    selection = progress_backend.selection_path(config)
    progress_backend.write_selection(config, 'rust', set_by='install')
    written = selection.lstat()
    beat = tmp_path / 'data' / watchdog.GUARD
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(serve.time, 'monotonic', lambda: next(clock))

    def sleep(seconds):  # the watchdog's next check lands while install waits
        if heartbeat != 'none':
            identity = [written.st_ino, written.st_mtime_ns + (0 if heartbeat == 'this selection' else 1)]
            beat.write_text(json.dumps({'pid': 1, 'selection': identity, 'state': 'waiting'}))
    monkeypatch.setattr(serve.time, 'sleep', sleep)
    assert serve.watchdog_saw(config, written) is (heartbeat == 'this selection')
