"""Bounded per-request presentation in the existing OS conversion sandbox.

Two jobs may run concurrently; excess requests fail immediately instead of
retaining an unbounded queue of private bytes. The child has no credentials,
network, or application-data access. Cancellation signals the group, reaps its
direct child and confirms no live group members remain before removing the
private job directory. No authorization is cached.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import io
import json
import math
import os
from pathlib import Path
import platform
import resource
import runpy
import signal
import subprocess
import sys
import tempfile
import threading
import time
import warnings


STAMP_SOURCE = Path(__file__).with_name('stamp.py')
_SLOTS = threading.BoundedSemaphore(2)
_STOP_TIMEOUT = 2.0
_PENDING_DRAINS: set[asyncio.Task] = set()


class ContentRenderError(ValueError):
    """Presentation could not be prepared safely; do not expose parser details."""


class ContentRenderBusy(ContentRenderError):
    """The finite renderer capacity is occupied."""


@dataclass(frozen=True)
class RenderLimits:
    timeout: float = 5.0
    max_input_bytes: int = 8 * 1024 * 1024
    max_output_bytes: int = 4 * 1024 * 1024
    max_pixels: int = 8_000_000
    max_dimension: int = 4096
    memory_bytes: int = 512 * 1024 * 1024

    def __post_init__(self):
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= 10):
            raise ContentRenderError('invalid render limits')
        for name, lower, upper in (
                ('max_input_bytes', 1, 8 * 1024 * 1024),
                ('max_output_bytes', 1, 4 * 1024 * 1024),
                ('max_pixels', 1, 8_000_000), ('max_dimension', 1, 4096),
                ('memory_bytes', 32 * 1024 * 1024, 512 * 1024 * 1024)):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ContentRenderError('invalid render limits')


def _command(source: Path, directory: Path) -> list[str]:
    from . import derive
    child = str(Path(__file__).resolve())
    stamp_source = str(directory / 'stamp_impl.py')
    command = derive.sandbox_command(source, directory,
        [sys.executable, '-I', child, '--worker', str(source), stamp_source])
    # Reuse the reviewed profiles without adding source-tree read permission.
    # Replace the unused face-model literal with the captured stamping module.
    # The renderer needs NO writable directory: only its bounded stdout pipe.
    replacements = {
        str(derive.CHILD): child, 'CHILD=' + str(derive.CHILD): 'CHILD=' + child,
        str(derive.FACE_MODEL): stamp_source,
        'FACE_MODEL=' + str(Path(derive.FACE_MODEL).resolve()): 'FACE_MODEL=' + stamp_source,
    }
    if derive.LINUX:
        # derive.bwrap already remounts / and /dev read-only; narrowing its only
        # writable bind also leaves /tmp, HOME and staging without writable paths.
        replacements['--bind'] = '--ro-bind'
    else:
        # Seatbelt's only writable subpath becomes the already allowed null device.
        replacements['OUTPUT=' + str(directory)] = 'OUTPUT=/dev/null'
    return [replacements.get(part, part) for part in command]


def _resident(pid: int) -> int:
    if platform.system() == 'Linux':
        from .derive import tree_resident
        return tree_resident(pid)
    result = subprocess.run(['/bin/ps', '-o', 'rss=', '-g', str(pid)],
                            capture_output=True, timeout=0.5, check=False)
    if result.returncode not in (0, 1):
        raise ContentRenderError('renderer accounting unavailable')
    return sum(int(line) for line in result.stdout.splitlines()) * 1024


async def _settle(task):
    """Finish bounded process creation/reaping even after repeated cancellation."""
    import anyio
    # Starlette/AnyIO cancellation is level-triggered. Without this scope every
    # shield await can immediately cancel again and starve the process reaper.
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
    return task.result()


def _group_has_live_members(pgid: int, deadline: float) -> bool:
    """Read group state without sending another signal after the leader is reaped."""
    if platform.system() == 'Linux':
        matched = False
        with os.scandir('/proc') as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    raise ContentRenderError('renderer group stop deadline exceeded')
                if not entry.name.isdecimal():
                    continue
                try:
                    with open(entry.path + '/stat', 'rb') as status:
                        # comm can contain spaces and closing parentheses.
                        fields = status.read(4096).rsplit(b')', 1)[1].split()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if int(fields[2]) == pgid:
                    matched = True
                    if fields[0] not in (b'Z', b'X', b'x'):
                        return True
        if matched:
            return False  # Every observed member has stopped executing.
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        return True  # Existing group with no observable members is unconfirmed.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ContentRenderError('renderer group stop deadline exceeded')
    result = subprocess.run(['/bin/ps', '-o', 'stat=', '-g', str(pgid)],
                            capture_output=True, timeout=min(0.25, remaining), check=False)
    if result.returncode not in (0, 1) or result.stderr:
        raise ContentRenderError('renderer group state unavailable')
    states = result.stdout.split()
    if states:
        return any(not state.startswith(b'Z') for state in states)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_group_stopped(pgid: int, deadline: float) -> None:
    try:
        while True:
            # Normal renders need only this cheap absence check, not a ps/proc scan.
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ContentRenderError('renderer group stop deadline exceeded')
            active = await asyncio.wait_for(
                asyncio.to_thread(_group_has_live_members, pgid, deadline), timeout=remaining)
            if time.monotonic() >= deadline:
                raise ContentRenderError('renderer group stop deadline exceeded')
            if not active:
                return  # Unreaped zombies cannot access the job or consume a slot.
            await asyncio.sleep(min(0.01, deadline - time.monotonic()))
    except ContentRenderError:
        raise
    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError, IndexError) as error:
        raise ContentRenderError('renderer group stop could not be confirmed') from error


def _drain_finished(task):
    _PENDING_DRAINS.discard(task)
    if not task.cancelled():
        task.exception()  # Consume a late pipe error after a failed-closed stop.


async def _stop(process, collector=None):
    # The caller shields this one cached task; internal deadline cancellation
    # must remain effective across both pipe draining and group confirmation.
    deadline = time.monotonic() + _STOP_TIMEOUT
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as error:
        raise ContentRenderError('renderer group stop could not be confirmed') from error
    try:
        if collector is not None:
            collector.cancel()
            try:
                await asyncio.wait_for(collector, timeout=max(0, deadline - time.monotonic()))
            except (asyncio.CancelledError, ContentRenderError):
                pass
        # Drain after kill: a paused over-limit pipe can otherwise prevent wait().
        # Do not cancel a late drain/reaper. Retained slots bound these at two.
        drain = asyncio.create_task(process.communicate())
        try:
            await asyncio.wait_for(asyncio.shield(drain), timeout=max(0, deadline - time.monotonic()))
        finally:
            if not drain.done():
                _PENDING_DRAINS.add(drain)
                drain.add_done_callback(_drain_finished)
        # A grandchild with closed pipes may still have SIGKILL pending.
        await _wait_group_stopped(process.pid, deadline)
    except TimeoutError as error:
        raise ContentRenderError('renderer group stop deadline exceeded') from error


def _write_at(directory: int, name: str, data: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    try:
        remaining = memoryview(data)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise ContentRenderError('renderer staging failed')
            remaining = remaining[count:]
    finally:
        os.close(fd)


async def _collect(stream, limit: int) -> bytes:
    parts, size = [], 0
    while True:
        part = await stream.read(min(64 * 1024, limit + 1 - size))
        if not part:
            break
        size += len(part)
        if size > limit:
            raise ContentRenderError('renderer output bound exceeded')
        parts.append(part)
    data = b''.join(parts)
    if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        raise ContentRenderError('renderer output unavailable')
    return data


async def render_page(data: bytes, principal_label: str, minute: int,
                      thumbnail: bool = False, *, limits: RenderLimits | None = None) -> bytes:
    """Stamp verified JPEG/PNG bytes with a server-resolved principal label.

    Stamp timestamps use UTC, independent of the machine's private timezone
    files. ``limits`` is server configuration, never derived from request parameters.
    This process must already be unprivileged; missing OS isolation fails closed.
    """
    limits = limits if limits is not None else RenderLimits()
    if type(limits) is not RenderLimits:
        raise ContentRenderError('invalid render limits')
    limits.__post_init__()
    if (type(data) is not bytes or not 0 < len(data) <= limits.max_input_bytes
            or type(principal_label) is not str or not 1 <= len(principal_label) <= 256
            or any(ord(character) < 32 or ord(character) == 127
                   or 0xD800 <= ord(character) <= 0xDFFF for character in principal_label)
            or type(minute) is not int or not 0 <= minute <= 68_374_080
            or type(thumbnail) is not bool):
        raise ContentRenderError('invalid render input')
    if os.geteuid() == 0 or platform.system() not in {'Darwin', 'Linux'}:
        raise ContentRenderError('unprivileged renderer unavailable')
    from . import derive
    if not _SLOTS.acquire(blocking=False):
        raise ContentRenderBusy('renderer capacity unavailable')
    staging, job, directory, process, waiter, collector = -1, -1, None, None, None, None
    stop_task = None
    try:
        parent = Path(tempfile.gettempdir()).resolve()
        staging = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory = Path(tempfile.mkdtemp(prefix='postern-render-', dir=parent))
        job = os.open(directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                      dir_fd=staging)
        request = json.dumps({'label': principal_label, 'minute': minute,
                              'thumbnail': thumbnail, 'limits': asdict(limits)})
        # A bounded private envelope keeps identity/configuration out of process
        # arguments while requiring only one literal input-file sandbox binding.
        _write_at(job, 'source', request.encode() + b'\n' + data)
        # Capture only trusted stamping code inside the allowed private job.
        # The child imports no application module, database configuration or key.
        _write_at(job, 'stamp_impl.py', STAMP_SOURCE.read_bytes())
        command = _command(directory / 'source', directory)
        started = time.monotonic()
        launch = asyncio.create_task(asyncio.create_subprocess_exec(
            *command, cwd=directory, env=derive.job_env(directory),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=64 * 1024))
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            process = await _settle(launch)
            raise
        waiter = asyncio.create_task(process.wait())
        collector = asyncio.create_task(_collect(process.stdout, limits.max_output_bytes))
        while not (waiter.done() and collector.done()):
            if collector.done():
                collector.result()  # Fail immediately on overflow, not at the wall timeout.
            remaining = limits.timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise ContentRenderError('renderer deadline exceeded')
            pending = {task for task in (waiter, collector) if not task.done()}
            await asyncio.wait(pending, timeout=min(0.05, remaining))
            if not waiter.done():
                resident = await asyncio.to_thread(_resident, process.pid)
                if resident > limits.memory_bytes:
                    raise ContentRenderError('renderer memory bound exceeded')
        if time.monotonic() - started > limits.timeout:
            raise ContentRenderError('renderer deadline exceeded')
        if waiter.result() != 0:
            raise ContentRenderError('presentation unavailable')
        data = collector.result()
        stop_task = asyncio.create_task(_stop(process, collector))
        await _settle(stop_task)
        process = None
        if not os.path.samestat(os.fstat(job), os.stat(directory.name, dir_fd=staging,
                                                      follow_symlinks=False)):
            raise ContentRenderError('renderer output unavailable')
        return data
    except (OSError, subprocess.SubprocessError) as error:
        raise ContentRenderError('presentation unavailable') from error
    finally:
        cleanup_safe = process is None
        try:
            if process is not None:
                # Reuse the same stop task if normal completion already tried it:
                # never signal a potentially reused process-group ID twice.
                if stop_task is None:
                    stop_task = asyncio.create_task(_stop(process, collector))
                await _settle(stop_task)
                cleanup_safe = True
        finally:
            try:
                if cleanup_safe:
                    if job >= 0:
                        derive.discard(staging, directory.name, job)
                    elif directory is not None:
                        os.rmdir(directory.name, dir_fd=staging)
            finally:
                if job >= 0:
                    os.close(job)
                if staging >= 0:
                    os.close(staging)
                # An unconfirmed stop fails closed: leave the private job and
                # occupied slot in place. At most the existing two jobs remain.
                if cleanup_safe:
                    _SLOTS.release()


def _worker() -> None:
    if os.geteuid() == 0:
        raise ValueError('unprivileged worker required')
    source_file = open(sys.argv[2], 'rb')
    header = source_file.readline(8193)
    if len(header) > 8192 or not header.endswith(b'\n'):
        raise ValueError('input envelope bound')
    request = json.loads(header)
    limits = RenderLimits(**request['limits'])
    cpu = max(1, math.ceil(limits.timeout))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits.max_output_bytes, limits.max_output_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    # The renderer never needs to fork. This also prevents an exploited decoder
    # from escaping process-group cleanup by forking into a fresh session.
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
    except (ValueError, OSError):
        if platform.system() != 'Darwin':
            raise
        # macOS does not reliably implement RLIMIT_AS; parent group RSS is bounded.
    os.umask(0o077)
    os.environ['TZ'] = 'UTC'
    time.tzset()
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = limits.max_pixels
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    stamp = runpy.run_path(sys.argv[3])['stamp']
    data = source_file.read(limits.max_input_bytes + 1)
    source_file.close()
    if not 0 < len(data) <= limits.max_input_bytes:
        raise ValueError('input bound')
    with Image.open(io.BytesIO(data), formats=('JPEG', 'PNG')) as source:
        width, height = source.size
        if (not 0 < width <= limits.max_dimension or not 0 < height <= limits.max_dimension
                or width * height > limits.max_pixels or getattr(source, 'n_frames', 1) != 1):
            raise ValueError('pixel bound')
    result = stamp(io.BytesIO(data), request['label'], request['thumbnail'], request['minute'])
    if not 0 < len(result) <= limits.max_output_bytes:
        raise ValueError('output bound')
    with Image.open(io.BytesIO(result), formats=('JPEG',)) as image:
        # Attribution adds a bounded footer; cap the final decoded footprint too.
        if image.width > limits.max_dimension or image.height > limits.max_dimension + 4096 or image.width * image.height > limits.max_pixels + 1_048_576:
            raise ValueError('output pixel bound')
        image.verify()
    sys.stdout.buffer.write(result)
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    try:
        if len(sys.argv) != 4 or sys.argv[1] != '--worker':
            raise ValueError('invalid worker invocation')
        _worker()
    except Exception:
        # Decoder diagnostics may contain document bytes; do not print them.
        sys.exit(1)
