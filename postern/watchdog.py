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
"""
from __future__ import annotations
import argparse
import json
import smtplib
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path


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
    print(f'ok   watching {url} every {arguments.interval}s, alerting after {arguments.failures} failures: {where}',
          flush=True)
    Watchdog(lambda: probe(url), where, arguments.failures, arguments.interval).run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
