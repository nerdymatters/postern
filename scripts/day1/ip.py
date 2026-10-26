"""Compare the proxy's observed IP with an independently known cellular public IP."""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path

from .common import checked_base_url, private_json
from .load import Client


def probe(base, expected):
    expected = str(ipaddress.ip_address(expected))
    client = Client(base)
    try:
        responses = []
        for headers in ({}, {'X-Forwarded-For': '192.0.2.123', 'X-Real-IP': '192.0.2.124'}):
            status, _, body, _ = client.get('/probe/ip', extra=headers)
            if status != 200:
                raise ValueError(f'IP probe returned HTTP {status}')
            responses.append(json.loads(body))
    finally:
        client.close()
    passed = all(r.get('observed_ip') == expected and r.get('forwarding_headers_trusted') for r in responses)
    return {'expected_ip': expected, 'responses': responses, 'ip_and_spoof_check_passed': passed,
            'note': 'Qualifies only when run from the stated external/cellular network through the office public port.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--expected-ip', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = probe(checked_base_url(args.base_url), args.expected_ip)
    private_json(args.output, result)
    print('IP check passed' if result['ip_and_spoof_check_passed'] else 'IP check failed')
    return 0 if result['ip_and_spoof_check_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
