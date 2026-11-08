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
import stat
import tempfile

KEY = 'POSTERN_PROGRESS_BACKEND'
SELECTION = 'progress-backend.json'
ACTIVE = 'progress-backend-active.json'


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
    """Atomically replace the selection; readers never see a partial file."""
    if backend not in ('python', 'rust'):
        raise ValueError('backend must be python or rust')
    path = selection_path(config_path)
    data = {'backend': backend, 'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), **detail}
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.progress-backend-', delete=False) as output:
        os.chmod(output.name, 0o600)
        json.dump(data, output, indent=2)
        output.write('\n')
    os.replace(output.name, path)
    return data


def apply_selection(config_path):
    """Run once at process start, before the service reads the backend. Returns (backend, source)."""
    if KEY in os.environ:
        return os.environ[KEY], 'environment'
    data = read_selection(config_path)
    if data is None:
        return 'python', 'default'
    os.environ[KEY] = data['backend']
    return data['backend'], SELECTION


def record_active(data_dir, backend, source):
    """Tell the watchdog which process runs which backend, and why. Never read by the server."""
    path = Path(data_dir) / ACTIVE
    data = {'pid': os.getpid(), 'backend': backend, 'source': source,
            'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.progress-backend-active-', delete=False) as output:
        os.chmod(output.name, 0o600)
        json.dump(data, output)
        output.write('\n')
    os.replace(output.name, path)
    return data


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
    backend = os.environ.get(KEY, 'python')
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
