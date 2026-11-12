"""Run Postern as a service on macOS or Linux.

    .venv/bin/python -m postern.serve run       --config ~/.config/postern/config.json
    .venv/bin/python -m postern.serve install   [--check-only] [--user-agent] [--user NAME] [--force]
    .venv/bin/python -m postern.serve uninstall [--user-agent]
    .venv/bin/python -m postern.serve status    [--user-agent]
    .venv/bin/python -m postern.serve backend   [show | python | rust]

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
        # The job mounts only /usr on Linux: Poppler or FFmpeg from Linuxbrew, snap or /opt passes `which`
        # but fails every document and video inside the sandbox, so run each tool the child runs, where and as the
        # job will. A tool missing from PATH is its own row's failure, not the sandbox's.
        programs = [['/usr/bin/true']] + [[str(directory / name), flag] for directory, first, names, flag in (
            (derive.poppler_dir(), 'pdftoppm', ('pdftoppm', 'pdfinfo'), '-v'),
            (derive.media_dir(), 'ffmpeg', ('ffmpeg', 'ffprobe'), '-version'))
            if derive.LINUX and shutil.which(first) for name in names]
        for program in programs:
            try:
                command = derive.sandbox_command(source, scratch, program)
                # A host without the sandbox program gets this row, not a traceback out of install.
                result = subprocess.run(command, capture_output=True, text=True, cwd=scratch,
                                        env=derive.job_env(scratch), timeout=60)
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                return False, f'{derive.PROFILE} cannot be applied: {error}'
            if result.returncode:
                break
    detail = (result.stderr or result.stdout).strip()
    if result.returncode == 0 and derive.LINUX:
        tools = ', '.join(Path(program[0]).name for program in programs[1:])
        return True, f'{derive.PROFILE} compiles and applies' + (f'; {tools} run inside it' if tools else '')
    if result.returncode and program[0] != '/usr/bin/true':
        return False, f'{program[0]} cannot run inside the sandbox: {detail.splitlines()[0] if detail else "no output"}'
    if result.returncode and derive.LINUX and userns_restricted() and namespace_refused(detail):
        detail += ('; this host restricts unprivileged user namespaces, so bubblewrap needs the AppArmor rule '
                   f'{derive.PROFILE.with_name("postern-bwrap.apparmor")} (see {derive.PROFILE.with_name("README.md")})')
    return result.returncode == 0, detail or f'{derive.PROFILE} compiles and applies'


def namespace_refused(detail):
    """bwrap's own refusal to set up its namespaces, which is what the AppArmor rule lifts, as opposed to a
    missing or unreadable mount source or an old bwrap: the restriction's sysctl stays 1 after the rule is loaded."""
    return any(sign in detail for sign in ('setting up uid map', 'setting up gid map', 'RTM_NEWADDR',
                                           'Creating new namespace failed', 'No permissions to create new namespace'))


def userns_restricted():
    """Ubuntu 23.10 and later let only AppArmor-approved programs use the user namespace bwrap needs."""
    try:
        return Path('/proc/sys/kernel/apparmor_restrict_unprivileged_userns').read_text().strip() == '1'
    except OSError:
        return False


