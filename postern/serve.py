"""Run Postern as a service on this Mac.

    .venv/bin/python -m postern.serve run       --config ~/.config/postern/config.json
    .venv/bin/python -m postern.serve install   [--check-only] [--user-agent] [--user NAME] [--force]
    .venv/bin/python -m postern.serve uninstall [--user-agent]
    .venv/bin/python -m postern.serve status    [--user-agent]

ENG-D8 wants Postern back after a power cut with nobody logged in, so the default install is a
LaunchDaemon in /Library/LaunchDaemons with UserName set: launchd starts it as soon as FileVault unlocks
the disk, not at the owner's next login. `--user-agent` keeps the CRM's login-scoped LaunchAgent as the
documented fallback for a Mac where the daemon cannot be given its data path.

Two jobs are installed, not one. The watchdog runs beside the server rather than inside it, so a server
that dies, hangs or crash-loops cannot take its own alerting with it.

Writing /Library/LaunchDaemons needs root. Run as the owner, install does the prerequisite checks,
renders the plists and prints the one sudo command that finishes the job.
"""
from __future__ import annotations
import argparse
import json
import os
import platform
import plistlib
import pwd
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .config import private_json
from . import derive
from .watchdog import probe

LABEL = 'org.nerdymatters.postern'
WATCHDOG = LABEL + '.watchdog'
PROXY = LABEL + '.proxy'
CADDYFILE = Path(__file__).resolve().parents[1] / 'ops' / 'postern' / 'Caddyfile'
ROOT = Path(__file__).resolve().parents[1]


def profile_compiles():
    """Compile and apply the profile exactly as a real job would, against a throwaway directory."""
    if not derive.PROFILE.is_file():
        return False, f'{derive.PROFILE} is missing'
    with tempfile.TemporaryDirectory(prefix='postern-profile-') as scratch:
        scratch = Path(scratch).resolve()
        source = scratch / 'probe.bin'
        source.write_bytes(b'')
        result = subprocess.run(['/usr/bin/sandbox-exec', *derive.sandbox_profile_args(source, scratch), '/usr/bin/true'],
                                capture_output=True, text=True)
    detail = (result.stderr or result.stdout).strip()
    return result.returncode == 0, detail or f'{derive.PROFILE} compiles and applies'


def face_check_in_sandbox():
    """Run the real converter child, under the real profile and its resource limits, on a blank photo.

    "No face found" proves the model loads and infers where enrollment runs it; inside the sandbox every
    detector failure, such as a denied read or a hit limit, shows only as face_check_unavailable.
    """
    from PIL import Image
    from .derive_child import FACE_FAILURES
    no_face = next(code for code, name in FACE_FAILURES.items() if name == 'face_required')
    with tempfile.TemporaryDirectory(prefix='postern-face-') as scratch:
        scratch = Path(scratch).resolve()
        source, output = scratch / 'blank.jpg', scratch / 'job'
        output.mkdir()
        Image.new('RGB', (256, 256), '#888888').save(source, 'JPEG')
        result = subprocess.run(derive.job_command(source, output, 'selfie', 1, derive.SELFIE_MAX_PIXELS, derive.SELFIE_OUTPUT_LIMIT, derive.poppler_dir()),
                                cwd=output, env=derive.job_env(output), capture_output=True, timeout=60)
    return result.returncode == no_face, result.returncode


def tool_version(path):
    if not path:
        return None
    result = subprocess.run([path, '-version' if path.endswith('ffmpeg') else '-v'], capture_output=True, text=True)
    return next((line.strip() for line in (result.stderr + result.stdout).splitlines() if line.strip()), '')


def settings(config_path):
    try:
        return json.loads(Path(config_path).read_text())
    except (OSError, ValueError):
        return {}


