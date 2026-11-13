"""LX-01: on Linux every conversion runs under bubblewrap with the same contract as macOS Seatbelt.

Tests marked BUBBLEWRAP run the real /usr/bin/bwrap and need a Linux host (CI's pytest-linux job). The others
run on both platforms, through whichever sandbox the host uses. The Seatbelt-only checks are in test_ops.py
and test_selfie_faces.py.
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import sysconfig
import textwrap
import time

import psutil
import pytest

from postern import derive, serve
from test_ops import write_config

BUBBLEWRAP = pytest.mark.skipif(not derive.LINUX, reason='runs the real bubblewrap, which needs a Linux host')
PROFILE = derive.ROOT / 'config' / 'sandbox' / 'derive.bwrap'
FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
# Every option derive.bwrap may use, with its operand count. A new kind of rule is a KeyError here.
ARITY = {'--unshare-all': 0, '--unshare-user': 0, '--disable-userns': 0, '--die-with-parent': 0, '--new-session': 0,
         '--cap-drop': 1, '--ro-bind': 2, '--ro-bind-try': 2, '--bind': 2, '--symlink': 2, '--proc': 1, '--dev': 1,
         '--chdir': 1, '--remount-ro': 1}


def rules(arguments):
    """bwrap arguments as (option, operands) pairs, read the way bwrap reads them."""
    pairs, index = [], 0
    while index < len(arguments):
        option = arguments[index]
        count = ARITY[option]
        pairs.append((option, tuple(arguments[index + 1:index + 1 + count])))
        index += 1 + count
    return pairs


def child(tmp_path, body):
    """A stand-in converter child, run under the real sandbox in place of derive_child.py."""
    script = tmp_path / 'child.py'
    script.write_text(textwrap.dedent(body))
    return script.resolve()


# Value: protects=the Linux converter reads and writes only its runtime and job paths, in a sandbox with no network
#   and no further namespaces; fails_when=derive.bwrap gains a writable or host-wide mount, loses a namespace or the
#   read-only remount, or a {NAME} stops being substituted; why_new=the Seatbelt pin in test_ops.py cannot see Linux
#   rules; seam=none (the BUBBLEWRAP tests below prove real jobs load this file)
def test_every_linux_job_runs_the_checked_in_bubblewrap_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(derive, 'PROFILE', PROFILE)
    source, output = tmp_path / 'upload.pdf', tmp_path / 'job'
    pairs = rules(derive.bubblewrap_args(source, output))
    text = '\n'.join(line.split('#', 1)[0] for line in PROFILE.read_text().splitlines())
    assert set(re.findall(r'\{([A-Z_]+)\}', text)) == {
        'SOURCE', 'CHILD', 'FACE_MODEL', 'PYTHON', 'PYVENV', 'STDLIB', 'PURELIB', 'PLATLIB', 'BASE_PREFIX', 'OUTPUT'}
    assert {option for option, operands in pairs if not operands} == {
        '--unshare-all', '--unshare-user', '--disable-userns', '--die-with-parent', '--new-session'}
    assert ('--cap-drop', ('ALL',)) in pairs
    # The job directory is the one writable mount, and the in-memory root and /dev end read-only.
    assert [operands for option, operands in pairs if option == '--bind'] == [(str(output), str(output))]
    assert pairs[-2:] == [('--remount-ro', ('/',)), ('--remount-ro', ('/dev',))]
    mounts = [operands for option, operands in pairs if option in ('--ro-bind', '--ro-bind-try', '--bind')]
    assert all(origin == target for origin, target in mounts), 'each path is mounted where the child looks for it'
    runtime = {sys.executable, sysconfig.get_path('stdlib'), sysconfig.get_path('purelib'),
               sysconfig.get_path('platlib'), sys.base_prefix}
    assert {operands[0] for option, operands in pairs if option == '--ro-bind'} == {
        '/usr', str(source), str(derive.CHILD), str(derive.FACE_MODEL), *map(os.path.abspath, runtime)}
    # Optional mounts: present on Debian and Ubuntu; pyvenv.cfg exists only when the server runs from a venv.
    assert {operands[0] for option, operands in pairs if option == '--ro-bind-try'} == {
        '/etc/ld.so.cache', '/etc/alternatives', '/etc/fonts', os.path.abspath(Path(sys.prefix) / 'pyvenv.cfg')}
    assert {operands for option, operands in pairs if option == '--symlink'} == {
        ('usr/bin', '/bin'), ('usr/sbin', '/sbin'), ('usr/lib', '/lib'), ('usr/lib64', '/lib64')}
    assert ('--proc', ('/proc',)) in pairs and ('--dev', ('/dev',)) in pairs and ('--chdir', (str(output),)) in pairs


# Value: protects=a derive.bwrap naming a parameter derive.py does not supply fails preflight's sandbox profile row and
#   every job as a host fault; fails_when=profile_compiles or Deriver.run stops catching the profile's ValueError
#   (install crashes, a selfie capture ends in a 500); why_new=only bubblewrap_args's own refusal was tested;
#   seam=none (LINUX selects the Linux branch on macOS; /usr/bin/true stands in for bwrap, which never starts);
#   no_claim=a brace token that is not a whole {NAME}, or the row's exact wording
def test_a_bubblewrap_profile_naming_an_unknown_parameter_is_refused(config, tmp_path, monkeypatch):
    broken = tmp_path / 'derive.bwrap'
    broken.write_text(PROFILE.read_text() + '\n--ro-bind {HOME} {HOME}\n')
    monkeypatch.setattr(derive, 'PROFILE', broken)
    with pytest.raises(ValueError, match='unknown parameter'):
        derive.bubblewrap_args(tmp_path / 'upload.pdf', tmp_path / 'job')
    monkeypatch.setattr(derive, 'LINUX', True)
    monkeypatch.setattr(derive, 'BWRAP', '/usr/bin/true')
    compiles, detail = serve.profile_compiles()
    assert not compiles and 'unknown parameter {HOME}' in detail
    config.validate()
    with pytest.raises(derive.DeriveFault, match='could not run') as caught:
        derive.Deriver(config).run(FIXTURES / 'photo.jpg', config.data_dir / 'derived' / 'unknown', 'photo')
    assert isinstance(caught.value.__cause__, ValueError)


@BUBBLEWRAP
def test_the_job_command_is_bwrap_then_the_converter(tmp_path):
    source, output = tmp_path / 'upload.pdf', tmp_path / 'job'
    command = derive.job_command(source, output, 'pdf', 1, 1000, 1000, '/usr/bin')
    arguments = derive.bubblewrap_args(source, output)
    assert command[:len(arguments) + 2] == [derive.BWRAP, *arguments, '--']
    assert command[len(arguments) + 2:len(arguments) + 5] == [sys.executable, '-I', str(derive.CHILD)]
    assert command[-1] == str(derive.media_dir()) == str(Path(shutil.which('ffmpeg')).parent)


# Value: protects=a converter running attacker-controlled parser code cannot read secrets, write outside its job,
#   reach the network or make a new user namespace; fails_when=a mount, namespace or
#   remount in derive.bwrap is dropped; why_new=Linux had no sandbox; seam=none
@BUBBLEWRAP
def test_the_bubblewrap_profile_applies_and_denies_secrets_writes_network_and_namespaces(tmp_path):
    compiles, detail = serve.profile_compiles()
    assert compiles, detail
    secrets = tmp_path / '.config' / 'postern'
    secrets.mkdir(parents=True)
    (secrets / 'config.json').write_text('{"host_credentials": "secret"}')
    source, output = tmp_path / 'upload.pdf', tmp_path / 'job'
    source.write_bytes(b'%PDF-1.4')
    output.mkdir()

    def run(*program):
        return subprocess.run(derive.sandbox_command(source, output, list(program)), capture_output=True, text=True,
                              timeout=30)

    read = run('/bin/cat', str(secrets / 'config.json'))
    assert read.returncode != 0 and 'secret' not in read.stdout
    assert run('/bin/cat', '/etc/passwd').returncode != 0
    for target in (tmp_path / 'escaped', Path('/postern-escaped'), Path('/dev/shm/postern-escaped')):
        write = run('/usr/bin/touch', str(target))
        assert write.returncode != 0 and 'Read-only file system' in write.stderr and not target.exists()
    kept = run('/usr/bin/touch', str(output / 'kept'))
    assert kept.returncode == 0 and (output / 'kept').is_file()
    assert run('/bin/sh', '-c', 'echo discarded > /dev/null').returncode == 0
    network = run(sys.executable, '-I', '-c', 'import socket; socket.create_connection(("1.1.1.1", 80), timeout=3)')
    assert network.returncode != 0 and 'Network is unreachable' in network.stderr
    assert run('/usr/bin/unshare', '--user', '/usr/bin/true').returncode != 0
    assert run('/bin/sh', '-c', 'ls /proc | grep -c "^[0-9]"').stdout.strip() in {'3', '4', '5'}


# Value: protects=a host whose bubblewrap cannot build the sandbox gives the upload back as a server fault, never as a
#   bad upload; fails_when=bwrap's own exit 1 is read as the converter's; why_new=sandbox-exec signals this with
#   exit 65, bwrap does not; seam=none
@BUBBLEWRAP
def test_a_bubblewrap_that_cannot_build_the_sandbox_is_a_host_fault(config, tmp_path, monkeypatch):
    broken = tmp_path / 'derive.bwrap'
    broken.write_text(PROFILE.read_text().replace('--remount-ro /\n', '--ro-bind /postern-missing /postern-missing\n--remount-ro /\n'))
    monkeypatch.setattr(derive, 'PROFILE', broken)
    config.validate()
    with pytest.raises(derive.DeriveFault, match='could not run'):
        derive.Deriver(config).run(FIXTURES / 'photo.jpg', config.data_dir / 'derived' / 'broken', 'photo')


# Value: protects=on a Linux host without /usr/bin/bwrap every job is a host fault naming bubblewrap, before any upload
#   is parsed; fails_when=the bwrap check is dropped (Popen's FileNotFoundError escapes Deriver.run) or raises a plain
#   DeriveError; why_new=CI always installs bubblewrap; seam=none (LINUX selects the Linux branch on macOS);
#   no_claim=a bwrap that is present but cannot build the sandbox (the test above)
def test_a_linux_host_without_bubblewrap_is_a_host_fault(config, tmp_path, monkeypatch):
    monkeypatch.setattr(derive, 'LINUX', True)
    monkeypatch.setattr(derive, 'BWRAP', str(tmp_path / 'missing' / 'bwrap'))
    config.validate()
    with pytest.raises(derive.DeriveFault, match='requires bubblewrap'):
        derive.Deriver(config).run(FIXTURES / 'photo.jpg', config.data_dir / 'derived' / 'no-bwrap', 'photo')


# Value: protects=an operator on a Linux host without bubblewrap gets the preflight table, with its bubblewrap and
#   sandbox profile rows failed, not a traceback; fails_when=profile_compiles lets the missing bwrap's FileNotFoundError
#   escape preflight; why_new=the bubblewrap row was tested alone, with subprocess.run mocked; seam=none;
#   no_claim=the sandbox profile row's wording or the other rows
def test_preflight_on_a_linux_host_without_bubblewrap_reports_it_instead_of_crashing(tmp_path, monkeypatch):
    missing = str(tmp_path / 'missing' / 'bwrap')
    monkeypatch.setattr(derive, 'LINUX', True)
    monkeypatch.setattr(derive, 'PROFILE', PROFILE)
    monkeypatch.setattr(derive, 'BWRAP', missing)
    checks = {name: (ok, detail) for name, ok, detail in serve.preflight(write_config(tmp_path))}
    assert checks['bubblewrap'] == (False, f'{missing} is missing — install bubblewrap; the converter cannot run')
    assert checks['sandbox profile'][0] is False


@pytest.mark.parametrize('linux, returncode, stderr, expected', [
    (True, 0, b'', 0),
    (True, 1, b'Traceback (most recent call last):', 1),
    (True, 1, b"bwrap: Can't find source path /postern-missing: No such file or directory", None),
    (True, 128 + signal.SIGXCPU, b'', -signal.SIGXCPU),
    (True, 128 + signal.SIGKILL, b'', -signal.SIGKILL),
    (True, 60, b'', 60),
    (True, 65, b'', 65),
    (True, -signal.SIGKILL, b'', -signal.SIGKILL),
    (False, 1, b'bwrap: not this platform', 1),
    (False, 65, b'sandbox-exec: unable to read profile', 65),
    (False, -signal.SIGXCPU, b'', -signal.SIGXCPU),
])
def test_job_exits_are_read_back_as_the_child_reported_them(monkeypatch, linux, returncode, stderr, expected):
    monkeypatch.setattr(derive, 'LINUX', linux)
    assert derive.outcome(returncode, stderr) == expected


# Value: protects=a converter killed by a signal (a CPU or file-size limit, a decoder crash) still counts against the
#   upload on both platforms; fails_when=bwrap's 128 + N is read as sandbox-exec's own refusal (>= 64) and refunded;
#   why_new=only sandbox-exec passed the child's signal through; seam=none
def test_a_job_killed_by_a_signal_counts_against_the_upload(config, tmp_path, monkeypatch):
    monkeypatch.setattr(derive, 'CHILD', child(tmp_path, 'import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n'))
    config.validate()
    with pytest.raises(derive.DeriveError) as caught:
        derive.Deriver(config).run(FIXTURES / 'photo.jpg', config.data_dir / 'derived' / 'killed', 'photo')
    assert type(caught.value) is derive.DeriveError


# Value: protects=the memory bound covers the whole job, including the tools a converter starts, and stops all of
#   them; fails_when=only the first process is measured (bwrap gives the job a new session), a grandchild survives
#   the kill, or /proc stat is split on whitespace before the name; why_new=no test covered the memory bound;
#   seam=none; no_claim=a process that exits while /proc is read
def test_a_job_over_the_memory_limit_is_stopped_with_its_whole_process_tree(config, tmp_path, monkeypatch):
    token = f'postern-greedy-{os.getpid()}'
    monkeypatch.setattr(derive, 'CHILD', child(tmp_path, f'''
        import subprocess, sys, time
        # Like pdftoppm or ffmpeg, a process the child starts holds the memory. On Linux it renames itself with spaces
        # and a ")", which a process name ("tmux: server") may hold: the bound parses the name in /proc/<pid>/stat.
        subprocess.Popen([sys.executable, '-I', '-c', 'import contextlib, time\\n'
                          'with contextlib.suppress(OSError), open("/proc/self/comm", "w") as name: name.write("greedy) 1 (2 3")\\n'
                          'block = bytearray(256 << 20); block[::4096] = bytes(len(block) // 4096); time.sleep(60)  # {token}'])
        time.sleep(60)
    '''))
    config.derive_memory_limit = 128 * 1024 * 1024
    config.validate()
    started = time.monotonic()
    with pytest.raises(derive.DeriveError, match='memory limit'):
        derive.Deriver(config).run(FIXTURES / 'photo.jpg', config.data_dir / 'derived' / 'greedy', 'photo')
    # gstack-timing-ok: an outer ceiling far above the 0.5 s poll; the child sleeps 60 s and derive_timeout is 90 s,
    # so only a bound that never fires reaches it ("memory limit" above already names which limit stopped the job).
    assert time.monotonic() - started < 30

    def survivors():
        found = []
        for process in psutil.process_iter(['cmdline']):
            if any(token in part for part in process.info['cmdline'] or []):
                found.append(process)
        return found

    deadline = time.monotonic() + 10
    while survivors() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert survivors() == []


@BUBBLEWRAP
def test_linux_preflight_checks_bubblewrap_and_the_real_sandbox(tmp_path):
    checks = {name: (ok, detail) for name, ok, detail in serve.preflight(write_config(tmp_path))}
    assert set(checks) == {'python', 'pdftoppm', 'pdfinfo', 'ffmpeg', 'ffprobe', 'front proxy', 'pillow-heif', 'quiz bank',
                           'final exam and subtitles', 'bubblewrap', 'selfie face detector', 'config file',
                           'data directory', 'sandbox profile', 'organization settings', 'lsof', 'ps'}
    # Caddy and lsof are host prerequisites, not test-runner ones; where one is absent its row must say so.
    absent = {'front proxy': not shutil.which('caddy'), 'lsof': not os.access('/usr/bin/lsof', os.X_OK)}
    assert all(checks[name][0] for name in checks if not absent.get(name)), checks
    assert checks['bubblewrap'][1].startswith(derive.BWRAP + ' ')
    assert checks['lsof'] == ((True, '/usr/bin/lsof') if not absent['lsof'] else
                              (False, '/usr/bin/lsof is missing — install lsof; the watchdog cannot guard Rust without it'))


@pytest.mark.parametrize('output, ok, detail', [
    ('bubblewrap 0.11.0\n', True, '/usr/bin/bwrap 0.11.0'),
    ('bubblewrap 0.8.0\n', True, '/usr/bin/bwrap 0.8.0'),
    ('bubblewrap 0.6.1\n', False, '/usr/bin/bwrap 0.6.1 — 0.8.0 or later is required'),
    (None, False, '/usr/bin/bwrap is missing — install bubblewrap; the converter cannot run'),
])
def test_preflight_requires_a_bubblewrap_that_knows_disable_userns(monkeypatch, output, ok, detail):
    def run(*args, **kwargs):
        if output is None:
            raise FileNotFoundError(derive.BWRAP)
        return subprocess.CompletedProcess(args, 0, output, '')
    monkeypatch.setattr(serve.subprocess, 'run', run)
    assert serve.bubblewrap_version() == (ok, detail)


# Value: protects=an operator on Ubuntu 23.10+ is told which file lifts the user-namespace restriction when that is
#   what stops the sandbox, and only then; fails_when=the hint is missing for bwrap's namespace refusal, or shown for a
#   missing mount or on a host with no restriction (the sysctl stays 1 after the rule loads); why_new=the restriction
#   arrived with Ubuntu 23.10; seam=userns_restricted; no_claim=userns_restricted's reading of the real sysctl, or the
#   exact wording each bwrap version prints (the real-refusal test below checks it where the restriction is on)
@pytest.mark.parametrize('restricted, message, hinted', [
    (True, 'bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted', True),
    (True, 'bwrap: setting up uid map: Permission denied', True),
    (True, "bwrap: Can't find source path /postern-missing: No such file or directory", False),
    (True, "bwrap: Can't bind mount /oldroot/srv/model on /newroot/srv/model: Permission denied", False),
    (False, 'bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted', False),
])
def test_the_apparmor_hint_names_the_rule_only_when_the_namespace_is_refused(monkeypatch, restricted, message, hinted):
    monkeypatch.setattr(derive, 'LINUX', True)
    monkeypatch.setattr(derive, 'PROFILE', PROFILE)
    monkeypatch.setattr(serve, 'userns_restricted', lambda: restricted)
    monkeypatch.setattr(serve.subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, '', message))
    compiles, detail = serve.profile_compiles()
    assert not compiles and detail.startswith(message)
    assert ('postern-bwrap.apparmor' in detail) is hinted
    assert (derive.ROOT / 'config' / 'sandbox' / 'postern-bwrap.apparmor').is_file()


# Value: protects=a Linux host whose Poppler or FFmpeg the job cannot reach (Linuxbrew, snap, /opt) fails the preflight
#   instead of failing every document and video as a bad upload; fails_when=the preflight leaves out a tool the child
#   runs (pdftoppm, ffmpeg) or runs only /usr/bin/true in the sandbox; why_new=the rows above only check `which`;
#   seam=none; no_claim=the second tool of each pair (pdfinfo, ffprobe), which the same loop probes next
@BUBBLEWRAP
@pytest.mark.parametrize('finder, names', [('poppler_dir', ('pdftoppm', 'pdfinfo')), ('media_dir', ('ffmpeg', 'ffprobe'))])
def test_preflight_fails_a_conversion_tool_the_sandbox_cannot_reach(tmp_path, monkeypatch, finder, names):
    outside = tmp_path / 'linuxbrew' / 'bin'
    outside.mkdir(parents=True)
    for name in names:
        (outside / name).symlink_to(shutil.which(name))
    monkeypatch.setattr(derive, finder, lambda: outside)
    compiles, detail = serve.profile_compiles()
    assert not compiles and detail.startswith(f'{outside / names[0]} cannot run inside the sandbox')
    monkeypatch.undo()
    compiles, detail = serve.profile_compiles()
    assert compiles and detail.endswith('pdftoppm, pdfinfo, ffmpeg, ffprobe run inside it')


# Value: protects=the preflight runs each converter tool with the job's own environment and directory, and leaves a tool
#   missing from PATH to its own row; fails_when=the probes inherit the operator's LD_LIBRARY_PATH (a library only it
#   finds passes preflight, then fails every job) or a missing ffmpeg is blamed on the sandbox; why_new=the probes
#   were new in this change; seam=none (LINUX selects the Linux branch on macOS); no_claim=the probes' real exit codes
def test_preflight_probes_tools_with_the_job_environment_and_skips_missing_ones(monkeypatch):
    monkeypatch.setattr(derive, 'LINUX', True)
    monkeypatch.setattr(derive, 'PROFILE', PROFILE)
    monkeypatch.setenv('LD_LIBRARY_PATH', '/opt/custom/lib')
    real_which = shutil.which
    monkeypatch.setattr(serve.shutil, 'which', lambda name, *args, **kwargs: None if name == 'ffmpeg' else real_which(name))
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(serve.subprocess, 'run', run)
    compiles, detail = serve.profile_compiles()
    programs = [Path(command[command.index('--') + 1]).name for command, _ in calls]
    assert compiles and programs == ['true', 'pdftoppm', 'pdfinfo'] and detail.endswith('; pdftoppm, pdfinfo run inside it')
    for command, kwargs in calls:
        assert kwargs['env'] == derive.job_env(kwargs['cwd']) and 'LD_LIBRARY_PATH' not in kwargs['env']
        assert kwargs['timeout'] and command[command.index('--bind') + 1] == str(kwargs['cwd'])


# Value: protects=on a host that restricts user namespaces, bwrap's real refusal is recognized and names the AppArmor rule;
#   fails_when=namespace_refused no longer matches the message this bwrap prints; why_new=the table above uses
#   hand-written messages; seam=none (a copy of bwrap is not covered by the rule, which attaches to /usr/bin/bwrap);
#   no_claim=hosts without the restriction (skipped there)
@BUBBLEWRAP
@pytest.mark.skipif(not serve.userns_restricted(), reason='needs the Ubuntu user-namespace restriction')
def test_a_real_namespace_refusal_names_the_apparmor_rule(tmp_path, monkeypatch):
    copy = tmp_path / 'bwrap'
    shutil.copy2(derive.BWRAP, copy)
    monkeypatch.setattr(derive, 'BWRAP', str(copy))
    compiles, detail = serve.profile_compiles()
    assert not compiles and 'postern-bwrap.apparmor' in detail, detail


@pytest.mark.parametrize('kind', ['symlink', 'fifo'])
def test_nonregular_manifest_is_a_converter_fault_before_reading(config, tmp_path, monkeypatch, kind):
    # The fake child supplies real filesystem objects. Never let a regression block this test on a FIFO read.
    config.validate()
    original = Path.read_text
    def read_text(path, *args, **kwargs):
        if path.name == 'manifest.json':
            pytest.fail('nonregular manifest reached a path-based read')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read_text)
    def convert(*args, cwd, **kwargs):
        path = Path(cwd) / 'manifest.json'
        if kind == 'symlink':
            target = Path(cwd) / 'target.json'
            target.write_text('{"pages":1,"bytes":1}')
            path.symlink_to(target)
        else:
            os.mkfifo(path)
        class Finished:
            returncode = 0
            def communicate(self, **kwargs):
                return b'', b''
        return Finished()
    monkeypatch.setattr(derive.subprocess, 'Popen', convert)
    monkeypatch.setattr(derive.json, 'load', lambda *args: pytest.fail('nonregular manifest reached JSON parsing'))
    destination = config.data_dir / 'derived' / 'invalid-manifest'
    with pytest.raises(derive.DeriveFault, match='Unexpected converter output'):
        derive.Deriver(config).run(FIXTURES / 'photo.jpg', destination, 'photo')
    assert not destination.exists()


# Value: protects=converter output swapped for a link after the output check, or hard-linked to a file outside the
#   job, never reaches the cache; fails_when=the move follows or keeps a link, or a hard link passes as a regular file;
#   why_new=the manifest test covers manifest.json only; seam=none;
#   no_claim=a real process outliving the sandboxed job (the swap is simulated at the rename; unit only)
@pytest.mark.parametrize('kind', ['link swapped in before the move', 'hard link'])
def test_converter_output_that_links_outside_never_reaches_the_cache(config, tmp_path, monkeypatch, kind):
    config.validate()
    secret = tmp_path / 'secret.json'
    secret.write_text('{"token": "synthetic"}')
    def convert(*args, cwd, **kwargs):
        (Path(cwd) / 'manifest.json').write_text('{"pages":1,"bytes":1}')
        page = Path(cwd) / 'page-1.jpg'
        if kind == 'hard link':
            os.link(secret, page)
        else:
            page.write_bytes(b'x')
        class Finished:
            returncode = 0
            def communicate(self, **kwargs):
                return b'', b''
        return Finished()
    monkeypatch.setattr(derive.subprocess, 'Popen', convert)
    rename = os.rename
    def swap_then_rename(source, target):
        if kind != 'hard link' and Path(source).name == 'page-1.jpg':
            Path(source).unlink()  # what a converter process that outlived the job could do after the check
            Path(source).symlink_to(secret)
        return rename(source, target)
    monkeypatch.setattr(derive.os, 'rename', swap_then_rename)
    destination = config.data_dir / 'derived' / 'linked-output'
    with pytest.raises(derive.DeriveFault, match='Unexpected converter output'):
        derive.Deriver(config).run(FIXTURES / 'photo.jpg', destination, 'photo')
    assert not destination.exists() and secret.read_text() == '{"token": "synthetic"}'


# Value: protects=a cached derivative that is a link reads as missing and is never followed; fails_when=read_path
#   checks is_file() alone, which follows links; why_new=no test reads through a link; seam=none;
#   no_claim=how a link could reach the cache (the run check above stops that; unit only)
def test_cached_derivative_link_is_never_followed(config, tmp_path):
    config.validate()
    secret = tmp_path / 'secret.txt'
    secret.write_text('synthetic')
    directory = config.data_dir / 'derived' / 'item'
    directory.mkdir(parents=True)
    (directory / 'video.mp4').symlink_to(secret)
    with pytest.raises(FileNotFoundError):
        derive.Deriver(config).read_path('item', 'video.mp4', lambda path: pytest.fail('a link reached the reader'))


@pytest.mark.parametrize('tool', ['true', 'pdftoppm', 'pdfinfo', 'ffmpeg', 'ffprobe'])
def test_preflight_timeout_names_the_probe_tool(monkeypatch, tool):
    monkeypatch.setattr(derive, 'LINUX', True)
    monkeypatch.setattr(derive, 'poppler_dir', lambda: Path('/usr/bin'))
    monkeypatch.setattr(derive, 'media_dir', lambda: Path('/usr/bin'))
    monkeypatch.setattr(serve.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(derive, 'sandbox_command', lambda source, output, program: ['/synthetic/bwrap', '--', *program])
    def run(command, **kwargs):
        if Path(command[2]).name == tool:
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(serve.subprocess, 'run', run)
    assert serve.profile_compiles() == (False, f'/usr/bin/{tool} timed out after 60s inside the sandbox')
