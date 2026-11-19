"""Real sandboxed JPEGs plus live process cancellation and cleanup checks."""
import asyncio
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import sys
import time
import threading

import anyio
from PIL import Image
import psutil
import pytest

from postern import content_render
from postern.content_render import ContentRenderBusy, ContentRenderError, RenderLimits, render_page
from postern.stamp import stamp


LABEL = 'principal:00000000-0000-4000-8000-000000000001'
MINUTE = 29_000_000


def source(size=(300, 400), *, format='PNG'):
    output = io.BytesIO()
    Image.new('RGB', size, 'white').save(output, format=format)
    return output.getvalue()


def render(data=None, **kwargs):
    return asyncio.run(render_page(source() if data is None else data, LABEL, MINUTE, **kwargs))


def expected_stamp(data, *, thumbnail=False):
    previous = os.environ.get('TZ')
    try:
        os.environ['TZ'] = 'UTC'
        time.tzset()
        return stamp(io.BytesIO(data), LABEL, thumbnail=thumbnail, minute=MINUTE)
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()


def test_actual_sandbox_stamp_is_existing_identity_and_minute_presentation():
    data = source()
    result = render(data)
    assert result == expected_stamp(data)
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == 'JPEG'
        assert image.width == 300
        assert 400 < image.height < 500
    another = asyncio.run(render_page(data, LABEL + ':other', MINUTE))
    later = asyncio.run(render_page(data, LABEL, MINUTE + 1))
    assert result != another != later


def test_actual_thumbnail_is_bounded_and_stamped():
    data = source((1000, 2000), format='JPEG')
    result = render(data, thumbnail=True)
    assert result == expected_stamp(data, thumbnail=True)
    with Image.open(io.BytesIO(result)) as image:
        assert image.width <= 400
        assert 600 < image.height < 700


@pytest.mark.parametrize('data,limits', [
    (b'not an image', RenderLimits()),
    (source((50, 50), format='GIF'), RenderLimits()),
    (source((4097, 1)), RenderLimits()),
    (source((100, 100)), RenderLimits(max_pixels=1000)),
    (source(), RenderLimits(max_output_bytes=512)),
    (source(), RenderLimits(max_input_bytes=10)),
], ids=['invalid-image', 'unsupported-format', 'dimension', 'pixels', 'output-bytes', 'input-bytes'])
def test_actual_format_pixel_dimension_and_byte_bounds(data, limits):
    with pytest.raises(ContentRenderError):
        render(data, limits=limits)


@pytest.mark.parametrize('kwargs', [
    {'principal_label': ''}, {'principal_label': 'x' * 257},
    {'principal_label': 'line\nbreak'}, {'principal_label': '\ud800'},
    {'minute': True}, {'minute': -1}, {'minute': 68_374_081}, {'thumbnail': 1},
    {'data': bytearray(b'private')},
])
def test_invalid_input_refused_before_process_creation(monkeypatch, kwargs):
    def unexpected(*args):
        raise AssertionError('invalid request started a renderer')

    monkeypatch.setattr(content_render, '_command', unexpected)
    arguments = dict(data=source(), principal_label=LABEL, minute=MINUTE)
    arguments.update(kwargs)
    with pytest.raises(ContentRenderError):
        asyncio.run(render_page(**arguments))


@pytest.mark.parametrize('changes', [
    {'timeout': 0}, {'timeout': float('nan')}, {'timeout': 11},
    {'max_pixels': 8_000_001}, {'max_output_bytes': 4 * 1024 * 1024 + 1},
    {'memory_bytes': 31 * 1024 * 1024}, {'max_dimension': True},
])
def test_server_configuration_has_hard_upper_bounds(changes):
    with pytest.raises(ContentRenderError):
        replace(RenderLimits(), **changes)