def bubblewrap_version():
    """(ok, detail) for /usr/bin/bwrap: derive.bwrap's --disable-userns needs bubblewrap 0.8.0 or later."""
    try:
        words = subprocess.run([derive.BWRAP, '--version'], capture_output=True, text=True, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        words = []
    if not words:
        return False, f'{derive.BWRAP} is missing — install bubblewrap; the converter cannot run'
    version = tuple(int(part) for part in re.findall(r'\d+', words[-1])[:3])
    if version < (0, 8, 0):
        return False, f'{derive.BWRAP} {words[-1]} — 0.8.0 or later is required'
    return True, f'{derive.BWRAP} {words[-1]}'


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


def front_proxy(caddy):
    """The preflight row for Caddy and the checked-in Caddyfile."""
    if not caddy:
        return 'front proxy', False, ('caddy not found (install caddy 2.8 or later from the Caddy project\'s Debian '
                                      'repository; Debian\'s own package is too old)' if derive.LINUX else
                                      'caddy not found (brew install caddy)')
    validated = subprocess.run([caddy, 'validate', '--config', str(CADDYFILE), '--adapter', 'caddyfile'],
                               capture_output=True, text=True, errors='replace')
    if validated.returncode == 0:
        return 'front proxy', True, f'{caddy} with {CADDYFILE}'
    # Caddy's last line names the cause; an old Caddy (Debian 13 ships 2.6) lacks the PROXY protocol listener.
    reason = (validated.stderr.strip().splitlines() or ['no reason given'])[-1][:300]
    return 'front proxy', False, f'{CADDYFILE} does not validate (Caddy 2.8 or later is required): {reason}'


def preflight(config_path):
    """Everything that has to be true before this host can serve. Returns (name, ok, detail) rows."""
    checks = []
    checks.append(('python', sys.version_info >= (3, 11), f'{platform.python_version()} at {sys.executable}'))

    for tool in ('pdftoppm', 'pdfinfo', 'ffmpeg', 'ffprobe'):
        found = shutil.which(tool)
        checks.append((tool, bool(found), f'{found} — {tool_version(found)}' if found else f'not found ({"apt install poppler-utils" if derive.LINUX else "brew install poppler"} ffmpeg)'))

    checks.append(front_proxy(shutil.which('caddy')))

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

    if derive.LINUX:
        checks.append(('bubblewrap', *bubblewrap_version()))
        # The watchdog's Rust guard finds and measures the serving API with these; without them it can only alert.
        for tool, package in (('/usr/bin/lsof', 'lsof'), ('/bin/ps', 'procps')):
            found = os.access(tool, os.X_OK)
            checks.append((Path(tool).name, found, tool if found else
                           f'{tool} is missing — install {package}; the watchdog cannot guard Rust without it'))
    else:
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
    if sys.platform == 'linux':
        from . import systemd
        if user_agent:
            print('FAIL --user-agent is macOS-only; Linux uses system services')
            return 1
        return systemd.install(config_path, user, home, check_only, force, label)
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
    if sys.platform == 'linux':
        from . import systemd
        if user_agent:
            print('FAIL --user-agent is macOS-only; Linux uses system services')
            return 1
        return systemd.uninstall(label)
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
    if sys.platform == 'linux':
        from . import systemd
        if user_agent:
            print('FAIL --user-agent is macOS-only; Linux uses system services')
            return 1
        return systemd.status(config_path, label)
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
    from . import progress_backend
    config = Config.load(config_path)
    if listening(config.port):
        print(f'FAIL 127.0.0.1:{config.port} is already listening. Stop that process, or change "port" '
              f'in {config_path}; Postern will not share a port it did not open.')
        return 1
    # The launchd environment wins; otherwise the owner's progress-backend.json; otherwise Python.
    try:
        backend, source = progress_backend.apply_selection(config_path)
    except Exception:
        # Replace the previous process's record, so the watchdog never mistakes it for this start
        # (an old "environment" record would read as a launchd override and stop the fallback).
        try:
            progress_backend.record_active(config.data_dir, None, 'failed')
        except OSError:
            pass
        raise
    progress_backend.record_active(config.data_dir, backend, source)
    print(f'postern: progress backend {backend} ({source})', file=sys.stderr, flush=True)
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(config), host='127.0.0.1', port=config.port, log_level='warning', access_log=False)
    return 0


def backend(config_path, choice):
    """Show or set the owner's progress backend selection. It takes effect at the next API start."""
    from . import progress_backend
    from .watchdog import GUARD
    if choice in ('python', 'rust') and os.geteuid() == 0:
        # A root-owned file is refused by the API, which runs as the owner: run this without sudo.
        print('FAIL run `postern.serve backend` as the account that owns the data, without sudo')
        return 2
    try:
        if choice in ('python', 'rust'):
            progress_backend.write_selection(config_path, choice, set_by='owner')
        selection = progress_backend.read_selection(config_path)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'FAIL {progress_backend.SELECTION}: {error}. Fix it, or remove it to serve Python.')
        return 2
    print(f'ok   {progress_backend.SELECTION}: {selection["backend"] if selection else "absent (Python)"}')
    try:
        values = json.loads(Path(config_path).expanduser().read_text())
        data_dir = Path(values['data_dir']).expanduser()
    except (OSError, ValueError, KeyError):
        data_dir = None
    records = {}
    for name in (progress_backend.ACTIVE, GUARD, progress_backend.CHECK):
        try:
            value = json.loads((data_dir / name).read_text())
        except (OSError, ValueError, TypeError):
            value = None
        records[name] = value if isinstance(value, dict) else None
    active = records[progress_backend.ACTIVE]
    if active is None:
        print('ok   serving process: no record yet')
    else:
        print(f'ok   serving process {active.get("pid")}: {active.get("backend")} from {active.get("source")}, '
              f'started {active.get("started_at")}')
    if choice == 'rust' and active and active.get('source') == 'environment' and process_exists(active.get('pid')):
        print(host_words(GUARD_STATES['override']))  # the launchd setting wins at the restart, so the file will not apply
    elif choice == 'rust':
        print('ok   watchdog guard: it checks this selection within 30 s of the API restart; run this again to confirm')
    else:
        print(guard_line(records[GUARD], config_path, selected=selection, active=active))
    line = check_line(records[progress_backend.CHECK], active) if choice not in ('python', 'rust') else None
    if line:  # the serving process's own checks; a new selection is judged after its restart
        print(line)
    if choice not in ('python', 'rust'):
        print(reads_line(data_dir))
    if choice in ('python', 'rust'):
        print(f'Restart the API to apply it. A {progress_backend.SETTING} setting in the {"systemd" if sys.platform == "linux" else "launchd"} job overrides this file.')
    return 0


