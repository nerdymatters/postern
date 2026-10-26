import asyncio
import concurrent.futures
import io
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image
from starlette.requests import Request

from scripts.day1.audit import AuditUnavailable, AuditWriter
from scripts.day1.common import checked_base_url, percentiles, private_json
from scripts.day1.load import check_uncached, hls_segments
from scripts.day1.prepare import prepare
from scripts.day1.server import create_app, observed_ip, stamp


def test_audit_serializes_concurrent_writes_and_survives_reopen(tmp_path):
    writer = AuditWriter(tmp_path / 'audit.sqlite3')
    assert writer.settings['journal_mode'] == 'wal'
    assert writer.settings['synchronous'] == 2
    assert writer.settings['fullfsync'] == 1
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:
            results = list(pool.map(lambda n: writer.submit('run', str(n), 'page').result(3), range(25)))
        assert all(r['total_ms'] >= r['commit_ms'] >= 0 for r in results)
    finally:
        writer.close()
    with sqlite3.connect(tmp_path / 'audit.sqlite3') as db:
        assert db.execute('SELECT count(*),count(DISTINCT reader) FROM attempts').fetchone() == (25, 25)


def test_busy_writer_refuses_then_recovers(tmp_path):
    writer = AuditWriter(tmp_path / 'audit.sqlite3', busy_ms=30)
    lock = sqlite3.connect(writer.path)
    try:
        lock.execute('BEGIN IMMEDIATE')
        with pytest.raises(AuditUnavailable):
            writer.submit('run', 'reader', 'page').result(2)
        lock.rollback()
        writer.submit('run', 'reader', 'page').result(2)
    finally:
        lock.close()
        writer.close()
    with sqlite3.connect(writer.path) as db:
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1


def test_percentiles_use_tail_not_average_and_empty_is_not_zero():
    assert percentiles(list(range(1, 101))) == {'p50': 50, 'p95': 95, 'p99': 99}
    assert percentiles([])['p99'] is None


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:password@example.com',
                                  'https://example.com?token=secret', 'file:///etc/passwd'])
def test_remote_credentials_and_plaintext_urls_rejected(url):
    with pytest.raises(ValueError):
        checked_base_url(url)


def test_output_never_overwrites_previous_run(tmp_path):
    path = tmp_path / 'report.json'
    private_json(path, {'first': True})
    with pytest.raises(FileExistsError):
        private_json(path, {'second': True})
    assert json.loads(path.read_text()) == {'first': True}
    assert path.stat().st_mode & 0o777 == 0o600


def test_hls_parser_rejects_external_urls():
    assert hls_segments('#EXTM3U\n#EXTINF:6,\nseg_001.ts\n') == [('seg_001.ts', 6)]
    with pytest.raises(ValueError):
        hls_segments('#EXTM3U\n#EXTINF:6,\nhttps://another-host/seg.ts\n')


@pytest.mark.parametrize('extra', [{'cf-cache-status': 'HIT'}, {'cf-cache-status': 'STALE'},
                                   {'cf-cache-status': 'MISS'}, {'age': '1'}])
def test_no_store_does_not_hide_evidence_of_cdn_caching(extra):
    with pytest.raises(RuntimeError, match='CDN caching'):
        check_uncached({'cache-control': 'no-store', **extra})
    assert check_uncached({'cache-control': 'no-store', 'cf-cache-status': 'DYNAMIC'}) == 'DYNAMIC'


def test_proxy_headers_are_ignored_without_explicit_trust():
    request = Request({'type': 'http', 'client': ('127.0.0.1', 123),
                       'headers': [(b'x-forwarded-for', b'192.0.2.8')]})
    assert observed_ip(request, False) == '127.0.0.1'
    assert observed_ip(request, True) == '192.0.2.8'
    remote = Request({'type': 'http', 'client': ('198.51.100.5', 123),
                      'headers': [(b'x-forwarded-for', b'192.0.2.8')]})
    assert observed_ip(remote, True) == '198.51.100.5'


@pytest.fixture
def fixture_dir(tmp_path):
    directory = tmp_path / 'fixture'
    prepare(directory, pages=2, readers=2)
    return directory


