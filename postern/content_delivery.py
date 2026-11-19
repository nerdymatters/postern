"""Opt-in native content delivery over the existing PostgreSQL authority.

Objects and presentations are prepared offline. Runtime receives a read-only
object capability, an explicit policy adapter, and the restricted SpaceService.
There is no original-file, export, upload, or arbitrary object-key endpoint.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import re
import secrets
import time
from typing import Protocol

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from psycopg.types.json import Jsonb

from .content_objects import ContentObject, ContentObjectError, ContentObjectReader
from .spaces import SpaceError, identifier, invalid_session


PRIVATE = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
           'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY'}


def unavailable():
    return SpaceError(503, 'content_unavailable', 'This content is temporarily unavailable.')


def absent():
    return SpaceError(404, 'not_found', 'This resource is not available.')


class ContentPolicy(Protocol):
    async def authorize(self, request: Request, snapshot: dict) -> None: ...


class DenyContent:
    async def authorize(self, request, snapshot):
        raise absent()


class NativeReadingPolicy:
    """Explicit opt-in for native spaces with no consent/enrollment prerequisites.

    This is not a bypass for a legacy/training installation. Such an installation
    needs its own policy adapter, independent identity binding, and live gates.
    The database still checks membership, view permission, publication and audit.
    """
    def __init__(self, space_ids):
        self.spaces = frozenset(identifier(value) for value in space_ids)

    async def authorize(self, request, snapshot):
        if (snapshot['context']['space_id'] not in self.spaces
                or snapshot['manifest']['kind'] not in {'document', 'video'}):
            raise absent()


@dataclass(frozen=True)
class _Ticket:
    session_hash: str
    space: str
    resource: str
    revision: int
    context: dict
    expires: float


class ContentService:
    def __init__(self, spaces, objects: ContentObjectReader, *, policy: ContentPolicy | None = None,
                 renderer=None):
        self.spaces, self.objects = spaces, objects
        self.policy = policy if policy is not None else DenyContent()
        if renderer is None:
            from .content_render import render_page
            renderer = render_page
        self.renderer = renderer
        self._reads = asyncio.Semaphore(4)
        self._pending: set[asyncio.Task] = set()
        self._tickets: dict[str, _Ticket] = {}

    async def close(self):
        self._tickets.clear()
        # A cancelled request cannot release a read slot while its bounded local
        # IO still runs. Tasks own their slots until the underlying read ends.
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        close = getattr(self.objects, 'close', None)
        if close is not None:
            close()

    async def _read(self, descriptor):
        try:
            await asyncio.wait_for(self._reads.acquire(), timeout=1)
        except TimeoutError:
            raise unavailable() from None
        task = asyncio.create_task(asyncio.to_thread(self.objects.read, descriptor))
        self._pending.add(task)
        def finished(task):
            self._pending.discard(task)
            self._reads.release()
            if not task.cancelled():
                task.exception()  # Consume failures after an HTTP timeout.
        task.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=2)
        except (ContentObjectError, OSError, TimeoutError):
            raise unavailable() from None

    def _params(self, hashed, space, resource, revision):
        try:
            identifier(space); identifier(resource)
            if type(revision) is not int or not 1 <= revision <= 2**63 - 1:
                raise ValueError('revision')
        except (ValueError, TypeError):
            raise absent() from None
        return (hashed, space, self.spaces.trust.audience, resource, revision)

    async def manifest(self, request, hashed, space, resource, revision, kind):
        params = self._params(hashed, space, resource, revision)
        first = await self.spaces.call('resource_content_manifest', params)
        if first['manifest']['kind'] != kind:
            raise absent()
        await self.policy.authorize(request, first)
        final = await self.spaces.call('resource_content_manifest', params)
        if final != first:
            raise absent()
        await self.policy.authorize(request, final)
        return final

    async def prepare(self, request, hashed, space, resource, revision, representation, index,
                      *, context=None):
        if (representation not in {'page', 'thumbnail', 'video'}
                or type(index) is not int or not 0 <= index <= 2**31 - 1):
            raise absent()
        params = (*self._params(hashed, space, resource, revision), representation, index)
        snapshot = await self.spaces.call('resource_content_admit', params)
        if context is not None and snapshot['context'] != context:
            raise absent()
        await self.policy.authorize(request, snapshot)
        try:
            descriptor = ContentObject(**snapshot['object'])
            # Also distrust a buggy adapter/schema response at the capability boundary.
            if (descriptor.space_id, descriptor.resource_id, descriptor.revision,
                    descriptor.representation, descriptor.index) != (space, resource, revision, representation, index):
                raise ContentObjectError('descriptor scope')
            data = await self._read(descriptor)
            if representation in {'page', 'thumbnail'}:
                data = await self.renderer(data, snapshot['context']['principal_id'],
                                           int(time.time()) // 60, thumbnail=representation == 'thumbnail')
        except (ContentObjectError, OSError, ValueError):
            raise unavailable() from None
        # Rendering/IO never holds policy locks. Recheck all gates and commit the
        # required audit before a response or the next video chunk can escape.
        await self.policy.authorize(request, snapshot)
        final = await self.spaces.call('resource_content_disclose', (*params, Jsonb(snapshot)))
        if final != snapshot:
            raise absent()
        return data, final

    def issue_ticket(self, hashed, space, resource, revision, snapshot):
        now = time.monotonic()
        self._tickets = {key: value for key, value in self._tickets.items() if value.expires > now}
        if len(self._tickets) >= 1024:
            raise unavailable()
        token = 'pc_' + secrets.token_urlsafe(32)
        self._tickets[hashlib.sha256(token.encode()).hexdigest()] = _Ticket(
            hashed, space, resource, revision, snapshot['context'], now + 30)
        return token

    def credential(self, request, space, resource, revision):
        token = request.query_params.get('ticket')
        if token is None:
            return self.spaces.token_hash(request), None
        if (request.headers.get('authorization') or len(request.query_params.getlist('ticket')) != 1
                or not re.fullmatch(r'pc_[A-Za-z0-9_-]{43}', token)):
            raise invalid_session()
        ticket = self._tickets.get(hashlib.sha256(token.encode()).hexdigest())
        if (ticket is None or ticket.expires <= time.monotonic()
                or (ticket.space, ticket.resource, ticket.revision) != (space, resource, revision)):
            raise invalid_session()
        # This is merely a reference to a bearer session, never cached permission.
        return ticket.session_hash, ticket.context


def _range(header, total):
    if header is None:
        return 0, total - 1, 200
    match = re.fullmatch(r'bytes=(\d{0,20})-(\d{0,20})', header)
    if not match or not any(match.groups()):
        raise SpaceError(416, 'invalid_range', 'A single byte range is required.')
    first, last = match.groups()
    if not first:
        first, last = max(0, total - int(last)), total - 1
    else:
        first, last = int(first), min(total - 1, int(last)) if last else total - 1
    if first > last or first >= total:
        raise SpaceError(416, 'invalid_range', 'That byte range is not available.')
    return first, last, 206


def install(app, service):
    router = APIRouter(prefix='/v1/spaces/{space}/resources/{resource}')

    def no_conditionals(request):
        if any(name in request.headers for name in ('if-range', 'if-none-match', 'if-modified-since')):
            raise SpaceError(400, 'conditional_content', 'Open a fresh content request.')

    async def image(request, space, resource, revision, representation, index):
        no_conditionals(request)
        if 'range' in request.headers:
            raise SpaceError(416, 'invalid_range', 'Images require a complete request.')
        hashed = service.spaces.token_hash(request)
        data, _ = await service.prepare(request, hashed, space, resource, revision, representation, index)
        async def pixels():
            for offset in range(0, len(data), 64 * 1024):
                yield data[offset:offset + 64 * 1024]
        return StreamingResponse(pixels(), media_type='image/jpeg',
                                 headers={**PRIVATE, 'Content-Length': str(len(data))})

    @router.get('/document')
    async def document(request: Request, space: str, resource: str, revision: int = Query(ge=1, le=2**63-1)):
        result = await service.manifest(request, service.spaces.token_hash(request), space, resource, revision, 'document')
        return JSONResponse({'resource_id': resource, 'revision': revision, 'page_count': result['manifest']['metadata']['page_count']}, headers=PRIVATE)

    @router.get('/document/pages/{page}.jpg')
    async def page(request: Request, space: str, resource: str, page: int, revision: int = Query(ge=1, le=2**63-1)):
        return await image(request, space, resource, revision, 'page', page)

    @router.get('/thumbnails/{index}.jpg')
    async def thumbnail(request: Request, space: str, resource: str, index: int, revision: int = Query(ge=1, le=2**63-1)):
        return await image(request, space, resource, revision, 'thumbnail', index)

    @router.get('/video')
    async def video(request: Request, space: str, resource: str, revision: int = Query(ge=1, le=2**63-1)):
        result = await service.manifest(request, service.spaces.token_hash(request), space, resource, revision, 'video')
        return JSONResponse({'resource_id': resource, 'revision': revision,
                             'duration_ms': result['manifest']['metadata']['duration_ms'],
                             'viewer_label': result['context']['principal_id']}, headers=PRIVATE)

    @router.post('/video/ticket')
    async def ticket(request: Request, space: str, resource: str, revision: int = Query(ge=1, le=2**63-1)):
        hashed = service.spaces.token_hash(request)
        result = await service.manifest(request, hashed, space, resource, revision, 'video')
        return JSONResponse({'ticket': service.issue_ticket(hashed, space, resource, revision, result),
                             'expires_in_seconds': 30}, headers=PRIVATE)

    @router.get('/video/content.mp4')
    async def video_bytes(request: Request, space: str, resource: str, revision: int = Query(ge=1, le=2**63-1)):
        no_conditionals(request)
        hashed, ticket_context = service.credential(request, space, resource, revision)
        snapshot = await service.manifest(request, hashed, space, resource, revision, 'video')
        if ticket_context is not None and snapshot['context'] != ticket_context:
            raise invalid_session()
        meta = snapshot['manifest']['metadata']
        first, last, status = _range(request.headers.get('range'), meta['total_bytes'])
        chunk_bytes = meta['chunk_bytes']
        start, end = first // chunk_bytes, last // chunk_bytes
        data, _ = await service.prepare(request, hashed, space, resource, revision, 'video', start, context=snapshot['context'])
        service.credential(request, space, resource, revision)
        async def stream():
            for index in range(start, end + 1):
                if index == start:
                    chunk = data
                else:
                    # Re-resolve expiring media tickets as well as every authority gate.
                    service.credential(request, space, resource, revision)
                    chunk, _ = await service.prepare(request, hashed, space, resource, revision, 'video', index, context=snapshot['context'])
                low = first % chunk_bytes if index == start else 0
                high = last % chunk_bytes + 1 if index == end else len(chunk)
                # Small sends cap bytes admitted into the transport after a check.
                for offset in range(low, high, 64 * 1024):
                    service.credential(request, space, resource, revision)
                    yield chunk[offset:min(high, offset + 64 * 1024)]
        headers = {**PRIVATE, 'Accept-Ranges': 'bytes', 'Content-Length': str(last-first+1)}
        if status == 206:
            headers['Content-Range'] = f'bytes {first}-{last}/{meta["total_bytes"]}'
        return StreamingResponse(stream(), status_code=status, media_type='video/mp4', headers=headers)

    app.include_router(router)


class ContentDeliveryDeadline:
    """Wrap the actual ASGI send, outside BaseHTTPMiddleware's internal stream.

    No request can prepare indefinitely or continue emitting for >10 seconds.
    A slow/disconnected socket cancels producers; an incomplete body must not be
    turned into a successful empty tail. The server closes the connection.
    """
    def __init__(self, app, *, prepare_seconds=30, emit_seconds=10, send_seconds=2):
        self.app = app
        self.prepare_seconds, self.emit_seconds, self.send_seconds = prepare_seconds, emit_seconds, send_seconds

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope['type'] != 'http' or not content_path(path):
            return await self.app(scope, receive, send)
        started = None
        deadline = None
        expected, emitted = None, 0
        async def bounded_send(message):
            nonlocal started, expected, emitted
            if message['type'] == 'http.response.start':
                started = time.monotonic()
                deadline.reschedule(asyncio.get_running_loop().time() + self.emit_seconds)
                length = next((v for k, v in message.get('headers', []) if k.lower() == b'content-length'), None)
                expected = int(length) if length is not None else None
            if message['type'] == 'http.response.body':
                emitted += len(message.get('body', b''))
                if (expected is not None and (emitted > expected
                        or (not message.get('more_body', False) and emitted != expected))):
                    # BaseHTTPMiddleware can synthesize an empty final message
                    # when its inner generator failed. Never relay that as success.
                    raise RuntimeError('incomplete content response')
            remaining = self.emit_seconds if started is None else self.emit_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError('content emission deadline')
            await asyncio.wait_for(send(message), timeout=min(self.send_seconds, remaining))
        try:
            async with asyncio.timeout(self.prepare_seconds) as deadline:
                await self.app(scope, receive, bounded_send)
        except TimeoutError:
            if started is not None:
                raise  # Headers/body escaped: abort instead of forging completion.
            async with asyncio.timeout(self.send_seconds):
                await JSONResponse({'error': 'content_unavailable', 'message': 'This content is temporarily unavailable.'},
                                   status_code=503, headers=PRIVATE)(scope, receive, send)


def content_path(path):
    return re.fullmatch(r'/v1/spaces/[^/]+/resources/[^/]+/(?:document(?:/.*)?|thumbnails/.*|video(?:/.*)?|revisions/[^/]+/training(?:/.*)?)', path) is not None


class ContentRequestBoundary:
    """Bound native JSON before framework parsing, including chunked requests."""
    LIMIT = 32 * 1024

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST' or not content_path(scope.get('path', '')):
            return await self.app(scope, receive, send)
        async def refuse():
            await JSONResponse({'error': 'request_too_large', 'message': 'This request exceeds its size limit.'},
                               status_code=413, headers=PRIVATE)(scope, receive, send)
        length = next((value for name, value in scope.get('headers', []) if name.lower() == b'content-length'), b'')
        if length and (not length.isdigit() or len(length) > 10 or int(length) > self.LIMIT):
            return await refuse()
        total, exceeded, refused = 0, False, False
        async def bounded_receive():
            nonlocal total, exceeded
            message = await receive()
            if message['type'] == 'http.request':
                total += len(message.get('body', b''))
                if total > self.LIMIT:
                    exceeded = True
                    raise ValueError('native request exceeds size limit')
            return message
        async def bounded_send(message):
            nonlocal refused
            if exceeded:
                if not refused:
                    refused = True
                    await refuse()
            else:
                await send(message)
        await self.app(scope, bounded_receive, bounded_send)
