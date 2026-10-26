"""Isolated, synthetic selfie preview. Never load the production Service.

Prepare: python -m scripts.selfie_preview prepare --host-config ~/.config/postern/config.json
Serve:   python -m scripts.selfie_preview serve
The optional public HTTPS tunnel MUST be protected by its own password gateway.
"""
import argparse
from dataclasses import fields
import json
from pathlib import Path

from postern.bootstrap import FOLDER, find_or_create, prepare_database
from postern.config import Config, private_json
from postern.storage import GoogleStore

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT/'.context'/'selfie-preview'
CONFIG = PREVIEW/'config.json'
PORT = 8801


class PreviewStore(GoogleStore):
    def allowlist(self):
        return [['email','name','role'], ['admin@example.invalid','Preview administrator','admin'],
                ['reader@example.invalid','Preview reader','reader']]


def isolated(config):
    if not config.preview or not config.selfie_enabled or config.mode != 'demo':
        raise ValueError('This command requires synthetic preview mode')
    if Path(config.data_dir).resolve() != (PREVIEW/'data').resolve() or config.port != PORT:
        raise ValueError('Preview data and port must stay isolated')
    if Path(config.static_dir).resolve() != (PREVIEW/'web').resolve():
        raise ValueError('Use the separate preview frontend build')
    if config.allowlist_sheet_id or config.administrators:
        raise ValueError('Preview must not use staff allowlists or identities')
    if not all((config.drive_folder_id, config.database_sheet_id, config.selfie_folder_id)):
        raise ValueError('Prepare dedicated private preview storage first')
    return config


def prepare(host_path):
    host = json.loads(Path(host_path).expanduser().read_text())
    if CONFIG.exists():
        config = isolated(Config.load(CONFIG))
    else:
        draft = Config(data_dir=PREVIEW/'data', mode='demo', host_credentials=host['host_credentials'])
        store = GoogleStore(draft)
        drive, sheets = store._service('drive', 'v3'), store._service('sheets', 'v4')
        folder, _ = find_or_create(drive, 'Postern Isolated Selfie Preview', FOLDER)
        selfies, _ = find_or_create(drive, 'Private Selfies', FOLDER, folder)
        catalog, _ = find_or_create(drive, 'Preview Catalog', 'application/vnd.google-apps.spreadsheet', folder)
        forbidden = {host.get(key) for key in ('drive_folder_id', 'database_sheet_id', 'selfie_folder_id')}
        if forbidden.intersection({folder, selfies, catalog}):
            raise ValueError('Preview storage overlaps production')
        prepare_database(sheets, catalog)
        config = Config(data_dir=PREVIEW/'data', mode='demo', host_credentials=host['host_credentials'],
                        drive_folder_id=folder, database_sheet_id=catalog, selfie_folder_id=selfies,
                        selfie_enabled=True, preview=True, port=PORT, converter_workers=1,
                        static_dir=str(PREVIEW/'web'), origins=['http://127.0.0.1:8801'],
                        notice_version='preview-1')
        values = {field.name: getattr(config, field.name) for field in fields(config) if field.init}
        private_json(CONFIG, {**values, 'data_dir': str(config.data_dir)})
    forbidden = {host.get(key) for key in ('drive_folder_id', 'database_sheet_id', 'selfie_folder_id')}
    if forbidden.intersection({config.drive_folder_id, config.database_sheet_id, config.selfie_folder_id}):
        raise ValueError('Preview storage overlaps production')
    if Path(host['data_dir']).expanduser().resolve() == config.data_dir.resolve():
        raise ValueError('Preview data overlaps production')
    isolated(config)
    print('Dedicated preview storage ready; production configuration was read only.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'serve'])
    parser.add_argument('--host-config', default='~/.config/postern/config.json')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.host_config)
    else:
        import uvicorn
        from postern.app import create_app
        from postern.service import Service
        config = isolated(Config.load(CONFIG))
        if not (Path(config.static_dir)/'index.html').is_file():
            raise ValueError('Build the separate preview frontend before serving')
        store = PreviewStore(config)
        # Require the expected app-owned preview root; never follow a swapped config into production.
        folder = store._service('drive','v3').files().get(fileId=config.drive_folder_id, fields='name,appProperties').execute()
        if folder.get('name') != 'Postern Isolated Selfie Preview' or folder.get('appProperties',{}).get('postern') != '1':
            raise ValueError('Unexpected preview storage root')
        for identity in (config.selfie_folder_id, config.database_sheet_id):
            metadata = store._service('drive','v3').files().get(fileId=identity, fields='parents').execute()
            if config.drive_folder_id not in metadata.get('parents',[]):
                raise ValueError('All preview storage must belong to its dedicated root')
        uvicorn.run(create_app(config, service=Service(config, store=store)), host='127.0.0.1', port=PORT)


if __name__ == '__main__':
    main()
