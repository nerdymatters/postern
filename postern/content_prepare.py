"""Offline native PDF/video preparation, with no serving database capability.

The caller supplies existing canonical resource identity and a private publisher.
Only the existing OS-sandboxed Deriver parses the original. All publication is
create-only; a failure may leave private unreferenced objects, never a SQL-ready
partial result. The owner publishes the original inventory and complete returned
manifest in its own transaction after preparation succeeds.

This local rehearsal backend accepts at most 64 MiB originals and 8 MiB objects.
Its private directory is not an implementation of per-Postern encryption.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import stat
import tempfile
import threading

from .config import Config
from .content_objects import ContentObject, LocalContentPublisher
from .derive import Deriver, DeriveError


MAX_ORIGINAL_BYTES = 64 * 1024 * 1024
MAX_OBJECT_BYTES = 8 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 1024 * 1024
_SLOT = threading.BoundedSemaphore(1)


class ContentPrepareError(ValueError):
    """Preparation failed; no complete presentation may be published."""


@dataclass(frozen=True)
class PreparedContent:
    original: ContentObject
    manifest: dict
    descriptors: tuple[ContentObject, ...]

    @property
    def inventory(self) -> list[dict]:
        return [dict(representation=o.representation, index=o.index, sha256=o.sha256, size=o.size)
                for o in self.descriptors]


def _regular(fd, limit):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or not 0 < info.st_size <= limit):
        raise ContentPrepareError('Converter output unavailable')
    return info


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _blocks(directory, filename, chunk_bytes, total_limit):
    """Consume only a constant converter filename, no symlinks or special files.

    The final stability check must finish before the caller receives its complete
    PreparedContent. A changed file can leave only unreferenced private objects.
    """
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    try:
        before = _regular(fd, total_limit)
        remaining = before.st_size
        while remaining:
            wanted = min(remaining, chunk_bytes)
            data = bytearray()
            while len(data) < wanted:
                block = os.read(fd, wanted-len(data))
                if not block:
                    raise ContentPrepareError('Converter output unavailable')
                data.extend(block)
            remaining -= wanted
            yield bytes(data), before.st_size
        if os.read(fd, 1) or _identity(before) != _identity(_regular(fd, total_limit)):
            raise ContentPrepareError('Converter output unavailable')
    finally:
        os.close(fd)


def prepare_content(publisher: LocalContentPublisher, *, space_id: str, resource_id: str,
                    revision: int, kind: str, source: bytes,
                    chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> PreparedContent:
    """Prepare one existing native resource; never infer identity or touch SQL.

    Construct the offline publisher with ``max_object_bytes=64*1024*1024`` when
    originals exceed the online reader's default 8 MiB cap. The returned original
    uses the existing ``space/resource/revision/hash`` key, while derivative keys
    also bind representation and index. Video posters are optional and omitted.
    """
    if (not isinstance(publisher, LocalContentPublisher) or type(kind) is not str or kind not in {'document', 'video'}
            or type(source) is not bytes or not 0 < len(source) <= MAX_ORIGINAL_BYTES
            or type(chunk_bytes) is not int or not 1 <= chunk_bytes <= MAX_OBJECT_BYTES):
        raise ContentPrepareError('Invalid native preparation input')
    if not _SLOT.acquire(blocking=False):
        raise ContentPrepareError('Content preparation capacity unavailable')
    try:
        original = ContentObject(space_id, resource_id, revision, 'original', 0,
                                 hashlib.sha256(source).hexdigest(), len(source))
        if original.size > publisher.max_object_bytes:
            raise ContentPrepareError('Offline publisher original size bound exceeded')
        descriptors = []

        def publish(representation, index, data):
            if not 0 < len(data) <= MAX_OBJECT_BYTES:
                raise ContentPrepareError('Converter output exceeded its object limit')
            descriptor = ContentObject(space_id, resource_id, revision, representation, index,
                                       hashlib.sha256(data).hexdigest(), len(data))
            publisher.publish(descriptor, data)
            descriptors.append(descriptor)

        with tempfile.TemporaryDirectory(prefix='postern-content-prepare-') as work:
            config = Config(data_dir=Path(work).resolve(), mode='demo', converter_workers=1,
                            max_pages=200, max_pixels=8_000_000, video_upload_limit=MAX_ORIGINAL_BYTES,
                            derive_output_limit=256 * 1024 * 1024).validate()
            staged = config.data_dir/'staging'/('source.pdf' if kind == 'document' else 'source.mp4')
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(source)
            destination = config.data_dir/'derived'/'prepared'
            converted = Deriver(config).run(staged, destination, 'pdf' if kind == 'document' else 'video')
            directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                if kind == 'document':
                    count = converted['pages']
                    if type(count) is not int or not 1 <= count <= config.max_pages:
                        raise ContentPrepareError('Converter page inventory unavailable')
                    for index in range(count):
                        blocks = list(_blocks(directory, f'page-{index}.jpg', MAX_OBJECT_BYTES, MAX_OBJECT_BYTES))
                        data = blocks[0][0]
                        publish('page', index, data)
                        # These are bounded, sanitized thumbnail *inputs*.
                        # Delivery resizes and stamps them inside its sandbox.
                        # No additional JPEG decoder runs in this parent process.
                        publish('thumbnail', index, data)
                    metadata = {'page_count': count}
                else:
                    duration = converted['duration_seconds']
                    if (type(duration) not in (int, float) or not math.isfinite(duration)
                            or not 0 < duration <= 10800):
                        raise ContentPrepareError('Converter video inventory unavailable')
                    total, count = 0, 0
                    for index, (data, size) in enumerate(_blocks(directory, 'video.mp4', chunk_bytes,
                                                               MAX_ORIGINAL_BYTES+16 * 1024 * 1024)):
                        if index >= 65536:
                            raise ContentPrepareError('Converter video inventory exceeded its chunk limit')
                        publish('video', index, data)
                        total, count = size, index+1
                    if total != converted['bytes'] or count < 1:
                        raise ContentPrepareError('Converter video inventory unavailable')
                    metadata = dict(duration_ms=math.ceil(duration*1000), total_bytes=total,
                                    chunk_bytes=chunk_bytes, chunk_count=count)
            finally:
                os.close(directory)
            publisher.publish(original, source)
            return PreparedContent(original, dict(resource_id=resource_id, revision=revision,
                                                  kind=kind, metadata=metadata), tuple(descriptors))
    except (OSError, ValueError, KeyError, TypeError, DeriveError) as error:
        if isinstance(error, ContentPrepareError):
            raise
        raise ContentPrepareError('Native content could not be prepared safely') from error
    finally:
        _SLOT.release()
