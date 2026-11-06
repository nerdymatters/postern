"""Acceptance cannot be inferred from an incomplete synthetic HTTP workload."""
import argparse
import asyncio
import gc
import hashlib
import importlib
import importlib.machinery
import json
import shutil
import sqlite3
import time
import weakref
from types import SimpleNamespace

import httpx
import pytest

from scripts import capacity
from postern.db import AuditUnavailable


@pytest.fixture(autouse=True)
def controlled_host_cpu(monkeypatch):
    # Functional HTTP contracts must not depend on unrelated runner processes.
    # The overload test below overrides this input and proves the real guard.
    # Process/thread CPU accounting and HTTP health checks remain real.
    monkeypatch.setattr(capacity.psutil, 'cpu_percent', lambda *args, **kwargs: 0.0)


@pytest.fixture
def isolated_signin(tmp_path, monkeypatch):
    fixtures = tmp_path / 'tests/fixtures'
    fixtures.mkdir(parents=True)
    for name in ('page.jpg', 'selfie-face.jpg'):
        shutil.copyfile(capacity.ROOT / 'tests/fixtures' / name, fixtures / name)
    monkeypatch.setattr(capacity, 'ROOT', tmp_path)
    return argparse.Namespace(scenario='signin', users=1, roster=1, seconds=10,
                              history_hours=0, ramp_seconds=0, video_mbps=1.5,
                              cloud_delay=0, with_reader=False)


def test_process_cpu_attribution_uses_identity_and_deltas(monkeypatch):
    def processes(attributes):
        assert attributes == ['pid', 'name', 'create_time', 'cpu_times']
        return iter([
            SimpleNamespace(info={'pid': 1, 'name': 'worker', 'create_time': 10,
                                  'cpu_times': SimpleNamespace(user=2, system=1)}),
            SimpleNamespace(info={'pid': 2, 'name': 'inaccessible', 'create_time': 11, 'cpu_times': None}),
        ])
    monkeypatch.setattr(capacity.psutil, 'process_iter', processes)
    assert capacity.process_cpu_snapshot() == {(1, 10): ('worker', 3)}
    before = {(1, 10): ('worker', 3), (2, 11): ('old', 50), (3, 12): ('reset', 10)}
    after = {(1, 10): ('worker', 5), (2, 20): ('reused pid', 80), (3, 12): ('reset', 1), (4, 13): ('new', 8)}
    assert capacity.process_cpu_changes(before, after, .5) == [
        {'pid': 1, 'name': 'worker', 'cpu_percent_one_core': 400.0}]
    assert capacity.process_cpu_changes(before, after, 0) == []


