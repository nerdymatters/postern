"""Object roots and descriptor integrity are independent of HTTP authorization."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path

import pytest

from postern.content_objects import (
    ContentObject, ContentObjectError, HARD_MAX_OBJECT_BYTES,
    LocalContentObjects, LocalContentPublisher,
)


SPACE = '00000000-0000-4000-8000-000000000001'
RESOURCE = '00000000-0000-4000-8000-000000000002'


def descriptor(data=b'private page', **changes):
    return replace(ContentObject(SPACE, RESOURCE, 1, 'page', 0,
                                 hashlib.sha256(data).hexdigest(), len(data)), **changes)


@pytest.fixture
def root(tmp_path):
    directory = tmp_path.resolve() / 'objects'
    directory.mkdir(mode=0o700)
    return directory


def test_verified_create_only_publication_and_read_only_runtime(root):
    item = descriptor()
    with LocalContentPublisher(root) as publisher:
        assert publisher.publish(item, b'private page') is True
        assert publisher.publish(item, b'private page') is False
    with LocalContentObjects(root) as reader:
        assert reader.read(item) == b'private page'
        assert not hasattr(reader, 'publish')
    path = root / item.key
    assert path.stat().st_mode & 0o777 == 0o400
    assert not list(root.rglob('.pending-*'))
    assert item.key == f'{SPACE}/{RESOURCE}/1/page/0/{item.sha256}'


def test_cross_space_objects_are_independent_even_when_hash_matches(root):
    first = descriptor()
    second = replace(first, space_id='00000000-0000-4000-8000-000000000003')
    with LocalContentPublisher(root) as publisher:
        publisher.publish(first, b'private page')
        with pytest.raises(ContentObjectError):
            publisher.read(second)
        publisher.publish(second, b'private page')
        assert publisher.read(second) == publisher.read(first)
    assert (root / first.key).stat().st_ino != (root / second.key).stat().st_ino


def test_private_objects_below_search_only_ancestor(tmp_path):
    ancestor = tmp_path / 'traversable-not-listable'
    ancestor.mkdir(mode=0o700)
    root = ancestor / 'objects'
    root.mkdir(mode=0o700)
    ancestor.chmod(0o100)
    try:
        with pytest.raises(PermissionError):
            os.listdir(ancestor)
        item = descriptor()
        with LocalContentPublisher(root) as publisher:
            assert publisher.publish(item, b'private page') is True
        with LocalContentObjects(root) as reader:
            assert reader.read(item) == b'private page'
        assert root.stat().st_mode & 0o777 == 0o700
    finally:
        ancestor.chmod(0o700)


def test_original_is_explicit_offline_inventory_with_legacy_key(root):
    item = descriptor(representation='original')
    assert item.key == f'{SPACE}/{RESOURCE}/1/{item.sha256}'
    with LocalContentPublisher(root) as offline:
        offline.publish(item, b'private page')
        assert offline.read(item) == b'private page'
    with pytest.raises(ContentObjectError):
        replace(item, index=1)


@pytest.mark.parametrize('changes', [
    {'space_id': '../escape'}, {'resource_id': RESOURCE.upper().replace('8000', 'ABCD')},
    {'space_id': SPACE.replace('-', '')}, {'revision': 0}, {'revision': True},
    {'revision': 2**63}, {'representation': '../page'}, {'representation': 'unknown'},
    {'index': -1}, {'index': False}, {'index': 2**31}, {'sha256': 'A' * 64},
    {'sha256': '../' + 'a' * 61}, {'size': 0}, {'size': True},
    {'size': HARD_MAX_OBJECT_BYTES + 1},
])
def test_invalid_descriptor_refused(changes):
    with pytest.raises(ContentObjectError):
        descriptor(**changes)


def test_configured_size_bound_and_publish_hash_refuse_before_writing(root):
    with LocalContentPublisher(root, max_object_bytes=8) as publisher:
        with pytest.raises(ContentObjectError):
            publisher.publish(descriptor(), b'private page')
    with LocalContentPublisher(root) as publisher:
        with pytest.raises(ContentObjectError):
            publisher.publish(descriptor(), b'PRIVATE PAGE')
    assert list(root.iterdir()) == []
    with pytest.raises(ContentObjectError):
        LocalContentObjects(root, max_object_bytes=HARD_MAX_OBJECT_BYTES + 1)


def test_missing_root_and_symlink_root_or_ancestor_refused(root):
    with pytest.raises(ContentObjectError):
        LocalContentObjects(root / 'missing')
    alias = root.parent / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ContentObjectError):
        LocalContentObjects(alias)
    nested = root / 'nested'
    nested.mkdir(mode=0o700)
    with pytest.raises(ContentObjectError):
        LocalContentObjects(alias / 'nested')
    with pytest.raises(ContentObjectError):
        LocalContentObjects(Path('relative'))
    with pytest.raises(ContentObjectError):
        LocalContentObjects(root / '..' / 'objects')


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'directory', 'hardlink', 'public', 'writable', 'short', 'long', 'changed'])
def test_untrusted_object_refused_without_repair(root, kind):
    item = descriptor()
    with LocalContentPublisher(root) as publisher:
        publisher.publish(item, b'private page')
        path = root / item.key
        path.unlink()
        if kind == 'symlink':
            outside = root.parent / 'outside'
            outside.write_bytes(b'private page')
            path.symlink_to(outside)
        elif kind == 'fifo':
            os.mkfifo(path, mode=0o600)
        elif kind == 'directory':
            path.mkdir(mode=0o700)
        elif kind == 'hardlink':
            outside = root.parent / 'outside'
            outside.write_bytes(b'private page')
            outside.chmod(0o400)
            os.link(outside, path)
        else:
            payload = {'short': b'private', 'long': b'private page!',
                       'changed': b'PRIVATE PAGE'}.get(kind, b'private page')
            path.write_bytes(payload)
            path.chmod(0o644 if kind == 'public' else 0o600 if kind == 'writable' else 0o400)
        info = path.lstat()
        with pytest.raises(ContentObjectError):
            publisher.read(item)
        with pytest.raises(ContentObjectError):
            publisher.publish(item, b'private page')
        assert path.lstat().st_ino == info.st_ino
        assert not list(root.rglob('.pending-*'))


def test_symlinked_internal_component_and_nonprivate_directory_refused(root):
    item = descriptor()
    with LocalContentPublisher(root) as publisher:
        publisher.publish(item, b'private page')
        space = root / SPACE
        moved = root / 'saved-space'
        space.rename(moved)
        space.symlink_to(moved, target_is_directory=True)
        with pytest.raises(ContentObjectError):
            publisher.read(item)
        with pytest.raises(ContentObjectError):
            publisher.publish(item, b'private page')
        space.unlink()
        moved.rename(space)
        space.chmod(0o750)
        with pytest.raises(ContentObjectError):
            publisher.read(item)


def test_root_permissions_checked_on_open_and_every_read(root):
    root.chmod(0o750)
    with pytest.raises(ContentObjectError):
        LocalContentObjects(root)
    root.chmod(0o700)
    with LocalContentPublisher(root) as publisher:
        publisher.publish(descriptor(), b'private page')
        root.chmod(0o755)
        with pytest.raises(ContentObjectError):
            publisher.read(descriptor())


def test_reader_stays_anchored_across_root_path_replacement(root):
    item = descriptor()
    with LocalContentPublisher(root) as publisher:
        publisher.publish(item, b'private page')
    with LocalContentObjects(root) as reader:
        root.rename(root.parent / 'original-root')
        root.mkdir(mode=0o700)
        assert reader.read(item) == b'private page'
        reader.close()
        reader.close()
        with pytest.raises(ContentObjectError):
            reader.read(item)


def test_read_detects_change_during_read_and_never_returns_unverified_bytes(root, monkeypatch):
    item = descriptor()
    with LocalContentPublisher(root) as publisher:
        publisher.publish(item, b'private page')
        path = root / item.key
        real_read = os.read
        changed = False

        def changing_read(fd, length):
            nonlocal changed
            result = real_read(fd, length)
            if not changed:
                changed = True
                path.chmod(0o600)
                path.write_bytes(b'PRIVATE PAGE')
            return result

        monkeypatch.setattr(os, 'read', changing_read)
        with pytest.raises(ContentObjectError):
            publisher.read(item)


def test_video_range_source_is_verified_as_a_whole_chunk(root):
    data = b'video chunk' * 1000
    item = descriptor(data, representation='video', index=3)
    with LocalContentPublisher(root) as publisher:
        publisher.publish(item, data)
        assert publisher.read(item)[20:40] == data[20:40]
        path = root / item.key
        path.chmod(0o600)
        # Even corruption outside the requested range must refuse that range.
        path.write_bytes(data[:-1] + b'!')
        with pytest.raises(ContentObjectError):
            publisher.read(item)
