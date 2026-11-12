"""Linux host contracts, using synthetic files and fake systemctl on every OS; one test asks the real systemd-analyze."""
import getpass
import json
import os
from pathlib import Path
import signal
import stat
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from postern import config, db, durability, progress_backend, serve, systemd, watchdog
from scripts import package_release


def result(stdout='', code=0):
    return SimpleNamespace(stdout=stdout, stderr='', returncode=code)


@pytest.fixture
def linux(monkeypatch, tmp_path):
    # Patching the module reference rather than sys.platform keeps third-party libraries on the host OS.
    for module in (serve, watchdog, durability, progress_backend, db):
        monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='linux', executable=serve.sys.executable))
    units = tmp_path / 'units'
    units.mkdir()
    monkeypatch.setattr(systemd, 'UNIT_DIR', units)
    monkeypatch.setattr(systemd, 'UNIT_DIRS', (units,))
    monkeypatch.setattr(systemd, 'systemctl', lambda *args: result('LoadState=not-found\nDropInPaths=\n'))
    monkeypatch.setattr(serve, 'launchctl', lambda *args: pytest.fail('Linux must never call launchctl'))
    monkeypatch.setattr(serve, 'listening', lambda port: False)
    monkeypatch.setattr(serve, 'preflight', lambda path: [('fixture prerequisites', True, 'ok')])
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'mode': 'demo', 'data_dir': str(tmp_path / 'data'), 'port': 12345}))
    return path


