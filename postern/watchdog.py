"""Watch the health endpoint from outside the server process (ENG-D8).

    .venv/bin/python -m postern.watchdog run   --config ~/.config/postern/config.json [--interval 30] [--failures 3]
    .venv/bin/python -m postern.watchdog check --config ~/.config/postern/config.json

Installed by postern.serve as its own launchd job. It imports nothing from the application — only the
config file's port, data_dir and optional smtp block — so a server that is wedged, crash-looping or
gone cannot take its own alerting down with it.

SMTP settings live under "watchdog" in the config file, or in watchdog.json beside it (that file can be
mode 0600 and hold the app password without the server ever reading it). With neither, alerts are
appended to a file in the data directory and the watchdog says so on startup.

/health carries no post titles, reader emails or other protected data, so its body is safe to put in an
alert. A degraded-but-answering server (paused allowlist, failing audit writes) shows up in that body;
the email is about the service not answering at all.

When progress-backend.json beside the config selects the optional Rust backend, the watchdog also
guards it: failed health checks, the API's memory and sustained CPU, and a periodic read-only parity
check against the Python reducer. On any breach it writes Python back into that file, restarts the
verified API process and sends one alert. It never selects Rust; the owner does.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
import signal
import smtplib
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

SELECTION = 'progress-backend.json'      # the same file postern.progress_backend reads at process start
ACTIVE = 'progress-backend-active.json'  # written by postern.serve run: the serving pid and its backend
GUARD = 'progress-backend-guard.json'    # written here while guarding: which watchdog checked which selection


def probe(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200, response.read(2000).decode(errors='replace').strip()
    except urllib.error.HTTPError as error:
        return False, f'HTTP {error.code}'
    except Exception as error:
        return False, f'{type(error).__name__}: {error}'


class Email:
    def __init__(self, mail):
        self.mail = mail

    def __str__(self):
        return f'email to {self.mail["to"]} via {self.mail["host"]}'

    def __call__(self, subject, body):
        message = EmailMessage()
        message['Subject'] = subject
        message['From'] = self.mail.get('sender') or self.mail.get('user')
        message['To'] = self.mail['to']
        message.set_content(body)
        port = int(self.mail.get('port', 587))
        server = (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)(self.mail['host'], port, timeout=20)
        with server:
            if port != 465:
                server.starttls()
            if self.mail.get('user'):
                server.login(self.mail['user'], self.mail['password'])
            server.send_message(message)


class LogFile:
    def __init__(self, path):
        self.path = Path(path)

    def __str__(self):
        return f'no smtp settings; alerts are appended to {self.path}'

    def __call__(self, subject, body):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a') as log:
            log.write(f'{time.strftime("%Y-%m-%dT%H:%M:%S%z")} {subject}\n{body}\n\n')


class Watchdog:
    """N consecutive failures raise one alert; the next success clears it with a recovery notice."""

    def __init__(self, check, notify, failures=3, interval=30, now=time.time):
        self.check, self.notify, self.limit, self.interval, self.now = check, notify, failures, interval, now
        self.consecutive, self.alerted, self.since = 0, False, None

    def tick(self):
        ok, detail = self.check()
        if ok:
            if self.alerted:
                self.notify('Postern is answering again',
                            f'Recovered after {int(self.now() - self.since)}s down and '
                            f'{self.consecutive} failed checks.\n\n{detail}')
            self.consecutive, self.alerted, self.since = 0, False, None
            return True
        self.consecutive += 1
        if self.since is None:
            self.since = self.now()
        if self.consecutive >= self.limit and not self.alerted:
            self.notify('Postern is not answering',
                        f'{self.consecutive} consecutive health checks failed, '
                        f'{self.interval}s apart.\n\nLast result: {detail}')
            self.alerted = True
        return False

    def run(self, sleep=time.sleep, cycles=None):
        while cycles is None or cycles > 0:
            self.tick()
            if cycles is not None:
                cycles -= 1
            sleep(self.interval)


def cpu_seconds(text):
    """ps's cumulative CPU time: [[dd-]hh:]mm:ss.ss."""
    days, _, clock = text.strip().rpartition('-')
    parts = [float(part) for part in clock.split(':')]
    seconds = 0.0
    for part in parts:
        seconds = seconds*60 + part
    return seconds + (int(days) if days else 0)*86400


