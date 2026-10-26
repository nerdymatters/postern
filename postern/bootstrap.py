"""One-time Google setup for a Postern host account.

Creates what the app needs in the host account's own Drive — the Postern folder and the Postern DB
spreadsheet with the exact tabs and headers GoogleStore expects — and records their ids in the config
file. Everything it creates belongs to the app, so the host credential only needs `drive.file`.

    .venv/bin/python -m postern.bootstrap --config ~/.config/postern/config.json \\
        --client-secrets ~/Downloads/client_secret.json

The existing staff allowlist Sheet is NOT created here: it already exists and was not made
by this app, so `drive.file` cannot see it. Share it with the service account named by `allowlist_credentials`
as Viewer; this command verifies that read and reports exactly what it found.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from .config import Config, private_json
from .storage import ITEM_COLUMNS, POST_COLUMNS, GoogleStore

SCOPES = ['https://www.googleapis.com/auth/drive.file']
FOLDER = 'application/vnd.google-apps.folder'


def authorize(client_secrets, destination):
    """Run the installed-app consent flow once and store the refresh token owner-only."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(Path(client_secrets).expanduser()), SCOPES)
    credentials = flow.run_local_server(port=0, prompt='consent',
                                        authorization_prompt_message='Sign in as the Postern host account: {url}')
    private_json(destination, json.loads(credentials.to_json()))
    Path(destination).chmod(0o600)
    return destination


def find_or_create(drive, name, mime, parent=None):
    query = [f"name='{name}'", f"mimeType='{mime}'", 'trashed=false', "appProperties has { key='postern' and value='1' }"]
    if parent:
        query.append(f"'{parent}' in parents")
    existing = drive.files().list(q=' and '.join(query), fields='files(id,name)', pageSize=10).execute().get('files', [])
    if existing:
        return existing[0]['id'], False
    body = {'name': name, 'mimeType': mime, 'appProperties': {'postern': '1'}}
    if parent:
        body['parents'] = [parent]
    return drive.files().create(body=body, fields='id').execute()['id'], True


def prepare_database(sheets, spreadsheet_id):
    """Give the database spreadsheet exactly the tabs and headers the store reads back."""
    data = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id, fields='sheets.properties').execute()
    present = {s['properties']['title']: s['properties']['sheetId'] for s in data['sheets']}
    requests = [{'addSheet': {'properties': {'title': tab}}} for tab in ('posts', 'items') if tab not in present]
    if requests:
        sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={'requests': requests}).execute()
    for tab, columns in (('posts', POST_COLUMNS), ('items', ITEM_COLUMNS)):
        values = sheets.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=f'{tab}!A1:Z1').execute().get('values', [])
        if values and values[0] != columns:
            raise SystemExit(f'The {tab} tab already has different headers. Resolve it by hand before continuing.')
        if not values:
            sheets.spreadsheets().values().update(spreadsheetId=spreadsheet_id, range=f'{tab}!A1', valueInputOption='RAW',
                                                  body={'values': [columns]}).execute()
    # The default first tab that Google adds is unused and confusing next to posts/items.
    if 'Sheet1' in present and len(present) > 1:
        sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id,
                                          body={'requests': [{'deleteSheet': {'sheetId': present['Sheet1']}}]}).execute()


def check_allowlist(config, report):
    from .auth import parse_allowlist
    try:
        values = GoogleStore(config).allowlist()
    except Exception as error:
        report.append(f'allowlist          UNREADABLE — {error}')
        report.append('                   Share the Sheet with the service account as Viewer, then run this again.')
        return False
    try:
        warnings = []
        members = parse_allowlist(values, config.email_header, warnings=warnings)
    except ValueError as error:
        report.append(f'allowlist          INVALID — {error}')
        return False
    admins = sorted(m['email'] for m in members.values() if m['role'] == 'admin')
    report.append(f'allowlist          {len(members)} member(s), {len(admins)} administrator(s)')
    report.append(f'                   administrators: {", ".join(admins) or "none — add a role column with admin"}')
    from .quiz import EXEMPT
    leaders = sum(m['role'] in EXEMPT - {'admin'} for m in members.values())
    report.append(f'                   {leaders} manager/C-suite member(s) may skip chapter quizzes')
    for warning in warnings:
        report.append(f"                   {warning['group']} row {warning['row']}: {warning['message']} — no login access")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare a Google account for Postern')
    parser.add_argument('--config', required=True)
    parser.add_argument('--client-secrets', help='OAuth client secrets for the desktop client, needed on first run')
    parser.add_argument('--folder-name', default='Postern')
    parser.add_argument('--database-name', default='Postern DB')
    arguments = parser.parse_args(argv)

    path = Path(arguments.config).expanduser()
    values = json.loads(path.read_text()) if path.exists() else {}
    values.setdefault('data_dir', str(Path('~/Library/Application Support/Postern').expanduser()))
    values.setdefault('mode', 'google')
    host = Path(values.get('host_credentials') or path.parent / 'host-credentials.json').expanduser()
    if not host.exists():
        if not arguments.client_secrets:
            raise SystemExit(f'{host} does not exist yet. Re-run with --client-secrets <downloaded client_secret.json>.')
        authorize(arguments.client_secrets, host)
    values['host_credentials'] = str(host)

    # Config.validate refuses an incomplete Google configuration, so bootstrap talks to the store directly.
    draft = Config(**{**values, 'mode': 'demo'})
    draft.data_dir = Path(draft.data_dir).expanduser()
    store = GoogleStore(draft)
    drive, sheets = store._service('drive', 'v3'), store._service('sheets', 'v4')

    report = []
    folder_id, created = find_or_create(drive, arguments.folder_name, FOLDER)
    report.append(f'Drive folder       {folder_id} ({"created" if created else "already present"})')
    database_id, created = find_or_create(drive, arguments.database_name, 'application/vnd.google-apps.spreadsheet', folder_id)
    prepare_database(sheets, database_id)
    report.append(f'Postern DB         {database_id} ({"created" if created else "already present"}), tabs posts/items ready')

    values['drive_folder_id'] = folder_id
    values['database_sheet_id'] = database_id
    private_json(path, values)
    path.chmod(0o600)
    report.append(f'config             {path} (0600)')

    ready = True
    if values.get('allowlist_sheet_id'):
        ready = check_allowlist(Config(**{**values, 'mode': 'demo'}), report)
    else:
        report.append('allowlist          not configured — set allowlist_sheet_id to the staff allowlist Sheet id')
        ready = False
    if not values.get('google_client_id'):
        report.append('sign-in            not configured — set google_client_id to the Web OAuth client id')
        ready = False

    print('\n'.join(report))
    print('\nReady to serve.' if ready else '\nNot ready yet: fix the lines above, then run this command again.')
    return 0 if ready else 1


if __name__ == '__main__':
    raise SystemExit(main())
