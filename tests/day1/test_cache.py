import concurrent.futures
import threading
from types import SimpleNamespace

import pytest

from scripts.day1.cache import CompositeCache, SessionInvalidated


def test_reader_asset_isolation_rollover_and_private_disk(tmp_path):
    now = [120]
    cache = CompositeCache(tmp_path, clock=lambda: now[0], free_floor=0)
    a = cache.get('a', 'page1', lambda minute: f'a:{minute}'.encode())
    assert a == (b'a:2', 'miss')
    assert cache.get('a', 'page1', lambda _: pytest.fail('re-rendered')) == (b'a:2', 'hit')
    assert cache.get('b', 'page1', lambda _: b'b')[0] == b'b'
    assert cache.get('a', 'thumb1', lambda _: b'thumb')[0] == b'thumb'
    assert cache.snapshot()['entries'] == 3
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in tmp_path.iterdir())
    now[0] = 180
    cache.sweep()
    assert not list(tmp_path.iterdir())
    assert cache.get('a', 'page1', lambda minute: f'a:{minute}'.encode()) == (b'a:3', 'miss')
    cache.close()
    assert not list(tmp_path.iterdir())


def test_lru_byte_entry_limits_and_disk_floor(tmp_path, monkeypatch):
    cache = CompositeCache(tmp_path, max_bytes=6, max_entries=2, free_floor=0)
    for page in ['a', 'b']:
        cache.get('reader', page, lambda _: b'123')
    cache.get('reader', 'a', lambda _: pytest.fail('missing a'))
    cache.get('reader', 'c', lambda _: b'456')
    assert cache.snapshot()['bytes'] == 6
    assert cache.get('reader', 'b', lambda _: b'new')[1] == 'miss'
    assert cache.get('reader', 'huge', lambda _: b'1234567')[1] == 'bypass'
    monkeypatch.setattr('scripts.day1.cache.shutil.disk_usage', lambda _: SimpleNamespace(free=0))
    assert cache.get('reader', 'low-disk', lambda _: b'new')[1] == 'bypass'
    assert cache.snapshot()['entries'] <= 2
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= 6


def test_same_key_concurrent_miss_renders_once(tmp_path):
    cache = CompositeCache(tmp_path, free_floor=0)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def render(_):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return b'pixels'
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(cache.get, 'reader', 'page', render) for _ in range(8)]
        assert entered.wait(3)
        release.set()
        results = [f.result(3) for f in futures]
    assert len(calls) == 1
    assert [s for _, s in results].count('miss') == 1
    assert all(body == b'pixels' for body, _ in results)


def test_revocation_during_render_never_reinserts_or_returns_pixels(tmp_path):
    cache = CompositeCache(tmp_path, free_floor=0)
    entered, release = threading.Event(), threading.Event()
    cache.get('reader', 'old', lambda _: b'old')
    def render(_):
        entered.set()
        assert release.wait(3)
        return b'secret'
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(cache.get, 'reader', 'new', render)
        assert entered.wait(3)
        cache.invalidate_session('reader')
        release.set()
        with pytest.raises(SessionInvalidated):
            future.result(3)
    assert not list(tmp_path.iterdir())
    with pytest.raises(SessionInvalidated):
        cache.get('reader', 'old', lambda _: b'new')


def test_rollover_during_render_retries_and_restart_discards_pixels(tmp_path):
    now = [120]
    cache = CompositeCache(tmp_path, clock=lambda: now[0], free_floor=0)
    def render(minute):
        now[0] = 180
        return str(minute).encode()
    assert cache.get('r', 'p', render) == (b'3', 'miss')
    restarted = CompositeCache(tmp_path, free_floor=0)
    assert restarted.snapshot()['entries'] == 0
    assert not list(tmp_path.iterdir())


def test_failed_eviction_retains_accounting_and_retries_without_serving_revoked_pixels(tmp_path, monkeypatch):
    from pathlib import Path
    cache = CompositeCache(tmp_path, max_bytes=6, max_entries=1, free_floor=0)
    cache.get('a', 'p', lambda _: b'123')
    unlink = Path.unlink
    def denied(*args, **kwargs):
        raise PermissionError('simulated deletion failure')
    monkeypatch.setattr(Path, 'unlink', denied)
    assert cache.get('b', 'p', lambda _: b'456') == (b'456', 'bypass')
    cache.invalidate_session('a')
    assert cache.snapshot()['bytes'] == 3 and cache.snapshot()['entries'] == 1
    with pytest.raises(SessionInvalidated):
        cache.get('a', 'p', lambda _: b'123')
    cache.sweep()  # A failed cleanup must not kill the sweeper.
    monkeypatch.setattr(Path, 'unlink', unlink)
    cache.sweep()
    assert cache.snapshot()['bytes'] == 0 and not list(tmp_path.iterdir())


def test_rollover_during_disk_write_never_returns_old_stamp(tmp_path, monkeypatch):
    import os
    now = [120]
    cache = CompositeCache(tmp_path, clock=lambda: now[0], free_floor=0)
    real_open = os.open
    def delayed_open(*args, **kwargs):
        result = real_open(*args, **kwargs)
        now[0] = 180
        return result
    monkeypatch.setattr(os, 'open', delayed_open)
    assert cache.get('r', 'p', lambda minute: str(minute).encode()) == (b'3', 'miss')
    assert cache.snapshot()['entries'] == 1


def test_failed_write_and_cleanup_never_serve_partial_pixels(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    cache = CompositeCache(tmp_path, free_floor=0)
    real_fdopen, unlink = os.fdopen, Path.unlink
    class BrokenFile:
        def __init__(self, fd, mode):
            self.output = real_fdopen(fd, mode)
        def __enter__(self):
            return self
        def write(self, body):
            self.output.write(body[:1])
            raise OSError('simulated full disk')
        def __exit__(self, *args):
            self.output.close()
    def denied(*args, **kwargs):
        raise PermissionError('simulated deletion failure')
    monkeypatch.setattr(os, 'fdopen', BrokenFile)
    monkeypatch.setattr(Path, 'unlink', denied)
    assert cache.get('r', 'p', lambda _: b'pixels') == (b'pixels', 'bypass')
    assert cache.snapshot()['bytes'] == 6
    assert cache.get('r', 'p', lambda _: b'fresh') == (b'fresh', 'bypass')
    monkeypatch.setattr(Path, 'unlink', unlink)
    cache.sweep()
    assert cache.snapshot()['bytes'] == 0 and not list(tmp_path.iterdir())


def test_failed_eviction_across_minute_retries_outer_render(tmp_path, monkeypatch):
    from pathlib import Path
    now = [120]
    cache = CompositeCache(tmp_path, max_entries=1, free_floor=0, clock=lambda: now[0])
    cache.get('a', 'p', lambda _: b'old')
    unlink = Path.unlink
    failed = [False]
    def delayed_failure(path, **kwargs):
        if not failed[0]:
            failed[0] = True
            now[0] = 180
            raise PermissionError('simulated slow eviction')
        return unlink(path, **kwargs)
    monkeypatch.setattr(Path, 'unlink', delayed_failure)
    assert cache.get('b', 'p', lambda minute: str(minute).encode()) == (b'3', 'miss')
