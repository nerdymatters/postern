"""Google receives the public app's origin; protected API responses disclose no referrer."""
from fastapi.testclient import TestClient

from postern.app import create_app
from postern.service import Service


def test_hosted_spa_policy_supports_google_without_relaxing_api_policy(config, store, tmp_path):
    root = tmp_path / 'web'
    (root / 'assets').mkdir(parents=True)
    (root / 'index.html').write_text('<!doctype html><title>Postern</title>')
    (root / 'assets' / 'app.js').write_text('console.log("Postern")')
    config.static_dir = str(root)
    service = Service(config, store=store)
    with TestClient(create_app(config, service=service)) as client:
        for path in ('/', '/index.html', '/admin', '/post/example'):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['referrer-policy'] == 'strict-origin-when-cross-origin', path
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['x-frame-options'] == 'DENY'
        for path in ('/api', '/api/config', '/api/session', '/api/posts', '/api/video/invalid', '/api/missing', '/assets/app.js'):
            response = client.get(path)
            assert response.headers['referrer-policy'] == 'no-referrer', path
