"""Config.validate: the deployment settings that have to be refused before the service can start.

Every one of these would otherwise fail later and quieter: a wildcard origin lets any site call the API
with a reader's token, a third converter worker breaks the memory bound, and a Google mode without its
ids would only be noticed at the first refresh.
"""
from __future__ import annotations
import json
import stat

import pytest

from postern.config import Config

GOOGLE = dict(mode='google', google_client_id='web-client', host_credentials='~/.config/postern/host.json',
              allowlist_sheet_id='sheet', drive_folder_id='folder', database_sheet_id='db')


def mode_of(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize('settings,complaint', [
    ({'mode': 'staging'}, 'mode must be google or demo'),
    ({**GOOGLE, 'database_sheet_id': ''}, 'database Sheet'),
    ({**GOOGLE, 'host_credentials': ''}, 'host credentials'),
    ({**GOOGLE, 'google_client_id': ''}, 'Google client'),
    ({'origins': ['*']}, 'origins'),
    ({'origins': []}, 'origins'),
    ({'origins': ['http://postern.example']}, 'origins'),
    ({'origins': ['https://postern.example', 'http://intranet:8080']}, 'origins'),
    ({'converter_workers': 0}, 'converter_workers'),
    ({'converter_workers': 3}, 'converter_workers'),
    ({'administrators': ['not an address']}, 'email'),
], ids=['unknown mode', 'google without database sheet', 'google without host credentials', 'google without client id',
        'wildcard origin', 'no origins', 'plain http origin', 'one bad origin among good ones',
        'zero converter workers', 'three converter workers', 'malformed administrator'])
def test_a_misconfiguration_is_refused_before_anything_starts(tmp_path, settings, complaint):
    with pytest.raises(ValueError) as refused:
        Config(data_dir=tmp_path / 'data', **{'mode': 'demo', **settings}).validate()
    assert complaint.lower() in str(refused.value).lower()


def test_a_valid_configuration_prepares_a_private_data_directory(tmp_path):
    settings = {'data_dir': str(tmp_path / 'nested' / 'data'), 'mode': 'demo', 'port': 8791,
                'origins': ['https://postern.example', 'http://localhost:5173', 'http://127.0.0.1:5173'],
                'administrators': [' Owner@Example.Invalid ', 'First.Last+postern@GMail.com']}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(settings))

    config = Config.load(path)

    assert config.data_dir == (tmp_path / 'nested' / 'data').resolve() and config.port == 8791
    assert config.administrators == ['owner@example.invalid', 'firstlast@gmail.com']
    assert mode_of(config.data_dir) == 0o700
    for name in ('staging', 'derived', 'composites', 'exports'):
        assert (config.data_dir / name).is_dir() and mode_of(config.data_dir / name) == 0o700, name
    # Validation is repeatable and never widens what an operator narrowed by hand.
    (config.data_dir / 'staging').chmod(0o700)
    assert config.validate() is config and mode_of(config.data_dir) == 0o700
    # The Google prerequisites are only demanded of Google mode.
    assert Config(data_dir=tmp_path / 'google', **GOOGLE).validate().mode == 'google'
