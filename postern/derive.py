from __future__ import annotations
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from .derive_child import FACE_MODEL, FACE_FAILURES

ROOT = Path(__file__).resolve().parents[1]
LINUX = platform.system() == 'Linux'
# The one profile every conversion job runs under, Seatbelt on macOS and bubblewrap on Linux (LX-01);
# preflight compiles and exercises this same file.
PROFILE = ROOT / 'config' / 'sandbox' / ('derive.bwrap' if LINUX else 'derive.sb')
BWRAP = '/usr/bin/bwrap'
CHILD = Path(__file__).with_name('derive_child.py').resolve()
# A selfie becomes a 256px square, so a small pixel budget and output are enough, whatever the document limits.
SELFIE_MAX_PIXELS = 4_000_000
SELFIE_OUTPUT_LIMIT = 512 * 1024


class DeriveError(Exception):
    pass


class DeriveFault(DeriveError):
    """The host or the converter, not the upload: worth retrying later, never a retake."""


class DeriveCapacity(DeriveFault):
    """The host is short of room."""


def poppler_dir():
    return Path(shutil.which('pdftoppm') or '/missing/pdftoppm').parent


def media_dir():
    """Where ffprobe and ffmpeg live: Homebrew's on macOS, as before; wherever PATH finds them on Linux."""
    return Path(shutil.which('ffmpeg') or '/missing/ffmpeg').parent if LINUX else Path('/opt/homebrew/bin')


