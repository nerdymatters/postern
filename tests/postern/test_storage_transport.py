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


def test_google_clients_are_reused_per_thread_until_the_credential_file_changes(tmp_path, monkeypatch):
    # Value: protects=the 30 s allowlist refresh reuses its token and connection instead of a new OAuth
    # exchange and TLS handshake each time (RS-08: most of the API's idle CPU), while threads never share
    # an httplib2 client, a changed file is reloaded and loosened permissions still refuse;
    # fails_when=the cache is dropped, shared across threads, keyed so a rotated file is ignored, or the
    # 0600 check runs only on the first load; why_new=no test counted client construction; seam=none;
    # no_claim=Google's token lifetime or connection keep-alive
    import os
    import pytest
    from postern.storage import StoreUnavailable
    loads = []
    monkeypatch.setattr(Credentials, 'from_authorized_user_info', lambda info: loads.append(info) or Credentials(token='test-token'))
    monkeypatch.setattr('googleapiclient.discovery.build', lambda *args, **kwargs: kwargs['http'])
    credentials = tmp_path / 'credentials.json'
    private_json(credentials, {'generation': 1})
    store = GoogleStore(Config(tmp_path, host_credentials=str(credentials)))
    first = store._service('drive', 'v3')
    assert store._service('sheets', 'v4') is first and len(loads) == 1
    other = []
    thread = threading.Thread(target=lambda: other.append(store._service('drive', 'v3')))
    thread.start(); thread.join()
    assert other[0] is not first and len(loads) == 2
    private_json(credentials, {'generation': 2})
    os.utime(credentials, ns=(1, 1))
    rotated = store._service('drive', 'v3')
    assert rotated is not first and loads[-1] == {'generation': 2}
    assert store._service('drive', 'v3') is rotated and len(loads) == 3
    credentials.chmod(0o644)
    with pytest.raises(StoreUnavailable, match='0600'):
        store._service('drive', 'v3')
