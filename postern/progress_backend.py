"""Opt-in Rust reducer. Python retains the query, snapshot and all durable effects.

Python is the default. At process start the backend comes from, in order:
POSTERN_PROGRESS_BACKEND in the environment (the launchd job's override),
then the owner's progress-backend.json beside the config file, then Python.
The file lets the owner and the watchdog switch backends with an ordinary restart,
without editing the root-owned launchd job; older releases ignore it and run Python.
An explicitly requested but unavailable/incompatible Rust module fails startup;
runtime faults never silently turn into successful or incomplete progress.
"""
from datetime import datetime, timezone
from functools import lru_cache
import importlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import threading

SETTING = 'POSTERN_PROGRESS_BACKEND'
SELECTION = 'progress-backend.json'
ACTIVE = 'progress-backend-active.json'
CHECK = 'progress-backend-check.json'  # written by a Rust-serving API: its own drift checks and native errors


def selection_path(config_path):
    return Path(config_path).expanduser().with_name(SELECTION)


def read_selection(config_path):
    """The owner's selection, or None without one. A selection only the owner can write is trusted."""
    path = selection_path(config_path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RuntimeError(f'{path.name} must be a regular file owned by this account and writable only by it')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('backend') not in ('python', 'rust'):
        raise RuntimeError(f'{path.name} must set "backend" to python or rust')
    return data


def write_selection(config_path, backend, **detail):
    """Atomically replace the selection; readers never see a partial file. The owner's watchdog
    `limits` carry over, so re-selecting a backend never silently resets them."""
    if backend not in ('python', 'rust'):
        raise ValueError('backend must be python or rust')
    path = selection_path(config_path)
    data = {'backend': backend, 'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), **detail}
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = None
    if isinstance(previous, dict) and isinstance(previous.get('limits'), dict) and 'limits' not in detail:
        data['limits'] = previous['limits']
    _write_private(path, data, '.progress-backend-', indent=2)
    return data


def _write_private(path, data, prefix, indent=None):
    """Atomically replace path with owner-only JSON; a failed write leaves no temporary file behind."""
    output = tempfile.NamedTemporaryFile('w', dir=path.parent, prefix=prefix, delete=False)
    try:
        with output:
            os.chmod(output.name, 0o600)
            json.dump(data, output, indent=indent)
            output.write('\n')
        os.replace(output.name, path)
    except BaseException:
        Path(output.name).unlink(missing_ok=True)
        raise


def apply_selection(config_path):
    """Run once at process start, before the service reads the backend. Returns (backend, source)."""
    if SETTING in os.environ:
        return os.environ[SETTING], 'environment'
    data = read_selection(config_path)
    if data is None:
        return 'python', 'default'
    os.environ[SETTING] = data['backend']
    return data['backend'], SELECTION


def record_active(data_dir, backend, source):
    """Tell the watchdog which process runs which backend, and why. Never read by the server."""
    path = Path(data_dir) / ACTIVE
    data = {'pid': os.getpid(), 'backend': backend, 'source': source,
            'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    _write_private(path, data, '.progress-backend-active-')
    return data


def recoverable(error):
    """An error that should fail one progress request, not the process: interrupts, exits and cancellation propagate."""
    # PyO3 reports a Rust panic as PanicException, which derives from BaseException.
    return isinstance(error, Exception) or type(error).__name__ == 'PanicException'


def native_fault(error):
    """An error the native reducer raised. A database error is not one: Python would meet it too."""
    return not isinstance(error, sqlite3.Error) and recoverable(error)


class Health:
    """What the serving process saw on its Rust progress paths, for the watchdog.

    The watchdog's own parity check replays history in another process, so it cannot see the serving
    process's checkpoint or native errors that leave health, CPU and memory normal. This record can:
    it counts native errors and the staff report's comparisons with a full Python replay. Counts
    belong to one process; a restart starts them again. Nothing here is read by the server.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.path = None
        self.write_failed = False
        self.started_at = None
        self.native_errors = self.checks = self.mismatches = self.check_errors = 0
        self.last_error = self.last_check = self.last_check_at = None
        self.last_check_error = self.last_check_error_at = None

    def configure(self, data_dir):
        """Called once by a service that serves Rust; other processes only count in memory."""
        with self.lock:
            self.path = Path(data_dir) / CHECK
            self.started_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self.native_errors = self.checks = self.mismatches = self.check_errors = 0
            self.last_error = self.last_check = self.last_check_at = None
            self.last_check_error = self.last_check_error_at = None
            self._write()

    def native_error(self, error):
        with self.lock:
            self.native_errors += 1
            self.last_error = type(error).__name__
            self._write()

    def checked(self, match):
        with self.lock:
            self.checks += 1
            self.mismatches += int(not match)
            self.last_check = 'match' if match else 'mismatch'
            self.last_check_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self._write()

    def check_failed(self, error):
        """The Python replay itself failed: the comparison did not run, and is not a mismatch. The last
        comparison's result and time stay as they were; the status shows the failures beside them."""
        with self.lock:
            self.check_errors += 1
            self.last_check_error = type(error).__name__
            self.last_check_error_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self._write()

    def _write(self):
        if self.path is None:
            return
        data = {'pid': os.getpid(), 'started_at': self.started_at, 'native_errors': self.native_errors,
                'last_error': self.last_error, 'checks': self.checks, 'mismatches': self.mismatches,
                'check_errors': self.check_errors, 'last_check_error': self.last_check_error,
                'last_check_error_at': self.last_check_error_at,
                'last_check': self.last_check, 'last_check_at': self.last_check_at,
                'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        try:
            _write_private(self.path, data, '.progress-backend-check-')
        except OSError as error:
            # Evidence for the watchdog, never a reason to fail a request. Say so once in the API log.
            if not self.write_failed:
                print(f'postern: {CHECK} not written ({type(error).__name__}); the watchdog cannot see '
                      'in-process checks', file=sys.stderr, flush=True)
                self.write_failed = True


health = Health()


@lru_cache(maxsize=1)
def native():
    try:
        module = importlib.import_module('postern_progress_native')
    except ImportError as error:
        raise RuntimeError('Rust progress requested; install the verified postern-progress-native wheel or select python') from error
    if (getattr(module, 'API_VERSION', None) != 1
            or getattr(module, 'CHECKPOINT_VERSION', None) != 1
            or not callable(getattr(module, 'reduce_incremental', None))):
        raise RuntimeError('Rust progress module has an incompatible API version')
    return module


def selected():
    backend = os.environ.get(SETTING, 'python')
    if backend not in ('python', 'rust'):
        raise RuntimeError('POSTERN_PROGRESS_BACKEND must be python or rust')
    if backend == 'rust':
        native()
    return backend


def metadata(items):
    """Keep unusual legacy metadata in Python before consuming any cursor rows."""
    result = []
    for identity, item in items.items():
        pages = item.get('pages', 0)
        duration = item.get('duration_seconds') or 0
        if (type(identity) is not str or type(item.get('kind')) is not str
                or type(pages) is not int or not 0 <= pages < 2**31
                or type(duration) not in (int, float) or not 0 <= duration < 2**31
                or not math.isfinite(duration)):
            return None
        result.append((identity, item['kind'], pages, duration))
    return result