class SelfieFaceError(DeriveError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def sandbox_profile_args(source, output):
    """The `-f`/`-D` arguments that apply config/sandbox/derive.sb to one conversion job.

    Every value is resolved: Seatbelt matches real paths, so an unresolved /var/folders/... would compile
    and then deny every write inside the job.
    """
    params = {'SOURCE': source, 'CHILD': CHILD, 'FACE_MODEL': FACE_MODEL, 'PYTHON': sys.executable,
              'PYVENV': Path(sys.prefix) / 'pyvenv.cfg', 'STDLIB': sysconfig.get_path('stdlib'),
              'PURELIB': sysconfig.get_path('purelib'), 'PLATLIB': sysconfig.get_path('platlib'),
              'BASE_PREFIX': sys.base_prefix, 'OUTPUT': output}
    arguments = ['-f', str(PROFILE)]
    for name, path in params.items():
        arguments += ['-D', f'{name}={Path(path).resolve()}']
    return arguments


def bubblewrap_args(source, output):
    """derive.bwrap with its {NAME} operands filled in, as bwrap arguments for one conversion job.

    Paths stay as the job uses them, not resolved: bwrap binds each path where the child will look for it
    (a venv's bin/python is a symlink outside the sandbox and a plain file inside it).
    """
    params = {'SOURCE': source, 'CHILD': CHILD, 'FACE_MODEL': FACE_MODEL, 'PYTHON': sys.executable,
              'PYVENV': Path(sys.prefix) / 'pyvenv.cfg', 'STDLIB': sysconfig.get_path('stdlib'),
              'PURELIB': sysconfig.get_path('purelib'), 'PLATLIB': sysconfig.get_path('platlib'),
              'BASE_PREFIX': sys.base_prefix, 'OUTPUT': output}
    arguments = []
    for line in PROFILE.read_text().splitlines():
        for token in line.split('#', 1)[0].split():
            if '{' in token or '}' in token:
                name = token[1:-1] if token.startswith('{') and token.endswith('}') else None
                if name not in params:
                    raise ValueError(f'{PROFILE.name}: unknown parameter {token}')
                token = os.path.abspath(params[name])
            arguments.append(token)
    return arguments


def sandbox_command(source, output, program):
    """The one place a job's sandbox is built: the same profile for conversions and preflight's checks."""
    if LINUX:
        return [BWRAP, *bubblewrap_args(source, output), '--', *program]
    return ['/usr/bin/sandbox-exec', *sandbox_profile_args(source, output), *program]


def job_command(source, output, kind, max_pages, max_pixels, max_bytes, poppler, media=None):
    """The exact sandboxed command for one conversion job."""
    return sandbox_command(source, output, [sys.executable, '-I', str(CHILD), str(source), str(output), kind,
                                            str(max_pages), str(max_pixels), str(max_bytes), str(poppler),
                                            str(media or media_dir())])


def outcome(returncode, stderr):
    """The job's exit as the child reported it, or None when the sandbox itself could not start it.

    sandbox-exec execs the child, so its status is the child's own. bwrap stays the parent: it reports a
    job killed by signal N as 128 + N, and its own setup failures as 1 with a "bwrap: " message.
    """
    if not LINUX or returncode <= 0:
        return returncode
    if returncode == 1 and stderr.startswith(b'bwrap: '):
        return None
    return 128 - returncode if returncode > 128 else returncode


def tree_resident(root):
    """Resident bytes of a process and all its descendants, from /proc (Linux).

    bwrap starts the job in a new session and PID namespace, so its process group is not the job's.
    """
    children, rss = {}, {}
    for entry in os.scandir('/proc'):
        if not entry.name.isdigit():
            continue
        try:
            with open(f'/proc/{entry.name}/stat', 'rb') as handle:
                fields = handle.read().rsplit(b')', 1)[1].split()
        except OSError:
            continue  # exited while the table was read
        pid = int(entry.name)
        children.setdefault(int(fields[1]), []).append(pid)
        rss[pid] = int(fields[21])
    total, pending = 0, [root]
    while pending:
        pid = pending.pop()
        total += rss.get(pid, 0)
        pending += children.get(pid, [])
    return total * os.sysconf('SC_PAGE_SIZE')


def job_env(output):
    return {'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'TMPDIR': str(output), 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'}


class Deriver:
    def __init__(self, config):
        self.config = config
        self.cache_lock = threading.RLock()
        self.leases = {}
        self.poppler = poppler_dir()

    def run(self, source, destination, kind):
        """One conversion job. Callers bound how many run at once (Service.convert); this method does not."""
        if LINUX:
            if not os.access(BWRAP, os.X_OK):
                raise DeriveFault('This converter requires bubblewrap at /usr/bin/bwrap')
        elif platform.system() != 'Darwin' or not shutil.which('sandbox-exec'):
            raise DeriveFault('This converter requires the configured macOS sandbox')
        source, destination = Path(source).resolve(), Path(destination).resolve()
        output_limit = (SELFIE_OUTPUT_LIMIT if kind == 'selfie' else self.config.video_upload_limit + 16 * 1024**2 if kind == 'video' else self.config.derive_output_limit)
        # Room for this kind's output only: a small selfie must not be refused for a document's budget.
        if shutil.disk_usage(self.config.data_dir).free < self.config.free_floor + output_limit:
            raise DeriveCapacity('Not enough free disk space to prepare this attachment')
        with tempfile.TemporaryDirectory(prefix='derive-',dir=self.config.data_dir/'staging') as tmp:
            output = Path(tmp)
            try:
                command = job_command(source, output, kind, self.config.max_pages,
                                      min(self.config.max_pixels, SELFIE_MAX_PIXELS) if kind == 'selfie' else self.config.max_pixels,
                                      output_limit, self.poppler)
            except (OSError, ValueError) as error:
                # A missing or malformed bubblewrap profile, like sandbox-exec refusing derive.sb, is the host's fault.
                raise DeriveFault('The converter could not run') from error
            process = subprocess.Popen(command,cwd=output,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                env=job_env(output),start_new_session=True)
            try:
                _, stderr = self.wait(process)
            except DeriveError:
                os.killpg(process.pid,signal.SIGKILL)
                process.communicate()
                raise
            returncode = outcome(process.returncode, stderr or b'')
            if returncode is None:
                raise DeriveFault('The converter could not run')
            if returncode:
                if kind == 'selfie' and returncode in FACE_FAILURES:
                    raise SelfieFaceError(FACE_FAILURES[returncode])
                if returncode >= 64:
                    # sandbox-exec itself refusing to start (65 for an unreadable profile).
                    raise DeriveFault('The converter could not run')
                # Signals can come from the upload: the child's CPU/file-size limits or a decoder crash.
                # Keep them counted as bad uploads, like a child that exits 1, rather than refunding retries.
                # Parser diagnostics may contain document text; keep them off user-facing responses.
                raise DeriveError('The attachment could not be converted safely. Check its format, size and page count.')
            try:
                # Do not follow a converter-created link or wait for a FIFO writer. Check the opened
                # descriptor too, so swapping a regular file for a FIFO cannot race a path-only check.
                descriptor = os.open(output/'manifest.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor) as document:
                    if not stat.S_ISREG(os.fstat(document.fileno()).st_mode):
                        raise ValueError('manifest is not a regular file')
                    manifest = json.load(document)
            except (OSError, ValueError) as error:
                # A missing or unreadable manifest is the converter's fault, never the upload's.
                raise DeriveFault('Unexpected converter output') from error
            valid_pages = manifest['pages'] == 0 if kind == 'video' else 1 <= manifest['pages'] <= self.config.max_pages
            if not valid_pages or manifest['bytes'] > output_limit:
                raise DeriveError('Converter output exceeded its configured limits')
            with self.cache_lock:
                if destination.exists():
                    shutil.rmtree(destination)
                destination.mkdir(parents=True,mode=0o700)
                try:
                    for path in output.iterdir():
                        if path.is_symlink() or not path.is_file():
                            raise DeriveFault('Unexpected converter output')
                        # A converter process that outlived the job could swap a checked file for a link before
                        # this rename. Staging and the cache share data_dir, so rename never falls back to a copy
                        # that follows a link; the sandbox cannot write the cache, so the check after it holds.
                        os.rename(path, destination/path.name)
                        moved = os.lstat(destination/path.name)
                        if not stat.S_ISREG(moved.st_mode) or moved.st_nlink != 1:
                            raise DeriveFault('Unexpected converter output')
                except DeriveFault:
                    shutil.rmtree(destination, ignore_errors=True)
                    raise
                # A selfie adds nothing to the derivative cache, so the cache's state must not refuse it.
                if kind != 'selfie':
                    self.trim(exclude=destination)
            return manifest

    def resident(self, process):
        # macOS refuses address-space rlimits, so the whole job's resident memory is bounded here.
        if LINUX:
            return tree_resident(process.pid)
        result = subprocess.run(['/bin/ps','-o','rss=','-g',str(process.pid)],capture_output=True)
        return sum(int(line) for line in result.stdout.split())*1024

    def wait(self, process):
        deadline = time.monotonic()+self.config.derive_timeout
        while True:
            try:
                return process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() >= deadline:
                raise DeriveError('The attachment exceeded the conversion time limit')
            if self.resident(process) > self.config.derive_memory_limit:
                raise DeriveError('The attachment exceeded the conversion memory limit')

    def trim(self, exclude=None):
        root = self.config.data_dir/'derived'
        directories = [p for p in root.iterdir() if p.is_dir()]
        def size(p):return sum(f.stat().st_size for f in p.iterdir() if f.is_file())
        sizes = {p:size(p) for p in directories}
        total = sum(sizes.values())
        for path in sorted(directories,key=lambda p:p.stat().st_mtime):
            if total <= self.config.derivative_budget:break
            if path == exclude or self.leases.get(path,0):continue
            shutil.rmtree(path);total-=sizes[path]
        if total > self.config.derivative_budget:
            raise DeriveCapacity('The derivative cache is at capacity; try again when readers finish')

    def read_path(self, item_id, filename, render):
        # Hold the lease while Pillow reads; eviction never removes an in-flight source.
        directory = self.config.data_dir/'derived'/item_id
        with self.cache_lock:
            self.leases[directory] = self.leases.get(directory,0)+1
        try:
            path=directory/filename
            # A cached derivative is always a regular file; never follow a link out of the cache.
            if path.is_symlink() or not path.is_file():raise FileNotFoundError(path)
            result=render(path)
            os.utime(directory,None)
            return result
        finally:
            with self.cache_lock:
                self.leases[directory]-=1