def test_stamped_pixels_differ_by_reader(fixture_dir):
    path = fixture_dir / 'pages' / '0.jpg'
    a, b = stamp(path, 'one@example.invalid'), stamp(path, 'two@example.invalid')
    assert a != b and a != path.read_bytes()
    with Image.open(io.BytesIO(stamp(path, 'one@example.invalid', True))) as thumb:
        assert thumb.width <= 400


async def request_app(app, path, token=None, method="GET", payload=b""):
    sent = []
    async def receive():
        return {'type': 'http.request', 'body': payload, 'more_body': False}
    async def send(message):
        sent.append(message)
    headers = [] if token is None else [(b'authorization', f'Bearer {token}'.encode())]
    await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
               'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
               'query_string': b'', 'root_path': '', 'headers': headers,
               'client': ('127.0.0.1', 50000), 'server': ('127.0.0.1', 8788)}, receive, send)
    return next(m['status'] for m in sent if m['type'] == 'http.response.start'), b''.join(
        m.get('body', b'') for m in sent if m['type'] == 'http.response.body'), sent


@pytest.mark.parametrize('cached', [False, True])
def test_no_jpeg_bytes_when_audit_is_unavailable(fixture_dir, cached):
    token = next(iter(json.loads((fixture_dir / 'credentials.json').read_text())['readers']))
    app = create_app(fixture_dir, composite_cache=cached)
    async def run():
        async with app.router.lifespan_context(app):
            status, body, _ = await request_app(app, '/probe/pages/0.jpg')
            assert status == 401 and not body.startswith(b'\xff\xd8')
            status, body, sent = await request_app(app, '/probe/pages/0.jpg', token)
            assert status == 200 and body.startswith(b'\xff\xd8')
            headers = dict(next(m['headers'] for m in sent if m['type'] == 'http.response.start'))
            assert headers[b'cache-control'] == b'no-store'
            def fail(*args):
                future = concurrent.futures.Future()
                future.set_exception(AuditUnavailable('simulated disk failure'))
                return future
            app.state.writer.submit = fail
            status, body, _ = await request_app(app, '/probe/pages/0.jpg', token)
            assert status == 503 and b'audit_unavailable' in body
            assert not body.startswith(b'\xff\xd8')
    asyncio.run(run())


@pytest.mark.parametrize('action', ['logout', 'revoke'])
def test_cache_hits_audit_each_request_and_eviction_blocks_access(fixture_dir, action):
    credentials = json.loads((fixture_dir / 'credentials.json').read_text())
    token, other = list(credentials['readers'])
    app = create_app(fixture_dir, composite_cache=True)
    async def run():
        async with app.router.lifespan_context(app):
            bodies = []
            for credential in [token, token, other]:
                status, body, sent = await request_app(app, '/probe/pages/0.jpg', credential)
                assert status == 200
                headers = dict(next(m['headers'] for m in sent if m['type'] == 'http.response.start'))
                assert headers[b'cache-control'] == b'no-store'
                bodies.append(body)
            assert bodies[0] == bodies[1] and bodies[1] != bodies[2]
            with sqlite3.connect(fixture_dir / 'audit.sqlite3') as db:
                assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 3
            assert app.state.cache.snapshot()['hits'] >= 1
            if action == 'logout':
                result = await request_app(app, '/probe/logout', token, method='POST')
            else:
                denied = await request_app(app, '/probe/revoke', other, method='POST',
                                           payload=json.dumps({'token': token}).encode())
                assert denied[0] == 401
                result = await request_app(app, '/probe/revoke', credentials['operator'], method='POST',
                                           payload=json.dumps({'token': token}).encode())
            assert result[0] == 204
            assert app.state.cache.snapshot()['entries'] == 1  # Only the other reader remains.
            status, body, _ = await request_app(app, '/probe/pages/0.jpg', token)
            assert status == 401 and not body.startswith(b'\xff\xd8')
            assert (await request_app(app, '/probe/pages/0.jpg', other))[0] == 200
    asyncio.run(run())
