"""Summarize private Caddy logs; byte counts are HTTP bodies, not ISP billing.

No URLs, headers, IPs or staff identities are collected by the traffic logger.
Health probes are excluded. Run once per minute to refresh the local report.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path


def summarize(paths):
    days = defaultdict(lambda: {'requests': 0, 'response_bytes': 0, 'request_bytes': 0,
                                'server_errors': 0, 'duration_seconds': 0})
    malformed = 0
    first = last = None
    for path in paths:
        opener = gzip.open if path.suffix == '.gz' else open
        try:
            with opener(path, 'rt') as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                        ts = float(entry['ts'])
                        day = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
                        size = int(entry['size'])
                        read = int(entry.get('bytes_read', 0))
                        status = int(entry['status'])
                        duration = float(entry.get('duration', 0))
                        if min(size, read, duration) < 0:
                            raise ValueError('Negative measurement')
                    except (KeyError, TypeError, ValueError, OverflowError):
                        malformed += 1
                        continue
                    first = min(first, ts) if first is not None else ts
                    last = max(last, ts) if last is not None else ts
                    row = days[day]
                    row['requests'] += 1
                    row['response_bytes'] += size
                    row['request_bytes'] += read
                    row['server_errors'] += status >= 500
                    row['duration_seconds'] += duration
        except FileNotFoundError:
            continue  # Rotation may rename a log between glob and open.
    return {'generated_at': datetime.now(timezone.utc).isoformat(), 'timezone': 'UTC',
            'scope': 'Postern API/media HTTP bodies; excludes health probes, frontend CDN, TLS overhead and Google Drive transfers',
            'first_observation': first, 'last_observation': last,
            'malformed_lines': malformed, 'days': dict(sorted(days.items()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--log-directory', type=Path, default=Path.home() / 'Library/Logs/postern/traffic')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = summarize(sorted(args.log_directory.glob('access*.log*')))
    text = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = args.output.with_suffix('.tmp')
        temporary.write_text(text)
        temporary.chmod(0o600)
        temporary.replace(args.output)
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