LIMITS = {'max_rss_mib': 1536.0, 'max_cpu_percent': 90.0, 'cpu_checks': 10, 'parity_every_seconds': 900.0}


def limits(selection):
    """The owner's guard limits; a missing, non-numeric or non-positive value keeps its default."""
    given = selection.get('limits') if isinstance(selection.get('limits'), dict) else {}
    chosen = {}
    for key, default in LIMITS.items():
        value = given.get(key)
        ok = type(value) in (int, float) and value > 0
        chosen[key] = type(default)(value) if ok else default
    return chosen


def write_python(path, reasons, previous):
    """Atomically select Python, keeping the owner's limits so a later re-selection of Rust keeps them."""
    data = {'backend': 'python', 'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'fallback': {'from': 'rust', 'reasons': reasons}}
    if isinstance(previous.get('limits'), dict):
        data['limits'] = previous['limits']
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.progress-backend-', delete=False) as output:
        os.chmod(output.name, 0o600)
        json.dump(data, output, indent=2)
        output.write('\n')
    os.replace(output.name, path)


class Fallback:
    """Return the optional Rust backend to Python when a guard fails. It never selects Rust.

    Guards apply while the owner's selection file says rust. A serving process that recorded a
    launchd environment override is outside the file, so a breach there is reported and never changed.
    """

    def __init__(self, config_path, data_dir, port, notify, *, failures=3, interval=30, run=subprocess.run,
                 kill=os.kill, now=time.monotonic, sleep=time.sleep, python=sys.executable, exit_wait=30):
        self.config_path, self.data_dir, self.port, self.notify = Path(config_path), Path(data_dir), port, notify
        self.failures, self.interval, self.run, self.kill, self.now = failures, interval, run, kill, now
        self.sleep, self.python, self.exit_wait = sleep, python, exit_wait
        self.watched = None  # the identity (inode, mtime) of the selection being guarded
        self.handled = None  # the selection a breach already returned to Python
        self.reset()

    def reset(self):
        self.unhealthy = self.big = self.hot = self.parity_errors = 0
        self.cpu = None
        self.next_parity = None
        self.reported = self.error_reported = False

    def selection(self):
        path = self.config_path.with_name(SELECTION)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        # Every write replaces the file, so (inode, mtime) identifies one selection, even within a second.
        identity = (info.st_ino, info.st_mtime_ns)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            return {'backend': 'invalid', 'identity': identity}
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return {'backend': 'invalid', 'identity': identity}
        if not isinstance(data, dict):
            return {'backend': 'invalid', 'identity': identity}
        return {**data, 'identity': identity}

    def active(self):
        try:
            data = json.loads((self.data_dir / ACTIVE).read_text())
            return data if isinstance(data, dict) and isinstance(data.get('pid'), int) else None
        except (OSError, ValueError):
            return None

    def listening(self):
        result = self.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{self.port}', '-sTCP:LISTEN', '-t'],
                          capture_output=True, text=True, timeout=10)
        return {int(line) for line in result.stdout.split() if line.isdigit()}

    def process(self, pid):
        result = self.run(['/bin/ps', '-o', 'uid=,rss=,time=,command=', '-p', str(pid)],
                          capture_output=True, text=True, timeout=10)
        fields = result.stdout.split(None, 3)
        if result.returncode != 0 or len(fields) != 4:
            return None
        uid, rss, cputime, command = fields
        return {'uid': int(uid), 'rss_mib': int(rss)/1024, 'cpu_seconds': cpu_seconds(cputime), 'command': command}

    def serving(self, active):
        """The verified API process: recorded, listening on our port, ours, and running postern.serve run.
        Any lookup failure means unverified, never an exception that would stop the health count."""
        pid = active.get('pid') if active else None
        try:
            if pid is None or pid not in self.listening():
                return None, None
            info = self.process(pid)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None, None
        if not info or info['uid'] != os.getuid() or '-m postern.serve run' not in info['command']:
            return None, None
        return pid, info

    def parity(self):
        # Bounded well inside the alert window: a slow comparison must not hide an outage for long.
        result = self.run([self.python, '-P', '-m', 'postern.progress_parity', '--config', str(self.config_path)],
                          capture_output=True, text=True, timeout=60)
        out = (result.stdout.strip().splitlines() or [''])[-1]
        if result.returncode not in (0, 3, 4):
            out = (result.stderr.strip().splitlines() or [out])[-1]
        return result.returncode, out[:500]

    def heartbeat(self, key, state):
        """Shows the owner (postern.serve backend) that a current watchdog is guarding this selection."""
        path = self.data_dir / GUARD
        data = {'pid': os.getpid(), 'selection': list(key), 'state': state,
                'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        try:
            with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.progress-backend-guard-', delete=False) as output:
                os.chmod(output.name, 0o600)
                json.dump(data, output)
                output.write('\n')
            os.replace(output.name, path)
        except OSError:
            pass  # A heartbeat is evidence for the owner, never a reason to stop guarding.

    def observe(self, healthy, detail=''):
        """Called after every health check. Returns the reasons when this tick returned Rust to Python."""
        selection = self.selection()
        backend = selection.get('backend') if selection else None
        if backend not in ('rust', 'invalid'):
            self.watched = None
            self.reset()
            return None
        # An unreadable or unsafe file stops the API from starting; health alone decides that case.
        identity = selection['identity']
        if identity != self.watched:
            self.watched = identity
            self.reset()
        if self.handled == identity:
            return None
        # Count health first: nothing below may stop a failing API from being returned to Python.
        self.unhealthy = 0 if healthy else self.unhealthy + 1
        reasons = []
        if self.unhealthy >= self.failures:
            reasons.append(f'{self.unhealthy} consecutive health checks failed ({detail[:200]})')
            if backend == 'invalid':
                reasons.append(f'{SELECTION} was unreadable or not private to this account')
        active = self.active()
        if active is not None and active.get('source') == 'environment':
            if active.get('backend') != 'rust':
                return None  # The launchd job forces Python; the file is not in effect.
            forced = True
        else:
            forced = False
        try:
            pid, info = self.serving(active)
            if info and active.get('backend') == 'rust':
                reasons += self.resources(selection, pid, info, healthy and not reasons)
        except Exception as error:  # A guard bug must never silently disarm the fallback.
            pid = None
            if not self.error_reported:
                self.notify('Postern: Rust backend guard error',
                            f'{type(error).__name__}: {error}\n\nHealth checks still return Rust to Python.')
                self.error_reported = True
        self.heartbeat(identity, 'guarding')
        if not reasons:
            return None
        if forced:
            if not self.reported:
                self.notify('Postern: Rust backend breach under a launchd override',
                            'The launchd job sets POSTERN_PROGRESS_BACKEND, which the watchdog cannot change.\n\n'
                            + '\n'.join(reasons))
                self.reported = True
            return None
        return self.fall_back(selection, reasons, pid)

    def resources(self, selection, pid, info, parity_allowed):
        chosen = limits(selection)
        reasons = []
        self.big = self.big + 1 if info['rss_mib'] > chosen['max_rss_mib'] else 0
        if self.big >= 2:
            reasons.append(f'API memory {info["rss_mib"]:.0f} MiB above {chosen["max_rss_mib"]:.0f} MiB')
        moment = self.now()
        if self.cpu and self.cpu[0] == pid and moment > self.cpu[2]:
            percent = (info['cpu_seconds']-self.cpu[1])/(moment-self.cpu[2])*100
            self.hot = self.hot + 1 if percent > chosen['max_cpu_percent'] else 0
            if self.hot >= chosen['cpu_checks']:
                reasons.append(f'API CPU above {chosen["max_cpu_percent"]:.0f}% for {self.hot} checks')
        self.cpu = (pid, info['cpu_seconds'], moment)
        if self.next_parity is None:
            self.next_parity = moment + min(chosen['parity_every_seconds'], 300)  # a first comparison soon after the switch
        if parity_allowed and not reasons and moment >= self.next_parity:
            self.next_parity = moment + chosen['parity_every_seconds']
            try:
                code, summary = self.parity()
            except (OSError, subprocess.SubprocessError) as error:
                code, summary = None, type(error).__name__
            if code == 3:
                reasons.append(f'Rust and Python progress differ: {summary}')
            elif code in (0, 4):
                self.parity_errors = 0
            else:
                self.parity_errors += 1
                print(f'FAIL parity check (exit {code}): {summary}', flush=True)
                if self.parity_errors >= 3:
                    reasons.append(f'the parity check failed {self.parity_errors} times: {summary}')
        return reasons

    def fall_back(self, selection, reasons, pid):
        """Select Python, then restart only a re-verified API; the alert reports what actually happened."""
        self.handled = self.watched
        outcome = 'The next API start uses Python.'
        try:
            write_python(self.config_path.with_name(SELECTION), reasons, selection)
        except OSError as error:
            outcome = f'Could not write Python into {SELECTION} ({type(error).__name__}); select it by hand.'
            pid = None
        try:
            if pid is not None and self.serving(self.active())[0] == pid:
                outcome = self.restart(pid)
        except Exception as error:
            outcome = f'Python is selected, but the API restart failed ({type(error).__name__}); restart it by hand.'
        finally:
            self.notify('Postern returned to the Python backend', '\n'.join(reasons) + '\n\n' + outcome)
        return reasons

    def alive(self, pid):
        """True while the same API process exists (it may already have closed its port), False once it is
        gone, None when ps cannot tell. Only a positive answer may lead to SIGKILL."""
        try:
            info = self.process(pid)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return bool(info) and info['uid'] == os.getuid() and '-m postern.serve run' in info['command']

    def restart(self, pid):
        try:
            self.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return 'The API had already exited; launchd starts it again with Python.'
        # Uvicorn closes its port at once but waits without limit for stuck requests; launchd restarts
        # the job only after the process exits, so wait for the process, not the port.
        deadline = self.now() + self.exit_wait
        while self.now() < deadline:
            self.sleep(1)
            if self.alive(pid) is False:
                return f'Restarted the API (pid {pid}); launchd starts it again with Python.'
        state = self.alive(pid)
        if state is True:
            try:
                self.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return f'The API (pid {pid}) ignored SIGTERM for {self.exit_wait} s and was killed; launchd starts it again with Python.'
        if state is False:
            return f'Restarted the API (pid {pid}); launchd starts it again with Python.'
        return f'Sent SIGTERM to the API (pid {pid}) but could not confirm it exited; check it by hand.'


def notifier(config_path, values):
    mail = values.get('watchdog')
    if mail is None:
        beside = Path(config_path).with_name('watchdog.json')
        mail = json.loads(beside.read_text()) if beside.is_file() else {}
    if mail.get('host') and mail.get('to'):
        return Email(mail)
    return LogFile(Path(values['data_dir']).expanduser() / 'watchdog-alerts.log')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='postern.watchdog', description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('run', 'check', 'test'):
        action = actions.add_parser(name)
        action.add_argument('--config', default='~/.config/postern/config.json')
        action.add_argument('--interval', type=int, default=30, help='seconds between checks (default 30)')
        action.add_argument('--failures', type=int, default=3, help='consecutive failures before an alert (default 3)')
    arguments = parser.parse_args(argv)

    config_path = Path(arguments.config).expanduser()
    values = json.loads(config_path.read_text())
    url = f'http://127.0.0.1:{values.get("port", 8790)}/health'
    if arguments.action == 'check':
        ok, detail = probe(url)
        print(f'{"ok  " if ok else "FAIL"} {url} {detail}')
        return 0 if ok else 2
    where = notifier(config_path, values)
    if arguments.action == 'test':
        # An alert nobody has ever received is not an alert; this sends one through the real path.
        if isinstance(where, LogFile):
            print(f'FAIL no email is configured — {where}')
            return 2
        try:
            where('Postern test alert', 'This is a test. If you can read it, outage alerts from Postern will reach you.')
        except Exception as error:
            print(f'FAIL could not send: {type(error).__name__}: {error}')
            return 2
        print(f'ok   test alert sent — {where}')
        return 0
    print(f'ok   watching {url} every {arguments.interval}s, alerting after {arguments.failures} failures: {where}; '
          f'the Rust backend guard runs while {SELECTION} selects rust', flush=True)
    guard = Fallback(config_path, Path(values['data_dir']).expanduser(), values.get('port', 8790), where,
                     failures=arguments.failures, interval=arguments.interval)

    def check():
        ok, detail = probe(url)
        try:
            guard.observe(ok, detail)
        except Exception as error:  # A broken guard must not stop outage alerts.
            print(f'FAIL backend guard: {type(error).__name__}: {error}', flush=True)
        return ok, detail
    Watchdog(check, where, arguments.failures, arguments.interval).run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
