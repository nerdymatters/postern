"""Profile replay stages on fresh synthetic history; never an acceptance result.

The CLI is restricted to disposable GitHub-hosted Macs. Every worker has CPU,
memory and time guards. Only JSON measurements and text profiles are uploaded.
"""
import argparse
import cProfile
import gc
import hashlib
from importlib.machinery import EXTENSION_SUFFIXES
import io
import json
import multiprocessing
import os
from pathlib import Path
import pstats
import sqlite3
import sys
import time

import psutil

from postern import progress, progress_backend
from postern.db import SCHEMA, event
from scripts.progress_benchmark import digest

ITEMS = {'video': {'kind': 'video', 'pages': 0, 'duration_seconds': 43200}}
SQL = ("SELECT e.email,e.item_id,e.type,e.ts,e.payload FROM events e "
       "JOIN members m ON m.email=e.email WHERE e.type IN "
       "('page_served','item_summary','video_progress','item_open','view_requested')")


def seed(path, readers=228, events_per_reader=2160):
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for reader in range(readers):
            email = f'reader{reader}@example.invalid'
            db.execute('INSERT INTO members VALUES (?,?,?)', (email, 'Synthetic', 'reader'))
            for n in range(events_per_reader):
                # Match the capacity fixture's all-video history and event encoder.
                event(db, 'video_progress', {'email': email, 'id': f'session{reader}'},
                      item_id='video', post_id='video', detail={'ranges': [[n*15, n*15+15]]},
                      timestamp=float(n*15))


def measure(operation):
    gc.collect()
    cpu, wall = time.process_time(), time.perf_counter()
    value = operation()
    return value, {'wall_ms': (time.perf_counter()-wall)*1000,
                   'cpu_ms': (time.process_time()-cpu)*1000}


def profile(path, backend):
    os.environ['POSTERN_PROGRESS_BACKEND'] = backend
    progress_backend.selected()
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    try:
        streamed, streaming = measure(lambda: progress.history(db, ITEMS))
        rows, fetching = measure(lambda: list(db.execute(SQL)))
        expected = digest(streamed)
        del streamed
        operation = (lambda: progress._reduce_python(ITEMS, rows)) if backend == 'python' else (
            lambda: progress_backend.native().reduce(progress_backend.metadata(ITEMS), iter(rows), progress._evidence))
        reduced, reduction = measure(operation)
        if digest(reduced) != expected:
            raise RuntimeError('Materialized and streaming progress differ')
        count = len(rows)
        del rows, reduced
        recorder = cProfile.Profile()
        recorded = recorder.runcall(progress.history, db, ITEMS)
        if digest(recorded) != expected:
            raise RuntimeError('Profiled and unprofiled progress differ')
        text = io.StringIO()
        pstats.Stats(recorder, stream=text).sort_stats('cumulative').print_stats(30)
        native_hash = None
        if backend == 'rust':
            binary = Path(sys.modules[progress_backend.native().reduce.__module__].__file__)
            if not any(str(binary).endswith(suffix) for suffix in EXTENSION_SUFFIXES):
                raise RuntimeError('Native provenance did not resolve to an extension binary')
            native_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
        return {'backend': backend, 'rows': count, 'digest': expected, 'streaming': streaming,
                'fetch_rows': fetching, 'reduce_materialized': reduction, 'profile': text.getvalue(),
                'native_sha256': native_hash, 'python': sys.version}
    finally:
        db.close()


def worker(output, mode, path, backend):
    if mode == 'seed':
        seed(path)
        result = {'seeded_events': 228*2160}
    else:
        result = profile(path, backend)
    output.write_text(json.dumps(result, indent=2)+'\n')


def guarded(output, mode, path, backend=None):
    child = multiprocessing.get_context('spawn').Process(target=worker, args=(output, mode, path, backend))
    record = {'status': 'running', 'samples': []}
    child.start()
    started, busy = time.monotonic(), 0
    process = psutil.Process(child.pid)
    psutil.cpu_percent()
    try:
        while child.is_alive():
            cpu = psutil.cpu_percent(interval=.5)
            child.join(0)
            if not child.is_alive():
                break
            try:
                rss = process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                child.join(1)
                if not child.is_alive():
                    break
                raise
            elapsed = time.monotonic()-started
            record['samples'].append({'elapsed_seconds': elapsed, 'host_cpu_percent': cpu, 'rss_mib': rss/1024**2})
            busy = busy+1 if cpu > 70 else 0
            if busy >= 10 or rss > 1024**3 or psutil.virtual_memory().available < 1024**3 or elapsed > 120:
                raise RuntimeError('Profiling worker crossed CPU, memory or 120-second diagnostic guard')
        child.join()
        if child.exitcode != 0:
            raise RuntimeError(f'Profiling worker exited {child.exitcode}')
        record['status'] = 'completed'
    except BaseException as error:
        record.update(status='stopped', error=str(error))
        raise
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)
            if child.is_alive():
                child.kill()
                child.join(5)
        record['exit_code'] = child.exitcode
        output.with_suffix('.guard.json').write_text(json.dumps(record, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not (os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted' and sys.platform == 'darwin'):
        parser.error('Run this diagnostic only on a disposable GitHub-hosted Mac')
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    path = folder/'synthetic.sqlite3'
    guarded(folder/'seed.json', 'seed', path)
    results = []
    for index, backend in enumerate(('python', 'rust', 'rust', 'python', 'python', 'rust'), 1):
        output = folder/f'{index}-{backend}.json'
        guarded(output, 'profile', path, backend)
        results.append(json.loads(output.read_text()))
    if any(value['rows'] != 228*2160 for value in results):
        raise RuntimeError('Synthetic history row count differs from declared workload')
    if len({value['digest'] for value in results}) != 1:
        raise RuntimeError('Python and Rust progress differ')
    (folder/'summary.json').write_text(json.dumps({
        'results': results, 'identical_progress': True,
        'limits': 'Sequential stage diagnostics, not HTTP or capacity acceptance. Materialization and cProfile alter costs; stage times are not additive.'}, indent=2)+'\n')


if __name__ == '__main__':
    main()