def preflight(config_path):
    """Everything that has to be true before this Mac can serve. Returns (name, ok, detail) rows."""
    checks = []
    checks.append(('python', sys.version_info >= (3, 11), f'{platform.python_version()} at {sys.executable}'))

    for tool in ('pdftoppm', 'pdfinfo', 'ffmpeg', 'ffprobe'):
        found = shutil.which(tool)
        checks.append((tool, bool(found), f'{found} — {tool_version(found)}' if found else 'not found (brew install poppler ffmpeg)'))

    caddy = shutil.which('caddy')
    valid = bool(caddy) and subprocess.run([caddy, 'validate', '--config', str(CADDYFILE), '--adapter', 'caddyfile'],
                                           capture_output=True).returncode == 0
    checks.append(('front proxy', valid, f'{caddy} with {CADDYFILE}' if valid else
                   'caddy not found (brew install caddy)' if not caddy else f'{CADDYFILE} does not validate'))

    try:
        import pillow_heif
        release = pillow_heif.__version__
        current = tuple(int(part) for part in release.split('.')[:3])
        checks.append(('pillow-heif', current >= (1, 3, 0),
                       f'{release} (libheif {pillow_heif.libheif_version()})'
                       + ('' if current >= (1, 3, 0) else ' — 1.3.0 or newer is required')))
    except Exception as error:
        checks.append(('pillow-heif', False, f'not importable: {error}'))

    content_root = None
    content_error = None
    try:
        from . import training
        values = settings(config_path)
        content_root = training.resolve(values['data_dir'], values.get('mode', 'google'))
        # Includes the optional private question-ID registry used during migration.
        training.load(content_root)
    except Exception as error:
        content_error = error

    try:
        from . import branding
        values = settings(config_path)
        branding.load(values['data_dir'], required=values.get('mode', 'google') == 'google')
        checks.append(('organization settings', True, 'private organization settings validated'))
    except Exception as error:
        checks.append(('organization settings', False, str(error)))

    try:
        from .quiz import load
        if content_error:
            raise content_error
        bank = load(content_root / 'quizzes.json')
        # A bank that lost a dealing rule loads, but its hands can be guessed or unbalanced: refuse it here.
        weak = [number for number, entry in bank.items() if entry['tier'] != 'full']
        checks.append(('quiz bank', not weak, f'{len(bank)} chapter quiz(zes), {sum(len(entry["questions"]) for entry in bank.values())} questions'
                       + (f'; chapters {", ".join(map(str, weak))} cannot deal balanced, unguessable hands' if weak else '')))
    except Exception as error:
        checks.append(('quiz bank', False, f'Training quiz bank is invalid: {error}'))

    try:
        from . import exam, captions
        if content_error:
            raise content_error
        final = exam.load(content_root / 'final_exam.json')[exam.KEY]
        tracks = captions.load(content_root / 'captions')
        checks.append(('final exam and subtitles', True,
                       f'{len(final["questions"])} bilingual questions, pass with {final["pass_mark"]}; {len(tracks)} English tracks'))
    except Exception as error:
        checks.append(('final exam and subtitles', False, f'Invalid training content: {error}'))

    sandbox = Path('/usr/bin/sandbox-exec')
    checks.append(('sandbox-exec', sandbox.is_file(), str(sandbox) if sandbox.is_file() else 'missing — the converter cannot run'))

    try:
        from PIL import Image
        from .derive_child import detect_faces
        # Check model integrity, native dependencies and an actual inference, not just imports.
        ready = detect_faces(Image.new('RGB', (256, 256))) == []
    except Exception:
        checks.append(('selfie face detector', False, 'Install the locked dependencies and bundled postern/models files'))
    else:
        detail = 'Local YuNet model and CPU inference, in and out of the converter sandbox' if ready else 'Unexpected detector result'
        if ready:
            # Then the same inside the converter sandbox, where enrollment runs it.
            try:
                ready, code = face_check_in_sandbox()
                if not ready:
                    detail = f'The detector fails inside the converter sandbox (exit {code}); new enrollments would be refused'
            except Exception as error:
                ready, detail = False, f'The face check could not run inside the converter sandbox ({error.__class__.__name__}: {error})'
        checks.append(('selfie face detector', ready, detail))

    config_path = Path(config_path)
    if config_path.is_file():
        mode = stat.S_IMODE(config_path.stat().st_mode)
        checks.append(('config file', mode == 0o600, f'{config_path} mode {mode:04o}'
                       + ('' if mode == 0o600 else ' — run chmod 600 on it')))
    else:
        checks.append(('config file', False, f'{config_path} does not exist'))

    data_dir = settings(config_path).get('data_dir')
    if data_dir and Path(data_dir).expanduser().is_dir():
        data_dir = Path(data_dir).expanduser()
        mode = stat.S_IMODE(data_dir.stat().st_mode)
        checks.append(('data directory', mode == 0o700, f'{data_dir} mode {mode:04o}'
                       + ('' if mode == 0o700 else ' — run chmod 700 on it')))
    else:
        checks.append(('data directory', False, f'{data_dir or "(not set in the config file)"} is not a directory'
                                                ' — run postern.bootstrap first'))

    compiles, detail = profile_compiles()
    checks.append(('sandbox profile', compiles, detail))
    return checks