def test_renderer_fails_closed_when_sandbox_cannot_start(monkeypatch):
    monkeypatch.setattr(content_render, '_command', lambda *args: ['/missing/postern-sandbox'])
    with pytest.raises(ContentRenderError):
        render()
    # Failed startup releases capacity; a subsequent real isolated render works.
    monkeypatch.undo()
    assert render().startswith(b'\xff\xd8')


def test_actual_sandbox_denies_private_file_and_network_and_child_fork(tmp_path, monkeypatch):
    secret = tmp_path / 'private-outside-job'
    secret.write_text('not accessible to renderer')
    parent_namespace = None
    if sys.platform == 'linux':
        namespace = os.stat('/proc/self/ns/net')
        parent_namespace = (namespace.st_dev, namespace.st_ino)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        listener.settimeout(0.5)
        endpoint = listener.getsockname()

        def assert_parent_can_connect():
            with socket.create_connection(endpoint, timeout=0.5):
                connection, _ = listener.accept()
                connection.close()

        assert_parent_can_connect()
        instrumented = tmp_path / 'stamp-instrumented.py'
        instrumented.write_text('''import os, socket, resource, sys
for target in ['forbidden-output', '/tmp/postern-render-forbidden-output', sys.argv[2], sys.argv[3]]:
    try:
        open(target, 'wb').write(b'not permitted')
    except OSError:
        pass
    else:
        raise AssertionError('writable filesystem allowed')
try:
    open(%r).read()
except OSError:
    pass
else:
    raise AssertionError('private-file read allowed')
if sys.platform == 'linux':
    namespace = os.stat('/proc/self/ns/net')
    assert (namespace.st_dev, namespace.st_ino) != %r, 'parent network namespace inherited'
try:
    connection = socket.create_connection(%r, timeout=0.2)
except OSError:
    # The parent proves this listener is live before and after the render.
    # Linux isolates the socket namespace; Seatbelt denies the connection.
    pass
else:
    connection.close()
    raise AssertionError('parent listener reachable from sandbox')
try:
    child = os.fork()
except OSError:
    pass
else:
    if child == 0:
        os._exit(0)
    os.waitpid(child, 0)
    raise AssertionError('fork allowed')
assert resource.getrlimit(resource.RLIMIT_CPU)[0] <= 5
assert resource.getrlimit(resource.RLIMIT_FSIZE)[0] <= 4 * 1024 * 1024
assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == 32
''' % (str(secret.resolve()), parent_namespace, endpoint) + content_render.STAMP_SOURCE.read_text())
        # __future__ must stay at the start of the staged trusted module.
        code = instrumented.read_text().replace('from __future__ import annotations', '')
        instrumented.write_text(code)
        monkeypatch.setattr(content_render, 'STAMP_SOURCE', instrumented)
        assert render().startswith(b'\xff\xd8')
        listener.setblocking(False)
        try:
            connection, _ = listener.accept()
        except BlockingIOError:
            pass
        else:
            connection.close()
            pytest.fail('unexpected connection reached the parent listener')
        listener.settimeout(0.5)
        assert_parent_can_connect()


def alive(pid):
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.fixture
def controlled_workers(tmp_path, monkeypatch):
    """Exercise real process groups separately from the sandbox success tests.

    This intentionally owned helper may fork to prove group cleanup, whereas
    the production worker denies fork as well as isolating filesystem/network.
    """
    helper = tmp_path / 'controlled_worker.py'
    helper.write_text('''import json, os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
pathlib.Path('pids.json').write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(60)
''')
    directories, known_pids = [], []

    def command(source_path, directory):
        directories.append(directory)
        return [sys.executable, '-I', str(helper)]

    monkeypatch.setattr(content_render, '_command', command)

    async def ready(count=1):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if len(directories) >= count:
                paths = [directory / 'pids.json' for directory in directories[:count]]
                if all(path.exists() for path in paths):
                    try:
                        pids = [pid for path in paths for pid in json.loads(path.read_text())]
                    except json.JSONDecodeError:
                        pass
                    else:
                        known_pids[:] = pids
                        assert all(alive(pid) for pid in pids)
                        return pids
            await asyncio.sleep(0.01)
        raise AssertionError('owned workers did not start')

    yield directories, ready
    for pid in known_pids:
        if alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_cancellation_reaps_live_process_group_and_removes_private_job(controlled_workers):
    directories, ready = controlled_workers

    async def scenario():
        task = asyncio.create_task(render_page(source(), LABEL, MINUTE))
        pids = await ready()
        task.cancel()
        asyncio.get_running_loop().call_later(0.001, task.cancel)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert all(not alive(pid) for pid in pids)
        assert all(not directory.exists() for directory in directories)

    asyncio.run(scenario())


