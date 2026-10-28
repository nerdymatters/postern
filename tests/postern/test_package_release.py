"""Public release archives must not disclose the build host's account metadata,
and must contain every file their bundled docs link to."""
import io
import posixpath
import re
import subprocess
import tarfile

import pytest

from scripts.package_release import ROOT, public_member, release_files

LINK = re.compile(r'\]\(([^)\s]+)\)')


def test_bundled_docs_link_only_to_bundled_files():
    # README, TODOS and docs/accounts.md linked to ROADMAP.md while the bundle left it out.
    try:
        listed = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        listed = b''
    tracked = listed.decode().split('\0')
    # An extracted release lists nothing, even inside another repository, so there is nothing to compare.
    if 'scripts/package_release.py' not in tracked:
        pytest.skip('needs a git checkout of this repository')
    bundled = set(release_files(tracked))
    missing = []
    for name in sorted(p for p in bundled if p.endswith('.md')):
        for target in LINK.findall((ROOT / name).read_text(encoding='utf-8')):
            path = target.split('#', 1)[0]
            if not path or re.match(r'[a-z][a-z0-9+.-]*:', path, re.I):
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), path))
            if resolved not in bundled and not any(p.startswith(resolved + '/') for p in bundled):
                missing.append(f'{name} -> {target}')
    assert missing == []


# Value: protects=release_files() keeps product-root paths and listed root files (incl. ROADMAP.md) and drops
#   other tracked paths and empty entries; fails_when=a wider filter (all .md, dotfiles, prefix or basename
#   match) ships CLAUDE.md or docs-private/, '' isn't skipped, or ROADMAP.md, postern/ or .github/ drops out;
#   why_new=test_bundled_docs_link_only_to_bundled_files only proves linked files are kept; a keep-everything
#   filter still passes it; seam=none; no_claim=that main() uses release_files() or what the tarball holds
#   (literal path lists stand in for git ls-files; unit only)
@pytest.mark.parametrize('path,kept', [
    ('postern/db.py', True), ('.github/workflows/tests.yml', True), ('README.md', True), ('ROADMAP.md', True),
    ('CLAUDE.md', False), ('.gstack-redact-allowlist', False), ('notes.md', False),
    ('docs-private/README.md', False), ('', False),
])
def test_release_files_keep_only_product_paths(path, kept):
    assert release_files([path]) == ([path] if kept else [])


@pytest.mark.parametrize('name', ['postern-0.16.0/README.md',
                                  'postern-0.16.0/' + 'nested/' * 20 + 'README.md'])
def test_archive_headers_are_public_and_long_paths_round_trip(name):
    member = tarfile.TarInfo(name)
    member.uid, member.gid = 501, 20
    member.uname, member.gname = 'private-builder', 'private-group'
    member.mtime = 1_700_000_000.125
    member.pax_headers = {'uid': '501', 'uname': 'private-builder',
                          'SCHILY.xattr.user.comment': 'private-build-note',
                          'path': '/private/incorrect-path', 'mtime': '1700000000.125'}
    member.size = 4
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        archive.addfile(public_member(member), io.BytesIO(b'test'))

    with tarfile.open(fileobj=io.BytesIO(output.getvalue()), mode='r') as archive:
        actual = archive.getmembers()[0]
        assert actual.name == name
        assert (actual.uid, actual.gid, actual.uname, actual.gname) == (0, 0, '', '')
        assert actual.mtime == 1_700_000_000
        assert set(actual.pax_headers) <= {'path'}
        assert archive.extractfile(actual).read() == b'test'
    assert b'private-builder' not in output.getvalue()
    assert b'private-group' not in output.getvalue()
    assert b'private-build-note' not in output.getvalue()


@pytest.mark.parametrize('name', ['/absolute/file', '../outside',
                                  'postern/../outside', 'postern\\..\\outside', ''])
def test_archive_rejects_unsafe_paths(name):
    with pytest.raises(ValueError, match='Unsafe release path'):
        public_member(tarfile.TarInfo(name))


@pytest.mark.parametrize('kind', [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_archive_rejects_links_and_special_files(kind):
    member = tarfile.TarInfo('postern-0.16.0/file')
    member.type = kind
    member.linkname = '../../private-file'
    with pytest.raises(ValueError, match='regular files'):
        public_member(member)
