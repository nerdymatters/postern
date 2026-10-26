"""Synthetic FastAPI stamp/audit/IP/HLS probe. Loopback only; never production auth."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import hashlib
import io
import ipaddress
import json
import re
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageDraw, ImageFont

from .audit import AuditUnavailable, AuditWriter
from .cache import CompositeCache, SessionInvalidated


def observed_ip(request, trust_proxy):
    peer = request.client.host if request.client else 'unknown'
    value = request.headers.get('x-forwarded-for')
    if trust_proxy and peer in ('127.0.0.1', '::1') and value:
        try:
            return str(ipaddress.ip_address(value))  # A single overwritten IP, never a client-supplied chain.
        except ValueError:
            raise HTTPException(400, 'invalid proxy IP')
    return peer


@lru_cache(maxsize=64)
def stamp_overlay(size, reader, minute):
    # Reuse only the overlay per reader/minute, as approved; no composite-page cache.
    overlay = Image.new('RGBA', size)
    draw = ImageDraw.Draw(overlay)
    label = f'{reader} | {time.strftime("%Y-%m-%d %H:%M", time.gmtime(minute * 60))} UTC'
    font = ImageFont.load_default(size=12 if size[0] <= 400 else 32)
    for y in range(0, size[1], 100 if size[0] <= 400 else 180):
        draw.text((20, y), label, font=font, fill=(110, 10, 10, 110))
    return overlay.rotate(30)


def stamp(path, reader, thumbnail=False, minute=None):
    with Image.open(path) as source:
        page = source.convert('RGBA')
    if thumbnail:
        page.thumbnail((400, 600))
    overlay = stamp_overlay(page.size, reader, int(time.time() // 60) if minute is None else minute)
    output = io.BytesIO()
    Image.alpha_composite(page, overlay).convert('RGB').save(output, format='JPEG', quality=85)
    return output.getvalue()


def create_app(directory: Path, trust_proxy=False, composite_cache=False):
    directory = directory.resolve()
    credentials = json.loads((directory / 'credentials.json').read_text())

    @asynccontextmanager
    async def lifespan(app):
        app.state.writer = AuditWriter(directory / 'audit.sqlite3')
        app.state.cache = CompositeCache(directory / 'composites') if composite_cache else None

        async def sweep():
            while True:
                await asyncio.sleep(1)
                await asyncio.to_thread(app.state.cache.sweep)

        sweeper = asyncio.create_task(sweep()) if app.state.cache else None
        try:
            yield
        finally:
            try:
                if sweeper:
                    sweeper.cancel()
                    try:
                        await sweeper
                    except asyncio.CancelledError:
                        pass
                    finally:
                        await asyncio.to_thread(app.state.cache.close)
            finally:
                await asyncio.to_thread(app.state.writer.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def session_key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    async def revoke_reader(token):
        # Shared eviction hook for logout and operator revocation in the probe.
        credentials['readers'].pop(token, None)
        if app.state.cache:
            await asyncio.to_thread(app.state.cache.invalidate_session, session_key(token))

    app.state.revoke_reader = revoke_reader

    def authorize(request, operator=False):
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer '):
            raise HTTPException(401, 'Bearer credential required')
        token = header.removeprefix('Bearer ')
        if operator:
            if not hmac.compare_digest(token, credentials['operator']):
                raise HTTPException(401, 'harness operator credential required')
            return 'operator'
        reader = credentials['readers'].get(token)
        if reader is None:
            raise HTTPException(401, 'harness reader credential required')
        return reader

    async def receipt(request, route):
        reader = authorize(request)
        run_id = request.headers.get('x-day1-run', 'smoke')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', run_id):
            raise HTTPException(400, 'invalid run id')
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(
                app.state.writer.submit(run_id, reader, route)), timeout=1.5)
        except (AuditUnavailable, asyncio.TimeoutError):
            raise HTTPException(503, 'audit_unavailable')
        return reader, result

    @app.get('/health')
    def health():
        return {'mode': 'synthetic-day1-harness', 'production_ready': False}

    @app.get('/probe/ip')
    def client_ip(request: Request):
        return {'observed_ip': observed_ip(request, trust_proxy),
                'source': 'configured_loopback_proxy' if trust_proxy else 'connection',
                'forwarding_headers_trusted': trust_proxy}

    @app.get('/probe/config')
    def config(request: Request):
        authorize(request, operator=True)
        return {'sqlite': app.state.writer.settings, 'pages': len(list((directory / 'pages').glob('*.jpg'))),
                'mode': 'synthetic-day1-harness', 'trust_proxy': trust_proxy,
                'composite_cache': app.state.cache.snapshot() if app.state.cache else {'enabled': False}}

    @app.post('/probe/logout')
    async def logout(request: Request):
        authorize(request)
        await revoke_reader(request.headers['authorization'].removeprefix('Bearer '))
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})

    @app.post('/probe/revoke')
    async def revoke(request: Request):
        authorize(request, operator=True)
        payload = await request.json()
        token = payload.get('token') if isinstance(payload, dict) else None
        if not isinstance(token, str) or len(token) > 256:
            raise HTTPException(400, 'synthetic token required')
        if token in credentials['readers']:
            await revoke_reader(token)
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})

    @app.get('/probe/audit')
    async def audit(request: Request):
        _, timing = await receipt(request, 'audit')
        return Response(b'ok', headers={'Cache-Control': 'no-store',
                                        'X-Audit-Ms': str(timing['total_ms']),
                                        'X-Audit-Queue-Ms': str(timing['queue_ms']),
                                        'X-Audit-Commit-Ms': str(timing['commit_ms'])})

    async def render(request, page, thumbnail, item="synthetic-pdf"):
        path = directory / 'pages' / f'{page}.jpg'
        if page < 0 or not path.is_file():
            raise HTTPException(404, 'synthetic page absent')
        reader = authorize(request)
        cache_result = 'disabled'
        if app.state.cache:
            session = session_key(request.headers['authorization'].removeprefix('Bearer '))
            version = path.stat()
            asset = [item, page, thumbnail, version.st_mtime_ns, version.st_size]
            try:
                body, cache_result = await asyncio.to_thread(
                    app.state.cache.get, session, asset,
                    lambda minute: stamp(path, reader, thumbnail, minute))
            except SessionInvalidated:
                raise HTTPException(401, 'harness reader credential revoked')
        else:
            body = await asyncio.to_thread(stamp, path, reader, thumbnail)
        _, timing = await receipt(request, 'thumb' if thumbnail else 'page')
        authorize(request)  # Revocation during render/audit must not release cached pixels.
        return Response(body, media_type='image/jpeg', headers={
            'X-Composite-Cache': cache_result,
            'Cache-Control': 'no-store', 'X-Audit-Ms': str(timing['total_ms']),
            'X-Audit-Queue-Ms': str(timing['queue_ms']), 'X-Audit-Commit-Ms': str(timing['commit_ms'])})

    @app.get('/probe/pages/{page}.jpg')
    async def page(request: Request, page: int):
        return await render(request, page, False)

    @app.get('/probe/thumbs/{post}.jpg')
    async def thumb(request: Request, post: int):
        if not 0 <= post < 50:
            raise HTTPException(404, 'synthetic post absent')
        return await render(request, 0, True, item=f"synthetic-post-{post}")

    @app.get('/probe/hls/{filename}')
    async def hls(request: Request, filename: str):
        if not re.fullmatch(r'(index\.m3u8|seg_\d{3}\.ts)', filename):
            raise HTTPException(404, 'synthetic HLS file absent')
        path = directory / 'hls' / filename
        if not path.is_file():
            raise HTTPException(404, 'run prepare with --video first')
        # Synthetic HLS is public so three real phones can test native playback.
        # This does NOT implement the production media-cookie/authentication gate.
        return FileResponse(path, media_type='application/vnd.apple.mpegurl' if filename.endswith('.m3u8')
                            else 'video/mp2t', headers={'Cache-Control': 'no-store'})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8788)
    parser.add_argument('--trust-loopback-proxy', action='store_true',
                        help='Only behind the supplied Caddy proxy, which overwrites forwarded IPs')
    parser.add_argument('--composite-cache', action='store_true', help='Enable bounded private on-disk stamped composites')
    args = parser.parse_args()
    uvicorn.run(create_app(args.directory, args.trust_loopback_proxy, args.composite_cache), host='127.0.0.1',
                port=args.port, proxy_headers=False, access_log=False)


if __name__ == '__main__':
    main()
