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


def test_a_bubblewrap_profile_naming_an_unknown_parameter_is_refused(tmp_path, monkeypatch):
    broken = tmp_path / 'derive.bwrap'
    broken.write_text(PROFILE.read_text() + '\n--ro-bind {HOME} {HOME}\n')
    monkeypatch.setattr(derive, 'PROFILE', broken)
    with pytest.raises(ValueError, match='unknown parameter'):
        derive.bubblewrap_args(tmp_path / 'upload.pdf', tmp_path / 'job')


@BUBBLEWRAP
def test_the_job_command_is_bwrap_then_the_converter(tmp_path):
    source, output = tmp_path / 'upload.pdf', tmp_path / 'job'
    command = derive.job_command(source, output, 'pdf', 1, 1000, 1000, '/usr/bin')
    arguments = derive.bubblewrap_args(source, output)
    assert command[:len(arguments) + 2] == [derive.BWRAP, *arguments, '--']
    assert command[len(arguments) + 2:len(arguments) + 5] == [sys.executable, '-I', str(derive.CHILD)]
    assert command[-1] == str(derive.media_dir()) == str(Path(shutil.which('ffmpeg')).parent)


# Value: protects=a converter running attacker-controlled parser code cannot read the host's secrets, store files
#   outside its job, reach the network or make itself a new user namespace; fails_when=a mount, namespace or
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
#   them; fails_when=only the first process is measured (bwrap moves the job to a new session, so its process group
#   is no longer the job) or a grandchild survives the kill; why_new=no test covered the memory bound; seam=none
def test_a_job_over_the_memory_limit_is_stopped_with_its_whole_process_tree(config, tmp_path, monkeypatch):
    token = f'postern-greedy-{os.getpid()}'
    monkeypatch.setattr(derive, 'CHILD', child(tmp_path, f'''
        import subprocess, sys, time
        # Like pdftoppm or ffmpeg, a process the child starts holds the memory.
        subprocess.Popen([sys.executable, '-I', '-c', 'import time; block = bytearray(256 << 20); '
                          'block[::4096] = bytes(len(block) // 4096); time.sleep(60)  # {token}'])
        time.sleep(60)
    '''))
    config.derive_memory_limit = 128 * 1024 * 1024
    config.validate()
    started = time.monotonic()
    with pytest.raises(derive.DeriveError, match='memory limit'):
        derive.Deriver(config).run(FIXTURES / 'photo.jpg', config.data_dir / 'derived' / 'greedy', 'photo')
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
                           'data directory', 'sandbox profile', 'organization settings'}
    required = {name for name in checks if name != 'front proxy' or shutil.which('caddy')}
    assert all(checks[name][0] for name in required), checks
    assert checks['bubblewrap'][1].startswith(derive.BWRAP + ' ')


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


# Value: protects=an operator on Ubuntu 23.10+ is told which file lifts the user-namespace restriction that stops
#   every conversion; fails_when=the preflight reports only bwrap's own mount error; why_new=the restriction
#   arrived with Ubuntu 23.10; seam=userns_restricted (CI may or may not run with the restriction)
@BUBBLEWRAP
def test_a_sandbox_stopped_by_the_ubuntu_namespace_restriction_names_the_apparmor_rule(tmp_path, monkeypatch):
    broken = tmp_path / 'derive.bwrap'
    broken.write_text(PROFILE.read_text().replace('--remount-ro /\n', '--ro-bind /postern-missing /postern-missing\n--remount-ro /\n'))
    monkeypatch.setattr(derive, 'PROFILE', broken)
    monkeypatch.setattr(serve, 'userns_restricted', lambda: True)
    compiles, detail = serve.profile_compiles()
    assert not compiles and 'postern-missing' in detail
    assert 'postern-bwrap.apparmor' in detail and (derive.ROOT / 'config' / 'sandbox' / 'postern-bwrap.apparmor').is_file()
