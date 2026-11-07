"""Measure replay and request spans inside one guarded synthetic mixed workload."""
import argparse
import asyncio
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import sys
import time

from postern import progress, progress_backend, service
from postern.progress_cache import HistoryCache
from scripts import capacity, progress_profile


@contextmanager
def capture_memory():
    """Tag existing RSS observations without changing measurement or acceptance."""
    record = {'samples': [], 'boundaries': [], 'dropped_samples': 0}
    phase = 'startup'
    began = time.monotonic()
    initialize, start = capacity.Metrics.__init__, service.Service.start
    verify = capacity.verify_dashboard

    def boundary(label):
        record['boundaries'].append({'phase': label, 'elapsed_seconds': time.monotonic()-began,
            'rss_mib': capacity.psutil.Process().memory_info().rss/1024**2})

    class Samples(list):
        def __init__(self, metrics):
            super().__init__()
            self.metrics = metrics

        def append(self, rss):
            super().append(rss)
            if len(record['samples']) < 1000:
                record['samples'].append({'phase': phase, 'elapsed_seconds': time.monotonic()-began,
                    'rss_mib': rss, 'active_requests': {k: v for k, v in self.metrics.active.items() if v}})
            else:
                record['dropped_samples'] += 1

    def initialized(metrics):
        initialize(metrics)
        metrics.rss = Samples(metrics)

    async def started(instance):
        nonlocal phase
        boundary('startup_begin')
        await start(instance)
        boundary('startup_end')
        phase = 'workload'

    async def verified(*args, **kwargs):
        nonlocal phase
        phase = 'verification'
        boundary('verification_begin')
        try:
            return await verify(*args, **kwargs)
        finally:
            boundary('verification_end')
            phase = 'cleanup'

    capacity.Metrics.__init__, service.Service.start = initialized, started
    capacity.verify_dashboard = verified
    try:
        yield record
    finally:
        capacity.Metrics.__init__, service.Service.start = initialize, start
        capacity.verify_dashboard = verify


@contextmanager
def capture_spans():
    spans = []
    history, dashboard = progress.history, service.dashboard
    checkpoint = HistoryCache.history
    request = service.Service.training_progress

    def timed(function, label):
        @wraps(function)
        def measured(*args, **kwargs):
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                return function(*args, **kwargs)
            finally:
                spans.append({'stage': label, 'wall_ms': (time.perf_counter()-wall)*1000,
                              'thread_cpu_ms': (time.thread_time()-cpu)*1000})
        return measured

    @wraps(history)
    def replay(db, items, email=None, coverage=False):
        return timed(history, 'roster_history' if email is None else 'reader_history')(
            db, items, email, coverage)

    @wraps(request)
    async def requested(*args, **kwargs):
        started = time.perf_counter()
        try:
            return await request(*args, **kwargs)
        finally:
            # Event-loop thread CPU spans include unrelated awaits, so record only wall time here.
            spans.append({'stage': 'service_request', 'wall_ms': (time.perf_counter()-started)*1000})

    progress.history = replay
    service.dashboard = timed(dashboard, 'dashboard_worker')
    service.Service.training_progress = requested
    HistoryCache.history = timed(checkpoint, 'roster_checkpoint')
    try:
        yield spans
    finally:
        progress.history, service.dashboard = history, dashboard
        service.Service.training_progress = request
        HistoryCache.history = checkpoint


async def run(output, *, quiet_profile=False):
    output.mkdir(parents=True, exist_ok=False)
    before = set((capacity.ROOT/'.context/capacity').glob('*/report.json'))
    args = argparse.Namespace(scenario='mixed', users=50, roster=228, seconds=120,
                              history_hours=9, ramp_seconds=10, video_mbps=1.5,
                              cloud_delay=0, with_reader=False)
    record = {'status': 'running', 'limits': 'One diagnostic attempt, not three-pair acceptance. Timing wrappers add overhead.'}
    fixture = None
    with capture_spans() as spans, capture_memory() as memory:
        try:
            try:
                passed = await capacity.run(args)
            finally:
                # Cleanup can raise after the harness writes its report. Preserve
                # that evidence without replacing the original failure.
                after = set((capacity.ROOT/'.context/capacity').glob('*/report.json'))-before
                record['fresh_report_count'] = len(after)
                if len(after) == 1:
                    try:
                        report_path = after.pop()
                        record['capacity'] = json.loads(report_path.read_text())
                        fixture = report_path.parent/'data/postern.sqlite3'
                    except (OSError, ValueError) as error:
                        record['report_error'] = type(error).__name__
            if 'capacity' not in record:
                raise RuntimeError('Expected exactly one fresh capacity report')
            if passed and quiet_profile:
                # Capacity cleanup has stopped the server and closed the writer.
                # Diagnose its actual fixture in a guarded fresh process; these
                # costs are excluded from the already-recorded HTTP measurements.
                destination = output/'quiet-replay.json'
                await asyncio.to_thread(progress_profile.guarded, destination, 'capacity-profile',
                                        fixture, progress_backend.selected())
                record['quiet_replay'] = json.loads(destination.read_text())
            record['status'] = 'completed' if passed else 'failed'
            return passed
        except BaseException as error:
            record.update(status='failed', error=type(error).__name__)
            raise
        finally:
            record['spans'] = spans
            record['memory'] = memory
            (output/'report.json').write_text(json.dumps(record, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not (os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted' and sys.platform == 'darwin'):
        parser.error('Run this diagnostic only on a disposable GitHub-hosted Mac')
    if not asyncio.run(run(args.output.resolve(), quiet_profile=True)):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
