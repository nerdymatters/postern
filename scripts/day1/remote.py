"""Run bounded S1 workloads on a GitHub-hosted runner, using synthetic credentials."""
import json
import ipaddress
import os
from pathlib import Path
import platform
import time
from urllib.request import urlopen

from .common import checked_base_url, private_json
from .load import Client, run_load


def verify_public_ip(client, observed):
    address = ipaddress.ip_address(observed['observed_ip'])
    endpoint = 'https://api4.ipify.org' if address.version == 4 else 'https://api6.ipify.org'
    with urlopen(endpoint, timeout=10) as response:
        expected = str(ipaddress.ip_address(response.read(100).decode().strip()))
    if str(address) != expected or not observed.get('forwarding_headers_trusted'):
        raise ValueError('Public client IP does not match independent observation')
    results = []
    for extra in ({'X-Forwarded-For': '192.0.2.123', 'X-Real-IP': '192.0.2.124'},
                  {'CF-Connecting-IP': '192.0.2.125', 'CF-Connecting-IPv6': '2001:db8::1'}):
        status, _, body, _ = client.get('/probe/ip', extra=extra)
        if status in (400, 403):
            results.append({'status': status, 'result': 'forged metadata rejected'})
        elif status == 200 and json.loads(body).get('observed_ip') == expected:
            results.append({'status': status, 'result': 'real client IP preserved'})
        else:
            raise ValueError('Client-IP spoof check failed')
    return {'independent_ip_match': True, 'spoof_checks': results}


def main():
    base = checked_base_url(os.environ['POSTERN_PROBE_URL'])
    credentials = json.loads(os.environ.pop('POSTERN_PROBE_CREDENTIALS'))
    if os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise ValueError('This workflow requires a GitHub-hosted external runner')
    output = Path('reports/day1')
    client = Client(base)
    try:
        status, headers, body, latency_ms = client.get('/probe/ip')
        if status != 200:
            raise ValueError(f'IP probe returned HTTP {status}')
        observed = json.loads(body)
        recorded_at = time.time()
        ip_checks = verify_public_ip(client, observed)
    finally:
        client.close()
    private_json(output / 'runner.json', {
        'recorded_at': recorded_at, 'platform': platform.platform(),
        'run_id': os.environ['GITHUB_RUN_ID'], 'head_sha': os.environ['GITHUB_SHA'],
        'runner_environment': os.environ['RUNNER_ENVIRONMENT'],
        'observed_client': observed,
        'ip_checks': ip_checks,
        'server_date': headers.get('date'), 'ip_request_ms': latency_ms,
    })
    failed = False
    for mode, readers in [('pages', 20), ('audit', 25)]:
        print(f'Starting {mode}: {readers} readers, 15s warmup, 300s measurement', flush=True)
        try:
            report = run_load(base, credentials, mode, readers, duration=300, warmup=15)
            private_json(output / f'{mode}.json', report)
            failed |= bool(report['errors'] or not report['requests'])
            print(json.dumps({key: report[key] for key in
                              ('mode', 'requests', 'errors', 'page_ms', 'audit_ms', 'mbps')}), flush=True)
        except Exception as error:
            private_json(output / f'{mode}-error.json', {'error': str(error), 'time': time.time()})
            failed = True
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