def test_sustained_cpu_guard_rejects_otherwise_complete_workload(isolated_signin, tmp_path, monkeypatch):
    # Value: protects=whole-host overload still refuses a functionally complete run;
    # fails_when=CPU stop condition is bypassed; seam=CPU sensor and HTTP scheduling;
    # no_claim=physical overload performance (no busy work is generated).
    original_request = httpx.AsyncClient.request
    async def exercise():
        sampled = asyncio.Event()
        calls = 0
        def overloaded(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls >= 11:  # Initial sensor priming plus ten busy observations.
                sampled.set()
            return 100.0
        async def request(client, method, url, **kwargs):
            if str(url) == 'http://127.0.0.1:8790/health':
                raise httpx.ConnectError('No existing service in this isolated test')
            if str(url) == '/api/auth/demo':
                await asyncio.wait_for(sampled.wait(), 15)
            return await original_request(client, method, url, **kwargs)
        monkeypatch.setattr(capacity.psutil, 'cpu_percent', overloaded)
        monkeypatch.setattr(httpx.AsyncClient, 'request', request)
        return await capacity.run(isolated_signin)
    assert asyncio.run(exercise()) is False
    report = json.loads(next(tmp_path.glob('.context/capacity/*/report.json')).read_text())
    assert report['aborted'] == 'whole-host CPU reserve below 30% for five seconds'
    assert report['functional_checks_passed'] is False
    assert report['workload_coverage']['passed'] is True
    assert report['whole_host_idle_percent']['count'] >= 10
    assert report['whole_host_idle_percent']['max'] == 0
    diagnostics = report['host_cpu_diagnostics']
    assert diagnostics and len(diagnostics) <= 10
    assert any(sample.get('active_requests', {}).get('signin') == 1 for sample in diagnostics)
    assert all(sample['host_cpu_percent'] == 100 for sample in diagnostics)


@pytest.mark.parametrize('phase,fault', [
    (phase, fault) for phase in ('final', 'periodic', 'inflight')
    for fault in ('malformed', 'status', 'timeout', 'backup')
])
def test_health_failures_preserve_report(isolated_signin, tmp_path, monkeypatch, phase, fault):
    # Value: protects=failed health probes retain a failed report including probes racing workload completion;
    # fails_when=JSON errors escape or pending probe failure is canceled or ignored;
    # why_new=existing harness integrations see healthy JSON only; seam=none;
    # no_claim=production health or WAN failure (fault injected at HTTP client boundary, real loopback app).
    original_request = httpx.AsyncClient.request

    async def exercise():
        periodic_started, final_seen = asyncio.Event(), asyncio.Event()

        async def request(client, method, url, **kwargs):
            path = str(url)
            if path == 'http://127.0.0.1:8790/health':
                raise httpx.ConnectError('No existing service in this isolated test')
            response = await original_request(client, method, url, **kwargs)
            if path == '/api/auth/demo' and phase != 'final':
                await asyncio.wait_for(periodic_started.wait(), 8)
            if path.endswith('/health'):
                periodic = path.startswith('http://')
                if periodic:
                    periodic_started.set()
                    if phase == 'inflight':
                        await asyncio.wait_for(final_seen.wait(), 3)
                else:
                    final_seen.set()
                if (periodic and phase != 'final') or (not periodic and phase == 'final'):
                    if fault == 'timeout':
                        raise httpx.ReadTimeout('Controlled health timeout', request=response.request)
                    if fault == 'malformed':
                        return httpx.Response(200, content=b'not JSON', request=response.request)
                    if fault == 'status':
                        return httpx.Response(503, json=capacity.HEALTH, request=response.request)
                    return httpx.Response(200, json={**capacity.HEALTH, 'backup': 'overdue'},
                                          request=response.request)
            return response

        monkeypatch.setattr(httpx.AsyncClient, 'request', request)
        return await capacity.run(isolated_signin)

    error, passed = None, None
    try:
        passed = asyncio.run(exercise())
    except ValueError as caught:
        error = caught
    reports = list(tmp_path.glob('.context/capacity/*/report.json'))
    assert len(reports) == 1, f'Health failure lost its diagnostic report: {error}'
    report = json.loads(reports[0].read_text())
    assert passed is False and report['functional_checks_passed'] is False
    expected_status = 'transport_error' if fault == 'timeout' else '503' if fault == 'status' else '200'
    assert report['requests']['health']['statuses'][expected_status] >= 1
    if fault == 'timeout':
        assert report['transport_errors']['health']['ReadTimeout'] >= 1
    assert report['workload_coverage']['passed'] is True
    if phase != 'final':
        assert report['aborted'] == 'isolated health check failed; rehearsal stopped'
        assert sum(report['requests']['health']['statuses'].values()) >= 2


def test_short_mixed_workload_cannot_pass_without_progress_and_dashboard(tmp_path, monkeypatch):
    # Regression: CAP-001, found by /qa 2026-11-06.
    # Report: .context/rust-representative-20261106/qa/qa-report-capacity-2026-11-06.md
    # Value: protects=incomplete workload cannot qualify; fails_when=only opens/ranges are counted;
    # why_new=capacity tests cover service behavior, not CLI acceptance; seam=none;
    # no_claim=representative load performance (one synthetic reader, real loopback HTTP).
    fixtures = tmp_path / 'tests/fixtures'
    fixtures.mkdir(parents=True)
    shutil.copyfile(capacity.ROOT / 'tests/fixtures/page.jpg', fixtures / 'page.jpg')
    monkeypatch.setattr(capacity, 'ROOT', tmp_path)
    args = argparse.Namespace(scenario='mixed', users=1, roster=1, seconds=10,
                              history_hours=0, ramp_seconds=0, video_mbps=1.5,
                              cloud_delay=0, with_reader=False)
    original_request = capacity.Metrics.request
    video_responses, retained_between_requests = [], []

    async def observe(self, client, label, method, path, **kwargs):
        response = await original_request(self, client, label, method, path, **kwargs)
        if label == 'video_range' and response is not None:
            video_responses.append(weakref.ref(response))
        if label == 'health' and video_responses:
            # Value: protects=idle load-generator clients do not retain 4 MiB
            # bodies after checking them; real HTTP and byte checks still run.
            gc.collect()
            retained_between_requests.append(sum(ref() is not None for ref in video_responses))
        return response

    monkeypatch.setattr(capacity.Metrics, 'request', observe)
    assert asyncio.run(capacity.run(args)) is False
    assert retained_between_requests and max(retained_between_requests) == 0
    report = json.loads(next(tmp_path.glob('.context/capacity/*/report.json')).read_text())
    assert report['aborted'] is None
    assert report['integrity']['progress_matches_acknowledged_requests'] is True
    assert report['integrity']['raw_python_dashboard_matches'] is True
    assert report['requests']['video_range']['statuses'] == {'206': 1}
    assert report['workload_coverage']['passed'] is False
    assert 'progress' in report['workload_coverage']['missing']
    assert 'dashboard' in report['workload_coverage']['missing']
    # Value: protects=report identifies the actual runtime and includes during-load health observations;
    # fails_when=backend/hash is mislabeled or only the final health probe remains;
    # why_new=previous assertions only accepted any backend and one final probe; seam=none;
    # no_claim=representative CPU or performance acceptance (real isolated ten-second workload).
    backend = capacity.progress_backend.selected()
    assert report['runtime']['backend'] == backend
    assert report['runtime']['revision'] is None  # The owned fixture root is not a git checkout.
    assert report['process_rss_after_setup_mib'] > 0
    assert report['host_cpu_diagnostics'] == []  # Quiet functional sensor; real CPU accounting remains.
    expected_hash = None
    if backend == 'rust':
        extension = importlib.import_module('postern_progress_native.postern_progress_native')
        assert isinstance(extension.__loader__, importlib.machinery.ExtensionFileLoader)
        expected_hash = hashlib.sha256(capacity.Path(extension.__file__).read_bytes()).hexdigest()
    assert report['runtime']['native_sha256'] == expected_hash
    # gstack-timing-ok: adjacent process/thread CPU clock reads need a 10ms precision allowance, not a performance budget.
    assert report['process_cpu_seconds'] + .01 >= report['main_thread_cpu_seconds'] > 0
    assert report['writer_by_caller']['request_or_other']['operation_cpu_ms']['count'] > 0
    assert report['writer_by_caller']['request_or_other']['commit_ms']['count'] > 0
    assert report['requests']['health']['statuses']['200'] >= 2


def test_seeded_history_never_builds_an_orphan_backlog(tmp_path):
    # Regression: mixed hosted campaign failed when one final batch insert cleared
    # 492,480 synthetic orphan rows inside the bounded audit transaction.
    # Value: protects=history fixture obeys batch relationships throughout setup;
    # fails_when=confirmed batch is inserted after its events; seam=writer observer;
    # no_claim=representative load performance or production recovery.
    service = capacity.Service(capacity.Config(data_dir=tmp_path/'data', mode='demo', preview=True),
                               store=capacity.FakeStore(tmp_path/'cloud'))
    original_submit = service.db.submit
    orphan_counts = []
    inserted_per_transaction = []
    def observed_submit(operation):
        def observed(db):
            before = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            result = operation(db)
            inserted_per_transaction.append(db.execute('SELECT COUNT(*) FROM events').fetchone()[0]-before)
            orphan_counts.append(db.execute('SELECT COUNT(*) FROM health_orphan_events').fetchone()[0])
            return result
        return original_submit(observed)
    service.db.submit = observed_submit
    try:
        diagnostics = {}
        capacity.seed(service, 2, 1.1, diagnostics=diagnostics)
        assert orphan_counts and max(orphan_counts) == 0
        assert max(inserted_per_transaction) <= 128
        def inspect(db):
            return (db.execute('SELECT COUNT(*) FROM events').fetchone()[0],
                    tuple(db.execute('SELECT count,summary_confirmed FROM batches').fetchone()),
                    db.execute('SELECT COUNT(*) FROM events WHERE batch_id IS NULL').fetchone()[0])
        assert service.db.read(inspect) == (528, (528, 1), 0)
        for reader in range(2):
            recorded = service.db.read(lambda db: list(db.execute(
                'SELECT ts,payload FROM events WHERE email=? ORDER BY seq', (f'reader{reader}@example.invalid',))))
            assert [json.loads(row['payload'])['detail']['ranges'] for row in recorded] == [
                [[n*15, n*15+15]] for n in range(264)]
            assert [row['ts']-recorded[0]['ts'] for row in recorded] == [n*15 for n in range(264)]
        assert diagnostics['operations'] == 8 and 'failure' not in diagnostics
        assert diagnostics['commit_ms_max'] > 0
        assert service.db.deadline == 1.5
        assert not service.db.failed
    finally:
        service.db.close()


def test_seed_failure_retains_position_and_does_not_start_load(isolated_signin, tmp_path, monkeypatch):
    isolated_signin.history_hours = 1
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('controlled fixture write failure')
    monkeypatch.setattr(capacity, 'event', broken)
    with pytest.raises(AuditUnavailable):
        asyncio.run(capacity.run(isolated_signin))
    folder = next(tmp_path.glob('.context/capacity/*'))
    setup = json.loads((folder/'setup.json').read_text())
    assert setup['status'] == 'failed' and setup['error'] == 'AuditUnavailable'
    assert setup['failure'] == {'phase': 'history', 'reader': 0, 'start': 0, 'end': 128,
                                'error': 'AuditUnavailable', 'queue_ms': None, 'commit_ms': None}
    assert setup['operations'] == 3
    assert not (folder/'report.json').exists()
    with sqlite3.connect(folder/'data/postern.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0


def test_fast_enrollment_keeps_reader_until_progress_is_saved(tmp_path, monkeypatch):
    # Value: protects=fast enrollment cannot truncate the reader observation window;
    # fails_when=viewer stops as soon as captures finish; why_new=mixed regression has no enrollment worker;
    # seam=none; no_claim=real camera/provider or representative load acceptance.
    fixtures = tmp_path / 'tests/fixtures'
    fixtures.mkdir(parents=True)
    for name in ('page.jpg', 'selfie-face.jpg'):
        shutil.copyfile(capacity.ROOT / 'tests/fixtures' / name, fixtures / name)
    monkeypatch.setattr(capacity, 'ROOT', tmp_path)
    args = argparse.Namespace(scenario='enrollment', users=1, roster=2, seconds=18,
                              history_hours=0, ramp_seconds=0, video_mbps=1.5,
                              cloud_delay=0, with_reader=True)
    assert asyncio.run(capacity.run(args)) is True
    report = json.loads(next(tmp_path.glob('.context/capacity/*/report.json')).read_text())
    assert report['elapsed_seconds'] >= 18  # gstack-timing-ok: 18s is the explicitly requested observation contract.
    assert report['workload_coverage']['passed'] is True
    assert report['integrity']['acknowledged_progress_records'] >= 1
    assert report['integrity']['progress_matches_acknowledged_requests'] is True
    assert report['requests']['enrollment_ready']['statuses'] == {'confirmed': 1}


@pytest.mark.parametrize('scenario,with_reader,users,counts,readers', [
    ('signin', False, 2, {'signin': ('200', 2)}, []),
    ('enrollment', False, 2, {'selfie_capture': ('202', 2), 'selfie_status': ('200', 2),
                           'enrollment_ready': ('confirmed', 2)}, []),
    ('enrollment', True, 2, {'selfie_capture': ('202', 2), 'selfie_status': ('200', 2),
                          'enrollment_ready': ('confirmed', 2), 'existing_reader_open': ('200', 1),
                          'existing_reader_player': ('200', 1), 'existing_reader_range': ('206', 1),
                          'existing_reader_progress': ('200', 1)}, [2]),
    ('mixed', False, 2, {'open_view': ('200', 4), 'player': ('200', 2), 'video_range': ('206', 2),
                      'progress': ('200', 2), 'catalog': ('200', 2), 'page': ('200', 1),
                      'dashboard': ('200', 1)}, [0, 1]),
    ('mixed', False, 6, {'open_view': ('200', 12), 'player': ('200', 6), 'video_range': ('206', 6),
                       'progress': ('200', 6), 'catalog': ('200', 6), 'page': ('200', 2),
                       'dashboard': ('200', 1)}, [0, 1, 2, 3, 4, 5]),
])
def test_missing_family_or_reader_never_qualifies(scenario, with_reader, users, counts, readers):
    # Value: protects=required successful traffic minimums and every reader acknowledgement;
    # fails_when=undercounts, wrong status, zero acknowledgements or rounded-down pages qualify;
    # why_new=original table only removed whole families and readers; seam=none;
    # no_claim=HTTP integration or representative performance (explicit protocol-count fixtures).
    args = argparse.Namespace(scenario=scenario, users=users, with_reader=with_reader)
    statuses = {label: {status: count} for label, (status, count) in counts.items()}
    progress = {(f'reader{i}@example.invalid', ((0., 15.),)): 1 for i in readers}
    assert capacity.workload_coverage(args, statuses, progress)['passed']
    for label in statuses:
        incomplete = {key: value for key, value in statuses.items() if key != label}
        result = capacity.workload_coverage(args, incomplete, progress)
        assert not result['passed'] and label in result['missing']
        status, count = counts[label]
        for replacement in ({status: count-1}, {'599': count}):
            result = capacity.workload_coverage(args, {**statuses, label: replacement}, progress)
            assert not result['passed'] and label in result['missing']
    if readers:
        # A repeated acknowledgement from one reader cannot stand in for another.
        missing = dict(progress)
        missing.pop(next(iter(missing)))
        assert not capacity.workload_coverage(args, statuses, missing)['passed']
        zero = {**progress, next(iter(progress)): 0}
        result = capacity.workload_coverage(args, statuses, zero)
        assert not result['passed']
        assert result['readers_without_progress'] == [next(iter(progress))[0]]


def test_writer_measurements_include_owned_work_and_exclude_lifecycle(isolated_signin, tmp_path, monkeypatch):
    # Value: protects=writer ownership/errors and CPU reports include workload while excluding lifecycle work;
    # fails_when=background writes are mislabeled, failed operations disappear or lifecycle CPU enters report;
    # why_new=earlier telemetry assertions checked positive counts only; seam=none;
    # no_claim=production CPU attribution (controlled real CPU work and synthetic Service/HTTP lifecycle).
    original_start, original_close = capacity.Service.start, capacity.Service.close
    original_request = httpx.AsyncClient.request
    observed = {}

    def cpu_work(seconds):
        before = time.thread_time()
        while time.thread_time()-before < seconds:
            hashlib.sha256(b'capacity measurement control').digest()

    def operation(db, seconds):
        cpu_work(seconds)
        return db.execute('SELECT 1').fetchone()[0]

    async def exercise():
        requested, finished = asyncio.Event(), asyncio.Event()

        async def background(service):
            await requested.wait()
            observed['background_result'] = await service.write(lambda db: operation(db, .06))

            def fail(db):
                operation(db, .04)
                raise ValueError('controlled writer failure')

            try:
                await service.write(fail)
            except ValueError as error:
                observed['writer_error'] = str(error)
            finally:
                finished.set()

        async def start(service):
            await original_start(service)
            observed['service'] = service
            await service.write(lambda db: operation(db, .4))

        async def close(service):
            await service.write(lambda db: operation(db, .4))
            await original_close(service)

        async def request(client, method, url, **kwargs):
            if str(url) == 'http://127.0.0.1:8790/health':
                raise httpx.ConnectError('No existing service in this isolated test')
            if str(url) == '/api/auth/demo':
                cpu_work(.03)
                requested.set()
                await asyncio.wait_for(finished.wait(), 3)
                # This submission has no running asyncio loop in its caller thread.
                observed['thread_result'] = await asyncio.to_thread(
                    lambda: observed['service'].db.submit(lambda db: operation(db, .07)).result(2))
            return await original_request(client, method, url, **kwargs)

        monkeypatch.setattr(capacity.Service, 'background_loop', background)
        monkeypatch.setattr(capacity.Service, 'start', start)
        monkeypatch.setattr(capacity.Service, 'close', close)
        monkeypatch.setattr(httpx.AsyncClient, 'request', request)
        observed['owner'] = background.__qualname__
        return await capacity.run(isolated_signin)

    before = time.process_time()
    assert asyncio.run(exercise()) is True
    whole_run_cpu = time.process_time()-before
    report = json.loads(next(tmp_path.glob('.context/capacity/*/report.json')).read_text())
    assert observed['background_result'] == observed['thread_result'] == 1
    assert observed['writer_error'] == 'controlled writer failure'
    owner = report['writer_by_caller'][observed['owner']]
    assert owner['operation_cpu_ms']['count'] == 2
    assert owner['operation_cpu_ms']['min'] >= 35  # gstack-timing-ok: actual .04s/.06s thread CPU work, with 5ms allowance.
    assert owner['operation_cpu_ms']['max'] >= 55  # gstack-timing-ok: successful writer operation burns .06s thread CPU.
    assert owner['queue_ms']['count'] == owner['commit_ms']['count'] == 1
    request_cpu = report['writer_by_caller']['request_or_other']['operation_cpu_ms']
    assert 65 <= request_cpu['max'] < 200  # gstack-timing-ok: .07s thread job is measured; neither .4s lifecycle job is.
    assert .18 <= report['process_cpu_seconds'] < whole_run_cpu-.7  # gstack-timing-ok: .20s real workload; .80s lifecycle excluded.
    # Startup's .4s writer job must remain visible in comparable CPU; moving
    # dashboard work before readiness cannot manufacture an apparent saving.
    assert report['startup']['process_cpu_seconds'] >= .38  # gstack-timing-ok: .4s real startup CPU, 20ms accounting allowance.
    assert report['comparable_process_cpu_seconds'] == report['process_cpu_seconds']+report['startup']['process_cpu_seconds']
    assert report['comparable_process_cpu_seconds'] < whole_run_cpu-.35  # gstack-timing-ok: shutdown's .4s is still outside both serving phases.
    assert report['main_thread_cpu_seconds'] >= .025  # gstack-timing-ok: .03s controlled main-thread CPU work.
    assert report['other_threads_cpu_seconds'] >= .15  # gstack-timing-ok: .17s controlled writer CPU work, with 20ms allowance.


def test_startup_guard_cancels_lifespan_and_keeps_failure_evidence(isolated_signin, tmp_path, monkeypatch):
    original_start, original_close = capacity.Service.start, capacity.Service.close
    observed = {}
    async def start(service):
        observed['service'] = service
        await original_start(service)
        observed['priming'] = True
        await asyncio.Event().wait()  # Controlled unfinished startup, no CPU work.
    async def close(service):
        await original_close(service)
        observed['closed'] = True
    # Preflight is healthy; a later resource sample fails during startup.
    monkeypatch.setattr(capacity.psutil, 'virtual_memory', lambda: SimpleNamespace(
        available=512*1024**2 if observed.get('priming') else 4*1024**3))
    monkeypatch.setattr(capacity.Service, 'start', start)
    monkeypatch.setattr(capacity.Service, 'close', close)
    with pytest.raises(RuntimeError, match='memory safety threshold'):
        asyncio.run(capacity.run(isolated_signin))
    assert observed['closed'] and not observed['service'].db.thread.is_alive()
    report = json.loads(next(tmp_path.glob('.context/capacity/*/startup-failure.json')).read_text())
    assert not report['functional_checks_passed'] and report['aborted'] == 'memory safety threshold'
    assert report['startup']['elapsed_seconds'] > 0
    assert not report['resources']['requests']  # No workload can run before readiness.


def test_readiness_observed_after_deadline_is_rejected_and_retained(isolated_signin, tmp_path, monkeypatch):
    async def exercise():
        caller = asyncio.current_task()
        original_sleep = asyncio.sleep
        delayed = False
        async def late_poll(seconds):
            nonlocal delayed
            if asyncio.current_task() is caller and seconds == .05 and not delayed:
                delayed = True
                await original_sleep(10.05)  # gstack-timing-ok: cross the fixed 10s readiness deadline while the server starts normally.
            else:
                await original_sleep(seconds)
        monkeypatch.setattr(capacity.asyncio, 'sleep', late_poll)
        return await capacity.run(isolated_signin)
    with pytest.raises(RuntimeError, match='startup timed out'):
        asyncio.run(exercise())
    report = json.loads(next(tmp_path.glob('.context/capacity/*/startup-failure.json')).read_text())
    assert not report['functional_checks_passed']
    assert report['startup']['ready'] and report['startup']['elapsed_seconds'] > 10
    assert 'signin' not in report['resources']['requests']


@pytest.mark.parametrize('fault', ['stale_output', 'raw_error'])
def test_wrong_dashboard_output_fails_independent_raw_replay_check(isolated_signin, tmp_path, monkeypatch, fault):
    args = isolated_signin
    args.scenario, args.seconds = 'mixed', 17
    original_request = httpx.AsyncClient.request
    async def stale(client, method, url, **kwargs):
        response = await original_request(client, method, url, **kwargs)
        if fault == 'stale_output' and str(url).endswith('/api/admin/progress') and response.status_code == 200:
            # Simulate stale/missing accumulated evidence behind a successful HTTP status.
            return httpx.Response(200, json={**response.json(), 'people': []}, request=response.request)
        return response
    monkeypatch.setattr(httpx.AsyncClient, 'request', stale)
    original_read = capacity.Service.read_progress
    async def read(service, operation):
        if fault == 'raw_error' and operation.__qualname__ == 'verify_dashboard.<locals>.reference':
            raise RuntimeError('injected reference replay failure')
        return await original_read(service, operation)
    monkeypatch.setattr(capacity.Service, 'read_progress', read)
    assert asyncio.run(capacity.run(args)) is False
    report = json.loads(next(tmp_path.glob('.context/capacity/*/report.json')).read_text())
    assert report['workload_coverage']['passed']
    assert report['integrity']['progress_matches_acknowledged_requests']
    assert report['integrity']['raw_python_dashboard_matches'] is False
    assert report['aborted'] is None
    if fault == 'raw_error':
        assert report['verification']['error'] == 'RuntimeError'
    assert report['process_cpu_seconds'] > 0


def test_mixed_resource_abort_skips_additional_verification(isolated_signin, tmp_path, monkeypatch):
    args = isolated_signin
    args.scenario, args.seconds = 'mixed', 17
    monkeypatch.setattr(capacity.psutil, 'cpu_percent', lambda *args, **kwargs: 100.)
    async def unexpected(*args):
        pytest.fail('Verification must not add work after a resource abort')
    monkeypatch.setattr(capacity, 'verify_dashboard', unexpected)
    assert asyncio.run(capacity.run(args)) is False
    report = json.loads(next(tmp_path.glob('.context/capacity/*/report.json')).read_text())
    assert report['aborted'] == 'whole-host CPU reserve below 30% for five seconds'
    assert report['verification']['skipped'] == 'workload aborted'
    assert report['integrity']['raw_python_dashboard_matches'] is False


@pytest.mark.parametrize('with_reader', [False, True])
def test_enrollment_wait_is_optional_and_obeys_health_stop(isolated_signin, tmp_path, monkeypatch, with_reader):
    # Value: protects=no-reader enrollment finishes promptly and reader observation obeys a health stop;
    # fails_when=the observation wait becomes unconditional or ignores the stop signal;
    # why_new=existing enrollment test covers the full successful window only; seam=none;
    # no_claim=real overload or camera/provider failures (isolated app with controlled health response).
    args = isolated_signin
    args.scenario, args.roster, args.seconds, args.with_reader = 'enrollment', 2, 30, with_reader
    original_request = httpx.AsyncClient.request

    async def request(client, method, url, **kwargs):
        path = str(url)
        if path == 'http://127.0.0.1:8790/health':
            raise httpx.ConnectError('No existing service in this isolated test')
        response = await original_request(client, method, url, **kwargs)
        if with_reader and path.startswith('http://') and path.endswith('/health'):
            return httpx.Response(200, json={**capacity.HEALTH, 'backup': 'overdue'}, request=response.request)
        return response

    monkeypatch.setattr(httpx.AsyncClient, 'request', request)
    assert asyncio.run(capacity.run(args)) is (not with_reader)
    report = json.loads(next(tmp_path.glob('.context/capacity/*/report.json')).read_text())
    assert report['elapsed_seconds'] < 20  # gstack-timing-ok: absence/stop must bypass the declared 30s observation wait.
    assert report['requests']['enrollment_ready']['statuses'] == {'confirmed': 1}
    if with_reader:
        assert report['aborted'] == 'isolated health check failed; rehearsal stopped'
        assert not report['workload_coverage']['passed']
        assert report['workload_coverage']['readers_without_progress'] == ['reader1@example.invalid']
    else:
        assert report['aborted'] is None and report['workload_coverage']['passed']
        assert 'existing_reader_progress' not in report['requests']
