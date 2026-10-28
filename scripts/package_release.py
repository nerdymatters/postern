"""Build a source + compiled-web pilot bundle from a clean tagged checkout."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]
# Explicit product directories keep private workspace data and developer tools out.
RELEASE_ROOTS = {'postern', 'config', 'ops', 'scripts', 'tests', 'docs', 'dashboard', 'status', '.github'}
RELEASE_FILES = {'.gitignore', 'README.md', 'CHANGELOG.md', 'ROADMAP.md', 'TODOS.md', 'VERSION', 'pyproject.toml', 'requirements-pilot.lock',
                 'vercel.json', 'LICENSE', 'LICENSE-MIT', 'LICENSE-APACHE', 'NOTICE', 'CONTRIBUTING.md', 'SECURITY.md'}


def release_files(tracked):
    """Tracked source paths that belong in the release bundle."""
    return [p for p in tracked if p and (Path(p).parts[0] in RELEASE_ROOTS or p in RELEASE_FILES)]


def public_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
    """Keep release entries portable and omit the builder's local account metadata."""
    path = PurePosixPath(member.name)
    if not member.name or path.is_absolute() or '..' in path.parts or '\\' in member.name:
        raise ValueError(f'Unsafe release path: {member.name}')
    if not member.isfile():
        raise ValueError(f'Release entries must be regular files: {member.name}')
    member.uid = member.gid = 0
    member.uname = member.gname = ''
    member.mtime = int(member.mtime)
    # tarfile will regenerate necessary long-path PAX headers from the safe name.
    member.pax_headers = {}
    return member


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.context' / 'release')
    args = parser.parse_args()
    version = (ROOT / 'VERSION').read_text().strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise SystemExit('VERSION must be a three-part release version')
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).strip():
        raise SystemExit('Commit all source changes before packaging')
    if json.loads((ROOT / 'dashboard/web/package.json').read_text())['version'] != version:
        raise SystemExit('Web version differs from VERSION')
    if not re.search(r'^version = "' + re.escape(version) + r'"$',
                     (ROOT / 'pyproject.toml').read_text(), re.M):
        raise SystemExit('Python version differs from VERSION')
    subprocess.run(['bun', 'install', '--frozen-lockfile'], cwd=ROOT / 'dashboard/web', check=True)
    subprocess.run(['bun', 'run', 'build'], cwd=ROOT / 'dashboard/web', check=True)
    args.output.mkdir(parents=True, exist_ok=True)
    prefix = f'postern-{version}'
    destination = args.output / f'{prefix}-macos-pilot.tar.gz'
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    selected = release_files(tracked)
    selected += [str(p.relative_to(ROOT)) for p in (ROOT / 'dashboard/web/dist').rglob('*') if p.is_file()]
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip()
    with tarfile.open(destination, 'w:gz') as archive:
        for name in sorted(set(selected)):
            path = ROOT / name
            if path.is_symlink():
                raise SystemExit(f'Release inputs must not be symlinks: {name}')
            archive.add(path, arcname=f'{prefix}/{name}', recursive=False, filter=public_member)
        metadata = (json.dumps({'version': version, 'commit': revision}, indent=2) + '\n').encode()
        member = tarfile.TarInfo(f'{prefix}/RELEASE.json')
        member.size = len(metadata)
        member.mode = 0o644
        archive.addfile(member, io.BytesIO(metadata))
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    (args.output / 'SHA256SUMS').write_text(f'{digest}  {destination.name}\n')
    print(f'Packaged {destination} from {revision}')


if __name__ == '__main__':
    main()
