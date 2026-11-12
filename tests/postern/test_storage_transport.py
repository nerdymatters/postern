"""Exercise Drive's resumable protocol through the configured real HTTP stack."""
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google.oauth2.credentials import Credentials
from googleapiclient.http import HttpRequest, MediaIoBaseUpload

from postern.config import Config, private_json
from postern.storage import GoogleStore


def test_resumable_upload_continues_after_308(tmp_path, monkeypatch):
    received = bytearray()
    payload = b'a' * (256 * 1024) + b'b' * 100

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(200)
            self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/session')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def do_PUT(self):
            chunk = self.rfile.read(int(self.headers['Content-Length']))
            received.extend(chunk)
            if len(received) < len(payload):
                self.send_response(308)
                self.send_header('Range', f'bytes=0-{len(received)-1}')
                self.send_header('Content-Length', '0')
                self.end_headers()
            else:
                result = json.dumps({'id': 'verified-upload'}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(result)))
                self.end_headers()
                self.wfile.write(result)

    credentials = tmp_path / 'credentials.json'
    private_json(credentials, {})
    monkeypatch.setattr(Credentials, 'from_authorized_user_info', lambda _: Credentials(token='test-token'))
    monkeypatch.setattr('googleapiclient.discovery.build', lambda *args, **kwargs: kwargs['http'])
    transport = GoogleStore(Config(tmp_path, host_credentials=str(credentials)))._service('drive', 'v3')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = HttpRequest(
            transport, lambda response, body: json.loads(body),
            f'http://127.0.0.1:{server.server_port}/upload', method='POST', body='{}',
            resumable=MediaIoBaseUpload(io.BytesIO(payload), mimetype='video/mp4',
                                       chunksize=256 * 1024, resumable=True),
        )
        assert request.execute() == {'id': 'verified-upload'}
        assert received == payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_google_credentials_are_reused_per_thread_until_the_credential_file_changes(tmp_path, monkeypatch):
    # Value: protects=each thread reuses its Google credentials (no OAuth exchange per call, RS-08), yet
    # reloads a changed file, refuses loosened permissions and opens a new connection every call;
    # fails_when=the cache is dropped or shared across threads, a renamed, overwritten or cp -p copied file is ignored,
    # 0600 is checked only on load, or a connection Google may have closed is reused; why_new=new cache;
    # seam=none; no_claim=Google's token lifetime or connection keep-alive
    import os
    import time
    import pytest
    from postern.storage import StoreUnavailable
    loads = []
    monkeypatch.setattr(Credentials, 'from_authorized_user_info', lambda info: loads.append(info) or Credentials(token='test-token'))
    monkeypatch.setattr('googleapiclient.discovery.build', lambda *args, **kwargs: kwargs['http'])
    credentials = tmp_path / 'credentials.json'
    private_json(credentials, {'generation': 1})
    store = GoogleStore(Config(tmp_path, host_credentials=str(credentials)))
    first = store._service('drive', 'v3')
    again = store._service('sheets', 'v4')
    assert again.credentials is first.credentials and len(loads) == 1
    assert again is not first and again.http is not first.http
    other = []
    thread = threading.Thread(target=lambda: other.append(store._service('drive', 'v3')))
    thread.start(); thread.join()
    assert other[0].credentials is not first.credentials and len(loads) == 2
    private_json(credentials, {'generation': 2})
    os.utime(credentials, ns=(1, 1))
    rotated = store._service('drive', 'v3').credentials
    assert rotated is not first.credentials and loads[-1] == {'generation': 2}
    assert store._service('drive', 'v3').credentials is rotated and len(loads) == 3
    # A rename that keeps size and mtime (cp -p, then mv) changes only the inode.
    previous = credentials.stat()
    private_json(credentials, {'generation': 3})
    os.utime(credentials, ns=(1, 1))
    renamed = credentials.stat()
    assert renamed.st_ino != previous.st_ino
    assert (renamed.st_size, renamed.st_mtime_ns) == (previous.st_size, previous.st_mtime_ns)
    reloaded = store._service('drive', 'v3').credentials
    assert reloaded is not rotated and loads[-1] == {'generation': 3}
    # An overwrite in place (cp onto the file) keeps the inode and size and changes only the mtime.
    credentials.write_text(json.dumps({'generation': 4}, indent=2) + '\n')
    os.utime(credentials, (2, 2))
    overwritten = credentials.stat()
    assert (overwritten.st_ino, overwritten.st_size) == (renamed.st_ino, renamed.st_size)
    assert overwritten.st_mtime_ns != renamed.st_mtime_ns
    assert store._service('drive', 'v3').credentials is not reloaded and loads[-1] == {'generation': 4}
    assert len(loads) == 5
    # A copy that keeps size and mtime (cp -p over the file in place) changes only the ctime.
    current = store._service('drive', 'v3').credentials
    before = credentials.stat()
    # Linux before 6.13 stamps ctime from a coarse clock tick, so let the clock pass the
    # last change first, or the rewrite could keep the old ctime and prove nothing.
    while time.time_ns() <= before.st_ctime_ns + 20_000_000:  # gstack-timing-ok: waits for a distinct ctime, asserts none
        time.sleep(0.005)
    credentials.write_text(json.dumps({'generation': 5}, indent=2) + '\n')
    os.utime(credentials, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = credentials.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert after.st_ctime_ns != before.st_ctime_ns
    assert store._service('drive', 'v3').credentials is not current and loads[-1] == {'generation': 5}
    assert len(loads) == 6
    credentials.chmod(0o644)
    with pytest.raises(StoreUnavailable, match='0600'):
        store._service('drive', 'v3')


def test_allowlist_and_host_credentials_are_kept_apart_on_one_thread(tmp_path, monkeypatch):
    # Value: protects=a worker thread alternating allowlist reads (service account) and host Drive calls
    # keeps both credentials, and allowlist reads use the allowlist file; fails_when=a thread caches one
    # credential file at a time, or allowlist reads load host credentials; why_new=the reuse test uses
    # one authorized-user file; seam=none; no_claim=the service account's Sheets scope or thread pool reuse
    from google.oauth2.service_account import Credentials as ServiceCredentials
    loads = []
    monkeypatch.setattr(Credentials, 'from_authorized_user_info',
                        lambda info: loads.append(info['file']) or Credentials(token='host-token'))
    monkeypatch.setattr(ServiceCredentials, 'from_service_account_info',
                        lambda info, **_: loads.append(info['file']) or Credentials(token='allowlist-token'))
    monkeypatch.setattr('googleapiclient.discovery.build', lambda *args, **kwargs: kwargs['http'])
    host, reader = tmp_path / 'host.json', tmp_path / 'allowlist.json'
    private_json(host, {'file': 'host'})
    private_json(reader, {'type': 'service_account', 'file': 'allowlist'})
    store = GoogleStore(Config(tmp_path, host_credentials=str(host), allowlist_credentials=str(reader)))
    sheet = store._service('sheets', 'v4', True).credentials
    drive = store._service('drive', 'v3').credentials
    assert sheet is not drive and loads == ['allowlist', 'host']
    for _ in range(3):
        assert store._service('sheets', 'v4', True).credentials is sheet
        assert store._service('drive', 'v3').credentials is drive
    assert loads == ['allowlist', 'host']
