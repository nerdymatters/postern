"""Run the actual service entry point: TestClient bypasses Uvicorn's header middleware."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

from test_ops import write_config

# Regression: ISSUE-001 — Uvicorn bypasses the application's proxy trust policy.
# Found by /qa on 2026-11-17.
# Report: .context/linux-host-20261117/qa/report.md
# Value: protects=real service honors explicit loopback trust and rejects malformed addresses;
# fails_when=Uvicorn rewrites request.client before Service.context;
# why_new=test_signin exercises ASGI directly, without the installed server entry point;
# seam=none; no_claim=public tunnel behavior (loopback HTTP and synthetic demo identity only).


@contextmanager
def running(tmp_path, trusted):
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    path = write_config(tmp_path, port=port, trust_loopback_proxy=trusted, converter_workers=1)
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ, POSTERN_PROGRESS_BACKEND='python', PYTHONPATH=str(root))
    with (tmp_path / 'server.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-P', '-m', 'postern.serve', 'run', '--config', str(path)],
                                   cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=2, trust_env=False) as client:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    assert process.poll() is None, (tmp_path / 'server.log').read_text()
                    try:
                        if client.get('/health').status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(.05)
                else:
                    pytest.fail('Owned service did not become ready within 20 seconds')
                yield client
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            assert process.poll() is not None


@pytest.mark.parametrize('trusted', [False, True])
def test_real_server_honors_explicit_proxy_trust(tmp_path, trusted):
    with running(tmp_path, trusted) as client:
        valid = client.post('/api/auth/demo', json={'email': 'reader@example.invalid'},
                            headers={'X-Forwarded-For': '203.0.113.7', 'X-Real-IP': '198.51.100.8'})
        assert valid.status_code == 200
        assert valid.json()['ip'] == ('203.0.113.7' if trusted else '127.0.0.1')
        for forwarded in (None, 'not an address', '203.0.113.7, 10.0.0.1'):
            headers = {} if forwarded is None else {'X-Forwarded-For': forwarded}
            response = client.post('/api/auth/demo', json={'email': 'reader@example.invalid'}, headers=headers)
            if trusted:
                assert response.status_code == 400
                assert response.json()['error'] == 'invalid_proxy'
            else:
                assert response.status_code == 200 and response.json()['ip'] == '127.0.0.1'
        with sqlite3.connect((tmp_path / 'data/postern.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
            addresses = [json.loads(row[0])['ip'] for row in db.execute("SELECT payload FROM events WHERE type='signin_ok'")]
            sessions = db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        assert addresses == (['203.0.113.7'] if trusted else ['127.0.0.1'] * 4)
        assert sessions == len(addresses)
