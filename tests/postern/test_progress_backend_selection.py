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
from types import SimpleNamespace

import pytest

from postern import progress, progress_backend, progress_parity, serve, watchdog
from postern.db import SCHEMA


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.delenv(progress_backend.SETTING, raising=False)
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

    def run(self, args, **kwargs):
        self.calls.append((args[0], kwargs.get('timeout')))
        if args[0].endswith(('lsof', 'ps')) and self.lookup_errors:
            self.lookup_errors -= 1
            raise subprocess.TimeoutExpired(args[0], kwargs.get('timeout'))
        if args[0].endswith('lsof'):
            return SimpleNamespace(returncode=0, stdout=f'{self.pid}\n' if self.listening and self.alive else '', stderr='')
        if args[0].endswith('ps'):
            if not self.alive:
                return SimpleNamespace(returncode=1, stdout='', stderr='')
            minutes, seconds = divmod(self.cpu, 60)
            line = f'{os.getuid()} {int(self.rss_mib*1024)} {int(minutes)}:{seconds:05.2f} {self.command}\n'
            return SimpleNamespace(returncode=0, stdout=line, stderr='')
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

    def respawn(self):
        """launchd started the API again (the fake keeps its pid)."""
        self.alive = self.listening = True


@pytest.fixture
def guard(config_path):
    host, sent, clock = Host(), [], [0.0]
    fallback = watchdog.Fallback(config_path, config_path.parent / 'data', 8790,
                                 lambda subject, body: sent.append((subject, body)), failures=3, interval=30,
                                 run=host.run, kill=host.kill, now=lambda: clock[0], python='python',
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
    assert [subject for subject, _ in guard.sent] == ['Postern: Rust backend breach under a launchd override']
    # A launchd job that forces Python makes the file moot: nothing to guard.
    serving(guard, backend='python', source='environment')
    select(guard.config, 'rust', note='again')
    assert all(guard.tick(healthy=False) is None for _ in range(5)) and len(guard.sent) == 1


def test_an_unsafe_selection_that_stops_the_api_is_replaced_with_python(guard):
    path = progress_backend.selection_path(guard.config)
    path.write_text('{not json')
    path.chmod(0o600)
    guard.host.listening = False  # the API cannot start with that file
    for _ in range(3):
        reasons = guard.tick(healthy=False)
    assert 'was unreadable or not private' in reasons[-1] and stored(guard)['backend'] == 'python'
    assert guard.host.killed == []


@pytest.mark.parametrize('text,seconds', [('0:12.50', 12.5), ('3:31.99', 211.99), ('1:02:03.00', 3723.0),
                                          ('2-01:00:00.00', 176400.0)])
def test_ps_cpu_time_parses(text, seconds):
    assert watchdog.cpu_seconds(text) == pytest.approx(seconds)


def catalog_database(tmp_path, pages=3):
    path = tmp_path / 'postern.sqlite3'
    with sqlite3.connect(path) as db:
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
    database = catalog_database(tmp_path)
    monkeypatch.setattr(progress_backend, 'native', lambda: Native(wrong))
    result, summary = progress_parity.compare(database)
    assert result == code and summary['keys'] == 1 and summary['source'] == 'events'
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
    assert [subject for subject, _ in guard.sent] == ['Postern: Rust backend breach under a launchd override']


def test_a_process_that_ignores_sigterm_is_killed_after_reverification(guard):
    select(guard.config, 'rust')
    serving(guard)
    guard.host.ignore_term = True  # uvicorn closed its port but a stuck request keeps it alive
    for _ in range(3):
        reasons = guard.tick(healthy=False)
    assert reasons and guard.host.killed == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert 'ignored SIGTERM for 30 s and was killed' in guard.sent[0][1]


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
    assert f'watchdog guard: pid {os.getpid()} guarding' in capsys.readouterr().out


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
