"""The owner's no-password backend switch: selection precedence, the watchdog's Python fallback and parity.

Nothing here signals a real process or runs launchctl: lsof, ps and the parity subprocess are fakes,
and the clock is a counter.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from postern import progress, progress_backend, progress_parity, progress_store, serve, watchdog
from postern.db import SCHEMA

SETTING_AT_IMPORT = os.environ.get(progress_backend.SETTING)


JOB = 'systemd' if sys.platform == 'linux' else 'launchd'  # the supervisor the watchdog names

@pytest.fixture
def config_path(tmp_path, monkeypatch):
    # setenv first records the original state (even "unset"), so teardown restores it after
    # apply_selection writes os.environ directly.
    monkeypatch.setenv(progress_backend.SETTING, 'python')
    monkeypatch.delenv(progress_backend.SETTING)
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'data_dir': str(data), 'mode': 'demo', 'port': 8790}))
    path.chmod(0o600)
    return path


def select(config_path, backend, **extra):
    return progress_backend.write_selection(config_path, backend, **extra)


def test_environment_wins_then_the_owner_file_then_python(config_path, monkeypatch):
    assert progress_backend.apply_selection(config_path) == ('python', 'default')
    assert progress_backend.SETTING not in os.environ
    select(config_path, 'rust')
    assert progress_backend.apply_selection(config_path) == ('rust', 'progress-backend.json')
    assert os.environ[progress_backend.SETTING] == 'rust'
    # Once set (by launchd or by the first call), the environment is the authority for this process.
    monkeypatch.setenv(progress_backend.SETTING, 'python')
    assert progress_backend.apply_selection(config_path) == ('python', 'environment')


@pytest.mark.parametrize('damage', ['json', 'value', 'mode', 'symlink'])
def test_an_unreadable_or_unsafe_selection_refuses_startup(config_path, tmp_path, damage):
    path = progress_backend.selection_path(config_path)
    if damage == 'json':
        path.write_text('{not json')
        path.chmod(0o600)
    elif damage == 'value':
        path.write_text(json.dumps({'backend': 'fast'}))
        path.chmod(0o600)
    elif damage == 'mode':
        select(config_path, 'rust')
        path.chmod(0o620)
    else:
        target = tmp_path / 'elsewhere.json'
        target.write_text(json.dumps({'backend': 'rust'}))
        path.symlink_to(target)
    with pytest.raises((RuntimeError, ValueError)):
        progress_backend.apply_selection(config_path)
    assert progress_backend.SETTING not in os.environ


def test_selection_and_active_records_are_private_and_complete(config_path):
    data = select(config_path, 'rust', set_by='owner')
    path = progress_backend.selection_path(config_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == data and data['backend'] == 'rust' and data['set_by'] == 'owner'
    with pytest.raises(ValueError):
        progress_backend.write_selection(config_path, 'fast')
    active = progress_backend.record_active(config_path.parent / 'data', 'rust', 'progress-backend.json')
    stored = config_path.parent / 'data' / progress_backend.ACTIVE
    assert stored.stat().st_mode & 0o777 == 0o600 and json.loads(stored.read_text()) == active
    assert active['pid'] == os.getpid()


@pytest.mark.parametrize('state,expected', [
    ('none', 'ok   progress reads: no database yet'),
    ('compact', 'ok   progress reads: the compact progress table'),
    ('building', 'WARN progress reads: the full event history, because the compact table is still being built from the event history'),
    ('nonportable', 'WARN progress reads: the full event history, because 1 row(s) cannot use the compact table'),
    ('unreadable', 'FAIL progress reads: could not read the database (DatabaseError)'),
    ('no_config', 'WARN progress reads: not checked, because the configuration names no readable data directory'),
    ('missing_dir', 'WARN progress reads: not checked, because the configuration names no readable data directory'),
    ('unenterable', 'WARN progress reads: not checked, because the configuration names no readable data directory'),
])
def test_the_backend_command_says_which_history_progress_reads_use(config_path, capsys, state, expected):
    # Value: protects=the owner sees when progress reads fall back to the full event history and why, without the API; fails_when=the line is missing, calls a fallback ok, or an unreadable database raises instead of printing FAIL; why_new=nothing outside the API showed the read source; seam=none; no_claim=the counts themselves (progress_store tests)
    path = config_path.parent / 'data' / 'postern.sqlite3'
    if state == 'unreadable':
        path.write_bytes(b'not a database' * 100)
    elif state == 'no_config':
        config_path.write_text('{')  # a damaged configuration must not read as "no database yet"
    elif state == 'missing_dir':
        values = json.loads(config_path.read_text())
        config_path.write_text(json.dumps({**values, 'data_dir': str(config_path.parent / 'absent')}))
    elif state == 'unenterable':
        path.write_bytes(b'')
        path.parent.chmod(0o000)  # stat of the database inside raises PermissionError
    elif state not in ('none', 'no_config', 'missing_dir', 'unenterable'):
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row  # as the writer's connection
        db.executescript(SCHEMA)
        db.execute("INSERT INTO events(seq,id,email,item_id,type,ts,payload,summary) VALUES "
                   "(NULL,'old','active','pdf','page_served',1,'{\"detail\":{\"page\":1}}',1)")
        if state != 'building':
            while not progress_store.rebuild_batch(db, limit=3):
                pass
        else:
            db.execute('DELETE FROM progress_events')
        if state == 'nonportable':
            db.execute('UPDATE progress_events SET portable=0')
        db.commit()
        db.close()
    try:
        assert serve.main(['backend', '--config', str(config_path)]) == 0
    finally:
        path.parent.chmod(0o700)
    line = next(line for line in capsys.readouterr().out.splitlines() if 'progress reads' in line)
    assert line.startswith(expected)
    if state in ('building', 'nonportable'):
        assert line.endswith('Results are unchanged.')


@pytest.mark.parametrize('state', ['both', 'no_table'])
def test_the_reads_line_names_every_reason_reads_use_the_full_history(tmp_path, state):
    # Value: protects=the owner's progress reads line names both reasons when an older release's row awaits classification after the build beside a row the compact table cannot hold, and names a database that predates the compact table; fails_when=one reason hides the other (an elif), rows waiting after the build is ready give no reason, or a missing table reads as an unfinished build; why_new=the backend-command cases show each reason alone, before the build was ready; seam=none; no_claim=the counts themselves (progress_store tests), that show prints the line (the backend-command test)
    db = sqlite3.connect(tmp_path / 'postern.sqlite3')
    db.row_factory = sqlite3.Row  # as the writer's connection
    if state == 'no_table':
        db.executescript(SCHEMA.replace(progress_store.SCHEMA, ''))  # a database from before the compact table
    else:
        db.executescript(SCHEMA)
        oversized = json.dumps({'detail': {'page': 1}, 'unused': 'x' * (65 * 1024)})
        db.execute("INSERT INTO events(id,email,item_id,type,ts,payload,summary) VALUES ('large','active','pdf','page_served',1,?,1)",
                   (oversized,))
        while not progress_store.rebuild_batch(db, limit=3):
            pass
        # An older release's insert after the build: its trigger row waits for classification.
        db.execute("INSERT INTO events(id,email,item_id,type,ts,payload,summary) VALUES "
                   "('rolled-back','active','pdf','page_served',2,'{\"detail\":{\"page\":2}}',1)")
    db.commit()
    db.close()
    line = serve.reads_line(tmp_path)
    assert line.startswith('WARN progress reads: the full event history, because ') and line.endswith('Results are unchanged.')
    if state == 'both':
        assert 'because 1 row(s) are waiting to be classified for the compact table' in line
        assert '; and 1 row(s) cannot use the compact table' in line
    else:
        assert 'because this database has no compact progress table yet' in line and 'still being built' not in line


def test_serve_run_applies_the_selection_before_the_service_starts(config_path, monkeypatch, capsys):
    select(config_path, 'python')
    seen = {}
    import uvicorn
    from postern import app as application
    monkeypatch.setattr(serve, 'listening', lambda port: False)
    monkeypatch.setattr(application, 'create_app', lambda config: seen.setdefault('backend', os.environ[progress_backend.SETTING]))
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kwargs: seen.setdefault('app', app))
    assert serve.run(config_path) == 0
    assert seen == {'backend': 'python', 'app': 'python'}
    active = json.loads((config_path.parent / 'data' / progress_backend.ACTIVE).read_text())
    assert (active['backend'], active['source'], active['pid']) == ('python', 'progress-backend.json', os.getpid())
    assert 'postern: progress backend python (progress-backend.json)' in capsys.readouterr().err


def test_the_backend_command_shows_and_sets_the_owner_selection(config_path, capsys):
    assert serve.main(['backend', '--config', str(config_path)]) == 0
    printed = capsys.readouterr().out
    assert 'progress-backend.json: absent (Python)' in printed and 'serving process: no record yet' in printed
    assert serve.main(['backend', 'rust', '--config', str(config_path)]) == 0
    printed = capsys.readouterr().out
    assert 'progress-backend.json: rust' in printed and 'Restart the API to apply it' in printed
    assert progress_backend.read_selection(config_path)['set_by'] == 'owner'
    progress_backend.record_active(config_path.parent / 'data', 'rust', 'progress-backend.json')
    assert serve.main(['backend', 'show', '--config', str(config_path)]) == 0
    assert f'serving process {os.getpid()}: rust from progress-backend.json' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        serve.main(['backend', 'fast', '--config', str(config_path)])


class Host:
    """lsof, ps and the parity subprocess for one fake API process."""

    def __init__(self, pid=4242, rss_mib=300, command='/venv/bin/python -P -m postern.serve run --config c'):
        self.pid, self.rss_mib, self.command, self.cpu = pid, rss_mib, command, 0.0
        self.listening, self.parity_codes, self.calls, self.killed = True, [], [], []
        self.alive, self.ignore_term, self.lookup_errors, self.timeouts = True, False, 0, {}
        self.processes = []  # extra `ps -A` rows: (pid, ppid, pgid, uid, command as str or raw bytes)

    def run(self, args, **kwargs):
        self.calls.append((args[0], kwargs.get('timeout')))
        if args[0].endswith(('lsof', 'ps')) and self.lookup_errors:
            self.lookup_errors -= 1
            raise subprocess.TimeoutExpired(args[0], kwargs.get('timeout'))
        if args[0].endswith('lsof'):
            return SimpleNamespace(returncode=0, stdout=f'{self.pid}\n' if self.listening and self.alive else '', stderr='')
        if args[0].endswith('ps') and '-A' in args:
            failure = getattr(self, 'listing_failure', None)
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(args[0], kwargs.get('timeout'))
            if failure == 'exit':
                return SimpleNamespace(returncode=1, stdout='', stderr='ps: failed')
            rows = [(self.pid, 1, self.pid, os.getuid(), self.command)] + self.processes
            raw = b''.join(b'%d %d %d %d %s\n' % (p, pp, g, u, c if isinstance(c, bytes) else c.encode())
                           for p, pp, g, u, c in rows)
            # Decoded as subprocess.run would with these arguments: strict decoding raises on a non-UTF-8 row.
            return SimpleNamespace(returncode=0, stdout=raw.decode(kwargs.get('encoding') or 'utf-8',
                                                                   kwargs.get('errors') or 'strict'), stderr='')
        if args[0].endswith('ps'):
            if not self.alive:
                return SimpleNamespace(returncode=1, stdout='', stderr='')
            minutes, seconds = divmod(self.cpu, 60)
            line = f'{os.getuid()} {int(self.rss_mib*1024)} {int(minutes)}:{seconds:05.2f} {self.command}\n'
            return SimpleNamespace(returncode=0, stdout=line, stderr='')
        if args[0] == 'systemctl':  # Linux reads the API unit's environment; this fake unit is loaded and sets none
            return SimpleNamespace(returncode=0, stdout='LoadState=loaded\nEnvironment=\nEnvironmentFiles=\n', stderr='')
        code = self.parity_codes.pop(0) if self.parity_codes else 0
        if isinstance(code, BaseException):
            raise code
        stderr = ''
        if isinstance(code, tuple):
            code, stderr = code
        return SimpleNamespace(returncode=code, stdout='{"match": %s}\n' % ('true' if code == 0 else 'false'), stderr=stderr)

    def kill(self, pid, sig):
        self.killed.append((pid, sig))
        if sig == signal.SIGTERM:
            self.listening = False  # uvicorn closes its port at once
            self.alive = self.alive and self.ignore_term
        elif sig == signal.SIGKILL:
            self.alive = self.listening = False

    def killpg(self, group, sig):
        self.killed.append(('group', group, sig))

    def respawn(self):
        """launchd started the API again (the fake keeps its pid)."""
        self.alive = self.listening = True


@pytest.fixture
def guard(config_path, monkeypatch):
    host, sent, clock = Host(), [], [0.0]
    monkeypatch.setattr(serve, 'process_exists', lambda pid: pid == host.pid and host.alive)  # the fake API's pid
    fallback = watchdog.Fallback(config_path, config_path.parent / 'data', 8790,
                                 lambda subject, body: sent.append((subject, body)), failures=3, interval=30,
                                 run=host.run, kill=host.kill, killpg=host.killpg, now=lambda: clock[0], python='python',
                                 sleep=lambda seconds: clock.__setitem__(0, clock[0]+seconds))

    def tick(healthy=True, seconds=30, cpu_percent=5.0):
        clock[0] += seconds
        host.cpu += seconds*cpu_percent/100
        return fallback.observe(healthy, 'detail')
    return SimpleNamespace(fallback=fallback, host=host, sent=sent, tick=tick, config=config_path)


def serving(guard, backend='rust', source='progress-backend.json'):
    path = guard.config.parent / 'data' / progress_backend.ACTIVE
    path.write_text(json.dumps({'pid': guard.host.pid, 'backend': backend, 'source': source}))


def stored(guard):
    return json.loads(progress_backend.selection_path(guard.config).read_text())


def test_python_or_no_selection_is_never_guarded(guard):
    assert all(guard.tick(healthy=False) is None for _ in range(5))
    select(guard.config, 'python')
    assert all(guard.tick(healthy=False) is None for _ in range(5))
    assert guard.host.killed == [] and guard.sent == [] and stored(guard)['backend'] == 'python'


def test_three_failed_health_checks_return_rust_to_python_and_restart_the_verified_api(guard):
    select(guard.config, 'rust')
    serving(guard)
    assert guard.tick(healthy=False) is None and guard.tick(healthy=False) is None
    reasons = guard.tick(healthy=False)
    assert reasons and '3 consecutive health checks failed' in reasons[0]
    assert stored(guard)['backend'] == 'python' and stored(guard)['fallback']['from'] == 'rust'
    assert guard.host.killed == [(guard.host.pid, signal.SIGTERM)]
    assert [subject for subject, _ in guard.sent] == ['Postern returned to the Python backend']
    assert 'Restarted the API (pid 4242)' in guard.sent[0][1]
    assert all(guard.tick(healthy=False) is None for _ in range(4))  # handled once, never flips back
    assert len(guard.host.killed) == 1 and stored(guard)['backend'] == 'python'
    # The restarted process has exited, so its leftover Rust record is not an unconfirmable one.
    assert [subject for subject, _ in guard.sent] == ['Postern returned to the Python backend']


def test_memory_cpu_and_parity_guards(guard):
    select(guard.config, 'rust')
    serving(guard)
    guard.host.rss_mib = 2000
    assert guard.tick() is None  # one sample over the limit is not a breach
    reasons = guard.tick()
    assert reasons and 'API memory 2000 MiB above 1536 MiB' in reasons[0]
    guard.host.respawn()

    select(guard.config, 'rust', limits={'cpu_checks': 3})  # the owner re-selects Rust: guarding resumes
    guard.host.rss_mib = 300
    guard.tick()  # first CPU sample
    assert guard.tick(cpu_percent=95) is None and guard.tick(cpu_percent=95) is None
    assert 'API CPU above 90% for 3 checks' in guard.tick(cpu_percent=95)[0]
    guard.host.respawn()

    select(guard.config, 'rust', limits={'parity_every_seconds': 60})
    guard.host.parity_codes = [0, 4, 3]
    assert guard.tick(seconds=60) is None  # first CPU sample; parity scheduled
    assert guard.tick(seconds=60) is None and guard.tick(seconds=60) is None  # match, then not applicable
    assert 'Rust and Python progress differ' in guard.tick(seconds=60)[0]
    assert len(guard.host.killed) == 3 and stored(guard)['backend'] == 'python'


def test_repeated_parity_errors_fail_safe_but_one_does_not(guard):
    select(guard.config, 'rust', limits={'parity_every_seconds': 60})
    serving(guard)
    # A hung or missing parity process counts like a failed run, and never escapes observe().
    guard.host.parity_codes = [1, 0, 1, subprocess.TimeoutExpired('parity', 300), OSError('gone')]
    results = [guard.tick(seconds=60) for _ in range(6)]
    assert results[:5] == [None]*5 and 'the parity check failed 3 times: OSError' in results[5][0]


def test_a_broken_guard_never_stops_outage_alerts(config_path, monkeypatch, capsys):
    # Value: protects=watchdog keeps probing and alerting when the backend guard raises;
    # fails_when=the try/except around guard.observe is removed or narrowed; why_new=other tests call
    # Fallback directly; seam=none; no_claim=real SMTP (alerts go to the data-directory log file)
    class Broken:
        def __init__(self, *args, **kwargs):
            pass

        def observe(self, healthy, detail=''):
            raise RuntimeError('synthetic guard failure')
    monkeypatch.setattr(watchdog, 'Fallback', Broken)
    monkeypatch.setattr(watchdog, 'probe', lambda url: (False, 'connection refused'))
    monkeypatch.setattr(watchdog.Watchdog, 'run', lambda self, sleep=None, cycles=None: [self.tick() for _ in range(3)])
    assert watchdog.main(['run', '--config', str(config_path)]) == 0
    assert capsys.readouterr().out.count('FAIL backend guard: RuntimeError: synthetic guard failure') == 3
    assert 'Postern is not answering' in (config_path.parent / 'data' / 'watchdog-alerts.log').read_text()


def test_unverified_process_is_never_signalled(guard):
    select(guard.config, 'rust')
    serving(guard)
    guard.host.command = '/usr/bin/python3 something-else.py'
    for _ in range(3):
        reasons = guard.tick(healthy=False)
    assert reasons and stored(guard)['backend'] == 'python' and guard.host.killed == []
    assert 'The next API start uses Python.' in guard.sent[0][1]


def test_launchd_override_is_reported_once_and_never_changed(guard):
    select(guard.config, 'rust')
    serving(guard, backend='rust', source='environment')
    results = [guard.tick(healthy=False) for _ in range(6)]
    assert results == [None]*6 and guard.host.killed == [] and stored(guard)['backend'] == 'rust'
    assert [subject for subject, _ in guard.sent] == [f'Postern: Rust backend breach under a {JOB} override']
    # A launchd job that forces Python makes the file moot: nothing to guard.
    serving(guard, backend='python', source='environment')
    select(guard.config, 'rust', note='again')
    assert all(guard.tick(healthy=False) is None for _ in range(5)) and len(guard.sent) == 1


@pytest.mark.parametrize('text', ['{not json', '{"backend": "Rust"}', '{"backend": null}', '["rust"]'])
def test_a_selection_that_stops_the_api_is_replaced_with_python(guard, text):
    # Valid JSON the API refuses (a misspelled or missing backend) stops startup just like a broken file.
    path = progress_backend.selection_path(guard.config)
    path.write_text(text)
    path.chmod(0o600)
    with pytest.raises((RuntimeError, ValueError)):  # the API refuses it at startup
        progress_backend.read_selection(guard.config)
    guard.host.listening = False  # the API cannot start with that file
    for _ in range(3):
        reasons = guard.tick(healthy=False)
    assert 'was unreadable, not private to this account, or not python or rust' in reasons[-1]
    assert stored(guard)['backend'] == 'python' and guard.host.killed == []


@pytest.mark.parametrize('text,seconds', [('0:12.50', 12.5), ('3:31.99', 211.99), ('1:02:03.00', 3723.0),
                                          ('2-01:00:00.00', 176400.0)])
def test_ps_cpu_time_parses(text, seconds):
    assert watchdog.cpu_seconds(text) == pytest.approx(seconds)


def catalog_database(tmp_path, pages=3, compact=False):
    path = tmp_path / 'postern.sqlite3'
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        db.execute("INSERT INTO members VALUES ('reader@example.invalid','Reader','staff')")
        db.execute("INSERT INTO posts(id,title,body,status,created_at,created_by,published_at) "
                   "VALUES ('p1','Chapter','', 'published', 1, 'owner', 1)")
        db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) "
                   "VALUES ('i1','p1','a.pdf','pdf','x',1,?, 'ready',0)", (pages,))
        for seq, page in enumerate([0, 1, 1], start=1):
            payload = json.dumps({'detail': {'page': page}})
            db.execute('INSERT INTO events(id,ts,type,email,post_id,item_id,summary,payload) VALUES (?,?,?,?,?,?,?,?)',
                       (f'e{seq}', 10.0+seq, 'page_served', 'reader@example.invalid', 'p1', 'i1', 1, payload))
        while compact and not progress_store.rebuild_batch(db):
            pass  # build the compact copy the Rust staff report reads
    return path


class Native:
    """The native reduce contracts, computed by the Python reference (or deliberately wrong in one)."""

    def __init__(self, wrong=False):
        self.wrong = wrong  # False, True (the full reducer) or 'incremental'

    def _reference(self, metadata, rows):
        items = {identity: {'kind': kind, 'pages': pages, 'duration_seconds': duration}
                 for identity, kind, pages, duration in metadata}
        return progress._reduce_python(items, rows)

    def reduce(self, metadata, cursor, evidence):
        result = self._reference(metadata, cursor)
        return {} if self.wrong is True else result

    def reduce_incremental(self, metadata, cursor, evidence, checkpoint):
        rows = list(checkpoint) + list(cursor)
        result = self._reference(metadata, rows)
        return ({} if self.wrong == 'incremental' and checkpoint else result), rows


@pytest.mark.parametrize('wrong,code', [(False, 0), (True, 3), ('incremental', 3)])
def test_parity_compares_both_reducers_on_one_read_only_snapshot(tmp_path, monkeypatch, wrong, code):
    # The incremental row protects the checkpoint interface the Rust staff report uses: a drift there
    # alone must count as a difference even when the full reducer agrees.
    database = catalog_database(tmp_path, compact=True)
    monkeypatch.setattr(progress_backend, 'native', lambda: Native(wrong))
    result, summary = progress_parity.compare(database)
    assert result == code and summary['keys'] == 1 and summary['source'] == 'progress_events'
    assert summary['match'] is (not wrong)
    assert summary['incremental_matches'] is (wrong != 'incremental')


@pytest.mark.parametrize('change', ["UPDATE posts SET status='draft'", 'UPDATE items SET pages=2147483648'])
def test_parity_is_not_applicable_without_native_eligible_items(tmp_path, monkeypatch, change):
    # Unpublished catalogs and metadata the native reducer leaves to Python are not a mismatch:
    # reporting 3 there would make the watchdog abandon a healthy Rust backend.
    database = catalog_database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute(change)
    monkeypatch.setattr(progress_backend, 'native', lambda: Native())
    assert progress_parity.compare(database)[0] == 4


def test_parity_command_reads_the_configured_database_and_prints_no_names(tmp_path, monkeypatch, capsys):
    data = tmp_path / 'data'
    data.mkdir()
    catalog_database(data)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'data_dir': str(data)}))
    monkeypatch.setattr(progress_backend, 'native', lambda: Native(wrong=True))
    assert progress_parity.main(['--config', str(config)]) == 3
    printed = capsys.readouterr().out.strip()
    assert json.loads(printed)['match'] is False and '@' not in printed


def test_parity_with_the_real_native_module(tmp_path):
    pytest.importorskip('postern_progress_native')
    try:
        progress_backend.native.cache_clear()
        progress_backend.native()
    except RuntimeError:
        pytest.skip('installed native module predates the checkpoint interface')
    result, summary = progress_parity.compare(catalog_database(tmp_path))
    assert result == 0 and summary['match'] is True


def test_launchd_override_with_a_down_api_is_never_changed(guard):
    select(guard.config, 'rust')
    serving(guard, backend='rust', source='environment')
    guard.host.listening = False  # the forced-Rust API crashed: nothing verifies, the override still holds
    results = [guard.tick(healthy=False) for _ in range(4)]
    assert results == [None]*4 and stored(guard)['backend'] == 'rust' and guard.host.killed == []
    assert [subject for subject, _ in guard.sent] == [f'Postern: Rust backend breach under a {JOB} override']


def test_a_process_that_ignores_sigterm_is_killed_after_reverification(guard):
    select(guard.config, 'rust')
    serving(guard)
    guard.host.ignore_term = True  # uvicorn closed its port but a stuck request keeps it alive
    for _ in range(3):
        reasons = guard.tick(healthy=False)
    assert reasons and guard.host.killed == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert 'ignored SIGTERM for 120 s and was killed' in guard.sent[0][1]


def test_failures_after_selecting_python_still_alert_with_the_actual_outcome(guard, monkeypatch):
    select(guard.config, 'rust')
    serving(guard)
    def gone(pid, sig):
        guard.host.killed.append((pid, sig))
        raise ProcessLookupError(pid)
    monkeypatch.setattr(guard.fallback, 'kill', gone)
    for _ in range(3):
        guard.tick(healthy=False)
    assert stored(guard)['backend'] == 'python' and 'had already exited' in guard.sent[-1][1]

    select(guard.config, 'rust', note='again')
    guard.host.respawn()
    guard.tick(healthy=False)
    guard.tick(healthy=False)
    guard.host.lookup_errors = 2  # lsof times out while the guard re-verifies before signalling
    reasons = guard.tick(healthy=False)
    assert reasons and stored(guard)['backend'] == 'python'
    assert guard.sent[-1][0] == 'Postern returned to the Python backend'


def test_inspection_errors_and_bad_limits_never_disarm_the_health_fallback(guard):
    select(guard.config, 'rust', limits={'parity_every_seconds': None, 'max_rss_mib': 'big', 'cpu_checks': -1})
    serving(guard)
    guard.host.lookup_errors = 99  # lsof and ps time out on every tick
    assert guard.tick(healthy=False) is None and guard.tick(healthy=False) is None
    assert '3 consecutive health checks failed' in guard.tick(healthy=False)[0]
    assert watchdog.limits({'limits': {'parity_every_seconds': None, 'max_rss_mib': 'big', 'cpu_checks': -1}}) == watchdog.LIMITS


def test_parity_is_bounded_and_failed_runs_say_why(guard, capsys):
    select(guard.config, 'rust', limits={'parity_every_seconds': 60})
    serving(guard)
    guard.host.parity_codes = [(1, 'sqlite3.OperationalError: database is locked')]*3
    results = [guard.tick(seconds=60) for _ in range(4)]
    assert all(timeout == 60 for name, timeout in guard.host.calls if name == 'python')
    assert 'failed 3 times: sqlite3.OperationalError: database is locked' in results[3][0]
    assert capsys.readouterr().out.count('FAIL parity check (exit 1): sqlite3.OperationalError') == 3


def test_owner_limits_survive_reselection_and_fallback(guard):
    select(guard.config, 'python', limits={'max_rss_mib': 2048})
    select(guard.config, 'rust')  # `postern.serve backend rust` keeps the owner's limits
    assert stored(guard)['limits'] == {'max_rss_mib': 2048}
    serving(guard)
    guard.host.rss_mib = 1800  # under the owner's limit, over the default
    assert guard.tick() is None and guard.tick() is None
    for _ in range(3):
        guard.tick(healthy=False)
    assert stored(guard)['backend'] == 'python' and stored(guard)['limits'] == {'max_rss_mib': 2048}


def test_the_guard_heartbeat_shows_which_watchdog_is_armed(guard, capsys):
    select(guard.config, 'rust')
    serving(guard)
    guard.tick()
    beat = json.loads((guard.config.parent / 'data' / watchdog.GUARD).read_text())
    assert beat['pid'] == os.getpid() and beat['state'] == 'guarding'
    assert serve.main(['backend', '--config', str(guard.config)]) == 0
    assert f'ok   watchdog guard: pid {os.getpid()}' in capsys.readouterr().out


def test_the_backend_command_refuses_root_and_reports_a_bad_file(config_path, monkeypatch, capsys):
    with monkeypatch.context() as patched:
        patched.setattr(os, 'geteuid', lambda: 0)
        assert serve.main(['backend', 'rust', '--config', str(config_path)]) == 2
    assert 'without sudo' in capsys.readouterr().out and not progress_backend.selection_path(config_path).exists()
    path = progress_backend.selection_path(config_path)
    path.write_text('{not json')
    path.chmod(0o600)
    assert serve.main(['backend', '--config', str(config_path)]) == 2
    assert 'FAIL progress-backend.json' in capsys.readouterr().out


def test_a_guard_bug_alerts_once_and_health_still_falls_back(guard, monkeypatch):
    select(guard.config, 'rust')
    serving(guard)
    def broken(*args):
        raise KeyError('synthetic guard bug')
    monkeypatch.setattr(guard.fallback, 'resources', broken)
    results = [guard.tick(healthy=False) for _ in range(3)]
    subjects = [subject for subject, _ in guard.sent]
    assert subjects == ['Postern: Rust backend guard error', 'Postern returned to the Python backend']
    assert results[2] and stored(guard)['backend'] == 'python'


def test_a_failed_python_write_is_reported_and_nothing_is_signalled(guard, monkeypatch):
    select(guard.config, 'rust')
    serving(guard)
    def unwritable(*args):
        raise PermissionError('read-only config directory')
    monkeypatch.setattr(watchdog, 'write_python', unwritable)
    for _ in range(3):
        guard.tick(healthy=False)
    assert guard.host.killed == [] and 'Select it by hand' in guard.sent[-1][1]


def test_a_restart_that_raises_still_alerts(guard, monkeypatch):
    select(guard.config, 'rust')
    serving(guard)
    def refused(pid, sig):
        raise PermissionError(pid)
    monkeypatch.setattr(guard.fallback, 'kill', refused)
    for _ in range(3):
        guard.tick(healthy=False)
    assert stored(guard)['backend'] == 'python' and 'restart it by hand' in guard.sent[-1][1]


def test_sigkill_needs_a_confirmed_process(guard, monkeypatch):
    select(guard.config, 'rust')
    serving(guard)
    guard.host.ignore_term = True
    real_kill = guard.host.kill
    def term_then_blind(pid, sig):
        real_kill(pid, sig)
        guard.host.lookup_errors = 999  # ps cannot answer during the exit wait
    monkeypatch.setattr(guard.fallback, 'kill', term_then_blind)
    for _ in range(3):
        guard.tick(healthy=False)
    assert guard.host.killed == [(4242, signal.SIGTERM)]
    assert 'could not confirm it exited' in guard.sent[-1][1]


def test_a_heartbeat_write_failure_never_stops_guarding(guard, monkeypatch):
    select(guard.config, 'rust')
    serving(guard)
    def unwritable(*args, **kwargs):
        raise OSError('disk full')
    guard.host.rss_mib = 2000
    with monkeypatch.context() as patched:
        patched.setattr(watchdog.tempfile, 'NamedTemporaryFile', unwritable)
        assert guard.tick() is None
    # Restored: the fallback itself must be able to write Python.
    assert 'API memory 2000 MiB above 1536 MiB' in guard.tick()[0]


def test_parity_leaves_legacy_history_to_the_full_reducers(tmp_path, monkeypatch):
    # On the original events table (legacy or pending rows) the service never uses the checkpoint
    # interface, so parity must not call it there: it rejects non-portable clocks the full reducers accept.
    database = catalog_database(tmp_path)
    native = Native()
    def refuse(*args):
        raise ValueError('incremental replay requires portable clocks')
    native.reduce_incremental = refuse
    monkeypatch.setattr(progress_backend, 'native', lambda: native)
    code, summary = progress_parity.compare(database)
    assert code == 0 and summary['source'] == 'events' and summary['incremental_matches'] is None


def test_parity_with_the_real_native_module_and_a_legacy_clock(tmp_path):
    pytest.importorskip('postern_progress_native')
    try:
        progress_backend.native.cache_clear()
        progress_backend.native()
    except RuntimeError:
        pytest.skip('installed native module predates the checkpoint interface')
    database = catalog_database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO events(id,ts,type,email,post_id,item_id,summary,payload) VALUES "
                   "('old',0.0,'view_requested','reader@example.invalid','p1','i1',1,'{}')")
    code, summary = progress_parity.compare(database)
    assert code == 0 and summary['full_matches'] is True and summary['incremental_matches'] is None


def test_a_failed_python_write_is_retried_and_alerted_once(guard, monkeypatch):
    select(guard.config, 'rust')
    serving(guard)
    failures = [PermissionError('full'), PermissionError('full')]
    real = watchdog.write_python
    def flaky(*args):
        if failures:
            raise failures.pop(0)
        return real(*args)
    monkeypatch.setattr(watchdog, 'write_python', flaky)
    for _ in range(4):
        guard.tick(healthy=False)  # ticks 3 and 4 fail to write; tick 5 succeeds
    assert stored(guard)['backend'] == 'rust' and guard.host.killed == []
    assert [subject for subject, _ in guard.sent] == ['Postern could not return to the Python backend']
    assert guard.tick(healthy=False)
    assert stored(guard)['backend'] == 'python' and guard.host.killed == [(4242, signal.SIGTERM)]


@pytest.mark.parametrize('given,expected', [({'cpu_checks': 0.5}, 10), ({'cpu_checks': 2.5}, 10),
                                            ({'cpu_checks': float('inf')}, 10), ({'cpu_checks': 3.0}, 3),
                                            ({'max_rss_mib': float('inf')}, None), ({'max_rss_mib': float('nan')}, None),
                                            ({'parity_every_seconds': 1e9}, None)])
def test_limits_refuse_values_that_would_disable_or_trip_a_guard(given, expected):
    chosen = watchdog.limits({'limits': given})
    if expected is None:
        assert chosen == watchdog.LIMITS
    else:
        assert chosen['cpu_checks'] == expected


def test_failed_private_writes_leave_no_temporary_files(config_path, monkeypatch):
    data = config_path.parent / 'data'
    def broken(*args):
        raise OSError('rename failed')
    monkeypatch.setattr(os, 'replace', broken)
    with pytest.raises(OSError):
        watchdog.write_python(progress_backend.selection_path(config_path), ['synthetic'], {})
    with pytest.raises(OSError):
        progress_backend.write_selection(config_path, 'rust')
    with pytest.raises(OSError):
        progress_backend.record_active(data, 'python', 'default')
    leftovers = [p.name for p in list(config_path.parent.iterdir()) + list(data.iterdir()) if p.name.startswith('.progress-backend')]
    assert leftovers == []


def test_a_stale_or_foreign_guard_heartbeat_is_not_reported_as_armed(guard, capsys):
    from datetime import datetime, timedelta, timezone
    select(guard.config, 'rust')
    serving(guard)
    guard.tick()
    beat_path = guard.config.parent / 'data' / watchdog.GUARD
    beat = json.loads(beat_path.read_text())
    later = datetime.fromisoformat(beat['checked_at']) + timedelta(minutes=5)
    assert serve.guard_line(beat, guard.config, now=later).startswith('FAIL watchdog guard: stale')
    select(guard.config, 'rust', note='new selection')  # the heartbeat names the previous file
    assert 'another selection' in serve.guard_line(beat, guard.config)


def beat(guard):
    return json.loads((guard.config.parent / 'data' / watchdog.GUARD).read_text())


def test_the_guard_line_is_judged_by_the_current_selection(guard, capsys):
    select(guard.config, 'rust')
    # No heartbeat while Rust is selected is not an armed guard (an old watchdog writes none).
    assert serve.guard_line(None, guard.config).startswith('FAIL watchdog guard: no heartbeat')
    serving(guard)
    guard.tick()
    assert serve.guard_line(beat(guard), guard.config).startswith('ok   watchdog guard: pid')
    # Python is selected, but the API serves Rust, unguarded, until it restarts.
    assert serve.main(['backend', 'python', '--config', str(guard.config)]) == 0
    assert 'FAIL watchdog guard: Python is selected, but the API (pid 4242) still serves Rust' in capsys.readouterr().out
    # Once the API runs Python, the last Rust heartbeat is history, not a failure.
    serving(guard, backend='python')
    assert serve.main(['backend', '--config', str(guard.config)]) == 0
    assert 'ok   watchdog guard: not needed while Python is selected' in capsys.readouterr().out
    # A Rust record whose process has exited is history too.
    serving(guard)
    guard.host.alive = False
    assert serve.main(['backend', '--config', str(guard.config)]) == 0
    assert 'ok   watchdog guard: not needed while Python is selected' in capsys.readouterr().out
    guard.host.respawn()
    # Selecting Rust cannot have been checked yet: say when to look, never FAIL in the same run.
    assert serve.main(['backend', 'rust', '--config', str(guard.config)]) == 0
    printed = capsys.readouterr().out
    assert 'checks this selection within 30 s' in printed and 'FAIL' not in printed


def test_the_heartbeat_records_what_the_guard_actually_ran(guard):
    select(guard.config, 'rust')
    serving(guard, backend='python')  # selected, but the API has not restarted with Rust yet
    guard.tick()
    assert beat(guard)['state'] == 'waiting'
    assert serve.guard_line(beat(guard), guard.config).startswith('ok   watchdog guard: watching')
    serving(guard)
    guard.host.lookup_errors = 99  # lsof and ps fail: memory, CPU and parity cannot run
    for _ in range(4):
        assert guard.tick() is None
    assert beat(guard)['state'] == 'unverified'
    assert serve.guard_line(beat(guard), guard.config).startswith('FAIL watchdog guard: it cannot verify')
    assert [subject for subject, _ in guard.sent] == ['Postern: cannot verify the Rust API process']
    guard.host.lookup_errors = 0
    guard.tick()
    assert beat(guard)['state'] == 'guarding'
    serving(guard, source='environment')
    guard.tick()
    assert serve.guard_line(beat(guard), guard.config).startswith(f'FAIL watchdog guard: the {JOB} job')


def test_a_parity_breach_whose_write_failed_is_retried_on_the_next_check(guard, monkeypatch):
    select(guard.config, 'rust', limits={'parity_every_seconds': 600})
    serving(guard)
    guard.host.parity_codes = [3]
    real = watchdog.write_python
    failures = [PermissionError('full')]
    def flaky(*args):
        if failures:
            raise failures.pop(0)
        return real(*args)
    monkeypatch.setattr(watchdog, 'write_python', flaky)
    results = [guard.tick() for _ in range(11)]  # the first parity run (330 s) differs; writing Python fails
    assert results[:10] == [None]*10 and 'Rust and Python progress differ' in results[10][0]
    assert stored(guard)['backend'] == 'rust'
    assert guard.tick() and stored(guard)['backend'] == 'python'  # the next check, not the next parity run
    assert guard.host.killed == [(4242, signal.SIGTERM)]


@pytest.mark.parametrize('value', [10**400, -10**400, 2**63])
def test_huge_whole_number_limits_keep_their_defaults(value):
    assert watchdog.limits({'limits': {name: value for name in watchdog.LIMITS}}) == watchdog.LIMITS


def test_parity_with_the_real_native_module_on_the_compact_copy(tmp_path):
    pytest.importorskip('postern_progress_native')
    try:
        progress_backend.native.cache_clear()
        progress_backend.native()
    except RuntimeError:
        pytest.skip('installed native module predates the checkpoint interface')
    code, summary = progress_parity.compare(catalog_database(tmp_path, pages=7, compact=True))
    assert code == 0 and summary['source'] == 'progress_events' and summary['incremental_matches'] is True


def test_a_skipped_restart_is_retried_while_rust_still_serves(guard):
    select(guard.config, 'rust')
    serving(guard)
    guard.tick(healthy=False)
    guard.tick(healthy=False)
    guard.host.lookup_errors = 2  # lsof times out while the fallback re-verifies: Python is written, no signal
    assert guard.tick(healthy=False)
    assert stored(guard)['backend'] == 'python' and guard.host.killed == []
    guard.host.lookup_errors = 0
    guard.tick()  # the same verified process still serves Rust from the file: finish the switch
    assert guard.host.killed == [(4242, signal.SIGTERM)]
    assert guard.sent[-1][0] == 'Postern finished returning to the Python backend'
    for _ in range(5):  # bounded: three attempts per fallback, and none once Python serves
        guard.host.respawn()
        guard.tick()
    assert len(guard.host.killed) == 3
    serving(guard, backend='python')
    guard.host.respawn()
    guard.tick()
    assert len(guard.host.killed) == 3


def test_a_failed_start_replaces_the_previous_serving_record(config_path, monkeypatch):
    progress_backend.record_active(config_path.parent / 'data', 'rust', 'environment')  # an earlier process
    path = progress_backend.selection_path(config_path)
    path.write_text('{"backend": "Rust"}')
    path.chmod(0o600)
    monkeypatch.setattr(serve, 'listening', lambda port: False)
    with pytest.raises(RuntimeError):
        serve.run(config_path)
    active = json.loads((config_path.parent / 'data' / progress_backend.ACTIVE).read_text())
    assert (active['source'], active['backend'], active['pid']) == ('failed', None, os.getpid())


def test_a_guard_bug_keeps_the_verified_process_for_the_restart(guard, monkeypatch):
    select(guard.config, 'rust')
    serving(guard)
    def broken(*args):
        raise KeyError('synthetic guard bug')
    monkeypatch.setattr(guard.fallback, 'resources', broken)
    for _ in range(3):
        guard.tick(healthy=False)
    assert guard.host.killed == [(4242, signal.SIGTERM)] and beat(guard)['state'] == 'error'


def test_a_failing_notifier_never_skips_the_fallback(guard, capsys):
    def down(subject, body):
        raise OSError('smtp unreachable')
    guard.fallback.notify = down
    select(guard.config, 'rust')
    serving(guard)
    guard.host.lookup_errors = 99
    for _ in range(3):
        guard.tick()  # the unverified alert fails to send
    guard.host.lookup_errors = 0
    for _ in range(3):
        guard.tick(healthy=False)
    assert stored(guard)['backend'] == 'python' and guard.host.killed == [(4242, signal.SIGTERM)]
    assert 'FAIL alert not sent (OSError): Postern returned to the Python backend' in capsys.readouterr().out


def test_an_erroring_or_unknown_guard_state_is_never_reported_as_armed(guard):
    # Value: protects=backend command says FAIL for error/unknown heartbeat states; fails_when=guard_line treats any fresh beat as armed; why_new=only waiting/unverified/override lines were checked; seam=none; no_claim=alert text
    select(guard.config, 'rust')
    serving(guard)
    guard.tick()
    record = beat(guard)
    assert serve.guard_line({**record, 'state': 'error'}, guard.config).startswith('FAIL watchdog guard: its checks raised')
    assert serve.guard_line({**record, 'state': 'bogus'}, guard.config).startswith("FAIL watchdog guard: unknown state 'bogus'")
    # Generated by Claude Code (ship test coverage audit, pass 1): the reasonless inconclusive row.
    # Value: protects=a fresh inconclusive heartbeat with no reason prints a FAIL line naming that gap, never ok or a
    # traceback; fails_when=guard_line indexes record['reason'] or drops its placeholder; why_new=every inconclusive
    # test writes a reason; seam=none; no_claim=that a current watchdog omits the reason (hand-edited record; unit only)
    for missing in ({}, {'reason': None}, {'reason': ''}):
        line = serve.guard_line({**record, **missing, 'state': 'inconclusive'}, guard.config)
        assert line.startswith('FAIL watchdog guard: the systemd unit environment cannot be verified: reason not recorded; ')
        assert line.endswith(f'(pid {record["pid"]}, last checked {record["checked_at"]})')


def test_a_finishing_restart_that_raises_still_alerts_with_the_manual_step(guard, monkeypatch):
    # Value: protects=finish_fallback alerts when its restart raises; fails_when=the exception escapes observe or the alert is dropped; why_new=retry test covers only a successful restart; seam=none; no_claim=retry bound
    select(guard.config, 'rust')
    serving(guard)
    guard.tick(healthy=False)
    guard.tick(healthy=False)
    guard.host.lookup_errors = 2
    assert guard.tick(healthy=False) and stored(guard)['backend'] == 'python'
    guard.host.lookup_errors = 0
    def broken(pid):
        raise OSError('signal refused')
    monkeypatch.setattr(guard.fallback, 'restart', broken)
    guard.tick()
    subject, body = guard.sent[-1]
    assert subject == 'Postern finished returning to the Python backend' and 'restart the API by hand' in body


def test_a_breach_under_an_override_is_not_replayed_after_the_override_is_removed(guard):
    # The owner removes the launchd override and restarts on the same Rust file: an old alert-only
    # breach must not return the new, healthy process to Python.
    select(guard.config, 'rust', limits={'parity_every_seconds': 60})
    serving(guard, source='environment')
    guard.host.parity_codes = [3]
    results = [guard.tick() for _ in range(4)]
    assert results == [None]*4 and [subject for subject, _ in guard.sent] == [f'Postern: Rust backend breach under a {JOB} override']
    serving(guard)  # the override is gone; the API now serves Rust from the file
    assert guard.tick() is None and stored(guard)['backend'] == 'rust' and guard.host.killed == []


def test_a_heartbeat_from_an_older_watchdog_is_not_reported_as_armed(guard):
    # A v0.24.0 watchdog wrote "guarding" on every tick without verifying anything.
    select(guard.config, 'rust')
    serving(guard)
    guard.tick()
    record = {key: value for key, value in beat(guard).items() if key != 'guard_version'}
    assert serve.guard_line(record, guard.config).startswith('FAIL watchdog guard: the heartbeat is from an older watchdog')
    assert beat(guard)['guard_version'] == watchdog.GUARD_VERSION


def test_sigkill_ends_only_this_apis_own_media_conversions(guard):
    select(guard.config, 'rust')
    serving(guard)
    guard.host.ignore_term = True
    uid = os.getuid()
    guard.host.processes = [
        (5001, 4242, 5001, uid, '/usr/bin/python3 -I /x/postern/derive_child.py a b document'),  # this API's converter
        (5002, 4242, 4242, uid, '/usr/bin/python3 -I /x/postern/derive_child.py a b video'),     # not its own session
        (5003, 7777, 5003, uid, '/usr/bin/python3 -I /x/postern/derive_child.py a b document'),  # another parent
        (5004, 4242, 5004, uid + 1, '/usr/bin/python3 -I /x/postern/derive_child.py a b document'),  # another user
        (5005, 4242, 5005, uid, '/usr/bin/caffeinate -i'),                                         # not a converter
    ]
    for _ in range(3):
        guard.tick(healthy=False)
    # The API goes first, so it can never record a converter it saw die as the upload's fault.
    assert guard.host.killed == [(4242, signal.SIGTERM), (4242, signal.SIGKILL), ('group', 5001, signal.SIGKILL)]
    assert 'with 1 media conversion(s) it was still running' in guard.sent[-1][1]


@pytest.mark.parametrize('failure', ['timeout', 'exit'])
def test_a_failed_converter_lookup_never_stops_the_sigkill(guard, failure):
    # ps may time out or fail. That may not skip SIGKILL, and the alert says the conversions are unknown.
    select(guard.config, 'rust')
    serving(guard)
    guard.host.ignore_term = True
    guard.host.listing_failure = failure
    for _ in range(3):
        guard.tick(healthy=False)
    assert guard.host.killed == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert ('ignored SIGTERM for 120 s and was killed; its media conversions could not be listed, so any still '
            'running finish by themselves') in guard.sent[-1][1]


def test_a_converter_is_found_beside_a_command_line_that_is_not_utf8(guard):
    # Another process's command line may hold bytes that are not UTF-8 (a Latin-1 file name).
    select(guard.config, 'rust')
    serving(guard)
    guard.host.ignore_term = True
    guard.host.processes = [
        (5006, 1, 5006, os.getuid(), b'/usr/bin/open /Users/x/caf\xe9.pdf'),
        (5001, 4242, 5001, os.getuid(), '/usr/bin/python3 -I /x/postern/derive_child.py a b document'),
    ]
    for _ in range(3):
        guard.tick(healthy=False)
    assert guard.host.killed == [(4242, signal.SIGTERM), (4242, signal.SIGKILL), ('group', 5001, signal.SIGKILL)]
    assert 'with 1 media conversion(s) it was still running' in guard.sent[-1][1]
    assert 'could not be listed' not in guard.sent[-1][1]


def test_rust_forced_by_launchd_is_reported_and_alerted_without_a_rust_file(guard, capsys):
    serving(guard, source='environment')  # the launchd job forces Rust; no selection file at all
    assert serve.main(['backend', '--config', str(guard.config)]) == 0
    assert f'FAIL watchdog guard: the {JOB} job sets POSTERN_PROGRESS_BACKEND' in capsys.readouterr().out
    guard.host.rss_mib = 2000
    results = [guard.tick() for _ in range(4)]
    assert results == [None]*4 and guard.host.killed == []
    assert not progress_backend.selection_path(guard.config).exists()
    assert [subject for subject, _ in guard.sent] == [f'Postern: Rust backend breach under a {JOB} override']
    assert 'API memory 2000 MiB above 1536 MiB' in guard.sent[0][1]


def test_selecting_rust_under_a_launchd_override_says_the_file_will_not_apply(guard, capsys):
    serving(guard, backend='python', source='environment')  # today's office state: the launchd job forces Python
    assert serve.main(['backend', 'rust', '--config', str(guard.config)]) == 0
    printed = capsys.readouterr().out
    assert f'FAIL watchdog guard: the {JOB} job sets POSTERN_PROGRESS_BACKEND' in printed
    assert 'checks this selection within 30 s' not in printed
    guard.host.alive = False  # that process has exited: its record is history, not an override
    assert serve.main(['backend', 'rust', '--config', str(guard.config)]) == 0
    assert 'ok   watchdog guard: it checks this selection within 30 s' in capsys.readouterr().out


def test_counts_from_a_launchd_override_never_carry_into_the_file_governed_process(guard):
    # Two failed health checks and one memory sample under the override, then the owner removes the
    # override and restarts on the same Rust file: the new process starts with clean counts.
    select(guard.config, 'rust')
    serving(guard, source='environment')
    guard.host.rss_mib = 2000
    assert guard.tick(healthy=False) is None and guard.tick(healthy=False) is None
    serving(guard)  # the same pid in the fake, now serving Rust from the file
    assert guard.tick(healthy=False) is None and stored(guard)['backend'] == 'rust' and guard.host.killed == []
    guard.host.rss_mib = 300
    assert guard.tick(healthy=False) is None and stored(guard)['backend'] == 'rust'
    assert '3 consecutive health checks failed' in guard.tick(healthy=False)[0]


def test_alerts_under_a_launchd_override_never_promise_a_return_to_python(guard, monkeypatch):
    select(guard.config, 'rust')
    serving(guard, source='environment')
    guard.host.lookup_errors = 99  # lsof and ps fail; the forced process cannot be confirmed or ruled out
    for _ in range(4):
        assert guard.tick() is None
    guard.host.lookup_errors = 0
    def broken(selection, pid, info, parity_allowed):
        raise ValueError('bad limits')
    monkeypatch.setattr(guard.fallback, 'resources', broken)
    guard.tick()
    assert [subject for subject, _ in guard.sent] == ['Postern: cannot verify the Rust API process',
                                                      'Postern: Rust backend guard error']
    for _, body in guard.sent:
        assert f'Nothing returns Rust to Python while the {JOB} job sets POSTERN_PROGRESS_BACKEND' in body
        assert 'still return Rust to Python' not in body
    assert stored(guard)['backend'] == 'rust' and guard.host.killed == []


def test_a_launchd_override_record_from_an_exited_process_does_not_stop_the_fallback(guard, capsys):
    # A start refused on a busy port leaves the previous process's record. Once that process is gone, the
    # file governs again: failed health checks return Rust to Python.
    select(guard.config, 'rust')
    serving(guard, source='environment')
    guard.host.alive = False
    for _ in range(2):
        assert guard.tick(healthy=False) is None
    assert guard.tick(healthy=False) and stored(guard)['backend'] == 'python'
    assert [subject for subject, _ in guard.sent] == ['Postern returned to the Python backend']
    # Only the launchd job knows whether it still forces Rust, so the alert does not promise Python.
    assert 'The next API start uses Python.' not in guard.sent[0][1] and 'not restarted' not in guard.sent[0][1]
    assert 'while the job sets it, that setting decides the next start' in guard.sent[0][1]
    # Without a Rust file, an exited forced-Rust process is neither watched nor reported as an override.
    progress_backend.selection_path(guard.config).unlink()
    guard.sent.clear()
    assert all(guard.tick(healthy=False) is None for _ in range(4)) and guard.sent == []
    assert serve.main(['backend', '--config', str(guard.config)]) == 0
    assert 'ok   watchdog guard: not needed while Python is selected' in capsys.readouterr().out


def test_a_dropped_launchd_record_never_promises_a_later_restart(guard, monkeypatch):
    # ps times out just after Python is written: the launchd-forced record is not one the watchdog restarts later.
    select(guard.config, 'rust')
    serving(guard, source='environment')
    guard.host.alive = False
    write = watchdog.write_python
    def write_then_ps_times_out(*args):
        write(*args)
        guard.host.lookup_errors = 1
    monkeypatch.setattr(watchdog, 'write_python', write_then_ps_times_out)
    for _ in range(3):
        guard.tick(healthy=False)
    assert stored(guard)['backend'] == 'python' and guard.host.killed == []
    body = guard.sent[-1][1]
    assert 'that setting decides the next start' in body and 'not restarted' not in body


def test_a_fallback_that_cannot_confirm_the_api_says_so_and_alerts_once_if_it_stays_unconfirmed(guard):
    select(guard.config, 'rust')
    serving(guard)
    guard.tick(healthy=False)
    guard.tick(healthy=False)
    guard.host.lookup_errors = 99  # from here lsof and ps fail
    assert guard.tick(healthy=False) and stored(guard)['backend'] == 'python' and guard.host.killed == []
    assert ('The API recorded as serving Rust (pid 4242) could not be confirmed, so it was not restarted'
            in guard.sent[-1][1])
    for _ in range(5):
        guard.tick()
    subjects = [subject for subject, _ in guard.sent]
    assert subjects == ['Postern returned to the Python backend', 'Postern could not confirm the return to Python']
    assert 'restart the API by hand if it still serves Rust' in guard.sent[-1][1]
    guard.host.lookup_errors = 0  # lookups recover while it still serves Rust: the restart finishes
    guard.tick()
    assert guard.host.killed == [(4242, signal.SIGTERM)]
    assert guard.sent[-1][0] == 'Postern finished returning to the Python backend'


def test_the_real_process_check_says_gone_only_when_the_process_is_certainly_gone():
    # Value: protects=serve.process_exists treats bad pids and exited processes as gone, unknown as alive; fails_when=PermissionError or a bool/str pid flips the answer; why_new=every other test monkeypatches process_exists; seam=none; no_claim=launchd state
    exited = subprocess.Popen(['/usr/bin/true'])
    exited.wait()
    assert serve.process_exists(os.getpid()) is True
    assert serve.process_exists(exited.pid) is False
    assert [serve.process_exists(pid) for pid in (None, 0, -1, True, '4242', 4242.0)] == [False]*6
    if os.getuid() != 0:
        assert serve.process_exists(1) is True  # launchd: not ours to signal, so unknown, and unknown counts as yes


def in_process(guard, **counts):
    record = {'pid': guard.host.pid, 'native_errors': 0, 'checks': 0, 'mismatches': 0, 'last_error': None, **counts}
    (guard.config.parent / 'data' / watchdog.CHECK).write_text(json.dumps(record))


def test_the_apis_own_drift_report_returns_rust_to_python(guard):
    # Value: protects=a mismatch the serving process found in its own checkpoint is a breach; fails_when=the watchdog ignores the record; why_new=the parity check runs in another process; seam=none; no_claim=how the API finds it (test_progress_cache)
    select(guard.config, 'rust')
    serving(guard)
    in_process(guard, checks=3, mismatches=1)
    reasons = guard.tick()
    assert reasons == ["the API's own check found its Rust staff report differs from a full Python replay (1 time(s))"]
    assert stored(guard)['backend'] == 'python' and guard.host.killed == [(4242, signal.SIGTERM)]


def test_native_errors_breach_at_three_and_only_for_the_serving_process(guard):
    select(guard.config, 'rust')
    serving(guard)
    in_process(guard, pid=999, native_errors=7, mismatches=2)  # an earlier process's record says nothing about this one
    assert guard.tick() is None and stored(guard)['backend'] == 'rust' and guard.host.killed == []
    in_process(guard, native_errors=2, last_error='PanicException')
    assert guard.tick() is None and guard.tick() is None and stored(guard)['backend'] == 'rust'
    in_process(guard, native_errors=3, last_error='PanicException')
    assert guard.tick() == ["3 native errors on the API's progress paths (last: PanicException)"]
    assert stored(guard)['backend'] == 'python'


def test_an_in_process_breach_under_a_launchd_override_only_alerts(guard):
    select(guard.config, 'rust')
    serving(guard, source='environment')
    in_process(guard, mismatches=2)
    assert guard.tick() is None and stored(guard)['backend'] == 'rust' and guard.host.killed == []
    assert [subject for subject, _ in guard.sent] == [f'Postern: Rust backend breach under a {JOB} override']


@pytest.mark.parametrize('record,expected', [
    (None, 'FAIL in-process check: the process serving Rust has written no record'),
    ({'pid': 999}, 'FAIL in-process check: the process serving Rust has written no record'),
    ({'checks': 0}, 'ok   in-process check: no staff-report comparison yet'),
    ({'checks': 2, 'last_check_at': '2026-11-09T17:00:00+00:00'},
     'ok   in-process check: 2 staff-report comparison(s) with a full Python replay, all matching'),
    ({'checks': 2, 'native_errors': 1, 'last_error': 'ValueError'}, '1 native error(s), last ValueError; the watchdog acts at 3'),
    ({'checks': 2, 'mismatches': 1}, 'FAIL in-process check: the staff report differed from a full Python replay 1 time(s)'),
    ({'native_errors': 3, 'last_error': 'PanicException'}, 'FAIL in-process check: 3 native errors (last PanicException)'),
    ({'check_errors': 2, 'last_check_error': 'MemoryError', 'last_check_error_at': '2026-11-09T17:00:00+00:00'},
     'FAIL in-process check: the last staff-report comparison could not run (the full Python replay raised '
     'MemoryError at 2026-11-09T17:00:00+00:00; 2 in all)'),
    ({'checks': 4, 'check_errors': 1, 'last_check_error': 'MemoryError', 'last_check_error_at': '2026-11-09T16:00:00+00:00',
      'last_check_at': '2026-11-09T17:00:00+00:00'},
     'all matching (last 2026-11-09T17:00:00+00:00), and 1 earlier that could not run (last error MemoryError)'),
    # One early success must not hide later failures: the latest attempt decides.
    ({'checks': 1, 'check_errors': 40, 'last_check_error': 'MemoryError', 'last_check_at': '2026-11-01T00:00:00+00:00',
      'last_check_error_at': '2026-11-09T17:00:00+00:00'}, 'FAIL in-process check: the last staff-report comparison'),
    ({'check_errors': 1, 'last_check_error': 'MemoryError', 'last_check_error_at': '2026-11-09T17:00:00+00:00',
      'native_errors': 1, 'last_error': 'ValueError'}, '1 native error(s), last ValueError'),
])
def test_the_status_shows_the_serving_processs_own_checks(guard, capsys, record, expected):
    # Value: protects=the owner sees the serving Rust process's own comparisons and errors; fails_when=a stale or missing record reads ok; why_new=new status line; seam=none; no_claim=the watchdog's reaction
    select(guard.config, 'rust')
    serving(guard)
    if record is not None:
        in_process(guard, **record)
    assert serve.main(['backend', '--config', str(guard.config)]) == 0
    assert expected in capsys.readouterr().out


def test_python_serving_shows_no_in_process_line(guard, capsys):
    serving(guard, backend='python')
    in_process(guard, mismatches=4)  # left by an earlier Rust process
    assert serve.main(['backend', '--config', str(guard.config)]) == 0
    assert 'in-process check' not in capsys.readouterr().out


def test_native_errors_are_counted_where_the_service_records_them(config_path, monkeypatch, tmp_path):
    # Value: protects=a native failure on reader progress is counted, a database error is not, and both still raise; fails_when=errors are swallowed or a shared database fault counts against Rust; why_new=new counter; seam=none; no_claim=PanicException from a real wheel
    database = catalog_database(tmp_path)
    data = tmp_path / 'data'
    data.mkdir(exist_ok=True)
    progress_backend.health.configure(data)
    monkeypatch.setenv(progress_backend.SETTING, 'rust')
    failure = [RuntimeError('synthetic native fault')]
    class Failing(Native):
        def reduce(self, metadata, cursor, evidence):
            raise failure[0]
    monkeypatch.setattr(progress_backend, 'native', lambda: Failing())
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        items = {item['id']: item for item in progress.dashboard_catalog(db)}
        with pytest.raises(RuntimeError):
            progress.history(db, items, 'reader@example.invalid')
        failure[0] = sqlite3.OperationalError('database is locked')
        with pytest.raises(sqlite3.OperationalError):
            progress.history(db, items, 'reader@example.invalid')
    record = json.loads((data / progress_backend.CHECK).read_text())
    assert (record['pid'], record['native_errors'], record['last_error']) == (os.getpid(), 1, 'RuntimeError')
    assert (data / progress_backend.CHECK).stat().st_mode & 0o777 == 0o600


def test_macos_override_record_and_line_remain_unchanged(guard, monkeypatch):
    monkeypatch.setattr(watchdog, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(serve, 'sys', SimpleNamespace(platform='darwin'))
    original = guard.host.run
    def run(args, **kwargs):
        assert args[0] != 'systemctl', 'macOS must not query systemd'
        return original(args, **kwargs)
    guard.fallback.run = run
    select(guard.config, 'rust')
    serving(guard, source='environment')
    guard.tick()
    record = beat(guard)
    assert record['state'] == 'override' and 'reason' not in record
    assert record['guard_version'] == watchdog.GUARD_VERSION == 2
    expected = ('FAIL watchdog guard: the launchd job sets POSTERN_PROGRESS_BACKEND, so this file is not in effect '
                'and the guard can only alert (runbook step 3 removes that setting)')
    assert serve.host_words(serve.GUARD_STATES['override']) == expected
    assert serve.guard_line(record, guard.config, active=guard.fallback.active()) == expected


def test_zz_the_selection_tests_leave_the_backend_setting_as_found():
    # Runs last in this module: no fixture may leak a selected backend into later tests.
    assert os.environ.get(progress_backend.SETTING) == SETTING_AT_IMPORT