def test_linux_units_and_owner_install_render_without_mutating_services(linux, tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(systemd, 'systemctl', lambda *args: calls.append(args) or result('LoadState=not-found\n', 1))
    assert serve.install(linux, 'owner', tmp_path, False, False, False, 'org.example.room') == 0
    stage, = (tmp_path / '.local/state/postern').glob('install-*')
    api = (stage / 'org.example.room.service').read_text()
    watcher = (stage / 'org.example.room.watchdog.service').read_text()
    assert 'User=owner\n' in api and 'Restart=always\n' in api
    assert 'After=network-online.target\n' in api and 'WantedBy=multi-user.target\n' in api
    assert 'KillMode=control-group\n' in api and 'StartLimitIntervalSec=0\n' in api
    assert '"-P" "-m" "postern.serve" "run" "--config"' in api
    assert 'POSTERN_API_UNIT=org.example.room.service' in watcher
    assert 'postern.watchdog' in watcher and 'Restart=always' in watcher
    script = (stage / 'install.sh').read_text()
    assert 'daemon-reload' in script and 'enable org.example.room.service' in script
    assert 'restart org.example.room.service' in script
    assert not list(systemd.UNIT_DIR.iterdir()) and all(call[0] == 'show' for call in calls)
    assert 'sudo /bin/sh' in capsys.readouterr().out
    assert subprocess.run(['/bin/sh', '-n', str(stage / 'install.sh')]).returncode == 0


def test_check_only_and_failed_preflight_write_nothing(linux, tmp_path, monkeypatch):
    monkeypatch.setattr(systemd, 'systemctl', lambda *args: pytest.fail('check-only queried units'))
    assert serve.install(linux, 'owner', tmp_path, False, True, False) == 0
    monkeypatch.setattr(serve, 'preflight', lambda path: [('sandbox', False, 'not checked')])
    assert serve.install(linux, 'owner', tmp_path, False, False, False) == 1
    assert not (tmp_path / '.local').exists()


def test_linux_conflict_shows_diff_and_stops(linux, tmp_path, capsys):
    path = systemd.UNIT_DIR / (serve.LABEL + '.service')
    path.write_text('[Service]\nUser=someone-else\n')
    original = path.read_bytes()
    assert serve.install(linux, 'owner', tmp_path, False, False, False) == 1
    assert 'User=someone-else' in capsys.readouterr().out
    assert path.read_bytes() == original and not (tmp_path / '.local').exists()


def test_linux_dropin_and_duplicate_cannot_be_overwritten_even_with_force(linux, tmp_path, monkeypatch):
    monkeypatch.setattr(systemd, 'systemctl', lambda *args: result('LoadState=loaded\nDropInPaths=/etc/example.conf\n'))
    assert serve.install(linux, 'owner', tmp_path, False, False, True) == 1
    monkeypatch.setattr(systemd, 'systemctl', lambda *args: result('LoadState=not-found\n'))
    (systemd.UNIT_DIR / 'old.service').write_text('ExecStart=python -m postern.serve run --config ' + str(linux))
    assert serve.install(linux, 'owner', tmp_path, False, False, True) == 1
    assert not (tmp_path / '.local').exists()


def test_reinstalling_or_updating_the_same_label_is_not_a_second_installation(linux, tmp_path, monkeypatch, capsys):
    # The installed family's watchdog names the same config; it must not block that family's own update.
    for path, document in systemd.documents(linux.resolve(), 'owner', tmp_path).values():
        path.write_text(document)
    state = tmp_path / '.local/state/postern'
    assert serve.install(linux, 'owner', tmp_path, False, False, False) == 0  # identical units: nothing to review
    monkeypatch.setattr(serve, 'ROOT', tmp_path / 'releases/next')
    assert serve.install(linux, 'owner', tmp_path, False, False, False) == 1
    assert 'differs' in capsys.readouterr().out
    before = set(state.glob('install-*'))
    assert serve.install(linux, 'owner', tmp_path, False, False, True) == 0
    stage, = set(state.glob('install-*')) - before
    assert (stage / 'install.sh').read_text().count('cmp -s') == 3  # each reviewed unit must be unchanged at install


def test_preflight_says_why_caddy_rejects_the_proxy(tmp_path, monkeypatch):
    # Debian 13's own Caddy (2.6) has no PROXY protocol listener; the row must say so, not just "does not validate".
    old = tmp_path / 'caddy'
    old.write_text("#!/bin/sh\necho '{\"level\":\"info\"}' >&2\n"
                   "echo 'Error: module not registered: caddy.listeners.proxy_protocol' >&2\nexit 1\n")
    old.chmod(0o755)
    name, ok, detail = serve.front_proxy(str(old))
    assert name == 'front proxy' and not ok
    assert 'Caddy 2.8 or later' in detail and detail.endswith('caddy.listeners.proxy_protocol')
    monkeypatch.setattr(serve.derive, 'LINUX', True)
    assert "Caddy project's Debian repository" in serve.front_proxy(None)[2]


def test_linux_busy_port_requires_selected_active_unit(linux, tmp_path, monkeypatch):
    monkeypatch.setattr(serve, 'listening', lambda port: True)
    monkeypatch.setattr(systemd, 'systemctl', lambda *args: result('inactive', 3))
    assert serve.install(linux, 'owner', tmp_path, False, False, True) == 1


def test_linux_status_requires_all_units_enabled_and_active(linux, monkeypatch):
    monkeypatch.setattr(serve, 'probe', lambda url: (True, 'ready'))
    monkeypatch.setattr(systemd, 'systemctl', lambda *args: result('enabled' if args[0] == 'is-enabled' else 'active'))
    assert serve.status(linux, linux.parent, False) == 0
    monkeypatch.setattr(systemd, 'systemctl', lambda *args: result('disabled', 1) if '.watchdog.' in args[-1] else result('enabled'))
    assert serve.status(linux, linux.parent, False) == 2


def test_linux_uninstall_is_an_explicit_root_command_for_only_selected_family(linux, capsys, monkeypatch):
    monkeypatch.setattr(systemd, 'systemctl', lambda *args: pytest.fail('owner uninstall must only print a command'))
    assert serve.uninstall('owner', linux.parent, False, 'org.example.room') == 0
    printed = capsys.readouterr().out
    assert 'disable --now org.example.room.service' in printed
    assert 'org.example.room.watchdog.service' in printed and serve.LABEL not in printed
    assert 'daemon-reload' in printed and str(linux) not in printed
    assert serve.install(linux, 'owner', linux.parent, True, False, False) == 1
    assert serve.status(linux, linux.parent, True) == 1
    assert serve.uninstall('owner', linux.parent, True) == 1


def test_systemd_quoting_cannot_expand_or_inject_directives(tmp_path):
    assert systemd.quote('a %h $HOME "b"\\c', command=True) == '"a %%h $$HOME \\"b\\"\\\\c"'
    with pytest.raises(ValueError, match='control'):
        systemd.quote('config\nUser=root')
    with pytest.raises(ValueError, match='user'):
        systemd.documents(tmp_path / 'config', 'root\nExecStart=/bin/false', tmp_path)
    assert systemd.path_value('/srv/a b/%i') == '/srv/a b/%%i'
    for bad in ('relative/path', '/srv/a\nUser=root', '/srv/a\\', ' /srv/a'):
        with pytest.raises(ValueError, match='absolute'):
            systemd.path_value(bad)


@pytest.mark.skipif(not shutil.which('systemd-analyze'), reason='needs systemd-analyze (Linux CI has it)')
def test_rendered_units_load_in_real_systemd(tmp_path, monkeypatch):
    # Fake systemctl elsewhere cannot tell whether systemd accepts the files; this asks it.
    caddy = tmp_path / 'caddy'
    caddy.write_text('#!/bin/sh\n')
    caddy.chmod(0o755)
    monkeypatch.setattr(systemd.shutil, 'which', lambda name: str(caddy))
    config_path = tmp_path / 'config $HOME %h.json'
    jobs = systemd.documents(config_path, getpass.getuser(), tmp_path, python=sys.executable)
    for path, document in jobs.values():
        (tmp_path / path.name).write_text(document)
    result = subprocess.run(['systemd-analyze', 'verify', '--man=no', *[str(tmp_path / path.name) for path, _ in jobs.values()]],
                            capture_output=True, text=True, timeout=60)
    ours = [line for line in result.stderr.splitlines() if 'postern' in line]
    assert result.returncode == 0 and not ours, result.stderr


def make_guard(linux, run, **kwargs):
    alerts = []
    guard = watchdog.Fallback(linux, linux.parent, 12345, lambda *args: alerts.append(args), run=run, **kwargs)
    return guard, alerts


@pytest.mark.parametrize('environment', [
    'Environment="POSTERN_PROGRESS_BACKEND=rust"\nEnvironmentFiles=\n',
    'Environment=\nEnvironmentFiles=/etc/override.env (ignore_errors=no)\n',
    'Environment=POSTERN_PROGRESS_BACKEND=python\nEnvironmentFiles=/etc/override.env (ignore_errors=no)\n',
])
def test_linux_override_alerts_without_changing_selection_or_signalling(linux, environment):
    progress_backend.write_selection(linux, 'rust')
    before = linux.with_name(watchdog.SELECTION).read_bytes()
    guard, alerts = make_guard(linux, lambda *args, **kwargs: result(environment),
                              kill=lambda *args: pytest.fail('override must not signal'))
    for _ in range(4):
        guard.observe(False, 'unhealthy')
    assert len(alerts) == 1 and 'systemd override' in alerts[0][0]
    assert linux.with_name(watchdog.SELECTION).read_bytes() == before


def test_linux_fallback_syncs_python_before_sigterm_and_names_systemd(linux, monkeypatch):
    progress_backend.write_selection(linux, 'rust')
    signals = []
    def kill(pid, sig):
        assert json.loads(linux.with_name(watchdog.SELECTION).read_text())['backend'] == 'python'
        signals.append((pid, sig))
    guard, alerts = make_guard(linux, lambda *args, **kwargs: result('Environment=\nEnvironmentFiles=\n'),
                              kill=kill, sleep=lambda seconds: None)
    monkeypatch.setattr(guard, 'serving', lambda active: (123, {'uid': os.getuid()}))
    monkeypatch.setattr(guard, 'active', lambda: {'pid': 123, 'backend': 'rust', 'source': watchdog.SELECTION})
    monkeypatch.setattr(guard, 'resources', lambda *args: [])
    monkeypatch.setattr(guard, 'alive', lambda pid: False)
    for _ in range(3):
        guard.observe(False, 'unhealthy')
    assert signals == [(123, signal.SIGTERM)]
    assert 'systemd starts it again with Python' in alerts[-1][1]


def test_linux_lsof_path_and_unreadable_environment_are_safe(linux):
    calls = []
    guard, alerts = make_guard(linux, lambda args, **kwargs: calls.append(args) or result('123\n'))
    assert guard.listening() == {123} and calls[0][0] == '/usr/bin/lsof'
    guard.run = lambda *args, **kwargs: result('', 1)
    progress_backend.write_selection(linux, 'rust')
    for _ in range(3):
        guard.observe(False)
    assert json.loads(linux.with_name(watchdog.SELECTION).read_text())['backend'] == 'rust'
    assert 'cannot be verified' in alerts[-1][1]


@pytest.mark.parametrize('writer', ['owner', 'watchdog', 'config'])
def test_linux_durable_replacement_syncs_file_then_directory(linux, monkeypatch, writer):
    events = []
    original = os.fsync
    def fsync(fd):
        events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
        original(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    if writer == 'owner':
        progress_backend.write_selection(linux, 'python')
    elif writer == 'watchdog':
        watchdog.write_python(linux.with_name(watchdog.SELECTION), ['health'], {})
    else:
        config.private_json(linux, {'mode': 'demo'})
    assert events == ['file', 'directory']


def test_failed_selection_sync_never_signals_the_api(linux, monkeypatch):
    progress_backend.write_selection(linux, 'rust')
    guard, alerts = make_guard(linux, lambda *args, **kwargs: result('Environment=\nEnvironmentFiles=\n'),
                              kill=lambda *args: pytest.fail('failed durable selection must not signal'))
    monkeypatch.setattr(os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError('disk unavailable')))
    for _ in range(3):
        guard.observe(False)
    assert json.loads(linux.with_name(watchdog.SELECTION).read_text())['backend'] == 'rust'
    assert 'could not return' in alerts[0][0] and not list(linux.parent.glob('.progress-backend-*'))


def test_linux_database_readiness_reports_effective_durability(linux):
    writer = db.Database(linux.parent / 'database.sqlite3')
    try:
        assert writer.settings == {'journal_mode': 'wal', 'synchronous': 2, 'fullfsync': 0,
                                   'busy_timeout': 1000, 'fullfsync_supported': False}
        writer.submit(lambda connection: connection.execute("INSERT INTO meta VALUES('linux-test','committed')")).result()
        assert writer.read(lambda connection: connection.execute("SELECT value FROM meta WHERE key='linux-test'").fetchone()[0]) == 'committed'
    finally:
        writer.close()


def test_release_keeps_linux_lock_template_and_explicit_native_wheel(tmp_path):
    paths = ['requirements-linux.lock', 'ops/postern/systemd/service.in', 'ops/postern/reboot-check-linux.sh']
    assert package_release.release_files(paths) == paths
    wheel = tmp_path / 'postern_progress_native-0.2.0-cp313-cp313-manylinux_2_28_x86_64.whl'
    wheel.write_bytes(b'fixture')
    assert package_release.native_wheels([wheel], 'linux') == [wheel]
    with pytest.raises(ValueError, match='for macos'):
        package_release.native_wheels([wheel], 'macos')
    with pytest.raises(ValueError, match='Duplicate'):
        package_release.native_wheels([wheel, wheel], 'linux')
    linked = tmp_path / 'symlink.whl'
    linked.symlink_to(wheel)
    with pytest.raises(ValueError, match='regular file'):
        package_release.native_wheels([linked], 'linux')


def test_linux_reboot_check_fails_for_disabled_watchdog(tmp_path):
    # Only fake executables enter PATH; the script cannot reach real systemctl, HTTP or a config.
    binary = tmp_path / 'bin'
    binary.mkdir()
    for name, body in {'systemctl': 'case "$*" in *is-enabled*watchdog*) echo disabled; exit 1;; *is-enabled*) echo enabled;; esac',
                       'curl': 'exit 0', 'uptime': 'echo synthetic-boot', 'python': 'echo 12345'}.items():
        path = binary / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)
    script = serve.ROOT / 'ops/postern/reboot-check-linux.sh'
    checked = subprocess.run(['/bin/sh', str(script), str(tmp_path / 'config.json'), 'org.example.room', str(binary / 'python')],
                             env={'PATH': str(binary)}, capture_output=True, text=True)
    assert checked.returncode == 1 and 'watchdog.service must be enabled and active' in checked.stdout


