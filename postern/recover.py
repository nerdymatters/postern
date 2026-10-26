"""Restore a Postern host onto a fresh Mac (ENG-D5).

    .venv/bin/python -m postern.recover --config ~/.config/postern/config.json --into ~/postern-restore

Only the cloud copies are trusted: the confirmed catalog, the sealed JSONL batches checked against their
recorded schema, byte length and SHA-256, the yearly summary log and the original files. Nothing is read
from the old data directory, no session or bearer credential is restored, and the restored host starts in
recovery mode with reader content hidden until an operator verifies the catalog.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import time

from . import quiz
from .config import Config, private_json
from .db import Database, set_meta
from .derive import Deriver, DeriveError
from .storage import EVENT_COLUMNS, ITEM_COLUMNS, POST_COLUMNS, FakeStore, GoogleStore

SCHEMA = '1'
# Sheets has no null and drops trailing blanks, so an empty or absent optional column means None.
NULLABLE = {'published_at', 'publish_op_id', 'error', 'drive_file_id', 'derive_error'}


class RecoveryError(Exception):
    """A batch that cannot be trusted. Its events become explicit unknown history, never a silent drop."""


def write(database, operation):
    return database.submit(operation).result(timeout=120)


def upsert(db, table, columns, record):
    assignments = ','.join(f'{c}=excluded.{c}' for c in columns[1:])
    values = [(record.get(c) or None) if c in NULLABLE else record.get(c, '') for c in columns]
    db.execute(f'INSERT INTO {table}({",".join(columns)}) VALUES ({",".join("?" * len(columns))})'
               f' ON CONFLICT(id) DO UPDATE SET {assignments}', values)


def batch_record(raw):
    """The metadata recorded beside the batch; Drive keeps app properties as strings."""
    try:
        record = {'drive_id': raw['id'], 'batch_id': raw['batch_id'], 'sha256': raw['sha256'],
                  'bytes': int(raw['bytes']), 'count': int(raw['count']), 'schema': str(raw['schema'])}
    except (KeyError, TypeError, ValueError) as error:
        raise RecoveryError(f'its recorded metadata is incomplete ({error})')
    if record['schema'] != SCHEMA:
        raise RecoveryError(f"its schema version is {record['schema']}, not {SCHEMA}")
    return record


def batch_events(path, record):
    data = path.read_bytes()
    if len(data) != record['bytes']:
        raise RecoveryError(f"it is {len(data)} bytes, not the recorded {record['bytes']}")
    if hashlib.sha256(data).hexdigest() != record['sha256']:
        raise RecoveryError('its SHA-256 does not match the recorded digest')
    events = []
    for number, line in enumerate(data.decode().splitlines(), 1):
        try:
            row = json.loads(line)
        except ValueError:
            raise RecoveryError(f'line {number} is not readable JSON')
        if str(row.get('schema')) != SCHEMA:
            raise RecoveryError(f"line {number} has schema {row.get('schema')}, not {SCHEMA}")
        if not row.get('event_id') or not row.get('event') or row.get('ts') is None:
            raise RecoveryError(f'line {number} has no event id, type or timestamp')
        events.append(row)
    if len(events) != record['count']:
        raise RecoveryError(f"it holds {len(events)} events, not the recorded {record['count']}")
    return events


def import_events(db, events, batch_id=None):
    """Insert at the original timestamps. event_id is the identity, so a repeated import inserts nothing."""
    imported = 0
    for row in events:
        payload = {k: v for k, v in row.items() if k != 'schema'}
        cursor = db.execute('INSERT OR IGNORE INTO events(id,ts,type,email,post_id,item_id,view_id,summary,payload,batch_id)'
                            ' VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (row['event_id'], row['ts'], row['event'], row.get('email'), row.get('post_id'),
                             row.get('item_id'), row.get('view_id'), int(row.get('summary', True)),
                             json.dumps(payload), batch_id))
        imported += cursor.rowcount
        if cursor.rowcount and row['event'] == 'quiz_attempt':
            quiz.record(db, db.execute('SELECT email,ts,seq,payload FROM events WHERE id=?', (row['event_id'],)).fetchone())
    return imported


def summary_events(store):
    """storage.py writes the yearly summary log but never reads it back; recovery is its only reader."""
    if isinstance(store, FakeStore):
        path = store.root / 'summary.json'
        return json.loads(path.read_text()) if path.exists() else []
    drive, sheets = store._service('drive', 'v3'), store._service('sheets', 'v4')
    response = drive.files().list(q=f"'{store.config.drive_folder_id}' in parents and trashed=false"
                                    " and mimeType='application/vnd.google-apps.spreadsheet'",
                                  fields='files(id,appProperties)', pageSize=1000).execute()
    events = []
    for log in [f['id'] for f in response.get('files', []) if f.get('appProperties', {}).get('postern_log_year')]:
        values = sheets.spreadsheets().values().get(spreadsheetId=log, range='A:M').execute().get('values', [])
        if not values or values[0] != EVENT_COLUMNS:
            raise RecoveryError(f'summary log {log} does not match the event schema')
        for cells in values[1:]:
            row = {k: (v if v != '' else None) for k, v in zip(EVENT_COLUMNS, cells)}
            if not row.get('event_id') or row.get('ts') is None:
                continue
            events.append({**row, 'ts': float(row['ts']), 'summary': True,
                           'detail': json.loads(row['detail']) if row.get('detail') else {}})
    return events


def restore_catalog(database, store):
    catalog = store.catalog()
    known = {post['id'] for post in catalog['posts']}
    items = [item for item in catalog['items'] if item['post_id'] in known]

    def apply(db):
        for post in catalog['posts']:
            upsert(db, 'posts', POST_COLUMNS, post)
        for item in items:
            # Queued, not ready: the derivative is rebuilt below, and an interrupted recovery leaves the
            # remaining conversions for Service.start to pick up rather than a post that shows nothing.
            upsert(db, 'items', ITEM_COLUMNS, {**item, 'derive_status': 'queued', 'derive_error': None})
    write(database, apply)
    return len(catalog['posts']), items, len(catalog['items']) - len(items)


def restore_batches(database, config, store, raws, report):
    exports = config.data_dir / 'exports'
    for raw in sorted(raws, key=lambda r: str(r.get('batch_id') or r['id'])):
        identity = raw.get('batch_id') or raw['id']
        try:
            record = batch_record(raw)
            # Corrupt bytes stay in exports/ as evidence; without a batches row nothing re-uploads them.
            store.download(record['drive_id'], exports / (record['batch_id'] + '.jsonl'))
            events = batch_events(exports / (record['batch_id'] + '.jsonl'), record)
        except Exception as error:
            reason = str(error) if isinstance(error, RecoveryError) else f'it could not be downloaded ({error})'
            report['missing_ranges'].append({'batch': identity, 'events': raw.get('count'), 'reason': reason})
            continue

        def seal(db, events=events, record=record):
            sealed = max(e['ts'] for e in events) if events else time.time()
            db.execute('INSERT OR IGNORE INTO batches(id,drive_id,sha256,bytes,count,created_at,confirmed_at,summary_confirmed)'
                       ' VALUES (?,?,?,?,?,?,?,1)', (record['batch_id'], record['drive_id'], record['sha256'],
                                                     record['bytes'], record['count'], sealed, sealed))
            return import_events(db, events, record['batch_id'])
        imported = write(database, seal)
        report['batches']['imported'] += 1
        report['events']['imported'] += imported
        report['events']['duplicate'] += len(events) - imported


def restore_summary(database, store, report):
    """Second, so an event that is in a good batch keeps that batch id and is never exported twice."""
    try:
        events = summary_events(store)
    except Exception as error:
        report['missing_ranges'].append({'batch': 'the summary log', 'events': None,
                                         'reason': f'it could not be read ({error})'})
        return
    imported = write(database, lambda db: import_events(db, events))
    report['events']['imported'] += imported
    report['events']['duplicate'] += len(events) - imported
    report['events']['from_summary'] = imported


def rebuild_derivatives(database, config, store, items, report):
    """From the originals in the store. Whatever the dead Mac had converted is not evidence of anything."""
    deriver = Deriver(config)
    for item in items:
        path = config.data_dir / 'staging' / (item['id'] + Path(item['filename']).suffix.lower())
        try:
            if not item.get('drive_file_id'):
                raise DeriveError('No original is recorded for this attachment.')
            store.download(item['drive_file_id'], path, item['sha256'])
            manifest = deriver.run(path, config.data_dir / 'derived' / item['id'], item['kind'])
            if item['kind'] == 'video':
                write(database, lambda db, i=item, m=manifest: db.execute('INSERT OR REPLACE INTO video_metadata VALUES (?,?)', (i['id'], m['duration_seconds'])))
        except Exception as error:
            message = str(error) if isinstance(error, DeriveError) else f'The original could not be read ({error}).'
            write(database, lambda db, m=message, i=item: db.execute(
                "UPDATE items SET derive_status='failed',derive_error=? WHERE id=?", (m, i['id'])))
            report['publications']['unavailable'].append({'id': item['id'], 'filename': item['filename'], 'error': message})
        else:
            write(database, lambda db, m=manifest, i=item: db.execute(
                "UPDATE items SET derive_status='ready',pages=?,derive_error=NULL WHERE id=?", (m['pages'], i['id'])))
            report['publications']['rebuilt'] += 1
        finally:
            path.unlink(missing_ok=True)


def restore(config, store, log=print):
    """Rebuild config.data_dir from the cloud copies; the caller guarantees the directory is safe to use."""
    report = {'batches': {'imported': 0, 'total': 0}, 'events': {'imported': 0, 'duplicate': 0, 'from_summary': 0},
              'publications': {'posts': 0, 'items': 0, 'rebuilt': 0, 'orphans': 0, 'unavailable': []},
              'missing_ranges': [], 'sessions': 0, 'recovery_mode': True, 'history_incomplete': False,
              'data_dir': str(config.data_dir), 'recovered_at': time.time()}
    database = Database(config.data_dir / 'postern.sqlite3', deadline=60)
    try:
        def begin(db):
            # Persist before the first cloud read: an interrupted restore must fail closed on restart.
            set_meta(db, 'recovery_mode', True)
            set_meta(db, 'history_incomplete', True)
        write(database, begin)
        posts, items, orphans = restore_catalog(database, store)
        report['publications'].update({'posts': posts, 'items': len(items), 'orphans': orphans})
        raws = store.batches()
        report['batches']['total'] = len(raws)
        restore_batches(database, config, store, raws, report)
        restore_summary(database, store, report)
        from .selfie import restore_metadata, relocate_restored_metadata
        relocations = store.selfie_relocations()
        def restore_portraits(db):
            restore_metadata(db)
            relocate_restored_metadata(db, relocations)
        write(database, restore_portraits)
        report['quarantined_selfies'] = database.read(lambda db: db.execute(
            "SELECT count(*) FROM selfies WHERE status='quarantined'").fetchone()[0])
        rebuild_derivatives(database, config, store, items, report)
        report['history_incomplete'] = bool(report['missing_ranges'])

        def finish(db):
            # Sessions and members stay empty on purpose: readers sign in again, and the members table is
            # filled by the fresh allowlist read that Service.start performs (ENG-D2).
            set_meta(db, 'recovery_mode', True)
            set_meta(db, 'history_incomplete', report['history_incomplete'])
        write(database, finish)
    finally:
        database.close()
    private_json(config.data_dir / 'recovery-report.json', report)
    log(describe(report))
    return report


def describe(report):
    publications, events = report['publications'], report['events']
    lines = [f"batches            {report['batches']['imported']} of {report['batches']['total']} imported",
             f"events             {events['imported']} imported, {events['duplicate']} already present,"
             f" {events['from_summary']} of them from the summary log",
             f"publications       {publications['posts']} post(s), {publications['items']} attachment(s),"
             f" {publications['rebuilt']} derivative(s) rebuilt from the originals",
             'sessions           0 restored — every reader signs in again']
    if publications['orphans']:
        lines.append(f"catalog            {publications['orphans']} attachment(s) name a post the catalog does not hold")
    if report.get('quarantined_selfies'):
        lines.append(f"selfies            {report['quarantined_selfies']} capture(s) lack confirmation history; "
                     'kept inaccessible until their original expiry plus retention')
    for item in publications['unavailable']:
        lines.append(f"missing original   {item['filename']} ({item['id']}): {item['error']}")
    for gap in report['missing_ranges']:
        lines.append(f"unknown history    {gap['batch']}: {gap['reason']} — "
                     f"{gap['events'] if gap['events'] is not None else 'an unknown number of'} event(s) are unknown, not 'not opened'")
    lines.append('history            ' + ('INCOMPLETE — verify the ranges above' if report['history_incomplete'] else 'complete'))
    lines.append('recovery mode      on — reader content stays hidden until an operator verifies the catalog')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Restore Postern into an empty data directory')
    parser.add_argument('--config', required=True)
    parser.add_argument('--into', required=True, help='a new, empty directory; never the live data directory')
    arguments = parser.parse_args(argv)

    values = json.loads(Path(arguments.config).expanduser().read_text())
    live = Path(values['data_dir']).expanduser().resolve()
    target = Path(arguments.into).expanduser().resolve()
    if target == live or live in target.parents or target in live.parents:
        raise SystemExit(f'Refusing to restore into {target}: it overlaps the live data directory {live}.')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise SystemExit(f'Refusing to restore into {target}: it is not an empty directory.')

    config = Config(**{**values, 'data_dir': target, 'recovery_mode': True}).validate()
    if config.mode == 'demo':
        cloud = live / 'fake-cloud'
        if not cloud.is_dir():
            raise SystemExit(f'Refusing to restore: {cloud} does not exist, so there is nothing to restore from.')
        store = FakeStore(cloud)
    else:
        store = GoogleStore(config)
    report = restore(config, store)
    print(f'\nRestored into {target}. Verify the catalog before serving anyone: docs/runbooks/recovery.md')
    # 0 restored everything the cloud holds, 2 restored with gaps to read, 1 refused before touching anything.
    return 2 if report['history_incomplete'] or report['publications']['unavailable'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
