"""Send only allowlisted launchd process states to Postern's cloud status page.

Run once per minute via launchd. No application credentials, data, logs, or PIDs
leave this host. Credentials belong in a separate owner-only config file.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

AGENTS = {
    'api-process': 'system/org.nerdymatters.postern',
    'proxy': 'system/org.nerdymatters.postern.proxy',
    'watchdog': 'system/org.nerdymatters.postern.watchdog',
    'tunnel': f'gui/{os.getuid()}/org.nerdymatters.postern.tailscaled',
}


def load_settings(path, *, sending=True):
    """Private deployment settings; tokens are sent only to an explicit HTTPS endpoint."""
    info = path.stat()
    if info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError('Config must be owned by this user with mode 0600')
    settings = json.loads(path.read_text())
    if not isinstance(settings, dict):
        raise ValueError('Config must be an object')
    agents = settings.get('agents', AGENTS)
    if not isinstance(agents, dict) or set(agents) != set(AGENTS) or any(
        not isinstance(target, str) or not re.fullmatch(r'(?:system|gui/\d+)/[A-Za-z0-9][A-Za-z0-9.-]{0,127}', target)
        for target in agents.values()
    ):
        raise ValueError('Invalid launchd agent targets')
    if sending:
        token, url = settings.get('token'), settings.get('url')
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError('Invalid heartbeat token')
        if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError('An HTTPS heartbeat URL is required')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('An HTTPS heartbeat URL without credentials or fragment is required')
        parsed.port  # Reject malformed ports before preparing an authenticated request.
    return {**settings, 'agents': agents}


def process_state(target, runner=subprocess.run):
    try:
        result = runner(['/bin/launchctl', 'print', target], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'
    if result.returncode != 0:
        # Permission errors are not proof that the monitored process is down.
        return 'outage' if 'Could not find service' in result.stderr else 'unknown'
    state = re.search(r'^\s*state = (.+)$', result.stdout, re.MULTILINE)
    if not state:
        return 'unknown'
    return 'operational' if state.group(1) == 'running' else 'outage'


def collect(targets=AGENTS):
    agents = {key: process_state(target) for key, target in targets.items()}
    return {'sent_at': int(time.time() * 1000), 'agents': agents}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the bearer token to a redirect target.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', default='~/.config/postern/status-heartbeat.json')
    parser.add_argument('--check', action='store_true', help='Print local states without sending anything')
    args = parser.parse_args(argv)
    path = Path(args.config).expanduser()
    try:
        settings = load_settings(path, sending=not args.check) if path.exists() or not args.check else {'agents': AGENTS}
        body = collect(settings['agents'])
        if args.check:
            print(json.dumps(body))
            return 0
        request = urllib.request.Request(settings['url'], data=json.dumps(body).encode(), method='POST',
                                         headers={'Authorization': f'Bearer {settings["token"]}', 'Content-Type': 'application/json',
                                                  'Accept': 'application/json', 'User-Agent': 'PosternStatusHeartbeat/1.0'})
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=15) as response:
            if response.status != 200 or json.loads(response.read(1024)).get('accepted') is not True:
                raise ValueError('Heartbeat was not accepted')
    except urllib.error.HTTPError as error:
        print(f'Postern status heartbeat failed: HTTP {error.code}', flush=True)
        return 1
    except Exception as error:
        # Exception text could contain a credential or local path; log only its type.
        print(f'Postern status heartbeat failed: {type(error).__name__}', flush=True)
        return 1
    print('Postern status heartbeat accepted', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
