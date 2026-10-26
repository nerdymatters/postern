"""External clients for the synthetic page/audit/HLS endpoints. No automatic retries."""
from __future__ import annotations

import argparse
import concurrent.futures
from collections import Counter
import http.client
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .common import checked_base_url, percentiles, private_json


class Client:
    def __init__(self, base):
        url = urlsplit(checked_base_url(base))
        cls = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        self.connection = cls(url.hostname, url.port, timeout=10)
        self.prefix = url.path.rstrip('/')

    def get(self, path, token=None, run_id='smoke', extra=None):
        headers = {'X-Day1-Run': run_id, **(extra or {})}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        start = time.perf_counter()
        try:
            self.connection.request('GET', self.prefix + path, headers=headers)
            response = self.connection.getresponse()
            body = response.read(16 * 1024 * 1024 + 1)
            if len(body) > 16 * 1024 * 1024:
                raise ValueError('unexpected response over 16 MiB')
            return response.status, dict((k.lower(), v) for k, v in response.getheaders()), body, (time.perf_counter() - start) * 1000
        except Exception:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()


def check_uncached(headers):
    if headers.get('cache-control') != 'no-store':
        raise RuntimeError('no-store missing')
    cache_status = headers.get('cf-cache-status', 'not_reported').upper()
    if cache_status not in ('NOT_REPORTED', 'DYNAMIC', 'BYPASS') or float(headers.get('age', 0)) > 0:
        raise RuntimeError('response indicates CDN caching')
    return cache_status


def hls_segments(text):
    duration = None
    segments = []
    for line in text.splitlines():
        if line.startswith('#EXTINF:'):
            duration = float(line.split(':')[1].split(',')[0])
        elif line and not line.startswith('#'):
            # Only our prepared media playlist is supported; never follow arbitrary remote URLs.
            import re
            if duration is None or duration <= 0 or not re.fullmatch(r'seg_\d{3}\.ts', line):
                raise ValueError('expected finite synthetic media playlist')
            segments.append((line, duration))
            duration = None
    if not segments:
        raise ValueError('playlist has no segments')
    return segments


