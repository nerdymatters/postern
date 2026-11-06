"""Progress scheduling preserves snapshots, cancellation and Python rollback."""
import asyncio
import contextvars
import threading

import pytest

from postern.service import Service


@pytest.fixture
def make_service(config, store, clock, monkeypatch):
    def make(backend):
        # Exercise scheduling independently of whether this CI lane has a wheel.
        monkeypatch.setattr('postern.service.progress_backend.selected', lambda: backend)
        return Service(config, store=store, clock=clock)
    return make


@pytest.mark.parametrize('backend', ['python', 'rust'])
def test_read_context_readonly_connection_and_errors(make_service, backend):
    service = make_service(backend)
    context = contextvars.ContextVar('request', default=None)

    async def run():
        context.set('request-context')
        event_loop_thread = threading.get_ident()
        def read(db):
            assert threading.get_ident() != event_loop_thread
            assert context.get() == 'request-context'
            assert db.execute('PRAGMA query_only').fetchone()[0] == 1
            return 42
        def failed(db):
            raise ValueError('injected read failure')
        try:
            assert (service.progress_executor is None) == (backend == 'python')
            assert await service.read_progress(read) == 42
            with pytest.raises(ValueError, match='injected read failure'):
                await service.read_progress(failed)
            assert await service.read_progress(read) == 42
        finally:
            await service.close()
    asyncio.run(run())


def test_cancelled_request_keeps_running_slot_and_drops_queued_work(make_service, monkeypatch):
    service = make_service('rust')
    entered, release = threading.Event(), threading.Event()
    submitted = []
    original = service.progress_executor.submit
    def submit(*args, **kwargs):
        future = original(*args, **kwargs)
        submitted.append(future)
        return future
    monkeypatch.setattr(service.progress_executor, 'submit', submit)
    calls = []
    def blocked(db):
        entered.set()
        assert release.wait(5)
        calls.append('first')
        return db.execute('SELECT 1').fetchone()[0]
    def later(name):
        def read(db):
            calls.append(name)
            return name
        return read

    async def run():
        tasks = []
        try:
            first = asyncio.create_task(service.read_progress(blocked)); tasks.append(first)
            assert await asyncio.to_thread(entered.wait, 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(service.read_progress(later('cancelled'))); tasks.append(second)
            await asyncio.sleep(0)  # Let the coroutine submit, without releasing the running worker.
            assert len(submitted) == 2
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            third = asyncio.create_task(service.read_progress(later('third'))); tasks.append(third)
            await asyncio.sleep(0)
            assert len(submitted) == 3 and submitted[1].cancelled()
            assert not submitted[2].running() and not calls
            release.set()
            assert await asyncio.wait_for(third, 5) == 'third'
            assert calls == ['first', 'third']
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await service.close()
    asyncio.run(run())


def test_shutdown_waits_for_running_read_and_cancels_queue_before_database_close(make_service, monkeypatch):
    service = make_service('rust')
    entered, release = threading.Event(), threading.Event()
    order = []
    close_db = service.db.close
    def closed():
        order.append('database-close')
        close_db()
    monkeypatch.setattr(service.db, 'close', closed)
    def read(db):
        entered.set()
        assert release.wait(5)
        assert db.execute('SELECT 1').fetchone()[0] == 1
        order.append('read-finished')
    def queued(db):
        pytest.fail('shutdown must cancel the queued read')

    async def run():
        tasks = []
        try:
            first = asyncio.create_task(service.read_progress(read)); tasks.append(first)
            assert await asyncio.to_thread(entered.wait, 5)
            second = asyncio.create_task(service.read_progress(queued)); tasks.append(second)
            await asyncio.sleep(0)
            close = asyncio.create_task(service.close()); tasks.append(close)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(second, 5)
            assert not close.done() and not order
            release.set()
            await asyncio.wait_for(asyncio.gather(first, close), 5)
            assert order == ['read-finished', 'database-close']
            with pytest.raises(RuntimeError, match='shutdown'):
                await service.read_progress(read)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            if 'database-close' not in order:
                await service.close()
    asyncio.run(run())