def test_anyio_scope_cancellation_drains_worker_without_cleanup_spin(controlled_workers):
    directories, ready = controlled_workers

    async def scenario():
        async with anyio.create_task_group() as group:
            group.start_soon(render_page, source(), LABEL, MINUTE)
            pids = await ready()
            group.cancel_scope.cancel()
        assert all(not alive(pid) for pid in pids)
        assert all(not directory.exists() for directory in directories)

    asyncio.run(scenario())



def test_group_barrier_holds_job_and_slots_during_anyio_cancellation(controlled_workers, monkeypatch):
    directories, ready = controlled_workers
    monkeypatch.setattr(content_render, '_SLOTS', threading.BoundedSemaphore(2))
    release = threading.Event(); inspected = set(); groups = set(); signals = []
    real_killpg = os.killpg; real_members = content_render._group_has_live_members

    def killpg(pgid, signum):
        if pgid in groups:
            if signum == 0 and not release.is_set():
                return  # Deterministically model a group still visible after reap.
            if signum != 0:
                signals.append((pgid, signum))
        return real_killpg(pgid, signum)

    def members(pgid, deadline):
        inspected.add(pgid)
        return not release.is_set() or real_members(pgid, deadline)

    monkeypatch.setattr(content_render.os, 'killpg', killpg)
    monkeypatch.setattr(content_render, '_group_has_live_members', members)

    async def scenario():
        async with anyio.create_task_group() as tasks:
            for _ in range(2):
                tasks.start_soon(render_page, source(), LABEL, MINUTE)
            pids = await ready(2)
            assert real_members(pids[0], time.monotonic() + 1)
            groups.update(pids[::2])
            tasks.cancel_scope.cancel()
            with anyio.CancelScope(shield=True):
                try:
                    deadline = time.monotonic() + 1
                    while inspected != groups and time.monotonic() < deadline:
                        await asyncio.sleep(0.005)
                    assert inspected == groups
                    assert all(directory.exists() for directory in directories)
                    with pytest.raises(ContentRenderBusy):
                        await render_page(source(), LABEL, MINUTE)
                    assert len(directories) == 2
                finally:
                    release.set()
        assert all(not alive(pid) for pid in pids)
        assert all(not directory.exists() for directory in directories)
        assert not real_members(pids[0], time.monotonic() + 1)
        assert sorted(signals) == sorted((pgid, signal.SIGKILL) for pgid in groups)
        assert content_render._SLOTS.acquire(blocking=False)
        assert content_render._SLOTS.acquire(blocking=False)
        content_render._SLOTS.release(); content_render._SLOTS.release()

    asyncio.run(scenario())


