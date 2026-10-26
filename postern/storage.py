"""Private cloud storage and an explicit local-only test double.

The adapter confirms original bytes and commits each publication atomically.
Google calls run outside SQLite transactions and outside the event loop.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import threading
import uuid
from .config import private_json

POST_COLUMNS = ['id','title','body','category','status','revision','created_at','created_by','published_at','publish_op_id','error']
ITEM_COLUMNS = ['id','post_id','filename','kind','sha256','bytes','pages','drive_file_id','derive_status','derive_error','sort','revision']
EVENT_COLUMNS = ['event_id','ts','event','email','name','post_id','item_id','view_id','detail','client','user_agent','ip','session_public_id']


class StoreUnavailable(Exception):
    pass


def file_digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


class HashSink:
    """Verify large cloud originals without retaining their bytes in RAM."""
    def __init__(self, limit):
        self.hasher = hashlib.sha256()
        self.size = 0
        self.limit = limit

    def write(self, chunk):
        self.size += len(chunk)
        if self.size > self.limit:
            raise StoreUnavailable('Remote file exceeds its expected bound')
        self.hasher.update(chunk)
        return len(chunk)


class FakeStore:
    """Persistent fixture storage. Never selected implicitly if Google fails."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root/'files').mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.available = True
        self.allowlist_available = True
        self.lose_commit_ack = False
        self.lose_upload_ack = False
        if not (self.root/'allowlist.json').exists():
            private_json(self.root/'allowlist.json', [['email','name','role'],['admin@example.invalid','Demo administrator','admin'],['reader@example.invalid','Demo reader','reader']])
        if not (self.root/'catalog.json').exists():
            private_json(self.root/'catalog.json', {'posts': [], 'items': []})

    def _check(self):
        if not self.available:
            raise StoreUnavailable('Storage temporarily unavailable')

    def write_allowlist(self, values):
        private_json(self.root/'allowlist.json', values)

    def allowlist(self):
        if not self.allowlist_available:
            raise StoreUnavailable('Allowlist temporarily unavailable')
        return json.loads((self.root/'allowlist.json').read_text())

    def new_id(self):
        return str(uuid.uuid4())

    def put_file(self, identity, path, name, digest, metadata=None):
        self._check()
        target = self.root/'files'/identity
        with self.lock:
            if target.exists():
                if file_digest(target) != digest:
                    raise StoreUnavailable('Stored file checksum does not match')
            else:
                shutil.copyfile(path, target)
                target.chmod(0o600)
            private_json(self.root/'files'/(identity+'.meta'), {'name': name, 'sha256': digest, **(metadata or {})})
        if self.lose_upload_ack:
            self.lose_upload_ack = False
            raise StoreUnavailable('Simulated lost upload acknowledgement')
        return identity

    def verify(self, identity, digest):
        self._check()
        target = self.root/'files'/identity
        if not target.exists() or file_digest(target) != digest:
            raise StoreUnavailable('The stored original could not be confirmed')

    def delete_selfie(self, identity):
        self._check()
        target = self.root/'files'/identity
        metadata = self.root/'files'/(identity+'.meta')
        if not metadata.exists() and not target.exists():
            return
        if not metadata.exists() or json.loads(metadata.read_text()).get('kind') != 'selfie':
            raise StoreUnavailable('Refusing to remove a non-selfie object')
        target.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)

    def download(self, identity, destination, digest=None, limit=None):
        self._check()
        source = self.root/'files'/identity
        if limit is not None and source.stat().st_size > limit:
            raise StoreUnavailable('Remote file exceeds its expected bound')
        if digest and file_digest(source) != digest:
            raise StoreUnavailable('Stored file checksum does not match')
        shutil.copyfile(source, destination)
        Path(destination).chmod(0o600)

    def catalog(self):
        self._check()
        return json.loads((self.root/'catalog.json').read_text())

    def commit_publication(self, post, items):
        self._check()
        with self.lock:
            catalog = self.catalog()
            old = next((r for r in catalog['posts'] if r['id'] == post['id']), None)
            if old and old['revision'] > post['revision']:
                raise StoreUnavailable('A newer publication revision already exists')
            catalog['posts'] = [r for r in catalog['posts'] if r['id'] != post['id']] + [post]
            catalog['items'] = [r for r in catalog['items'] if r['post_id'] != post['id']] + items
            private_json(self.root/'catalog.json', catalog)
        if self.lose_commit_ack:
            self.lose_commit_ack = False
            raise StoreUnavailable('Simulated lost publication acknowledgement')

    def append_summary(self, events):
        self._check()
        with self.lock:
            path = self.root/'summary.json'
            existing = json.loads(path.read_text()) if path.exists() else []
            keyed = {r['event_id']: r for r in existing}
            keyed.update({r['event_id']: r for r in events})
            private_json(path, list(keyed.values()))

    def selfie_relocations(self):
        return {}

    def batches(self):
        self._check()
        return [{'id': p.stem, **json.loads(p.read_text())} for p in (self.root/'files').glob('*.meta')
                if json.loads(p.read_text()).get('kind') == 'event_batch']


