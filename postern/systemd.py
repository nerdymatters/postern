"""Linux system services. Rendering is unprivileged; the operator runs the printed root command."""
from __future__ import annotations
import difflib
import os
from pathlib import Path
import re
import shlex
import shutil
from string import Template
import subprocess
import sys
import tempfile

UNIT_DIR = Path('/etc/systemd/system')
UNIT_DIRS = (UNIT_DIR, Path('/run/systemd/system'), Path('/usr/local/lib/systemd/system'),
             Path('/usr/lib/systemd/system'), Path('/lib/systemd/system'))
TEMPLATE = Path(__file__).resolve().parents[1] / 'ops/postern/systemd/service.in'


def systemctl(*arguments):
    try:
        return subprocess.run(['systemctl', '--system', *arguments], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(['systemctl', *arguments], 1, '', str(error))


def quote(value, command=False):
    # systemd does not use shell quoting. Percent specifiers apply even inside quotes;
    # ExecStart also expands dollar signs. Reject controls instead of emitting new directives.
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('systemd values must not contain control characters')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if command:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def documents(config_path, user, home, python=None, label='org.nerdymatters.postern'):
    from . import serve
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*', user):
        raise ValueError('invalid Linux service user')
    label, watcher, proxy = serve.service_labels(label)
    config_path = Path(config_path).expanduser().absolute()
    python = python or sys.executable
    jobs = {
        label: ('Postern API', [python, '-P', '-m', 'postern.serve', 'run', '--config', str(config_path)], ''),
        watcher: ('Postern watchdog', [python, '-P', '-m', 'postern.watchdog', 'run', '--config', str(config_path)],
                  'Environment=' + quote('POSTERN_API_UNIT=' + label + '.service')),
        proxy: ('Postern front proxy', [shutil.which('caddy') or '/usr/bin/caddy', 'run', '--config',
                                      str(serve.CADDYFILE), '--adapter', 'caddyfile'], ''),
    }
    template = Template(TEMPLATE.read_text())
    return {key: (UNIT_DIR / (key + '.service'), template.substitute(
        description=description, user=user, root=quote(serve.ROOT), home=quote('HOME=' + str(home)),
        command=' '.join(quote(arg, command=True) for arg in arguments), environment=environment))
        for key, (description, arguments, environment) in jobs.items()}


def conflicting(path, document):
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        return f'{path} is a symlink (possibly masked); review it before installing'
    try:
        existing = path.read_text()
    except (OSError, UnicodeError):
        return f'{path} is not a readable unit'
    if existing == document:
        return None
    return f'{path} differs:\n' + ''.join(difflib.unified_diff(
        existing.splitlines(True), document.splitlines(True), fromfile=str(path), tofile='rendered unit'))


def other_installation(config_path, label):
    from . import serve
    for directory in UNIT_DIRS:
        for path in directory.glob('*.service'):
            if path.name == label + '.service':
                continue
            try:
                content = path.read_text()
                # Check the exact generated argument, including systemd escaping.
                if 'postern.serve' in content and quote(str(config_path), command=True) in content:
                    return f'{path} already supervises this configuration; keep its label'
                if 'postern.serve' in content and str(config_path) in content:
                    return f'{path} already supervises this configuration; keep its label'
            except (OSError, UnicodeError):
                continue
    if serve.listening(serve.settings(config_path).get('port', 8790)):
        if systemctl('is-active', '--quiet', label + '.service').returncode:
            return 'the API port is already in use by another service; stop it explicitly'
    return None


def install(config_path, user, home, check_only, force, label):
    from . import serve
    config_path = Path(config_path).expanduser().resolve()
    print(f'Postern systemd system services for {user} ({home})')
    if not serve.report(serve.preflight(config_path)):
        print('FAIL prerequisites are not met; nothing was installed')
        return 1
    if check_only:
        return 0
    duplicate = other_installation(config_path, label)
    if duplicate:
        print(f'FAIL {duplicate}; nothing was installed')
        return 1
    jobs = documents(config_path, user, home, label=label)
    for key, (path, document) in jobs.items():
        # A vendor unit or drop-in is deliberate too; never silently shadow it.
        for directory in UNIT_DIRS:
            candidate = directory / path.name
            conflict = conflicting(candidate, document)
            if conflict and (not force or candidate != path or candidate.is_symlink()):
                print(f'FAIL {conflict}; review before replacing (--force only replaces {path})')
                return 1
        installed = systemctl('show', key + '.service', '--property=LoadState', '--property=DropInPaths')
        properties = dict(line.split('=', 1) for line in installed.stdout.splitlines() if '=' in line)
        if ((installed.returncode and properties.get('LoadState') != 'not-found')
                or properties.get('DropInPaths', '').strip()):
            print(f'FAIL cannot verify an unmodified unit for {key}: review systemctl cat {key}.service')
            return 1
    state = Path(home) / '.local/state/postern'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix='install-', dir=state))
    commands = ['#!/bin/sh', 'set -eu']
    for key, (path, document) in jobs.items():
        source = stage / path.name
        source.write_text(document)
        source.chmod(0o600)
        # Refuse a change made after the owner reviewed the install, before touching any unit.
        if path.exists() or path.is_symlink():
            snapshot = stage / (path.name + '.before')
            snapshot.write_bytes(path.read_bytes())
            commands.append(f'cmp -s {shlex.quote(str(snapshot))} {shlex.quote(str(path))}')
        else:
            commands.append(f'test ! -e {shlex.quote(str(path))} && test ! -L {shlex.quote(str(path))}')
    for path, _ in jobs.values():
        commands.append(shlex.join(['install', '-o', 'root', '-g', 'root', '-m', '0644', str(stage / path.name), str(path)]))
    units = [path.name for path, _ in jobs.values()]
    commands += ['systemctl --system daemon-reload',
                 shlex.join(['systemctl', '--system', 'enable', *units]),
                 shlex.join(['systemctl', '--system', 'restart', *units])]
    script = stage / 'install.sh'
    script.write_text('\n'.join(commands) + '\n')
    script.chmod(0o600)
    print(f'ok   rendered units in {stage}; review them, then run this root command:\n\n'
          f'  {shlex.join(["sudo", "/bin/sh", str(script)])}\n\n'
          f'     Run status and ops/postern/reboot-check-linux.sh after installation and a real reboot.')
    return 0


def uninstall(label):
    from . import serve
    units = [key + '.service' for key in serve.service_labels(label)]
    command = (shlex.join(['systemctl', '--system', 'disable', '--now', *units]) + ' && ' +
               shlex.join(['rm', '-f', *[str(UNIT_DIR / unit) for unit in units]]) +
               ' && systemctl --system daemon-reload')
    print('Run this root command to remove only the selected unit family (data is retained):\n\n  ' +
          shlex.join(['sudo', '/bin/sh', '-c', command]))
    return 0


def status(config_path, label):
    from . import serve
    good = True
    for key in serve.service_labels(label):
        unit = key + '.service'
        enabled = systemctl('is-enabled', unit)
        active = systemctl('is-active', unit)
        ok = enabled.returncode == 0 and enabled.stdout.strip() == 'enabled' and active.returncode == 0
        good = good and ok
        print(f'{"ok  " if ok else "FAIL"} {unit}: {enabled.stdout.strip()}, {active.stdout.strip()}')
    answered, detail = serve.probe(serve.health_url(config_path))
    print(f'{"ok  " if answered else "FAIL"} {serve.health_url(config_path)} {detail}')
    return 0 if good and answered else 2
