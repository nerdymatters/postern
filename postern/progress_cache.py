"""Bounded, disposable Rust evidence checkpoint for the all-staff report.

Only committed read-only snapshots owned by one Database can reuse evidence.
Authorization, roster display, quizzes and certificates are always read afresh.
The original audit history remains authoritative; nothing here is exported.
At most every 15 minutes, a report that extends the checkpoint is also compared with a full
Python replay of the same snapshot; a difference serves the Python result, drops the checkpoint
and is recorded for the watchdog, which returns the API to Python.
"""
from collections import deque
import sys
import threading
import time

from . import progress, progress_backend, progress_store
from .db import ReadConnection


def retained_size(value):
    # Native-generated primitive tuples/lists have no cycles. Counting shared
    # objects more than once is intentionally conservative.
    return sys.getsizeof(value) + (sum(retained_size(v) for v in value)
                                  if isinstance(value, (tuple, list)) else 0)


FULL = ("SELECT e.email,e.item_id,e.type,e.ts,e.payload FROM progress_events e "
        "JOIN members m ON m.email=e.email WHERE e.type IN "
        "('page_served','item_summary','video_progress','item_open','view_requested')")


class HistoryCache:
    def __init__(self, owner, budget=16*1024**2, verify_every=900, clock=time.monotonic):
        self.owner, self.budget = owner, budget
        self.verify_every, self.clock = verify_every, clock
        self.next_verify = None  # the first extended checkpoint is compared at once
        self.lock = threading.Lock()
        self.full_replays = self.delta_replays = 0
        self.row_counts = deque(maxlen=16)
        self.clear()

    def clear(self):
        self.checkpoint, self.stamp = [], None
        self.watermark = None
        self.retained_bytes = 0

    def diagnostics(self):
        with self.lock:
            return {'full_replays': self.full_replays, 'delta_replays': self.delta_replays,
                    'recent_row_counts': list(self.row_counts), 'retained_bytes': self.retained_bytes}

    def history(self, db, items):
        # Service's Rust worker serializes normal use. The lock also protects
        # direct callers; an older snapshot safely rebuilds its own evidence.
        with self.lock:
            return self._history(db, items)

    def _history(self, db, items):
        metadata = progress_backend.metadata(items)
        if (progress_backend.selected() != 'rust'
                or not isinstance(db, ReadConnection)
                or getattr(db, 'progress_owner', None) is not self.owner
                or not db.in_transaction or db.execute('PRAGMA query_only').fetchone()[0] != 1
                or metadata is None or progress_store.source(db) != 'progress_events'):
            self.clear()
            return progress.history(db, items)
        module = progress_backend.native()
        roster = tuple(row[0] for row in db.execute('SELECT email FROM members ORDER BY email'))
        generation = db.execute('SELECT value FROM progress_generation WHERE id=1').fetchone()[0]
        maximum = db.execute('SELECT MAX(seq) FROM progress_events').fetchone()[0]
        stamp = (db.progress_file, tuple(sorted(metadata)), roster, generation)
        # Empty history has no watermark: zero and negative sequence values are
        # valid old-writer inputs and must be included on the first continuation.
        reuse = (self.stamp == stamp and self.watermark is not None
                 and maximum is not None and maximum >= self.watermark)
        sql = FULL
        if reuse:
            # A primary-key range scan visits only appended rows. An email-first
            # join can otherwise rescan every old row merely to filter its seq.
            sql = ("SELECT e.email,e.item_id,e.type,e.ts,e.payload FROM progress_events e NOT INDEXED "
                   "WHERE e.seq>? AND e.email IN (SELECT email FROM members) AND e.type IN "
                   "('page_served','item_summary','video_progress','item_open','view_requested')")
        cursor = db.execute(sql, (self.watermark,) if reuse else ())
        consumed = 0

        def rows():
            nonlocal consumed
            for row in cursor:
                consumed += 1
                yield row

        try:
            result, checkpoint = module.reduce_incremental(
                metadata, rows(), progress._evidence, self.checkpoint if reuse else [])
        except BaseException as error:
            if progress_backend.native_fault(error):
                progress_backend.health.native_error(error)
            raise
        # Publish only after a successful replay; native never mutates our input.
        self.row_counts.append(consumed)
        self.delta_replays += int(reuse)
        self.full_replays += int(not reuse)
        if reuse and (self.next_verify is None or self.clock() >= self.next_verify):
            self.next_verify = self.clock() + self.verify_every
            try:
                reference = progress._reduce_python(items, db.execute(FULL))
            except Exception as error:  # The comparison did not run; the native result stands.
                progress_backend.health.check_failed(error)
            else:
                progress_backend.health.checked(reference == result)
                if reference != result:
                    self.clear()  # Never extend evidence that disagreed with the full replay.
                    return reference
        retained = retained_size((checkpoint, stamp))
        if retained <= self.budget:
            self.checkpoint, self.stamp, self.watermark = checkpoint, stamp, maximum
            self.retained_bytes = retained
        else:
            self.clear()  # Return the full correct result even when it cannot fit.
        return result