def test_linux_packager_emits_installable_inputs_and_wheel_metadata(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    (root / 'dashboard/web/dist').mkdir(parents=True)
    inputs = {'VERSION': '1.2.3', 'pyproject.toml': 'version = "1.2.3"',
              'dashboard/web/package.json': '{"version":"1.2.3"}',
              'requirements-linux.lock': 'pillow==13.0.0', 'ops/postern/systemd/service.in': '[Service]',
              'dashboard/web/dist/index.html': '<html>fixture</html>'}
    for name, text in inputs.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    wheel = tmp_path / 'postern_progress_native-0.2.0-cp313-cp313-linux_x86_64.whl'
    wheel.write_bytes(b'fixture wheel')
    output = tmp_path / 'release'
    monkeypatch.setattr(package_release, 'ROOT', root)
    monkeypatch.setattr('sys.argv', ['package_release.py', '--platform', 'linux', '--output', str(output), '--native-wheel', str(wheel)])
    def git(args, **kwargs):
        if args[1] == 'status':
            return b''
        if args[1] == 'ls-files':
            return '\0'.join(name for name in inputs if '/dist/' not in name).encode()
        return b'fixture-revision'
    monkeypatch.setattr(package_release.subprocess, 'check_output', git)
    monkeypatch.setattr(package_release.subprocess, 'run', lambda *args, **kwargs: result())
    package_release.main()
    import tarfile
    import hashlib
    archive = output / 'postern-1.2.3-linux-pilot.tar.gz'
    with tarfile.open(archive) as bundled:
        names = bundled.getnames()
        assert 'postern-1.2.3/requirements-linux.lock' in names
        assert 'postern-1.2.3/ops/postern/systemd/service.in' in names
        assert 'postern-1.2.3/dashboard/web/dist/index.html' in names
        assert bundled.extractfile('postern-1.2.3/wheels/' + wheel.name).read() == wheel.read_bytes()
        metadata = json.load(bundled.extractfile('postern-1.2.3/RELEASE.json'))
        assert metadata['platform'] == 'linux' and metadata['native_wheels'] == [wheel.name]
        assert all(not member.uid and not member.uname for member in bundled.getmembers())
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in (output / 'SHA256SUMS').read_text()


def test_force_cannot_replace_a_masked_unit(linux, tmp_path):
    (systemd.UNIT_DIR / (serve.LABEL + '.service')).symlink_to('/dev/null')
    assert serve.install(linux, 'owner', tmp_path, False, False, True) == 1
    assert not (tmp_path / '.local').exists()


def test_directory_sync_failure_cannot_restart_on_the_next_tick(linux, monkeypatch):
    progress_backend.write_selection(linux, 'rust')
    guard, alerts = make_guard(linux, lambda *args, **kwargs: result('Environment=\nEnvironmentFiles=\n'),
                              kill=lambda *args: pytest.fail('directory sync failed'))
    monkeypatch.setattr(guard, 'active', lambda: {'pid': 123, 'backend': 'rust', 'source': watchdog.SELECTION})
    monkeypatch.setattr(guard, 'serving', lambda active: (123, {}))
    monkeypatch.setattr(guard, 'resources', lambda *args: [])
    original = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('directory sync failed')
        original(fd)
    monkeypatch.setattr(os, 'fsync', fail_directory)
    for _ in range(5):
        guard.observe(False)
    assert json.loads(linux.with_name(watchdog.SELECTION).read_text())['backend'] == 'python'
    assert any('could not return' in subject for subject, _ in alerts)