def reads_line(data_dir):
    """Which history the progress reads use, from a read-only look at the database. Both backends read it."""
    import sqlite3
    from . import progress_store
    not_checked = 'WARN progress reads: not checked, because the configuration names no readable data directory'
    if data_dir is None:
        return not_checked
    path = data_dir / 'postern.sqlite3'
    try:  # a missing or unenterable directory is not "no database yet"
        if not data_dir.is_dir():
            return not_checked
        present = path.exists()
    except OSError:
        return not_checked
    if not present:
        return 'ok   progress reads: no database yet'
    try:
        db = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True, timeout=1)
        try:
            state = progress_store.diagnostics(db)
        finally:
            db.close()
    except sqlite3.Error as error:
        return f'FAIL progress reads: could not read the database ({type(error).__name__})'
    if state['source'] == 'progress_events':
        return 'ok   progress reads: the compact progress table'
    reasons = []
    if state['pending_rows'] is None:
        reasons.append('this database has no compact progress table yet; the API creates it at startup')
    elif not state['ready']:
        reasons.append('the compact table is still being built from the event history; the API builds it on idle '
                       'time, and its log shows "Progress read table rebuild paused" if a batch fails')
    elif state['pending_rows']:
        reasons.append(f'{state["pending_rows"]} row(s) are waiting to be classified for the compact table; the API '
                       'classifies them on idle time, and its log shows "Progress read table rebuild paused" if a '
                       'batch fails')
    if state['original_query_rows']:
        reasons.append(f'{state["original_query_rows"]} row(s) cannot use the compact table (for example a payload over '
                       '64 KiB or a nonpositive timestamp), so every history read scans the original events')
    return 'WARN progress reads: the full event history, because ' + '; and '.join(reasons) + '. Results are unchanged.'


def check_line(record, active):
    """The serving Rust process's own checks: its staff-report comparisons and native errors. Nothing
    is printed unless a live process serves Rust; a record from another process is not its record."""
    from .watchdog import NATIVE_ERRORS
    if not active or active.get('backend') != 'rust' or not process_exists(active.get('pid')):
        return None
    if not record or record.get('pid') != active.get('pid'):
        return ('FAIL in-process check: the process serving Rust has written no record '
                '(v0.25.0 or later writes one at startup); the watchdog cannot see its own checks')
    checks, mismatches = record.get('checks', 0), record.get('mismatches', 0)
    errors, last_error = record.get('native_errors', 0), record.get('last_error')
    if mismatches:
        return (f'FAIL in-process check: the staff report differed from a full Python replay {mismatches} '
                'time(s); the watchdog returns Rust to Python')
    if errors >= NATIVE_ERRORS:
        return (f'FAIL in-process check: {errors} native errors (last {last_error}); '
                'the watchdog returns Rust to Python')
    noted = f'{errors} native error(s), last {last_error}; the watchdog acts at {NATIVE_ERRORS}' if errors else 'no native errors'
    failed, failure = record.get('check_errors', 0), record.get('last_check_error')
    failed_at, checked_at = record.get('last_check_error_at'), record.get('last_check_at')
    # Judged by the latest attempt, not lifetime totals: one early success must not hide later failures.
    if failed and (not checked_at or str(failed_at or '') > str(checked_at)):
        return (f'FAIL in-process check: the last staff-report comparison could not run (the full Python replay '
                f'raised {failure} at {failed_at}; {failed} in all), so Rust drift is not being checked until one '
                f'runs; {noted}')
    compared = (f'{checks} staff-report comparison(s) with a full Python replay, all matching '
                f'(last {checked_at})' if checks else
                'no staff-report comparison yet (the first runs when the report is opened again)')
    if failed:
        compared += f', and {failed} earlier that could not run (last error {failure})'
    return f'ok   in-process check: {compared}; {noted}'