def report(checks):
    for name, ok, detail in checks:
        print(f'{"ok  " if ok else "FAIL"} {name:<15} {detail}')
    return all(ok for _, ok, _ in checks)


def plist(label, arguments, log, user=None, home=None):
    document = {'Label': label, 'ProgramArguments': arguments, 'WorkingDirectory': str(ROOT),
                'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin',
                                         'PYTHONWARNINGS': 'ignore', 'HOME': str(home)},
                'RunAtLoad': True, 'KeepAlive': True,
                'StandardOutPath': str(log), 'StandardErrorPath': str(log)}
    if user:
        # A daemon starts before anyone logs in, so it has to be told whose account owns the data.
        document['UserName'] = user
    return document


def plist_path(label, user_agent, home):
    directory = Path(home) / 'Library' / 'LaunchAgents' if user_agent else Path('/Library/LaunchDaemons')
    return directory / f'{label}.plist'


def service_labels(label):
    if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,127}', label):
        raise argparse.ArgumentTypeError('label must contain only letters, numbers, dots and hyphens')
    return label, label + '.watchdog', label + '.proxy'


def documents(config_path, user, home, user_agent=False, python=None, label=LABEL):
    """The launchd jobs and where each one belongs."""
    python, logs = python or sys.executable, Path(home) / 'Library' / 'Logs' / 'postern'
    caddy = shutil.which('caddy') or '/opt/homebrew/bin/caddy'
    # -P keeps the current directory off the import path: a checkout is usually a folder named "postern",
    # and run from its parent that folder shadows the package as an empty namespace.
    label, watcher, proxy = service_labels(label)
    jobs = {label: ([python, '-P', '-m', 'postern.serve', 'run', '--config', str(config_path)], logs / 'postern.log'),
            watcher: ([python, '-P', '-m', 'postern.watchdog', 'run', '--config', str(config_path)], logs / 'watchdog.log'),
            # The Funnel hands connections to this listener, not to the server: without it a reboot leaves
            # the public address answering nothing while the server itself looks healthy.
            proxy: ([caddy, 'run', '--config', str(CADDYFILE), '--adapter', 'caddyfile'], logs / 'proxy.log')}
    return {label: (plist_path(label, user_agent, home), plist(label, arguments, log, None if user_agent else user, home))
            for label, (arguments, log) in jobs.items()}


def conflicting(path, document):
    """A plist this installer did not write is somebody's deliberate work; say what differs and stop."""
    if not path.exists():
        return None
    try:
        existing = plistlib.loads(path.read_bytes())
    except Exception:
        return f'{path} exists and is not a readable plist'
    if existing == document:
        return None
    keys = sorted(key for key in set(existing) | set(document) if existing.get(key) != document.get(key))
    return f'{path} already exists and differs in: {", ".join(keys)}'


def launchctl(*arguments):
    return subprocess.run(['/bin/launchctl', *arguments], capture_output=True, text=True)


def domain(user_agent):
    return f'gui/{os.getuid()}' if user_agent else 'system'


