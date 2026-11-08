"""Read-only check, run by the watchdog, that the Rust reducer agrees with Python on the live database.

    .venv/bin/python -P -m postern.progress_parity --config ~/.config/postern/config.json

Exit 0: both reducers produce the same staff progress for the current snapshot. 3: they differ.
4: not applicable (no published items, or catalog metadata the native reducer leaves to Python).
Anything else: the check could not run. It reads the database only with mode=ro and query_only,
so it never starts a second writer, and it prints counts and timings, never names or emails.
"""
from __future__ import annotations
import argparse
from itertools import islice
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

KINDS = "('page_served','item_summary','video_progress','item_open','view_requested')"


def compare(database):
    from . import progress, progress_backend, progress_store
    module = progress_backend.native()
    with sqlite3.connect('file:' + quote(str(database)) + '?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')  # One snapshot for the catalog and both replays.
        items = {item['id']: item for item in progress.dashboard_catalog(db)}
        metadata = progress_backend.metadata(items)
        if not items or metadata is None:
            return 4, {'applicable': False, 'items': len(items)}
        source = progress_store.source(db)
        sql = (f'SELECT e.email,e.item_id,e.type,e.ts,e.payload FROM {source} e '
               f'JOIN members m ON m.email=e.email WHERE e.type IN {KINDS}')
        began = time.monotonic()
        python = progress._reduce_python(items, db.execute(sql))
        python_seconds = time.monotonic()-began
        began = time.monotonic()
        native = module.reduce(metadata, db.execute(sql), progress._evidence)
        native_seconds = time.monotonic()-began
        incremental = None
        if source == 'progress_events':
            # The Rust staff report extends a checkpoint only on the compact copy (progress_cache); replay
            # the same history that way too, in two parts, so that interface is compared on real data.
            # On the original events table the service uses the full reducer, which handles legacy clocks.
            # Stream one cursor in two parts so the check never holds the whole history in memory.
            half = db.execute(f'SELECT COUNT(*) FROM ({sql})').fetchone()[0]//2
            rows = db.execute(sql)
            _, checkpoint = module.reduce_incremental(metadata, islice(rows, half), progress._evidence, [])
            incremental, _ = module.reduce_incremental(metadata, rows, progress._evidence, checkpoint)
    match = python == native and (incremental is None or python == incremental)
    return (0 if match else 3), {'match': match, 'full_matches': python == native,
                                 'incremental_matches': None if incremental is None else python == incremental,
                                 'source': source, 'items': len(items), 'keys': len(python),
                                 'python_seconds': round(python_seconds, 3), 'native_seconds': round(native_seconds, 3)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='postern.progress_parity', description=__doc__.splitlines()[0])
    parser.add_argument('--config', default='~/.config/postern/config.json')
    arguments = parser.parse_args(argv)
    values = json.loads(Path(arguments.config).expanduser().read_text())
    code, summary = compare(Path(values['data_dir']).expanduser() / 'postern.sqlite3')
    print(json.dumps(summary), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