def test_group_stop_timeout_retains_job_and_slot_without_resignalling(monkeypatch):
    # A successful real render enters normal cleanup; its failed barrier must
    # not cause the finally block to signal the already reaped group again.
    monkeypatch.setattr(content_render, '_SLOTS', threading.BoundedSemaphore(2))
    monkeypatch.setattr(content_render, '_STOP_TIMEOUT', 0.05)
    directories, signals = [], []
    real_command = content_render._command; real_killpg = os.killpg
    real_members = content_render._group_has_live_members

    def command(source_path, directory):
        directories.append(directory)
        return real_command(source_path, directory)

    def killpg(pgid, signum):
        if signum == 0:
            return  # Existing group remains unconfirmed until the real deadline.
        signals.append((pgid, signum))
        return real_killpg(pgid, signum)

    monkeypatch.setattr(content_render, '_command', command)
    monkeypatch.setattr(content_render.os, 'killpg', killpg)
    monkeypatch.setattr(content_render, '_group_has_live_members', lambda pgid, deadline: True)
    try:
        with pytest.raises(ContentRenderError, match='group stop'):
            render()
        assert len(signals) == 1 and signals[0][1] == signal.SIGKILL
        assert len(directories) == 1 and directories[0].exists()
        assert content_render._SLOTS.acquire(blocking=False)
        assert not content_render._SLOTS.acquire(blocking=False)
        content_render._SLOTS.release()
    finally:
        # Restore the real observation before reclaiming this deliberately
        # retained synthetic job; the test's semaphore is isolated by monkeypatch.
        monkeypatch.setattr(content_render.os, 'killpg', real_killpg)
        for pgid, _ in signals:
            assert not real_members(pgid, time.monotonic() + 1)
        for directory in directories:
            if directory.exists():
                shutil.rmtree(directory)



def test_stop_deadline_keeps_late_drain_alive_without_resignalling(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(content_render, '_STOP_TIMEOUT', 0.03)
    signals = []
    monkeypatch.setattr(content_render.os, 'killpg', lambda pgid, signum: signals.append((pgid, signum)))

    async def scenario():
        release = asyncio.Event(); drained = asyncio.Event()
        async def communicate():
            await release.wait()
            drained.set()
            return b'', None
        process = SimpleNamespace(pid=123456, communicate=communicate)
        started = time.monotonic()
        try:
            with pytest.raises(ContentRenderError, match='stop deadline'):
                await content_render._settle(asyncio.create_task(content_render._stop(process)))
            assert time.monotonic() - started < 0.5
            assert signals == [(123456, signal.SIGKILL)]
            assert len(content_render._PENDING_DRAINS) == 1
            assert not drained.is_set()
        finally:
            release.set()
            await asyncio.wait_for(drained.wait(), timeout=0.5)
            await asyncio.sleep(0)
        assert not content_render._PENDING_DRAINS
        assert signals == [(123456, signal.SIGKILL)]

    asyncio.run(scenario())


@pytest.mark.parametrize('system', ['Linux', 'Darwin'])
def test_group_observation_ignores_zombies_but_not_unobservable_groups(tmp_path, monkeypatch, system):
    from types import SimpleNamespace
    monkeypatch.setattr(content_render.platform, 'system', lambda: system)
    pgid = 123456; states = [b'Z']; real_scandir = os.scandir
    process = tmp_path / '123'; process.mkdir()
    status = process / 'stat'
    def write_state(value):
        status.write_bytes(b'123 (name with ) parenthesis) ' + value + b' 1 123456 123456 0 0\n')
    write_state(b'Z')
    monkeypatch.setattr(content_render.os, 'scandir', lambda path: real_scandir(tmp_path if path == '/proc' else path))
    monkeypatch.setattr(content_render.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0 if states else 1, stdout=b'\n'.join(states), stderr=b''))
    monkeypatch.setattr(content_render.os, 'killpg', lambda *args: None)
    assert not content_render._group_has_live_members(pgid, time.monotonic() + 1)
    states[:] = [b'T']; write_state(b'T')
    assert content_render._group_has_live_members(pgid, time.monotonic() + 1)
    states.clear(); status.unlink(); process.rmdir()
    assert content_render._group_has_live_members(pgid, time.monotonic() + 1)
    def absent(*args):
        raise ProcessLookupError
    monkeypatch.setattr(content_render.os, 'killpg', absent)
    assert not content_render._group_has_live_members(pgid, time.monotonic() + 1)


