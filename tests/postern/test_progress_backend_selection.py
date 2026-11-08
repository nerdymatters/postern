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
from types import SimpleNamespace

import pytest

from postern import progress, progress_backend, progress_parity, serve, watchdog
from postern.db import SCHEMA


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.delenv(progress_backend.KEY, raising=False)
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
    assert progress_backend.KEY not in os.environ
    select(config_path, 'rust')
    assert progress_backend.apply_selection(config_path) == ('rust', 'progress-backend.json')
    assert os.environ[progress_backend.KEY] == 'rust'
    # Once set (by launchd or by the first call), the environment is the authority for this process.
    monkeypatch.setenv(progress_backend.KEY, 'python')
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
    assert progress_backend.KEY not in os.environ


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
    monkeypatch.setattr(application, 'create_app', lambda config: seen.setdefault('backend', os.environ[progress_backend.KEY]))
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kwargs: seen.setdefault('app', app))
    assert serve.run(config_path) == 0
    assert seen == {'backend': 'python', 'app': 'python'}
    active = json.loads((config_path.parent / 'data' / progress_backend.ACTIVE).read_text())
    assert (active['backend'], active['source'], active['pid']) == ('python', 'progress-backend.json', os.getpid())
    assert 'postern: progress backend python (progress-backend.json)' in capsys.readouterr().err


def test_the_backend_command_shows_and_sets_the_owner_selection(config_path, capsys):
    assert serve.main(['backend', '--config', str(config_path)]) == 0
    printed = capsys.readouterr().out
    assert 'progress-backend.json: absent (Python)' in printed and 'no serving process' in printed
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

    def run(self, args, **kwargs):
        self.calls.append(args[0])
        if args[0].endswith('lsof'):
            return SimpleNamespace(returncode=0, stdout=f'{self.pid}\n' if self.listening else '', stderr='')
        if args[0].endswith('ps'):
            minutes, seconds = divmod(self.cpu, 60)
            line = f'{os.getuid()} {int(self.rss_mib*1024)} {int(minutes)}:{seconds:05.2f} {self.command}\n'
            return SimpleNamespace(returncode=0, stdout=line, stderr='')
        code = self.parity_codes.pop(0) if self.parity_codes else 0
        return SimpleNamespace(returncode=code, stdout='{"match": %s}\n' % ('true' if code == 0 else 'false'), stderr='')

    def kill(self, pid, sig):
        self.killed.append((pid, sig))


@pytest.fixture
def guard(config_path):
    host, sent, clock = Host(), [], [0.0]
    fallback = watchdog.Fallback(config_path, config_path.parent / 'data', 8790,
                                 lambda subject, body: sent.append((subject, body)), failures=3, interval=30,
                                 run=host.run, kill=host.kill, now=lambda: clock[0], python='python')

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

    select(guard.config, 'rust', limits={'cpu_checks': 3})  # the owner re-selects Rust: guarding resumes
    guard.host.rss_mib = 300
    guard.tick()  # first CPU sample
    assert guard.tick(cpu_percent=95) is None and guard.tick(cpu_percent=95) is None
    assert 'API CPU above 90% for 3 checks' in guard.tick(cpu_percent=95)[0]

    select(guard.config, 'rust', limits={'parity_every_seconds': 60})
    guard.host.parity_codes = [0, 4, 3]
    assert guard.tick(seconds=60) is None  # first CPU sample; parity scheduled
    assert guard.tick(seconds=60) is None and guard.tick(seconds=60) is None  # match, then not applicable
    assert 'Rust and Python progress differ' in guard.tick(seconds=60)[0]
    assert len(guard.host.killed) == 3 and stored(guard)['backend'] == 'python'


def test_repeated_parity_errors_fail_safe_but_one_does_not(guard):
    select(guard.config, 'rust', limits={'parity_every_seconds': 60})
    serving(guard)
    guard.host.parity_codes = [1, 0, 1, 1, 1]
    results = [guard.tick(seconds=60) for _ in range(6)]
    assert results[:5] == [None]*5 and 'the parity check failed 3 times' in results[5][0]


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
    """The native reduce contract, computed by the Python reference (or deliberately wrong)."""

    def __init__(self, wrong=False):
        self.wrong = wrong

    def reduce(self, metadata, cursor, evidence):
        items = {identity: {'kind': kind, 'pages': pages, 'duration_seconds': duration}
                 for identity, kind, pages, duration in metadata}
        result = progress._reduce_python(items, cursor)
        return {} if self.wrong else result


@pytest.mark.parametrize('wrong,code', [(False, 0), (True, 3)])
def test_parity_compares_both_reducers_on_one_read_only_snapshot(tmp_path, monkeypatch, wrong, code):
    database = catalog_database(tmp_path)
    monkeypatch.setattr(progress_backend, 'native', lambda: Native(wrong))
    result, summary = progress_parity.compare(database)
    assert result == code and summary['keys'] == 1 and summary['source'] == 'events'
    assert summary['match'] is (not wrong)


def test_parity_is_not_applicable_without_published_items(tmp_path, monkeypatch):
    database = catalog_database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE posts SET status='draft'")
    monkeypatch.setattr(progress_backend, 'native', lambda: Native())
    assert progress_parity.compare(database)[0] == 4


def test_parity_with_the_real_native_module(tmp_path):
    pytest.importorskip('postern_progress_native')
    try:
        progress_backend.native.cache_clear()
        progress_backend.native()
    except RuntimeError:
        pytest.skip('installed native module predates the checkpoint interface')
    result, summary = progress_parity.compare(catalog_database(tmp_path))
    assert result == 0 and summary['match'] is True
