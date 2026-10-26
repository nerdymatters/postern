"""Install the optional status heartbeat as a user LaunchAgent, after cloud setup.

Copies the sender out of the checkout so workspace removal cannot stop reporting.
Requires ~/.config/postern/status-heartbeat.json with a mode-0600 token first.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', type=Path, default=Path('~/.config/postern/status-heartbeat.json'))
    parser.add_argument('--label', default='org.nerdymatters.postern.status-heartbeat')
    args = parser.parse_args(argv)
    home = Path.home()
    config = args.config.expanduser().resolve()
    source = Path(__file__).with_name('status-heartbeat.py')
    spec = importlib.util.spec_from_file_location('postern_heartbeat_installer', source)
    sender = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sender)
    try:
        sender.load_settings(config)
    except Exception:
        raise SystemExit('Create an owner-only heartbeat config with a valid token, HTTPS URL and agent targets first.')
    label = args.label
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,127}', label):
        raise SystemExit('Invalid launchd label.')
    target = f'gui/{os.getuid()}/{label}'
    # Avoid overwriting an existing deliberate installation.
    plist = home / 'Library/LaunchAgents' / (label + '.plist')
    if plist.exists():
        raise SystemExit('Heartbeat plist already exists; inspect it before updating.')
    for path in plist.parent.glob('*.plist'):
        try:
            arguments = plistlib.loads(path.read_bytes()).get('ProgramArguments', [])
        except (OSError, ValueError, AttributeError, plistlib.InvalidFileException):
            continue
        if isinstance(arguments, list) and any(isinstance(value, str) and Path(value).name == 'status-heartbeat.py' for value in arguments):
            raise SystemExit('A heartbeat job is already installed; update that installation explicitly instead.')
    # A .venv interpreter vanishes with its workspace. Validate before writing any installed files.
    python = Path(getattr(sys, '_base_executable', sys.executable)).resolve()
    checkout = Path(__file__).resolve().parents[2]
    if python.is_relative_to(checkout) or not os.access(python, os.X_OK):
        raise SystemExit('Install using a Python interpreter outside this checkout.')
    installed = home / 'Library/Application Support/PosternStatus/status-heartbeat.py'
    installed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, installed)
    logs = home / 'Library/Logs/postern'
    logs.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    document = {'Label': label, 'ProgramArguments': [str(python), str(installed), '--config', str(config)],
                'RunAtLoad': True, 'StartInterval': 60,
                'StandardOutPath': str(logs / 'status-heartbeat.log'),
                'StandardErrorPath': str(logs / 'status-heartbeat.log')}
    plist.write_bytes(plistlib.dumps(document))
    subprocess.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist)], check=True)
    print('Installed Postern status heartbeat; runs every minute while this user is logged in.')
    print('Inspect:', target)


if __name__ == '__main__':
    main()
