from __future__ import annotations
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from .derive_child import FACE_MODEL, FACE_FAILURES

ROOT = Path(__file__).resolve().parents[1]
# The one Seatbelt profile every conversion job runs under; preflight compiles and exercises this same file.
PROFILE = ROOT / 'config' / 'sandbox' / 'derive.sb'
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


def job_command(source, output, kind, max_pages, max_pixels, max_bytes, poppler):
    """The exact sandboxed command for one conversion job."""
    return ['/usr/bin/sandbox-exec', *sandbox_profile_args(source, output), sys.executable, '-I', str(CHILD),
            str(source), str(output), kind, str(max_pages), str(max_pixels), str(max_bytes), str(poppler)]


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
        if platform.system() != 'Darwin' or not shutil.which('sandbox-exec'):
            raise DeriveFault('This converter requires the configured macOS sandbox')
        source, destination = Path(source).resolve(), Path(destination).resolve()
        output_limit = (SELFIE_OUTPUT_LIMIT if kind == 'selfie' else self.config.video_upload_limit + 16 * 1024**2 if kind == 'video' else self.config.derive_output_limit)
        # Room for this kind's output only: a small selfie must not be refused for a document's budget.
        if shutil.disk_usage(self.config.data_dir).free < self.config.free_floor + output_limit:
            raise DeriveCapacity('Not enough free disk space to prepare this attachment')
        with tempfile.TemporaryDirectory(prefix='derive-',dir=self.config.data_dir/'staging') as tmp:
            output = Path(tmp)
            command = job_command(source, output, kind, self.config.max_pages,
                                  min(self.config.max_pixels, SELFIE_MAX_PIXELS) if kind == 'selfie' else self.config.max_pixels,
                                  output_limit, self.poppler)
            process = subprocess.Popen(command,cwd=output,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                env=job_env(output),start_new_session=True)
            try:
                self.wait(process)
            except DeriveError:
                os.killpg(process.pid,signal.SIGKILL)
                process.communicate()
                raise
            if process.returncode:
                if kind == 'selfie' and process.returncode in FACE_FAILURES:
                    raise SelfieFaceError(FACE_FAILURES[process.returncode])
                if process.returncode >= 64:
                    # sandbox-exec itself refusing to start (65 for an unreadable profile).
                    raise DeriveFault('The converter could not run')
                # Signals can come from the upload: the child's CPU/file-size limits or a decoder crash.
                # Keep them counted as bad uploads, like a child that exits 1, rather than refunding retries.
                # Parser diagnostics may contain document text; keep them off user-facing responses.
                raise DeriveError('The attachment could not be converted safely. Check its format, size and page count.')
            try:
                manifest = json.loads((output/'manifest.json').read_text())
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
                for path in output.iterdir():
                    if path.is_symlink() or not path.is_file():
                        raise DeriveFault('Unexpected converter output')
                    shutil.move(str(path),destination/path.name)
                # A selfie adds nothing to the derivative cache, so the cache's state must not refuse it.
                if kind != 'selfie':
                    self.trim(exclude=destination)
            return manifest

    def resident(self, process):
        # macOS refuses address-space rlimits, so the whole job's resident memory is bounded here.
        result = subprocess.run(['/bin/ps','-o','rss=','-g',str(process.pid)],capture_output=True)
        return sum(int(line) for line in result.stdout.split())*1024

    def wait(self, process):
        deadline = time.monotonic()+self.config.derive_timeout
        while True:
            try:
                process.communicate(timeout=0.5)
                return
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
            if not path.is_file():raise FileNotFoundError(path)
            result=render(path)
            os.utime(directory,None)
            return result
        finally:
            with self.cache_lock:
                self.leases[directory]-=1
