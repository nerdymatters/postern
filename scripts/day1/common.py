from __future__ import annotations

import json
import math
import os
from pathlib import Path
from urllib.parse import urlparse


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {f'p{p}': None for p in (50, 95, 99)}
    return {f'p{p}': ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)]
            for p in (50, 95, 99)}


def private_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally replacing a previous run or credentials.
    with path.open('x') as output:
        os.chmod(path, 0o600)
        json.dump(value, output, indent=2)
        output.write('\n')


def checked_base_url(value):
    parsed = urlparse(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('URL must not contain credentials, query parameters or fragments')
    if parsed.scheme != 'https' and not (
        parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
    ):
        raise ValueError('Use HTTPS except for a loopback smoke test')
    return value.rstrip('/')
