"""Private, immutable content objects; no caller-supplied filesystem paths.

The online adapter receives only ``ContentObjectReader``. ``LocalContentPublisher``
is an offline preparation/restore capability, never an HTTP upload API. Video
objects are independently hashed chunks: verify the complete bounded chunk before
slicing a range. ``original`` is reserved for offline inventory/restore and uses
the existing objects-table key; content admission must never return it.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Protocol
from uuid import UUID


DEFAULT_MAX_OBJECT_BYTES = 8 * 1024 * 1024
HARD_MAX_OBJECT_BYTES = 64 * 1024 * 1024
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Ancestors need traversal authority, not directory-listing authority. Linux
# O_PATH and macOS O_SEARCH both keep traversal anchored to a verified descriptor.
_SEARCH = (getattr(os, 'O_PATH', getattr(os, 'O_SEARCH', os.O_RDONLY))
           | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class ContentObjectError(ValueError):
    """The private object is unavailable or does not match its descriptor."""


def _integer(value: int, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


@dataclass(frozen=True)
class ContentObject:
    space_id: str
    resource_id: str
    revision: int
    representation: str
    index: int
    sha256: str
    size: int

    def __post_init__(self):
        try:
            identifiers = all(type(v) is str and str(UUID(v)) == v
                              for v in (self.space_id, self.resource_id))
        except (ValueError, AttributeError, TypeError):
            identifiers = False
        if not (identifiers and _integer(self.revision, 1, 2**63 - 1)
                and type(self.representation) is str
                and self.representation in {'page', 'thumbnail', 'video', 'training', 'original'}
                and _integer(self.index, 0, 2**31 - 1)
                and (self.representation != 'original' or self.index == 0)
                and type(self.sha256) is str
                and re.fullmatch('[0-9a-f]{64}', self.sha256)
                and _integer(self.size, 1, HARD_MAX_OBJECT_BYTES)):
            raise ContentObjectError('invalid content descriptor')

    @property
    def parts(self) -> tuple[str, ...]:
        prefix = (self.space_id, self.resource_id, str(self.revision))
        if self.representation == 'original':
            return (*prefix, self.sha256)
        return (*prefix, self.representation, str(self.index), self.sha256)

    @property
    def key(self) -> str:
        return '/'.join(self.parts)


class ContentObjectReader(Protocol):
    def read(self, descriptor: ContentObject) -> bytes: ...


def _private(info: os.stat_result, *, directory: bool) -> bool:
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    return (kind(info.st_mode) and info.st_uid == os.geteuid()
            and info.st_mode & 0o077 == 0
            and (directory or (info.st_nlink == 1 and info.st_mode & 0o222 == 0)))


def _open_root(root: str | os.PathLike[str]) -> int:
    # Do not resolve symlinks. Even trusted configuration must identify a real
    # private directory, whose descriptor remains anchored after a path rename.
    path = Path(root)
    if not path.is_absolute() or '..' in path.parts:
        raise ContentObjectError('private object root required')
    current = os.open('/', _SEARCH)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _SEARCH, dir_fd=current)
            os.close(current)
            current = child
            info = os.fstat(current)
            # Root-owned sticky temporary ancestors are safe when the final
            # root is owned by us. Other writable/untrusted ancestors are not.
            writable = info.st_mode & 0o022
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in {0, os.geteuid()} or (writable and not sticky_root):
                raise ContentObjectError('private object root required')
        if not _private(os.fstat(current), directory=True):
            raise ContentObjectError('private object root required')
        # Keep a normal readable descriptor for root reads and publication fsync.
        readable = os.open('.', _DIR, dir_fd=current)
        os.close(current)
        return readable
    except BaseException:
        os.close(current)
        raise


class LocalContentObjects:
    """Read-only, descriptor-anchored adapter for an existing private root.

    Call ``close`` when its owning service stops. Reads may be offloaded to a
    bounded IO executor; this class does not hold authorization or content caches.
    """

    def __init__(self, root: str | os.PathLike[str], *,
                 max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES):
        if not _integer(max_object_bytes, 1, HARD_MAX_OBJECT_BYTES):
            raise ContentObjectError('invalid object size bound')
        self.max_object_bytes = max_object_bytes
        self._lock = threading.Lock()
        self._root = -1
        try:
            self._root = _open_root(root)
        except (OSError, ValueError) as error:
            raise ContentObjectError('private object root unavailable') from error

    def close(self) -> None:
        with self._lock:
            if self._root >= 0:
                os.close(self._root)
                self._root = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _descriptor(self, descriptor: ContentObject) -> None:
        if type(descriptor) is not ContentObject:
            raise ContentObjectError('invalid content descriptor')
        descriptor.__post_init__()
        if descriptor.size > self.max_object_bytes:
            raise ContentObjectError('content object exceeds size bound')

    def _directory(self, descriptor: ContentObject, *, create: bool = False) -> int:
        self._descriptor(descriptor)
        with self._lock:
            if self._root < 0:
                raise ContentObjectError('private object reader is closed')
            current = os.dup(self._root)
        try:
            if not _private(os.fstat(current), directory=True):
                raise ContentObjectError('private object root unavailable')
            for component in descriptor.parts[:-1]:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current)
                        os.fsync(current)
                    except FileExistsError:
                        pass
                child = os.open(component, _DIR, dir_fd=current)
                os.close(current)
                current = child
                if not _private(os.fstat(current), directory=True):
                    raise ContentObjectError('private object directory unavailable')
            return current
        except BaseException:
            os.close(current)
            raise

    def _read_at(self, directory: int, descriptor: ContentObject) -> bytes:
        fd = os.open(descriptor.sha256, _FILE, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not _private(before, directory=False) or before.st_size != descriptor.size:
                raise ContentObjectError('content object unavailable')
            blocks, remaining = [], descriptor.size + 1
            while remaining:
                block = os.read(fd, min(remaining, 128 * 1024))
                if not block:
                    break
                blocks.append(block)
                remaining -= len(block)
            data = b''.join(blocks)
            after = os.fstat(fd)
            stable = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                      before.st_ctime_ns) == (after.st_dev, after.st_ino, after.st_size,
                                             after.st_mtime_ns, after.st_ctime_ns)
            if (not stable or not _private(after, directory=False)
                    or len(data) != descriptor.size
                    or hashlib.sha256(data).hexdigest() != descriptor.sha256):
                raise ContentObjectError('content object unavailable')
            return data
        finally:
            os.close(fd)

    def read(self, descriptor: ContentObject) -> bytes:
        try:
            directory = self._directory(descriptor)
            try:
                return self._read_at(directory, descriptor)
            finally:
                os.close(directory)
        except OSError as error:
            raise ContentObjectError('content object unavailable') from error


class LocalContentPublisher(LocalContentObjects):
    """Offline create-only publication; never give this capability to routes."""

    def publish(self, descriptor: ContentObject, data: bytes) -> bool:
        """Publish atomically; return False only for verified identical content.

        Existing corrupt files, links, special files and writable/nonprivate files are
        refused rather than repaired or overwritten. SQL manifest publication
        occurs only after every required object has been successfully published.
        """
        self._descriptor(descriptor)
        if (type(data) is not bytes or len(data) != descriptor.size
                or hashlib.sha256(data).hexdigest() != descriptor.sha256):
            raise ContentObjectError('publication does not match descriptor')
        directory, temporary, fd = -1, None, -1
        try:
            directory = self._directory(descriptor, create=True)
            temporary = '.pending-' + secrets.token_hex(16)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise ContentObjectError('content publication failed')
                view = view[count:]
            os.fchmod(fd, 0o400)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            created = True
            try:
                os.link(temporary, descriptor.sha256, src_dir_fd=directory,
                        dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                created = False
            os.unlink(temporary, dir_fd=directory)
            temporary = None
            os.fsync(directory)
            # Includes collision validation and proves the published bytes via
            # the same read contract that will be used by the runtime.
            if self._read_at(directory, descriptor) != data:
                raise ContentObjectError('content publication failed')
            return created
        except OSError as error:
            raise ContentObjectError('content publication failed') from error
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary is not None and directory >= 0:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
            if directory >= 0:
                os.close(directory)