def owner(name):
    name = name or os.environ.get('SUDO_USER') or pwd.getpwuid(os.getuid()).pw_name
    return name, Path(pwd.getpwnam(name).pw_dir)


def as_owner(path, user):
    if os.geteuid() == 0:
        entry = pwd.getpwnam(user)
        os.chown(path, entry.pw_uid, entry.pw_gid)


def listening(port, host='127.0.0.1'):
    with socket.socket() as knock:
        knock.settimeout(0.25)
        return knock.connect_ex((host, port)) == 0


def health_url(config_path):
    return f'http://127.0.0.1:{settings(config_path).get("port", 8790)}/health'


def other_installation(config_path, label, user_agent, home):
    """Do not create a second supervisor for an already configured Postern instance."""
    selected = plist_path(label, user_agent, home)
    directories = {plist_path(label, False, home).parent, plist_path(label, True, home).parent}
    for directory in directories:
        for path in directory.glob('*.plist'):
            if path == selected:
                continue
            try:
                arguments = plistlib.loads(path.read_bytes()).get('ProgramArguments', [])
                if 'postern.serve' not in arguments or 'run' not in arguments or '--config' not in arguments:
                    continue
                existing = Path(arguments[arguments.index('--config') + 1]).expanduser().resolve()
                if existing == Path(config_path).expanduser().resolve():
                    return f'{path} already supervises this configuration; use its label and launchd domain'
            except (OSError, ValueError, TypeError, IndexError, AttributeError, plistlib.InvalidFileException):
                continue  # Unrelated unreadable plists do not identify this application's service.
    if listening(settings(config_path).get('port', 8790)):
        current = launchctl('print', f'{domain(user_agent)}/{label}')
        if current.returncode:
            return 'the API port is already in use by another service; keep the existing label or stop it explicitly'
    return None


def install(config_path, user, home, user_agent, check_only, force, label=LABEL):
    label, watcher, _ = service_labels(label)
    print(f'Postern {"user agent" if user_agent else "LaunchDaemon"} for {user} ({home})')
    checks = preflight(config_path)
    if not report(checks):
        print('FAIL prerequisites are not met; nothing was installed')
        return 1
    if check_only:
        return 0

    duplicate = other_installation(config_path, label, user_agent, home)
    if duplicate:
        print(f'FAIL {duplicate}; nothing was installed')
        return 1

    logs = Path(home) / 'Library' / 'Logs' / 'postern'
    logs.mkdir(parents=True, exist_ok=True)
    as_owner(logs, user)
    record = logs / 'preflight.json'
    # Pin the converter library versions at install time so a later Homebrew upgrade is a deliberate act.
    private_json(record, {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'user': user,
                          'ffmpeg': tool_version(shutil.which('ffmpeg')),
                          'checks': {name: detail for name, _, detail in checks}})
    as_owner(record, user)

    jobs = documents(config_path, user, home, user_agent, label=label)
    for path, document in jobs.values():
        conflict = conflicting(path, document)
        if conflict and not force:
            print(f'FAIL {conflict}\n     read it, then re-run with --force to replace it')
            return 1

    where = domain(user_agent)
    try:
        for job_label, (path, document) in jobs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                launchctl('bootout', f'{where}/{job_label}')
            path.write_bytes(plistlib.dumps(document))
            path.chmod(0o644)
            if not user_agent and os.geteuid() == 0:
                os.chown(path, 0, 0)  # launchd ignores a daemon plist that is not root-owned
    except PermissionError:
        command = ['sudo', sys.executable, '-P', '-m', 'postern.serve', 'install', '--config', str(config_path),
                   '--user', user, '--label', label] + (['--user-agent'] if user_agent else []) + (['--force'] if force else [])
        print(f'\n     {plist_path(label, user_agent, home).parent} needs root. Run exactly this:\n\n  {shlex.join(command)}\n')
        return 1

    for job_label, (path, _) in jobs.items():
        result = launchctl('bootstrap', where, str(path))
        if result.returncode:
            print(f'FAIL launchctl bootstrap {job_label}: {(result.stderr or result.stdout).strip()}')
            return 1
        launchctl('kickstart', '-k', f'{where}/{job_label}')
    print(f'ok   {label} and {watcher} installed in {where}\n'
          f'     {health_url(config_path)} · logs {logs}\n'
          f'     ENG-D8 is not satisfied until a real reboot proves it: run {ROOT}/ops/postern/reboot-check.sh')
    return 0


