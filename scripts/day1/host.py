"""Sample whole-host CPU while repeatedly rendering the synthetic PDF in Seatbelt.

This is a bounded fixture-only load generator, not the production converter sandbox.
It permits runtime/library reads; it must never process confidential or untrusted PDFs.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import resource
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import psutil

from .common import private_json


def sandbox_profile(output):
    # Runtime dependencies differ between Homebrew installs. Broad read access is
    # acceptable ONLY for our generated fixture load. Production D6 remains unbuilt.
    quoted = json.dumps(str(output.resolve()))
    return '(version 1)(deny default)(allow process*)(allow sysctl-read)' \
           '(allow file-read*)(allow file-write* (subpath ' + quoted + '))'


def child():
    """Apply resource limits after exec, never preexec_fn inside a threaded parent."""
    import sys
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    os.execv(sys.argv[2], sys.argv[2:])


def run_host(directory, seconds, workers, crm_activity):
    if seconds <= 0 or not 0 <= workers <= 8:
        raise ValueError('seconds must be positive and workers 0..8')
    executable = shutil.which('pdftoppm')
    if workers and (platform.system() != 'Darwin' or not executable or not shutil.which('sandbox-exec')):
        raise ValueError('converter load requires macOS, sandbox-exec and poppler (pdftoppm)')
    start = time.time()
    deadline = time.monotonic() + seconds

    def convert(_):
        import sys
        completed, errors = 0, []
        while time.monotonic() < deadline:
            with tempfile.TemporaryDirectory(prefix='day1-derive-', dir=directory) as temp:
                output = Path(temp)
                command = ['/usr/bin/sandbox-exec', '-p', sandbox_profile(output),
                           sys.executable, '-m', 'scripts.day1.host', '--child', executable,
                           '-f', '1', '-l', '4', '-scale-to', '1600', '-jpeg',
                           str((directory / 'synthetic.pdf').resolve()), str(output / 'page')]
                try:
                    result = subprocess.run(command, capture_output=True, timeout=40, env={
                        'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'TMPDIR': str(output),
                        'PYTHONPATH': str(Path.cwd()), 'PYTHONDONTWRITEBYTECODE': '1'})
                    if result.returncode:
                        errors.append(result.stderr.decode(errors='replace')[-500:])
                        break
                    if not list(output.glob('page-*.jpg')):
                        errors.append('converter produced no pages')
                        break
                    completed += 1
                except subprocess.TimeoutExpired:
                    errors.append('converter wall timeout')
                    break
        return {'completed': completed, 'errors': errors}

    psutil.cpu_times_percent(interval=None)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(convert, i) for i in range(workers)]
        cpu, intervals = [], []
        while time.monotonic() < deadline:
            interval = min(1, deadline - time.monotonic())
            if interval > 0:
                sample_start = time.time()
                idle = psutil.cpu_times_percent(interval=interval).idle
                cpu.append(idle)
                intervals.append({'started_at': sample_start, 'finished_at': time.time(),
                                  'idle_percent': idle})
        conversions = [future.result() for future in futures]
    return {'started_at': start, 'finished_at': time.time(), 'hardware': platform.platform(),
            'cpu_count': os.cpu_count(), 'crm_activity': crm_activity, 'seconds': seconds,
            'cpu_idle_mean_percent': sum(cpu) / len(cpu) if cpu else None,
            'cpu_idle_samples': cpu, 'cpu_intervals': intervals,
            'converter_workers': workers, 'conversions': conversions,
            'sandbox_scope': 'fixture_only_runtime_read_access_no_network_bounded_writes',
            'production_converter_isolation_verified': False,
            'feasibility_gate': 'pending_comparison_with_external_load_report'}


def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        child()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=330)
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--crm-activity', required=True, help='Describe actual CRM activity, including idle if idle')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run_host(args.directory.resolve(), args.seconds, args.workers, args.crm_activity)
    private_json(args.output, result)
    if any(r['errors'] for r in result['conversions']):
        raise SystemExit('Converter errors; the run cannot qualify. Inspect the report.')
    print(f'Host report written to {args.output}')


if __name__ == '__main__':
    main()