def test_timeout_reaps_live_process_group_and_removes_private_job(controlled_workers):
    directories, ready = controlled_workers

    async def scenario():
        started = time.monotonic()
        task = asyncio.create_task(render_page(source(), LABEL, MINUTE,
                                   limits=RenderLimits(timeout=0.35)))
        pids = await ready()
        with pytest.raises(ContentRenderError, match='deadline'):
            await task
        assert time.monotonic() - started < 2
        assert all(not alive(pid) for pid in pids)
        assert all(not directory.exists() for directory in directories)

    asyncio.run(scenario())


def test_renderer_has_no_unbounded_queue_and_cancelled_slots_are_reusable(controlled_workers):
    directories, ready = controlled_workers

    async def scenario():
        tasks = [asyncio.create_task(render_page(source(), LABEL, MINUTE)) for _ in range(2)]
        try:
            pids = await ready(2)
            with pytest.raises(ContentRenderBusy):
                await render_page(source(), LABEL, MINUTE)
            assert len(directories) == 2
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert all(not alive(pid) for pid in pids)
        assert all(not directory.exists() for directory in directories)
        # A fresh job starts after both cancelled requests release their slots.
        task = asyncio.create_task(render_page(source(), LABEL, MINUTE))
        await asyncio.sleep(0.1)
        assert len(directories) == 3
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_rss_limit_kills_worker_before_returning_bytes(controlled_workers, monkeypatch):
    directories, ready = controlled_workers

    async def scenario():
        task = asyncio.create_task(render_page(source(), LABEL, MINUTE))
        pids = await ready()
        monkeypatch.setattr(content_render, '_resident', lambda pid: 512 * 1024 * 1024 + 1)
        with pytest.raises(ContentRenderError, match='memory bound'):
            await task
        assert all(not alive(pid) for pid in pids)
        assert all(not directory.exists() for directory in directories)

    asyncio.run(scenario())


def test_cancel_during_process_creation_still_reaps_child(controlled_workers, monkeypatch):
    directories, ready = controlled_workers
    real_create = asyncio.create_subprocess_exec

    async def delayed_create(*args, **kwargs):
        process = await real_create(*args, **kwargs)
        await asyncio.sleep(0.15)
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_create)

    async def scenario():
        task = asyncio.create_task(render_page(source(), LABEL, MINUTE))
        pids = await ready()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(not alive(pid) for pid in pids)
        assert all(not directory.exists() for directory in directories)

    asyncio.run(scenario())


def test_output_pipe_limit_kills_and_reaps_a_writer_without_deadlocking(tmp_path, monkeypatch):
    helper = tmp_path / 'excessive_output.py'
    helper.write_text('''import os, pathlib, sys, time
pathlib.Path('pid').write_text(str(os.getpid()))
time.sleep(0.1)
while True:
    sys.stdout.buffer.write(b'x' * 65536)
    sys.stdout.buffer.flush()
''')
    directories = []

    def command(source_path, directory):
        directories.append(directory)
        return [sys.executable, '-I', str(helper)]

    monkeypatch.setattr(content_render, '_command', command)

    async def scenario():
        task = asyncio.create_task(render_page(source(), LABEL, MINUTE,
                                  limits=RenderLimits(max_output_bytes=128 * 1024)))
        pid = None
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if directories and (directories[0] / 'pid').exists():
                    pid = int((directories[0] / 'pid').read_text())
                    break
                await asyncio.sleep(0.005)
            assert pid is not None and alive(pid)
            with pytest.raises(ContentRenderError, match='output bound'):
                await asyncio.wait_for(task, timeout=2)
            assert not alive(pid)
            assert not directories[0].exists()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if pid is not None and alive(pid):
                os.kill(pid, signal.SIGKILL)

    asyncio.run(scenario())
