"""Bounded, disposable on-disk composites for the single-process synthetic probe.

Authorization and durable audit remain the caller's responsibility on every hit.
This directory belongs exclusively to one probe process; it is never an original store.
"""
from collections import Counter, OrderedDict
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time


class SessionInvalidated(Exception):
    pass


class CompositeCache:
    def __init__(self, directory, max_bytes=128 * 1024 * 1024, max_entries=1024,
                 free_floor=1024 * 1024 * 1024, clock=time.time):
        if max_bytes <= 0 or max_entries <= 0 or free_floor < 0:
            raise ValueError('invalid cache limits')
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        # Never reuse pixels from a previous process/session registry.
        for path in self.directory.glob('*.jpg'):
            path.unlink()
        self.max_bytes, self.max_entries, self.free_floor = max_bytes, max_entries, free_floor
        self.clock = clock
        self._lock = threading.RLock()
        self._stripes = [threading.Lock() for _ in range(64)]
        self._entries = OrderedDict()
        self._bytes = 0
        self._minute = None
        self._revoked = set()  # Bounded by the probe's fixed synthetic credential set.
        self._closed = False
        self._counts = Counter()

    def _remove(self, key):
        path, size, _, _ = self._entries[key]
        try:
            path.unlink(missing_ok=True)
        except OSError:
            self._counts['cleanup_errors'] += 1
            self._minute = None  # Retry on the next sweep; retained bytes still count.
            return False
        del self._entries[key]
        self._bytes -= size
        return True

    def _expire(self, minute):
        if self._minute == minute:
            return True
        clean = True
        for key, (_, _, owner, saved_minute) in list(self._entries.items()):
            if saved_minute != minute or owner in self._revoked:
                removed = self._remove(key)
                clean &= removed
                self._counts['expired'] += int(removed)
        self._minute = minute if clean else None
        return clean

    def sweep(self):
        with self._lock:
            self._expire(int(self.clock() // 60))

    def invalidate_session(self, session):
        with self._lock:
            self._revoked.add(session)
            for key, (_, _, owner, _) in list(self._entries.items()):
                if owner == session:
                    self._remove(key)

    def close(self):
        with self._lock:
            self._closed = True
            for key in list(self._entries):
                self._remove(key)

    def snapshot(self):
        with self._lock:
            return {**self._counts, 'entries': len(self._entries), 'bytes': self._bytes,
                    'max_bytes': self.max_bytes, 'max_entries': self.max_entries,
                    'free_floor_bytes': self.free_floor, 'storage': 'private_disk',
                    'expiry': 'overlay_minute', 'enabled': True}

    def get(self, session, asset, render):
        # A fixed lock array bounds coordination memory and coalesces same-key misses.
        identity = json.dumps([session, asset], separators=(',', ':'))
        stripe = self._stripes[int(hashlib.sha256(identity.encode()).hexdigest(), 16) % len(self._stripes)]
        with stripe:
            while True:
                minute = int(self.clock() // 60)
                key = hashlib.sha256(f'{identity}:{minute}'.encode()).hexdigest()
                with self._lock:
                    if self._closed or session in self._revoked:
                        raise SessionInvalidated()
                    clean = self._expire(minute)
                    if clean and key in self._entries:
                        try:
                            body = self._entries[key][0].read_bytes()
                        except OSError:
                            clean = self._remove(key)
                        else:
                            if minute != int(self.clock() // 60):
                                continue
                            self._entries.move_to_end(key)
                            self._counts['hits'] += 1
                            return body, 'hit'
                    self._counts['misses'] += 1
                body = render(minute)
                with self._lock:
                    if self._closed or session in self._revoked:
                        raise SessionInvalidated()
                    # A render spanning rollover cannot insert or return last minute's stamp.
                    if minute != int(self.clock() // 60):
                        continue
                    try:
                        room = shutil.disk_usage(self.directory).free - len(body) >= self.free_floor
                    except OSError:
                        room = False
                    if not clean or len(body) > self.max_bytes or not room:
                        self._counts['bypasses'] += 1
                        if minute != int(self.clock() // 60):
                            continue
                        return body, 'bypass'
                    eviction_failed = False
                    while self._entries and (self._bytes + len(body) > self.max_bytes or len(self._entries) >= self.max_entries):
                        if not self._remove(next(iter(self._entries))):
                            eviction_failed = True
                            break
                        self._counts['evictions'] += 1
                    if eviction_failed:
                        self._counts['bypasses'] += 1
                        if minute != int(self.clock() // 60):
                            continue
                        return body, 'bypass'
                    path = self.directory / f'{key}.jpg'
                    created = False
                    try:
                        # Readers share this lock: a partially written entry is never visible.
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        created = True
                        with os.fdopen(fd, 'wb') as output:
                            output.write(body)
                    except OSError:
                        if created:
                            # Reserve the full attempted size until even a partial file is removed.
                            self._entries[key] = (path, len(body), session, minute)
                            self._bytes += len(body)
                            if not self._remove(key):
                                # Never reuse a partial file; sweep will retry its deletion.
                                self._entries[key] = (path, len(body), session, -1)
                        self._counts['bypasses'] += 1
                        if minute != int(self.clock() // 60):
                            continue
                        return body, 'bypass'
                    self._entries[key] = (path, len(body), session, minute)
                    self._bytes += len(body)
                    if minute != int(self.clock() // 60):
                        self._expire(int(self.clock() // 60))
                        continue
                    return body, 'miss'
