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

from postern import progress, service
from scripts import capacity


@contextmanager
def capture_spans():
    spans = []
    history, dashboard = progress.history, service.dashboard
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
    try:
        yield spans
    finally:
        progress.history, service.dashboard = history, dashboard
        service.Service.training_progress = request


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    before = set((capacity.ROOT/'.context/capacity').glob('*/report.json'))
    args = argparse.Namespace(scenario='mixed', users=50, roster=228, seconds=120,
                              history_hours=9, ramp_seconds=10, video_mbps=1.5,
                              cloud_delay=0, with_reader=False)
    record = {'status': 'running', 'limits': 'One diagnostic attempt, not three-pair acceptance. Timing wrappers add overhead.'}
    with capture_spans() as spans:
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
                        record['capacity'] = json.loads(after.pop().read_text())
                    except (OSError, ValueError) as error:
                        record['report_error'] = type(error).__name__
            if 'capacity' not in record:
                raise RuntimeError('Expected exactly one fresh capacity report')
            record['status'] = 'completed' if passed else 'failed'
            return passed
        except BaseException as error:
            record.update(status='failed', error=type(error).__name__)
            raise
        finally:
            record['spans'] = spans
            (output/'report.json').write_text(json.dumps(record, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not (os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted' and sys.platform == 'darwin'):
        parser.error('Run this diagnostic only on a disposable GitHub-hosted Mac')
    if not asyncio.run(run(args.output.resolve())):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
