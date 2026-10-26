from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import os
from .branding import Branding, IP_NOTICE, load as load_branding

NOTICE = ('Postern records your sign-ins, documents opened, device information and IP address. '
          'Administrators can see this activity, including video sections played. Pages and the video player display your email and the time. '
          'Please keep staff material confidential. Opening a document is recorded when its content is served; '
          'this does not prove it was read.')


IP_NOTICE_VERSION = 'ip-1'


@dataclass
class Config:
    data_dir: Path
    mode: str = 'google'
    origins: list[str] = field(default_factory=lambda: ['http://localhost:5173', 'http://127.0.0.1:5173'])
    google_client_id: str = ''
    host_credentials: str = ''
    allowlist_credentials: str = ''
    allowlist_sheet_id: str = ''
    allowlist_range: str = 'A:Z'
    allowlist_tabs: list[str] = field(default_factory=list)
    email_header: str = 'email'
    administrators: list[str] = field(default_factory=list)
    drive_folder_id: str = ''
    database_sheet_id: str = ''
    notice_version: str = '2'
    ip_notice_version: str = IP_NOTICE_VERSION
    notice: str = NOTICE
    trust_loopback_proxy: bool = False
    recovery_mode: bool = False
    port: int = 8790
    converter_workers: int = 2
    upload_limit: int = 100 * 1024 * 1024
    video_upload_limit: int = 2 * 1024 * 1024 * 1024
    derivative_budget: int = 20 * 1024 * 1024 * 1024
    free_floor: int = 1024 * 1024 * 1024
    max_pages: int = 200
    max_pixels: int = 40_000_000
    derive_output_limit: int = 256 * 1024 * 1024
    derive_timeout: int = 90
    derive_memory_limit: int = 2 * 1024 * 1024 * 1024
    static_dir: str = ''
    selfie_enabled: bool = False
    selfie_folder_id: str = ''
    selfie_retention_days: int = 90
    selfie_notice_version: str = 'selfie-1'
    preview: bool = False
    branding: Branding = field(init=False, default_factory=Branding)

    def validate(self):
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        self.branding = load_branding(self.data_dir)
        if (not isinstance(self.allowlist_tabs, list) or
                any(not isinstance(tab, str) or not tab.strip() for tab in self.allowlist_tabs) or
                len(set(self.allowlist_tabs)) != len(self.allowlist_tabs)):
            raise ValueError('allowlist_tabs must be a list of distinct worksheet names')
        if self.mode not in ('google', 'demo'):
            raise ValueError('mode must be google or demo')
        if self.mode == 'google' and not all([self.google_client_id, self.host_credentials,
                                            self.allowlist_sheet_id, self.drive_folder_id, self.database_sheet_id]):
            raise ValueError('Google client, host credentials, allowlist Sheet, Drive folder and database Sheet are required')
        if not self.origins or any(o == '*' or not o.startswith(('http://localhost:', 'http://127.0.0.1:', 'https://')) for o in self.origins):
            raise ValueError('Configure exact HTTPS application origins (or local development origins)')
        from .auth import canonical_email
        # The Sheet decides who may enter; this only raises named members to administrator, so a
        # hand-made list with no role column can still have an owner.
        self.administrators = [canonical_email(value) for value in self.administrators]
        if not 1 <= self.converter_workers <= 2:
            raise ValueError('converter_workers must be one or two')
        if not 0 <= self.selfie_retention_days <= 365:
            raise ValueError('selfie_retention_days must be between 0 and 365')
        if self.selfie_enabled and self.mode == 'google' and not self.selfie_folder_id:
            raise ValueError('Configure a private selfie folder before enabling selfies')
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        for name in ('staging', 'derived', 'composites', 'exports', 'selfies'):
            (self.data_dir / name).mkdir(exist_ok=True, mode=0o700)
        return self

    @classmethod
    def load(cls, path):
        values = json.loads(Path(path).expanduser().read_text())
        return cls(**values).validate()


def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as output:
        os.chmod(temporary, 0o600)
        json.dump(data, output, indent=2)
        output.write('\n')
    temporary.replace(path)