def uninstall(user, home, user_agent, label=LABEL):
    where = domain(user_agent)
    for job_label in service_labels(label):
        path = plist_path(job_label, user_agent, home)
        launchctl('bootout', f'{where}/{job_label}')
        if not path.exists():
            print(f'ok   {job_label} was not installed in {where}')
            continue
        try:
            path.unlink()
        except PermissionError:
            command = ['sudo', sys.executable, '-P', '-m', 'postern.serve', 'uninstall', '--user', user,
                       '--label', label] + (['--user-agent'] if user_agent else [])
            print(f'     removing {path} needs root. Run exactly this:\n\n  {shlex.join(command)}\n')
            return 1
        print(f'ok   {job_label} removed from {where}')
    return 0


def status(config_path, home, user_agent, label=LABEL):
    where, down = domain(user_agent), False
    application, watcher, proxy = service_labels(label)
    for job_label in (application, watcher, proxy):
        result = launchctl('print', f'{where}/{job_label}')
        if result.returncode:
            print(f'FAIL {job_label:<28} not loaded in {where}')
            down = down or job_label != watcher
            continue
        lines = [line.strip() for line in result.stdout.splitlines()]
        state = next((line for line in lines if line.startswith('state =')), 'state = ?')
        pid = next((line for line in lines if line.startswith('pid =')), 'pid = none')
        print(f'ok   {job_label:<28} {state}, {pid}')
        if job_label in (application, proxy):
            # Both have to be up for anyone outside to reach Postern; the server alone answering is not enough.
            down = down or 'running' not in state
    url = health_url(config_path)
    answered, detail = probe(url)
    print(f'{"ok  " if answered else "FAIL"} {url:<28} {detail}')
    return 0 if answered and not down else 2


def run(config_path):
    from .config import Config
    config = Config.load(config_path)
    if listening(config.port):
        print(f'FAIL 127.0.0.1:{config.port} is already listening. Stop that process, or change "port" '
              f'in {config_path}; Postern will not share a port it did not open.')
        return 1
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(config), host='127.0.0.1', port=config.port, log_level='warning', access_log=False)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='postern.serve', description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('install', 'uninstall', 'status', 'run'):
        action = actions.add_parser(name)
        action.add_argument('--config', default=None, help='default ~/.config/postern/config.json for --user')
        if name != 'run':
            action.add_argument('--label', default=LABEL, type=lambda value: service_labels(value)[0],
                                help='launchd application label; existing hosts must pass their installed label')
            action.add_argument('--user', default=None, help='the macOS account that owns the data (default $SUDO_USER or you)')
            action.add_argument('--user-agent', action='store_true',
                                help='fallback: install a login-scoped LaunchAgent instead of the boot LaunchDaemon')
    install_action = actions.choices['install']
    install_action.add_argument('--check-only', action='store_true', help='run the prerequisite checks and stop')
    install_action.add_argument('--force', action='store_true', help='replace an existing plist that differs')
    arguments = parser.parse_args(argv)

    user, home = owner(getattr(arguments, 'user', None))
    config_path = Path(arguments.config).expanduser() if arguments.config else home / '.config' / 'postern' / 'config.json'
    if arguments.action == 'install':
        return install(config_path, user, home, arguments.user_agent, arguments.check_only, arguments.force, arguments.label)
    if arguments.action == 'uninstall':
        return uninstall(user, home, arguments.user_agent, arguments.label)
    if arguments.action == 'status':
        return status(config_path, home, arguments.user_agent, arguments.label)
    return run(config_path)


if __name__ == '__main__':
    raise SystemExit(main())
