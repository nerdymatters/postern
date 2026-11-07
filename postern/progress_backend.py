"""Opt-in Rust reducer. Python retains the query, snapshot and all durable effects.

POSTERN_PROGRESS_BACKEND=python is the default and the rollback switch.
An explicitly requested but unavailable/incompatible Rust module fails startup;
runtime faults never silently turn into successful or incomplete progress.
"""
from functools import lru_cache
import importlib
import math
import os


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
    backend = os.environ.get('POSTERN_PROGRESS_BACKEND', 'python')
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
