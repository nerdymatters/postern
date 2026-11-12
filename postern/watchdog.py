"""Watch the health endpoint from outside the server process (ENG-D8).

    .venv/bin/python -m postern.watchdog run   --config ~/.config/postern/config.json [--interval 30] [--failures 3]
    .venv/bin/python -m postern.watchdog check --config ~/.config/postern/config.json

Installed by postern.serve as its own launchd job or systemd unit. It imports nothing from the application — only the
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
import math
import os
import signal
import shlex
import re
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
CHECK = 'progress-backend-check.json'    # written by a Rust-serving API: its own drift checks and native errors
NATIVE_ERRORS = 3  # native errors the serving process may report before the guard counts a breach
GUARD_VERSION = 2  # heartbeats from v0.24.1 on record what ran; postern.serve backend refuses older ones


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
# Above these the guard would be off in practice; keep the default instead.
CEILINGS = {'max_rss_mib': 1024.0*1024, 'max_cpu_percent': 10000.0, 'cpu_checks': 10000, 'parity_every_seconds': 86400.0}


def limits(selection):
    """The owner's guard limits. A missing, non-numeric, non-finite, out-of-range or (for cpu_checks)
    fractional value keeps its default, so a typo can neither disable a guard nor trip it at once."""
    given = selection.get('limits') if isinstance(selection.get('limits'), dict) else {}
    chosen = {}
    for name, default in LIMITS.items():
        value = given.get(name)
        # Compare before any float conversion: a huge JSON integer must keep the default, not raise.
        ok = (type(value) in (int, float) and 0 < value <= CEILINGS[name] and math.isfinite(value)
              and (name != 'cpu_checks' or (value >= 1 and float(value).is_integer())))
        chosen[name] = type(default)(value) if ok else default
    return chosen


def write_private_json(path, data, prefix, indent=None):
    """Atomically replace path with owner-only JSON; a failed write leaves no temporary file behind."""
    output = tempfile.NamedTemporaryFile('w', dir=path.parent, prefix=prefix, delete=False)
    try:
        with output:
            os.chmod(output.name, 0o600)
            json.dump(data, output, indent=indent)
            output.write('\n')
            if sys.platform == 'linux' and path.name == SELECTION:
                output.flush()
                os.fsync(output.fileno())
        os.replace(output.name, path)
        if sys.platform == 'linux' and path.name == SELECTION:
            # Keep this stdlib-only: selecting Python must survive a crash before we stop Rust.
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException:
        Path(output.name).unlink(missing_ok=True)
        raise


def write_python(path, reasons, previous):
    """Atomically select Python, keeping the owner's limits so a later re-selection of Rust keeps them."""
    data = {'backend': 'python', 'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'fallback': {'from': 'rust', 'reasons': reasons}}
    if isinstance(previous.get('limits'), dict):
        data['limits'] = previous['limits']
    write_private_json(path, data, '.progress-backend-', indent=2)


class Fallback:
    """Return the optional Rust backend to Python when a guard fails. It never selects Rust.

    Guards apply while the owner's selection file says rust. A serving process that recorded a
    launchd environment override is outside the file, so a breach there is reported and never changed.
    The heartbeat records what actually ran, so `postern.serve backend` can tell an armed guard from one
    that cannot verify the serving process.
    """

    def __init__(self, config_path, data_dir, port, notify, *, failures=3, interval=30, run=subprocess.run,
                 kill=os.kill, killpg=os.killpg, now=time.monotonic, sleep=time.sleep, python=sys.executable,
                 exit_wait=120):
        self.config_path, self.data_dir, self.port, self.notify = Path(config_path), Path(data_dir), port, notify
        self.failures, self.interval, self.run, self.kill, self.killpg, self.now = failures, interval, run, kill, killpg, now
        self.sleep, self.python, self.exit_wait = sleep, python, exit_wait
        self.watched = None  # the identity (inode, mtime) of the selection being guarded
        self.handled = None  # the selection a breach already returned to Python
        self.retry = (None, 0)  # (fallback selection, restarts tried) for a restart the fallback could not finish
        self.unconfirmed = (None, 0, False)  # (fallback selection, checks the Rust process stayed unconfirmable, alerted)
        self.reset()

    def reset(self):
        self.unhealthy = self.big = self.hot = self.parity_errors = 0
        self.cpu = None
        self.next_parity = None
        self.unverified = 0
        self.pending = []  # a memory, CPU or parity breach stays a breach until Python is written
        self.reported = self.error_reported = self.write_reported = self.unverified_reported = False

    def alert(self, subject, body):
        """A failing notifier must never skip the fallback or the restart that follows it."""
        try:
            self.notify(subject, body)
        except Exception as error:
            print(f'FAIL alert not sent ({type(error).__name__}): {subject}', flush=True)

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
        # The API refuses any other value at startup (for example "Rust"), so treat it like an unreadable file.
        if not isinstance(data, dict) or data.get('backend') not in ('python', 'rust'):
            return {'backend': 'invalid', 'identity': identity}
        return {**data, 'identity': identity}

    def active(self):
        try:
            data = json.loads((self.data_dir / ACTIVE).read_text())
            return data if isinstance(data, dict) and isinstance(data.get('pid'), int) else None
        except (OSError, ValueError):
            return None

    def listening(self):
        result = self.run(['/usr/bin/lsof' if sys.platform == 'linux' else '/usr/sbin/lsof', '-nP', f'-iTCP:{self.port}', '-sTCP:LISTEN', '-t'],
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
        data = {'pid': os.getpid(), 'guard_version': GUARD_VERSION, 'selection': list(key), 'state': state,
                'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        try:
            write_private_json(path, data, '.progress-backend-guard-')
        except OSError:
            pass  # A heartbeat is evidence for the owner, never a reason to stop guarding.

    def supervisor(self):
        return 'systemd' if sys.platform == 'linux' else 'launchd'

    def removal(self):
        """Where the owner's runbook says how to remove the backend setting from the job."""
        return "the Linux runbook's Python fallback section" if sys.platform == 'linux' else 'runbook step 3'

    def unit_override(self):
        """Read systemd's merged Environment, including drop-ins, even if the API failed to start.
        EnvironmentFile contents are not expanded by show; refuse automatic fallback when those
        could override the selection. The live API record also detects inherited overrides."""
        if sys.platform != 'linux':
            return None
        unit = os.environ.get('POSTERN_API_UNIT', 'org.nerdymatters.postern.service')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,127}\.service', unit):
            raise ValueError('invalid POSTERN_API_UNIT')
        result = self.run(['systemctl', '--system', 'show', unit, '--property=Environment',
                           '--property=EnvironmentFiles'], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise OSError('cannot read systemd unit environment')
        properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        # EnvironmentFile values win over Environment, so even a visible Python override is
        # inconclusive when a file could replace it with Rust.
        if properties.get('EnvironmentFiles', '').strip():
            return 'unknown (EnvironmentFile)'
        for value in shlex.split(properties.get('Environment', '')):
            if value.startswith('POSTERN_PROGRESS_BACKEND='):
                return value.split('=', 1)[1]
        return None

    def observe(self, healthy, detail=''):
        """Called after every health check. Returns the reasons when this tick returned Rust to Python."""
        selection = self.selection()
        backend = selection.get('backend') if selection else None
        try:
            override = self.unit_override()
        except (OSError, ValueError, subprocess.SubprocessError):
            override = 'unknown (unit environment unreadable)'
        unknown = (override or '').startswith('unknown')
        # finish_fallback acts only on a process whose own record says it took Rust from the file, which an
        # unprovable unit environment cannot have overridden; a unit that visibly sets the backend still stops it.
        if backend == 'python' and selection.get('fallback') and (override is None or unknown):
            self.finish_fallback(selection)
        active, dropped = self.active(), False
        if (active is not None and active.get('source') == 'environment'
                and (backend in ('rust', 'invalid') or active.get('backend') == 'rust')
                and self.alive(active['pid']) is False):
            # A record left by a process that has exited (say, before a start refused on a busy port) is not an
            # override. Only a process that is certainly gone is ignored: an unknown answer keeps the override.
            active, dropped = None, True
        if unknown and backend not in ('rust', 'invalid') and not (
                active is not None and active.get('backend') == 'rust'):
            # Nothing selects or serves Rust, so an unprovable unit environment has nothing to breach; watching it
            # would raise a Rust breach alert on a Python-only host. A live API forced to Rust still reports it.
            override = None
        elif (unknown and active is not None and active.get('source') == SELECTION
                and self.alive(active['pid']) is not False):
            # The serving process took its backend from the file, so the unit did not override it. A failed or
            # inconclusive lookup must not make this process's guard alert-only or reset its counts: a blip on
            # every other tick would otherwise stall the fallback for good.
            override = None
        forced = override is not None or (active is not None and active.get('source') == 'environment')
        forced_rust = forced and (override not in (None, 'python') or (active and active.get('backend') == 'rust'))
        # A launchd job that forces Rust is watched (alert only) even without a Rust selection file.
        if backend not in ('rust', 'invalid') and not forced_rust:
            self.watched = None
            self.reset()
            return None
        # An unreadable, unsafe or unknown selection stops the API from starting; health alone decides that case.
        identity = selection['identity'] if backend in ('rust', 'invalid') else ('environment', active.get('pid') if active else override)
        # Counts from an alert-only override period never carry into the file-governed process, or back.
        key = (identity, forced)
        if key != self.watched:
            self.watched = key
            self.reset()
        if self.handled == key:
            return None
        # Count health first: nothing below may stop a failing API from being returned to Python.
        self.unhealthy = 0 if healthy else self.unhealthy + 1
        reasons = []
        if self.unhealthy >= self.failures:
            reasons.append(f'{self.unhealthy} consecutive health checks failed ({detail[:200]})')
            if backend == 'invalid':
                reasons.append(f'{SELECTION} was unreadable, not private to this account, or not python or rust')
        if forced and not forced_rust:
            self.heartbeat(identity, 'override')
            return None  # The launchd job forces Python; the file is not in effect.
        pid, info = self.serving(active)  # never raises: a lookup failure is "unverified"
        override_detail = (f'The systemd unit environment overrides the selection or cannot be verified ({override}).'
                           if sys.platform == 'linux' else
                           'The launchd job sets POSTERN_PROGRESS_BACKEND, which the watchdog cannot change.')
        fallback = (f'Nothing returns Rust to Python while the {self.supervisor()} job sets POSTERN_PROGRESS_BACKEND; remove that '
                    f'setting ({self.removal()}) so the watchdog can guard the selection file.' if forced
                    else 'Failed health checks still return Rust to Python.')
        state = 'waiting'  # Rust is selected but the API has not started with it yet
        breach = []
        if active is not None and active.get('backend') == 'rust':
            if info:
                state = 'guarding'
                self.unverified = 0
                try:
                    breach = self.resources(selection or {}, pid, info, healthy and not reasons and not self.pending)
                except Exception as error:  # A guard bug must never silently disarm the fallback.
                    state = 'error'
                    if not self.error_reported:
                        self.alert('Postern: Rust backend guard error', f'{type(error).__name__}: {error}\n\n{fallback}')
                        self.error_reported = True
            else:
                state = 'unverified'
                self.unverified = self.unverified + 1 if healthy else 0
                if self.unverified >= self.failures and not self.unverified_reported:
                    self.alert('Postern: cannot verify the Rust API process',
                               f'lsof or ps could not confirm the process serving Rust on port {self.port}, so the '
                               f'memory, CPU and parity checks are paused. {fallback}')
                    self.unverified_reported = True
        if forced:
            reasons += breach  # alert only: a breach under an override is never kept for the file
        else:
            self.pending = breach or self.pending
            reasons += self.pending
        self.heartbeat(identity, 'override' if forced else state)
        if not reasons:
            return None
        if forced:
            if not self.reported:
                self.alert(f'Postern: Rust backend breach under a {self.supervisor()} override',
                           override_detail + '\n\n' + '\n'.join(reasons))
                self.reported = True
            return None
        return self.fall_back(selection, reasons, pid, dropped)

    def finish_fallback(self, selection):
        """A fallback selected Python but could not restart the API (a re-verification error, or a restart
        that failed): restart it once it verifies as still serving Rust from the file, at most three times."""
        active = self.active()
        if not active or active.get('backend') != 'rust' or active.get('source') != SELECTION:
            return
        if sys.platform == 'linux':
            try:
                # A previous rename may have succeeded while its directory sync failed. Do not
                # treat the visible Python selection as durable until that sync can finish.
                with self.config_path.with_name(SELECTION).open('rb') as selected:
                    os.fsync(selected.fileno())
                descriptor = os.open(self.config_path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                return  # The earlier write alert remains true; retry on the next observation.
        identity = selection['identity']
        if self.retry[0] != identity:
            self.retry = (identity, 0)
        if self.retry[1] >= 3:
            return
        pid, _ = self.serving(active)
        if pid is None and self.alive(active['pid']) is False:
            return  # the recorded Rust process has exited; launchd starts the API again with Python
        if pid is None:
            seen, count, reported = self.unconfirmed
            count = count + 1 if seen == identity else 1
            reported = reported if seen == identity else False
            if count >= self.failures and not reported:
                self.alert('Postern could not confirm the return to Python',
                           f'Python is selected, but the serving record still shows Rust (pid {active.get("pid")}) '
                           'and lsof or ps cannot confirm that process. Check it and restart the API by hand if it '
                           'still serves Rust.')
                reported = True
            self.unconfirmed = (identity, count, reported)
            return
        self.unconfirmed = (identity, 0, self.unconfirmed[2] if self.unconfirmed[0] == identity else False)
        self.retry = (identity, self.retry[1] + 1)
        try:
            outcome = self.restart(pid)
        except Exception as error:
            outcome = f'The restart failed ({type(error).__name__}); restart the API by hand.'
        self.alert('Postern finished returning to the Python backend',
                   f'Python was selected, but the API (pid {pid}) still served Rust.\n\n{outcome}')

    def in_process(self, pid):
        """What the verified serving process reported about its own Rust progress paths. A record from
        another process (an earlier start) is ignored; a missing or unreadable one reports nothing."""
        try:
            data = json.loads((self.data_dir / CHECK).read_text())
        except (OSError, ValueError):
            return []
        if not isinstance(data, dict) or data.get('pid') != pid:
            return []
        reasons = []
        mismatches, errors = data.get('mismatches'), data.get('native_errors')
        if type(mismatches) is int and mismatches > 0:
            reasons.append("the API's own check found its Rust staff report differs from a full Python replay "
                           f'({mismatches} time(s))')
        if type(errors) is int and errors >= NATIVE_ERRORS:
            reasons.append(f"{errors} native errors on the API's progress paths "
                           f'(last: {str(data.get("last_error"))[:80]})')
        return reasons

    def resources(self, selection, pid, info, parity_allowed):
        chosen = limits(selection)
        reasons = self.in_process(pid)
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

    def fall_back(self, selection, reasons, pid, dropped_override=False):
        """Select Python, then restart only a re-verified API; the alert reports what actually happened."""
        outcome = 'The next API start uses Python.'
        if dropped_override:  # the last start was forced by launchd; the file decides only once that setting is gone
            outcome = ('Python is selected, but the last API start took its backend from POSTERN_PROGRESS_BACKEND in the '
                       f'{self.supervisor()} job; while the job sets it, that setting decides the next start ({self.removal()} removes it).')
        try:
            write_python(self.config_path.with_name(SELECTION), reasons, selection)
        except OSError as error:
            # Not handled: the next tick tries again (self.pending keeps a resource breach). Alert once per selection.
            if not self.write_reported:
                self.alert('Postern could not return to the Python backend',
                            '\n'.join(reasons) + f'\n\nCould not write Python into {SELECTION} '
                            f'({type(error).__name__}); the watchdog keeps retrying. Select it by hand if this persists.')
                self.write_reported = True
            return reasons
        self.handled = self.watched
        self.pending = []
        try:
            active = self.active()
            if pid is not None and self.serving(active)[0] == pid:
                outcome = self.restart(pid)
            elif (active and active.get('backend') == 'rust' and active.get('source') == SELECTION
                  and self.alive(active['pid']) is not False):  # only a file-governed process is restarted later
                outcome += (f' The API recorded as serving Rust (pid {active.get("pid")}) could not be confirmed, so it '
                            'was not restarted; the watchdog restarts it once it can confirm it still serves Rust.')
        except Exception as error:
            outcome = f'Python is selected, but the API restart failed ({type(error).__name__}); restart it by hand.'
        finally:
            self.alert('Postern returned to the Python backend', '\n'.join(reasons) + '\n\n' + outcome)
        return reasons

    def alive(self, pid):
        """True while the same API process exists (it may already have closed its port), False once it is
        gone, None when ps cannot tell. Only a positive answer may lead to SIGKILL."""
        try:
            info = self.process(pid)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return bool(info) and info['uid'] == os.getuid() and '-m postern.serve run' in info['command']

    def converters(self, pid):
        """Process groups of this API's own media converters (derive_child.py in a session of its own).
        None means the lookup failed, so nothing is known; a converter is never guessed. Other processes' command
        lines may hold bytes that are not UTF-8, so they are decoded leniently and never stop the restart."""
        try:
            result = self.run(['/bin/ps', '-A', '-o', 'pid=,ppid=,pgid=,uid=,command='],
                              capture_output=True, encoding='utf-8', errors='replace', timeout=10)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        groups = []
        for line in result.stdout.splitlines():
            fields = line.split(None, 4)
            if len(fields) == 5 and all(field.isdigit() for field in fields[:4]):
                child, parent, group, uid = map(int, fields[:4])
                if parent == pid and group == child and uid == os.getuid() and 'derive_child.py' in fields[4]:
                    groups.append(group)
        return groups

    def restart(self, pid):
        try:
            self.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return f'The API had already exited; {self.supervisor()} starts it again with Python.'
        # Uvicorn closes its port at once but waits without limit for stuck requests and in-flight media
        # conversions (90 s each); launchd restarts the job only after the process exits, so wait for the
        # process, not the port, and longer than one conversion before SIGKILL could orphan its converter.
        deadline = self.now() + self.exit_wait
        while self.now() < deadline:
            self.sleep(1)
            if self.alive(pid) is False:
                return f'Restarted the API (pid {pid}); {self.supervisor()} starts it again with Python.'
        state = self.alive(pid)
        if state is True:
            try:
                groups = self.converters(pid)  # media conversions run in sessions of their own
            except Exception:
                groups = None
            try:
                self.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # After the API is gone, so it can never record a killed conversion as the upload's fault.
            ended = 0
            for group in groups or []:
                try:
                    self.killpg(group, signal.SIGKILL)
                    ended += 1
                except (ProcessLookupError, PermissionError):
                    pass
            also = (f' with {ended} media conversion(s) it was still running' if ended else
                    '; its media conversions could not be listed, so any still running finish by themselves'
                    if groups is None else '')
            return (f'The API (pid {pid}) ignored SIGTERM for {self.exit_wait} s and was killed{also}; '
                    f'{self.supervisor()} starts it again with Python.')
        if state is False:
            return f'Restarted the API (pid {pid}); {self.supervisor()} starts it again with Python.'
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