def process_exists(pid):
    """Whether a recorded process still exists. Unknown answers count as yes, so a status never reads ok by guess."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


GUARD_STATES = {
    'waiting': 'ok   watchdog guard: watching; Rust is selected but the API has not restarted with it yet',
    'override': 'FAIL watchdog guard: the launchd job sets POSTERN_PROGRESS_BACKEND, so this file is not in effect '
                'and the guard can only alert (runbook step 3 removes that setting)',
    'unverified': 'FAIL watchdog guard: it cannot verify the API process, so memory, CPU and parity checks are paused; '
                  'failed health checks still return Rust to Python',
    'error': 'FAIL watchdog guard: its checks raised an error (see the alert); failed health checks still return '
             'Rust to Python',
}


def host_words(text):
    """The guard messages name launchd and the macOS runbook step; on Linux name systemd and its runbook."""
    if sys.platform != 'linux':
        return text
    return text.replace('launchd', 'systemd').replace('runbook step 3', "the Linux runbook's Python fallback section")


def guard_line(record, config_path, now=None, selected=None, active=None):
    """Judged by the current selection: a guard is needed only while Rust is selected, and is armed only if
    a current watchdog checked this selection within the last two minutes and verified the process serving it.
    A launchd job that forces Rust is reported whatever the file says: the guard can only alert then."""
    from datetime import datetime, timezone
    from . import progress_backend
    from .watchdog import GUARD_VERSION
    live = active is not None and process_exists(active.get('pid'))  # a record left by an earlier process is history
    if live and active.get('backend') == 'rust' and active.get('source') == 'environment':
        return host_words(GUARD_STATES['override'])
    try:
        info = progress_backend.selection_path(config_path).lstat()
        current = [info.st_ino, info.st_mtime_ns]
        if selected is None:
            selected = progress_backend.read_selection(config_path)
    except (OSError, ValueError, RuntimeError):
        current = selected = None
    if not selected or selected.get('backend') != 'rust':
        if live and active.get('backend') == 'rust' and active.get('source') == progress_backend.SELECTION:
            return (f'FAIL watchdog guard: Python is selected, but the API (pid {active.get("pid")}) still serves Rust '
                    'unguarded; restart it to apply Python')
        return 'ok   watchdog guard: not needed while Python is selected'
    if record is None:
        return ('FAIL watchdog guard: no heartbeat for the Rust selection; restart the watchdog job '
                '(v0.24.1 or later) and check again')
    if record.get('guard_version') != GUARD_VERSION:
        return ('FAIL watchdog guard: the heartbeat is from an older watchdog that cannot report what it checked; '
                'restart the watchdog job (v0.24.1 or later)')
    try:
        age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(record.get('checked_at'))).total_seconds()
    except (TypeError, ValueError):
        age = None
    where = f'pid {record.get("pid")}, last checked {record.get("checked_at")}'
    if record.get('selection') != current:
        return f'FAIL watchdog guard: its last check was for another selection ({where}); it is not guarding this one'
    if age is None or age > 120:
        return f'FAIL watchdog guard: stale ({where}); check that a current watchdog is running'
    state = record.get('state')
    if state == 'guarding':
        return f'ok   watchdog guard: {where}, guarding this selection'
    return host_words(GUARD_STATES.get(state, f'FAIL watchdog guard: unknown state {state!r}')) + f' ({where})'


def main(argv=None):
    parser = argparse.ArgumentParser(prog='postern.serve', description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('install', 'uninstall', 'status', 'run', 'backend'):
        action = actions.add_parser(name)
        action.add_argument('--config', default=None, help='default ~/.config/postern/config.json for --user')
        if name == 'backend':
            action.add_argument('choice', nargs='?', default='show', choices=('show', 'python', 'rust'))
        if name not in ('run', 'backend'):
            action.add_argument('--label', default=LABEL, type=lambda value: service_labels(value)[0],
                                help='service label; existing hosts must pass their installed label')
            action.add_argument('--user', default=None, help='the account that owns the data (default $SUDO_USER or you)')
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
    if arguments.action == 'backend':
        return backend(config_path, arguments.choice)
    return run(config_path)


if __name__ == '__main__':
    raise SystemExit(main())
