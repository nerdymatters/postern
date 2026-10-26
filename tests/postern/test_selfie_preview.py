"""Preview preparation keeps runtime settings out of the persisted constructor config."""
import json
from unittest.mock import Mock

from postern.config import Config
from scripts import selfie_preview as preview


def test_prepare_persists_reloadable_isolated_config_without_runtime_branding(tmp_path, monkeypatch):
    root = tmp_path / 'preview'
    config_path = root / 'config.json'
    monkeypatch.setattr(preview, 'PREVIEW', root)
    monkeypatch.setattr(preview, 'CONFIG', config_path)
    host = tmp_path / 'host.json'
    host.write_text(json.dumps({'data_dir': str(tmp_path / 'live'), 'host_credentials': 'private-credentials.json',
                               'drive_folder_id': 'live-root', 'selfie_folder_id': 'live-selfies',
                               'database_sheet_id': 'live-catalog'}))
    original = host.read_bytes()
    store = Mock()
    monkeypatch.setattr(preview, 'GoogleStore', store)
    create = Mock(side_effect=[('preview-root', True), ('preview-selfies', True), ('preview-catalog', True)])
    monkeypatch.setattr(preview, 'find_or_create', create)
    prepare_database = Mock()
    monkeypatch.setattr(preview, 'prepare_database', prepare_database)

    preview.prepare(host)

    saved = json.loads(config_path.read_text())
    assert 'branding' not in saved
    assert config_path.stat().st_mode & 0o777 == 0o600
    restored = preview.isolated(Config.load(config_path))
    assert (restored.drive_folder_id, restored.selfie_folder_id, restored.database_sheet_id) == (
        'preview-root', 'preview-selfies', 'preview-catalog')
    assert restored.mode == 'demo' and restored.branding.certificate_prefix == 'PT'
    assert create.call_count == 3 and prepare_database.call_count == 1

    # Repeating prepare reuses only the isolated config; it cannot create another cloud tree.
    preview.prepare(host)
    assert store.call_count == 1 and create.call_count == 3 and prepare_database.call_count == 1
    assert host.read_bytes() == original
