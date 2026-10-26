"""Exercise the actual Caddy config; skip when the optional binary is absent."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import threading
import time

import pytest


@pytest.fixture
def echo_upstream():
    class Echo(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({'observed_ip': self.headers.get('X-Forwarded-For', ''),
                               'real_ip': self.headers.get('X-Real-IP', ''),
                               'forwarded': self.headers.get('Forwarded', '')}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('config_name,listen_port', [('Caddyfile', 18789), ('Caddyfile.cloudflare', 18790)])
def test_proxy_requires_metadata_and_overwrites_forwarding_headers(tmp_path, echo_upstream, config_name, listen_port):
    caddy = shutil.which('caddy')
    if not caddy:
        pytest.skip('Caddy is required for the proxy integration check')
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    source = Path(__file__).resolve().parents[2] / 'ops/probes' / config_name
    config = source.read_text().replace(str(listen_port), str(port))
    # Exercise the unmodified listener/proxy rules with an isolated upstream.
    config = config.replace('127.0.0.1:8788', f'127.0.0.1:{echo_upstream}')
    path = tmp_path / 'Caddyfile'
    path.write_text(config)
    with (tmp_path / 'caddy.log').open('w') as log:
        proc = subprocess.Popen([caddy, 'run', '--config', str(path), '--adapter', 'caddyfile'],
                                stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while True:
                assert proc.poll() is None, (tmp_path / 'caddy.log').read_text()
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    assert time.monotonic() < deadline, 'Caddy did not start'
                    time.sleep(.05)
            proxy = (b'\r\n\r\n\x00\r\nQUIT\n' + bytes([0x21, 0x11]) + struct.pack('!H', 12)
                     + socket.inet_aton('192.0.2.123') + socket.inet_aton('127.0.0.1')
                     + struct.pack('!HH', 50000, 443))
            request = (b'GET /probe/ip HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                       b'X-Forwarded-For: 198.51.100.99\r\nX-Real-IP: 198.51.100.98\r\n'
                       b'Forwarded: for=198.51.100.97\r\n\r\n')

            def exchange(prefix):
                with socket.create_connection(('127.0.0.1', port), timeout=5) as conn:
                    if config_name == 'Caddyfile.cloudflare':
                        message = request if not prefix else request.replace(
                            b'Host: localhost\r\n', b'Host: localhost\r\nCF-Connecting-IP: 192.0.2.123\r\n')
                    else:
                        message = prefix + request
                    conn.sendall(message)
                    result = b''
                    while True:
                        try:
                            chunk = conn.recv(4096)
                        except ConnectionResetError:
                            break
                        if not chunk:
                            break
                        result += chunk
                    return result

            response = exchange(proxy)
            assert response.startswith(b'HTTP/1.1 200'), response
            if config_name == 'Caddyfile.cloudflare':
                assert b'Cache-Control: no-store' in response
                assert response.count(b'\r\nCache-Control:') == 1
                assert b'Cloudflare-Cdn-Cache-Control: no-store' in response
            body = json.loads(response.split(b'\r\n\r\n', 1)[1])
            assert body == {'observed_ip': '192.0.2.123', 'real_ip': '', 'forwarded': ''}
            assert b'200 OK' not in exchange(b''), 'Accepted request without PROXY metadata'
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
