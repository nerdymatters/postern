"""Compare Python/Rust history replay on fresh synthetic data, never a live store.

Run with the workspace interpreter and the optional native wheel installed:
  .venv/bin/python -m scripts.progress_benchmark --output .context/progress-benchmark
Both workers read the same fixture, run sequentially in alternating order, and
report their own CPU/peak RSS. This is a reducer benchmark, not hosted capacity.
"""
import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import resource
import sqlite3
import statistics
import sys
import time

from postern.db import SCHEMA


ITEMS = {'video': {'kind': 'video', 'pages': 0, 'duration_seconds': 43200},
         'document': {'kind': 'pdf', 'pages': 20}}


def digest(result):
    values = [[*key, {**value, 'pages': sorted(value['pages'])}]
              for key, value in sorted(result.items())]
    # JSON 0 and 0.0 are the same API number; make the checksum numerical too.
    for _, _, value in values:
        value['watched_seconds'] = float(value['watched_seconds'])
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def worker(channel, path, backend):
    from postern.progress import history
    from postern.progress_backend import selected
    os.environ['POSTERN_PROGRESS_BACKEND'] = backend
    db = None
    try:
        selected()
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        channel.send({'ready': True})
        while (scope := channel.recv()) is not None:
            cpu, wall = time.process_time(), time.perf_counter()
            value = history(db, ITEMS, 'r0@example.invalid' if scope == 'reader' else None)
            elapsed = {'wall_ms': (time.perf_counter()-wall)*1000,
                       'cpu_ms': (time.process_time()-cpu)*1000}
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            channel.send({**elapsed, 'peak_rss_mib': peak / (1024**2 if sys.platform == 'darwin' else 1024),
                          'digest': digest(value), 'states': len(value)})
    except BaseException as error:
        channel.send({'error': f'{type(error).__name__}: {error}'})
    finally:
        if db is not None:
            db.close()
        channel.close()


def receive(channel):
    if not channel.poll(60):
        raise RuntimeError('Benchmark worker exceeded its 60-second deadline')
    result = channel.recv()
    if 'error' in result:
        raise RuntimeError(result['error'])
    return result


def seed(path, readers, per_reader):
    db = sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
        db.executemany('INSERT INTO members VALUES (?,?,?)',
                       [(f'r{i}@example.invalid', 'Synthetic', 'reader') for i in range(readers)])
        def events():
            for i in range(readers * per_reader):
                reader, n = divmod(i, per_reader)
                email = f'r{reader}@example.invalid'
                kind, item = ('page_served', 'document') if n % 5 == 0 else ('video_progress', 'video')
                payload = {'event_id': f'event-{i}', 'ts': float(i), 'event': kind, 'email': email,
                           'name': 'Synthetic reader', 'post_id': 'post', 'item_id': item, 'view_id': 'view',
                           'session_public_id': f'session-{reader}', 'client': 'web', 'ip': '192.0.2.1',
                           'user_agent': 'synthetic benchmark', 'summary': True,
                           'detail': {'page': n % 20} if item == 'document' else {'ranges': [[n*15, n*15+15]]}}
                yield str(i), float(i), kind, email, item, json.dumps(payload)
        db.executemany('INSERT INTO events(id,ts,type,email,item_id,summary,payload) VALUES (?,?,?,?,?,1,?)', events())
        db.commit()
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--readers', type=int, default=50)
    parser.add_argument('--events-per-reader', type=int, default=2160)
    parser.add_argument('--runs', type=int, default=7)
    args = parser.parse_args()
    if not (1 <= args.readers <= 500 and 1 <= args.events_per_reader <= 2880
            and args.readers * args.events_per_reader <= 1_000_000 and 3 <= args.runs <= 30):
        parser.error('Use 1–500 readers, 1–2880 events each, at most 1M events, and 3–30 runs')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    path = args.output / 'synthetic.sqlite3'
    seed(path, args.readers, args.events_per_reader)
    context = multiprocessing.get_context('spawn')
    children = {}
    samples = {backend: {'reader': [], 'roster': []} for backend in ('python', 'rust')}
    try:
        for backend in samples:
            parent, child = context.Pipe()
            process = context.Process(target=worker, args=(child, path, backend))
            process.start(); child.close()
            children[backend] = parent, process
            receive(parent)
        for scope in ('reader', 'roster'):
            for run in range(args.runs + 1):
                pair = {}
                order = ('python', 'rust') if run % 2 == 0 else ('rust', 'python')
                for backend in order:
                    channel, _ = children[backend]
                    channel.send(scope)
                    pair[backend] = receive(channel)
                    if run:
                        samples[backend][scope].append(pair[backend])
                assert pair['python']['digest'] == pair['rust']['digest'], 'Replay result mismatch'
        summary = {backend: {scope: {key: statistics.median(s[key] for s in runs)
                                    for key in ('wall_ms', 'cpu_ms', 'peak_rss_mib')}
                             for scope, runs in scopes.items()} for backend, scopes in samples.items()}
        p, r = summary['python'], summary['rust']
        gates = {
            'roster_wall_reduced_30_percent': r['roster']['wall_ms'] <= p['roster']['wall_ms'] * .7,
            'roster_cpu_reduced_30_percent': r['roster']['cpu_ms'] <= p['roster']['cpu_ms'] * .7,
            'reader_no_material_regression': r['reader']['wall_ms'] <= p['reader']['wall_ms'] + max(2, p['reader']['wall_ms'] * .1),
            'peak_rss_within_32_mib': max(s['peak_rss_mib'] for runs in samples['rust'].values() for s in runs)
                                    <= max(s['peak_rss_mib'] for runs in samples['python'].values() for s in runs) + 32,
        }
        report = {'fixture': {'readers': args.readers, 'events_per_reader': args.events_per_reader,
                              'bytes': path.stat().st_size}, 'platform': sys.platform,
                  'python': sys.version, 'host_load': os.getloadavg(), 'summary': summary,
                  'gates': gates, 'samples': samples,
                  'limits': 'Synthetic reducer+SQLite+binding only; workers run sequentially. No HTTP, Google, media or production capacity claim.'}
        (args.output / 'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps({'summary': summary, 'gates': gates}, indent=2))
        return 0 if all(gates.values()) else 1
    finally:
        for channel, process in children.values():
            if process.is_alive():
                try:
                    channel.send(None)
                except (BrokenPipeError, EOFError):
                    pass
            process.join(5)
            if process.is_alive():
                process.terminate(); process.join(5)
            channel.close()


if __name__ == '__main__':
    raise SystemExit(main())