class GoogleStore:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()

    def _service(self, name, version, allowlist=False):
        from google.oauth2.credentials import Credentials
        from google.oauth2.service_account import Credentials as ServiceCredentials
        from googleapiclient.discovery import build
        from googleapiclient.http import build_http
        from google_auth_httplib2 import AuthorizedHttp
        path = Path(self.config.allowlist_credentials if allowlist and self.config.allowlist_credentials else self.config.host_credentials).expanduser()
        if path.stat().st_mode & 0o077:
            raise StoreUnavailable('Credential files must have permissions 0600')
        info = json.loads(path.read_text())
        credentials = (ServiceCredentials.from_service_account_info(info, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
                       if info.get('type') == 'service_account' else Credentials.from_authorized_user_info(info))
        # Google's transport leaves resumable-upload 308 responses for the API
        # client instead of treating them as redirects requiring Location.
        http = build_http()
        http.timeout = 10
        return build(name, version, http=AuthorizedHttp(credentials, http=http), cache_discovery=False)

    def allowlist(self):
        values = self._service('sheets','v4',True).spreadsheets().values()
        if self.config.allowlist_tabs:
            ranges = ["'" + tab.replace("'", "''") + "'!A:Z" for tab in self.config.allowlist_tabs]
            result = values.batchGet(spreadsheetId=self.config.allowlist_sheet_id,
                                     ranges=ranges, majorDimension='ROWS').execute().get('valueRanges', [])
            if len(result) != len(ranges):
                raise StoreUnavailable('Google did not return every configured staff worksheet')
            return {tab: result[index].get('values', []) for index, tab in enumerate(self.config.allowlist_tabs)}
        return values.get(
            spreadsheetId=self.config.allowlist_sheet_id, range=self.config.allowlist_range).execute().get('values', [])

    def new_id(self):
        return self._service('drive','v3').files().generateIds(count=1, space='drive', type='files').execute()['ids'][0]

    def put_file(self, identity, path, name, digest, metadata=None):
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
        drive = self._service('drive','v3')
        properties = {'postern': '1', 'sha256': digest, **{k:str(v) for k,v in (metadata or {}).items()}}
        folder = self.config.selfie_folder_id if properties.get('kind') == 'selfie' else self.config.drive_folder_id
        if not folder:
            raise StoreUnavailable('A private storage folder is required')
        try:
            drive.files().create(body={'id': identity, 'name': name, 'parents': [folder], 'appProperties': properties},
                media_body=MediaFileUpload(str(path), resumable=True, chunksize=8*1024*1024), fields='id').execute()
        except HttpError as error:
            if error.resp.status != 409:
                raise
        # Verify bytes even after an ambiguous prior acknowledgement or conflict.
        self.verify(identity, digest)
        return identity

    def delete_selfie(self, identity):
        from googleapiclient.errors import HttpError
        drive = self._service('drive', 'v3')
        try:
            metadata = drive.files().get(fileId=identity, fields='parents,appProperties').execute()
            if (not self.config.selfie_folder_id or self.config.selfie_folder_id not in metadata.get('parents', [])
                    or metadata.get('appProperties', {}).get('kind') != 'selfie'):
                raise StoreUnavailable('Refusing to remove a non-selfie object')
            drive.files().delete(fileId=identity).execute()
        except HttpError as error:
            if error.resp.status != 404:
                raise

    def verify(self, identity, digest):
        from googleapiclient.http import MediaIoBaseDownload
        sink = HashSink(max(self.config.upload_limit, self.config.video_upload_limit) + self.config.derive_output_limit)
        download = MediaIoBaseDownload(sink, self._service('drive','v3').files().get_media(fileId=identity), chunksize=8*1024*1024)
        done = False
        while not done:
            _, done = download.next_chunk()
        if sink.hasher.hexdigest() != digest:
            raise StoreUnavailable('The stored original could not be confirmed')

    def download(self, identity, destination, digest=None, limit=None):
        from googleapiclient.http import MediaIoBaseDownload
        with Path(destination).open('wb') as output:
            Path(destination).chmod(0o600)
            download = MediaIoBaseDownload(output, self._service('drive','v3').files().get_media(fileId=identity), chunksize=1024*1024)
            done = False
            while not done:
                _, done = download.next_chunk()
                if output.tell() > (limit if limit is not None else max(self.config.upload_limit, self.config.video_upload_limit) + self.config.derive_output_limit):
                    raise StoreUnavailable('Remote file exceeds its expected bound')
        if digest and file_digest(destination) != digest:
            raise StoreUnavailable('Remote file checksum does not match')

    def _tabs(self, service):
        data = service.spreadsheets().get(spreadsheetId=self.config.database_sheet_id, fields='sheets.properties').execute()
        return {s['properties']['title']: s['properties']['sheetId'] for s in data['sheets']}

    def catalog(self):
        service = self._service('sheets','v4')
        result = {}
        for tab, columns in [('posts',POST_COLUMNS),('items',ITEM_COLUMNS)]:
            values = service.spreadsheets().values().get(spreadsheetId=self.config.database_sheet_id, range=tab+'!A:Z').execute().get('values', [])
            if not values or values[0] != columns:
                raise StoreUnavailable('Postern database headers do not match the schema')
            data = []
            for row in values[1:]:
                obj = dict(zip(columns,row))
                for key in ['revision','bytes','pages','sort']:
                    if key in obj: obj[key] = int(obj[key])
                for key in ['created_at','published_at']:
                    if key in obj: obj[key] = float(obj[key]) if obj[key] != '' else None
                data.append(obj)
            result[tab] = data
        return result

    def commit_publication(self, post, items):
        # One worker serializes catalog commits; the two tab changes are atomic.
        with self.lock:
            service = self._service('sheets','v4')
            catalog = self.catalog()
            old = next((r for r in catalog['posts'] if r['id'] == post['id']), None)
            if old and old['revision'] > post['revision']:
                raise StoreUnavailable('A newer publication revision already exists')
            catalog['posts'] = [r for r in catalog['posts'] if r['id'] != post['id']] + [post]
            catalog['items'] = [r for r in catalog['items'] if r['post_id'] != post['id']] + items
            tabs = self._tabs(service)
            requests = []
            for tab, columns in [('posts',POST_COLUMNS),('items',ITEM_COLUMNS)]:
                matrix = [columns] + [[r.get(c, '') if r.get(c) is not None else '' for c in columns] for r in catalog[tab]]
                def cell(v):
                    return {'userEnteredValue': {'numberValue': v} if isinstance(v,(int,float)) else {'stringValue': str(v)}}
                requests.append({'updateCells': {'range': {'sheetId': tabs[tab]}, 'rows':[{'values':[cell(v) for v in row]} for row in matrix], 'fields':'userEnteredValue'}})
            service.spreadsheets().batchUpdate(spreadsheetId=self.config.database_sheet_id, body={'requests':requests}).execute()
            confirmed = self.catalog()
            actual = next((r for r in confirmed['posts'] if r['id'] == post['id']), None)
            if not actual or actual.get('publish_op_id') != post['publish_op_id'] or actual['revision'] != post['revision']:
                raise StoreUnavailable('Publication acknowledgement could not be verified')

    def append_summary(self, events):
        # Yearly summary spreadsheets are app-created and discovered by appProperties.
        from datetime import datetime, timezone
        drive = self._service('drive','v3');sheets = self._service('sheets','v4')
        for year in sorted({datetime.fromtimestamp(e['ts'], timezone.utc).year for e in events}):
            q = f"trashed=false and appProperties has {{ key='postern_log_year' and value='{year}' }}"
            files = drive.files().list(q=q, fields='files(id)', pageSize=100).execute().get('files',[])
            if files:
                identity = files[0]['id']
            else:
                identity = drive.files().create(body={'name':f'Postern Log {year}', 'mimeType':'application/vnd.google-apps.spreadsheet', 'parents':[self.config.drive_folder_id], 'appProperties':{'postern_log_year':str(year)}},fields='id').execute()['id']
                sheets.spreadsheets().values().update(spreadsheetId=identity, range='A1', valueInputOption='RAW',body={'values':[EVENT_COLUMNS]}).execute()
            values = sheets.spreadsheets().values().get(spreadsheetId=identity,range='A:A').execute().get('values',[])
            known = {r[0] for r in values if r}
            pending = [e for e in events if datetime.fromtimestamp(e['ts'],timezone.utc).year == year and e['event_id'] not in known]
            if pending:
                matrix = [[json.dumps(e.get(c)) if c=='detail' else e.get(c) or '' for c in EVENT_COLUMNS] for e in pending]
                sheets.spreadsheets().values().append(spreadsheetId=identity,range='A:M',valueInputOption='RAW',body={'values':matrix}).execute()

    def selfie_relocations(self):
        """Resolve copied portraits without rewriting immutable enrollment events."""
        if not self.config.selfie_folder_id:
            return {}
        drive = self._service('drive', 'v3')
        result = {}; page = None
        while True:
            response = drive.files().list(
                q=f"'{self.config.selfie_folder_id}' in parents and trashed=false",
                fields='nextPageToken,files(id,appProperties)', pageToken=page, pageSize=1000).execute()
            for file in response.get('files', []):
                props = file.get('appProperties', {})
                source = props.get('postern_migration_source')
                if props.get('kind') != 'selfie' or not source:
                    continue
                if source in result:
                    raise StoreUnavailable('Multiple migrated portraits claim the same source')
                if not props.get('sha256'):
                    raise StoreUnavailable('Migrated portrait is missing its checksum')
                result[source] = {'id': file['id'], 'sha256': props['sha256']}
            page = response.get('nextPageToken')
            if not page:
                return result

    def batches(self):
        drive = self._service('drive','v3');result=[];page=None
        while True:
            response=drive.files().list(q=f"'{self.config.drive_folder_id}' in parents and trashed=false and appProperties has {{ key='kind' and value='event_batch' }}",fields='nextPageToken,files(id,appProperties)',pageToken=page,pageSize=1000).execute()
            result += [{'id':r['id'], **r.get('appProperties',{})} for r in response.get('files',[])]
            page=response.get('nextPageToken')
            if not page:return result
