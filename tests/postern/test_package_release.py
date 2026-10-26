"""Public release archives must not disclose the build host's account metadata."""
import io
import tarfile

import pytest

from scripts.package_release import public_member


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