def run_load(base, credentials, mode, readers=20, duration=300, warmup=15):
    if duration <= 0 or warmup < 0 or not 1 <= readers <= 100:
        raise ValueError('duration must be positive, warmup nonnegative and readers 1..100')
    tokens = list(credentials['readers'])
    if mode != 'hls' and len(tokens) < readers:
        raise ValueError('not enough distinct synthetic reader credentials')
    client = Client(base)
    try:
        status, _, body, _ = client.get('/probe/config', credentials['operator'])
        if status != 200:
            raise ValueError(f'harness configuration request returned {status}')
        config = json.loads(body)
        playlist = None
        if mode == 'hls':
            status, _, body, _ = client.get('/probe/hls/index.m3u8')
            if status != 200:
                raise ValueError('generate the synthetic video with prepare --video first')
            playlist = hls_segments(body.decode())
    finally:
        client.close()
    run_id = uuid.uuid4().hex
    start_wall = time.time()
    measurement_start = time.monotonic() + warmup
    end = measurement_start + duration

    def reader(index):
        client = Client(base)
        samples, errors, measured_bytes, stalls = [], [], 0, 0
        cache_statuses, composite_statuses = Counter(), Counter()
        iteration, playback_deadline = 0, None
        first_segment_ms = None
        while time.monotonic() < end:
            if mode == 'hls' and playback_deadline is not None:
                delay = min(playback_deadline - time.monotonic() - 12, end - time.monotonic())
                if delay > 0:
                    time.sleep(delay)
            if time.monotonic() >= end:
                break
            segment_duration = 0
            if mode == 'hls':
                name, segment_duration = playlist[iteration % len(playlist)]
                path, kind = '/probe/hls/' + name, 'segment'
            elif mode == 'audit':
                path, kind = '/probe/audit', 'audit'
            elif iteration < 6 or iteration % 4 == 0:
                path, kind = f'/probe/thumbs/{iteration % 50}.jpg', 'thumb'
            else:
                # Repeated pages model scroll-back under either server cache policy.
                page = (iteration // 2) % config['pages']
                path, kind = f'/probe/pages/{page}.jpg', 'page'
            sampled = time.monotonic() >= measurement_start
            try:
                status, headers, body, latency = client.get(path, tokens[index % len(tokens)], run_id)
                if status != 200:
                    raise RuntimeError(f'HTTP {status}')
                cache_status = check_uncached(headers) if mode != 'hls' else 'not_checked'
                if mode != 'hls' and any(key not in headers for key in ('x-audit-ms', 'x-audit-queue-ms', 'x-audit-commit-ms')):
                    raise RuntimeError('audit measurement missing')
                if kind in ('page', 'thumb') and not body.startswith(b'\xff\xd8'):
                    raise RuntimeError('not a JPEG')
                now = time.monotonic()
                if mode == 'hls':
                    if playback_deadline is None:
                        first_segment_ms = latency
                        playback_deadline = now + segment_duration
                    else:
                        if now > playback_deadline and sampled:
                            stalls += 1
                        playback_deadline = max(now, playback_deadline) + segment_duration
                if sampled:
                    cache_statuses[cache_status] += 1
                    if kind in ('page', 'thumb'):
                        composite_statuses[headers.get('x-composite-cache', 'not_reported')] += 1
                    measured_bytes += len(body)
                    samples.append({'kind': kind, 'ms': latency,
                                    'audit_ms': float(headers['x-audit-ms']) if 'x-audit-ms' in headers else None,
                                    'queue_ms': float(headers['x-audit-queue-ms']) if 'x-audit-queue-ms' in headers else None,
                                    'commit_ms': float(headers['x-audit-commit-ms']) if 'x-audit-commit-ms' in headers else None})
            except Exception as error:
                if sampled:
                    errors.append(str(error))
                time.sleep(0.05)
            iteration += 1
        client.close()
        return {'samples': samples, 'errors': errors, 'bytes': measured_bytes,
                'stalls': stalls, 'first_segment_ms': first_segment_ms,
                'cache_statuses': cache_statuses, 'composite_statuses': composite_statuses}

    with concurrent.futures.ThreadPoolExecutor(max_workers=readers) as pool:
        results = list(pool.map(reader, range(readers)))
    samples = [sample for result in results for sample in result['samples']]
    errors = [error for result in results for error in result['errors']]
    page_ms = [s['ms'] for s in samples if s['kind'] == 'page']
    audit_ms = [s['audit_ms'] for s in samples if s['audit_ms'] is not None]
    measured_elapsed = time.monotonic() - measurement_start
    return {'run_id': run_id, 'mode': mode, 'base_url': base, 'started_at': start_wall,
            'finished_at': time.time(), 'measured_elapsed_s': measured_elapsed, 'readers': readers, 'duration_s': duration, 'warmup_s': warmup,
            'server_config': config, 'requests': len(samples), 'errors': len(errors), 'error_examples': errors[:10],
            'page_ms': percentiles(page_ms), 'audit_ms': percentiles(audit_ms),
            'queue_ms': percentiles([s['queue_ms'] for s in samples if s['queue_ms'] is not None]),
            'commit_ms': percentiles([s['commit_ms'] for s in samples if s['commit_ms'] is not None]),
            'per_kind_ms': {kind: percentiles([s['ms'] for s in samples if s['kind'] == kind])
                            for kind in sorted({s['kind'] for s in samples})},
            'mbps': sum(r['bytes'] for r in results) * 8 / measured_elapsed / 1_000_000,
            'simulated_stalls': sum(r['stalls'] for r in results),
            'cache_statuses': dict(sum((r['cache_statuses'] for r in results), Counter())),
            'composite_statuses': dict(sum((r['composite_statuses'] for r in results), Counter())),
            'first_segment_ms': [r['first_segment_ms'] for r in results if r['first_segment_ms'] is not None],
            'feasibility_gate': 'pending_office_host_converter_load_and_external_network_evidence',
            'notes': ['Synthetic credentials bypass Google/allowlist; this is not an authentication acceptance test.',
                      'Host CPU and converter load are recorded separately with scripts.day1.host.',
                      'HLS simulated buffering is not real-phone decode/playback evidence.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--mode', choices=['pages', 'audit', 'hls'], required=True)
    parser.add_argument('--readers', type=int, default=20)
    parser.add_argument('--seconds', type=float, default=300)
    parser.add_argument('--warmup', type=float, default=15)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = run_load(checked_base_url(args.base_url), json.loads(args.credentials.read_text()),
                      args.mode, args.readers, args.seconds, args.warmup)
    private_json(args.output, report)
    print(json.dumps({k: report[k] for k in ['mode', 'requests', 'errors', 'page_ms', 'audit_ms', 'mbps', 'feasibility_gate']}, indent=2))
    return 1 if report['errors'] or report['requests'] == 0 else 0


if __name__ == '__main__':
    raise SystemExit(main())
