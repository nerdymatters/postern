"""Installation identity kept outside code and release bundles.

Older releases ignore branding.json, so the file can be prepared before an
upgrade and retained during rollback without changing their configuration.
"""
from dataclasses import dataclass, fields
import json
from pathlib import Path
import re
import stat
import unicodedata


IP_NOTICE = ('Training materials belong to their respective rights holders. '
             'You may not copy, revise, update, or reproduce these materials without '
             'prior permission from the rights holder.')


@dataclass(frozen=True)
class Branding:
    organization_name: str = ''
    privacy_contact_email: str = ''
    ip_notice: str = IP_NOTICE
    default_group: str = 'Staff'
    certificate_prefix: str = 'PT'
    certificate_title: str = 'Training Certificate'

    def public(self):
        return {name: getattr(self, name) for name in
                ('organization_name', 'privacy_contact_email', 'certificate_title')}


def load(data_dir, *, required=False):
    path = Path(data_dir).expanduser() / 'branding.json'
    if not path.exists() and not path.is_symlink():
        if required:
            raise ValueError('Google mode requires a private branding.json; preserve existing notices, groups and certificate identity before upgrading')
        return Branding()
    if path.is_symlink() or not path.is_file():
        raise ValueError('branding.json must be a regular owner-only file')
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError('branding.json must have mode 0600')
    if path.stat().st_size > 16 * 1024:
        raise ValueError('branding.json must be at most 16 KiB')
    values = json.loads(path.read_text(encoding='utf-8'))
    allowed = {field.name for field in fields(Branding)}
    if not isinstance(values, dict) or values.keys() - allowed:
        raise ValueError('branding.json must contain only documented branding fields')
    limits = {'organization_name': 80, 'privacy_contact_email': 254, 'ip_notice': 4000,
              'default_group': 80, 'certificate_prefix': 12, 'certificate_title': 80}
    for name, value in values.items():
        if (not isinstance(value, str) or len(value) > limits[name] or value != value.strip() or
                any(unicodedata.category(char).startswith('C') for char in value)):
            raise ValueError(f'Invalid branding field: {name}')
        if name not in ('organization_name', 'privacy_contact_email') and not value:
            raise ValueError(f'Branding field must not be empty: {name}')
    result = Branding(**values)
    if not re.fullmatch(r'[A-Z][A-Z0-9]{0,11}', result.certificate_prefix):
        raise ValueError('certificate_prefix must be 1–12 uppercase ASCII letters/digits, starting with a letter')
    if result.privacy_contact_email and not re.fullmatch(
            r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}",
            result.privacy_contact_email):
        raise ValueError('privacy_contact_email must be an email address')
    return result
