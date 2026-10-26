"""The HTTP surface.

Every protected handler goes through Service.execute, which authorizes and commits the audit row
inside one transaction before the handler can produce a body. Images use the reader's bearer token;
native video uses a short-lived, view-scoped ticket with the same live session checks.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import re
import shutil

from fastapi import FastAPI, Request, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException

from .auth import Refusal, verify_google_identity
from .service import Service
from . import quiz, video, selfie

PRIVATE = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
           'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY'}


class UploadBoundary:
    """Authorize before multipart parsing and bound bytes even without Content-Length."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        match = re.fullmatch(r'/api/admin/posts/([^/]+)/(items|videos)/?', scope.get('path', ''))
        enrollment = scope.get('path', '').rstrip('/') == '/api/selfie'
        if scope['type'] != 'http' or scope.get('method') != 'POST' or not (match or enrollment):
            return await self.app(scope, receive, send)
        request = Request(scope)
        service = scope['app'].state.service
        limit = selfie.LIMIT if enrollment else service.config.video_upload_limit if match[2] == 'videos' else service.config.upload_limit
        try:
            if enrollment:
                # Bound the burst entering the writer. Release before receiving
                # bytes so a slow uploader cannot occupy an admission slot.
                async with service.selfie_admissions:
                    await service.execute(request, selfie=False, event_name=None)
                selfie.enabled(service)
            else:
                await service.execute(request, admin=True, post_id=match[1])
            if shutil.disk_usage(service.config.data_dir).free < service.config.free_floor + limit:
                raise Refusal(507, 'storage_low', 'Uploads are paused because the Mac is low on free space.')
            # Reserve a small, bounded allowance for the multipart envelope around one file.
            limit += 64 * 1024
            length = request.headers.get('content-length')
            if length and length.isdecimal() and int(length) > limit:
                raise Refusal(413, 'file_too_large', 'The upload exceeds its size limit.')
        except Refusal as error:
            return await JSONResponse({'error': error.code, 'message': error.message},
                                      status_code=error.status, headers=PRIVATE)(scope, receive, send)

        received, exceeded = 0, False

        async def bounded_receive():
            nonlocal received, exceeded
            message = await receive()
            if message['type'] == 'http.request':
                received += len(message.get('body', b''))
                if received > limit:
                    exceeded = True
                    # Starlette closes any spooled multipart files on this exception.
                    raise MultiPartException('Upload body exceeds its limit')
            return message

        async def bounded_send(message):
            if not exceeded:
                await send(message)
            elif message['type'] == 'http.response.start':
                # The multipart parser's 400 becomes the normal Postern size refusal.
                await JSONResponse({'error': 'file_too_large', 'message': 'The upload exceeds its size limit.'},
                                   status_code=413, headers=PRIVATE)(scope, receive, send)

        await self.app(scope, bounded_receive, bounded_send)


def timing_headers(context):
    timings = context.get('timings') or {}
    return {'X-Audit-Ms': f"{timings.get('audit_ms', 0):.3f}", 'X-Audit-Queue-Ms': f"{timings.get('queue_ms', 0):.3f}",
            'X-Audit-Commit-Ms': f"{timings.get('commit_ms', 0):.3f}"}


class Playback(BaseModel):
    ranges: list[tuple[float, float]] = Field(max_length=32)


class Credential(BaseModel):
    credential: str = Field(max_length=8192)


class DemoSignIn(BaseModel):
    email: str = Field(max_length=320)
    name: str = Field(default='', max_length=200)


class Acceptance(BaseModel):
    version: str = Field(max_length=40)
    ip_version: str = Field(default='', max_length=40)
    monitoring_accepted: StrictBool = False
    ip_accepted: StrictBool = False


class CategoryRename(BaseModel):
    category: str = Field(max_length=200)
    name: str = Field(min_length=1, max_length=200)


class PostRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class Draft(BaseModel):
    title: str = Field(max_length=400)
    body: str = Field(default='', max_length=2000)
    category: str = Field(default='', max_length=200)


class Attempt(BaseModel):
    attempt_id: str = Field(max_length=64)


class QuizAnswers(BaseModel):
    version: str = Field(max_length=64)
    answers: dict[str, str] = Field(max_length=quiz.LIMIT)


class CertificateName(BaseModel):
    name: str = Field(max_length=200)


def create_app(config, store=None, service=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.service = service if service is not None else Service(config, store=store)
        await app.state.service.start()
        try:
            yield
        finally:
            await app.state.service.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(UploadBoundary)
    app.add_middleware(CORSMiddleware, allow_origins=config.origins, allow_credentials=False,
                       allow_methods=['GET', 'POST'], allow_headers=['authorization', 'content-type', 'x-postern-client', 'x-postern-device'],
                       max_age=600)

    def current(request) -> Service:
        return request.app.state.service

    @app.exception_handler(Refusal)
    async def refused(request, error):
        return JSONResponse({'error': error.code, 'message': error.message}, status_code=error.status, headers=PRIVATE)

    @app.middleware('http')
    async def never_store(request, call_next):
        # Nothing this server returns may sit in a browser or CDN cache.
        response = await call_next(request)
        for header, value in PRIVATE.items():
            response.headers[header] = value
        # Google needs the public app's origin to initialize its sign-in button.
        # Keep the stricter policy on all API/media responses, including errors.
        if (config.static_dir and request.url.path != '/api' and not request.url.path.startswith('/api/')
                and response.headers.get('content-type', '').startswith('text/html')):
            response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        return response

    @app.get('/health')
    async def health(request: Request):
        return await current(request).health()

    @app.get('/api/config')
    async def configuration(request: Request):
        service = current(request)
        return {'mode': config.mode, 'google_client_id': config.google_client_id,
                'notice': config.notice, 'notice_version': config.notice_version,
                'ip_notice': config.branding.ip_notice, 'ip_notice_version': config.ip_notice_version,
                'branding': config.branding.public(),
                'selfie_enabled': config.selfie_enabled, 'selfie_notice_version': config.selfie_notice_version,
                'selfie_retention_days': config.selfie_retention_days, 'preview': config.preview,
                'status': await service.health()}

    @app.post('/api/auth/google')
    async def sign_in(request: Request, body: Credential):
        service = current(request)
        service.rate_limit('ip:'+service.context(request)['ip'])
        # Google's key fetch and signature check must not block video/progress.
        # Bound verification work and waiting independently from the shared-network budget.
        try:
            await asyncio.wait_for(service.signin_verifiers.acquire(), timeout=2)
        except asyncio.TimeoutError:
            raise Refusal(503, 'signin_busy', 'Sign-in is busy. Please try again shortly.')
        try:
            identity = await run_in_threadpool(verify_google_identity, body.credential, [config.google_client_id])
        finally:
            service.signin_verifiers.release()
        return await service.signin(identity, request, client=request.headers.get('x-postern-client', 'web'))

    @app.post('/api/auth/demo')
    async def sign_in_demo(request: Request, body: DemoSignIn):
        # Only ever reachable in the local demo mode, which has no Google credentials at all.
        if config.mode != 'demo':
            raise Refusal(404, 'not_found', 'This route is not available.')
        service = current(request)
        service.rate_limit('ip:'+service.context(request)['ip'])
        return await service.signin({'sub': 'demo-' + body.email, 'email': body.email, 'name': body.name}, request,
                                    client=request.headers.get('x-postern-client', 'web'))

    @app.get('/api/session')
    async def session(request: Request):
        def describe(db, context):
            if config.selfie_enabled and not selfie.browser(db, context):
                raise Refusal(401, 'sign_in_required', 'Please sign in again to set up selfie enrollment in this browser.')
            return {'email': context['email'], 'name': context['name'], 'role': context['role'],
                    'consent_required': not current(request).permissions.consent_current(db, context),
                    'notice': config.notice, 'notice_version': config.notice_version,
                    'selfie': selfie.status(db, context, config)}
        return await current(request).execute(request, describe, consent=False, selfie=False, event_name=None)

    @app.get('/api/selfie')
    async def selfie_status(request: Request):
        def describe(db, ctx):
            selfie.enabled(current(request))
            return selfie.status(db, ctx, config)
        return await current(request).execute(request, describe, selfie=False, event_name=None)

    @app.post('/api/selfie', status_code=202)
    async def selfie_capture(request: Request, file: UploadFile, consent_version: str = Form(max_length=40)):
        return await selfie.capture(current(request), request, file, consent_version)

    @app.get('/api/selfies/{identity}.jpg')
    async def selfie_image(request: Request, identity: str):
        return Response(await selfie.image(current(request), request, identity), media_type='image/jpeg', headers=PRIVATE)

    @app.get('/api/admin/selfies')
    async def selfie_history(request: Request):
        return await selfie.history(current(request), request)

    @app.post('/api/consent')
    async def consent(request: Request, body: Acceptance):
        return await current(request).consent(request, body.version, body.ip_version, body.monitoring_accepted, body.ip_accepted)

    @app.post('/api/auth/signout')
    async def sign_out(request: Request):
        await current(request).logout(request)
        return {'signed_out': True}

    @app.get('/api/posts')
    async def posts(request: Request):
        return await current(request).posts(request)

    @app.get('/api/quizzes/{chapter}')
    async def quiz(request: Request, chapter: int):
        return await current(request).quiz(request, chapter)

    @app.post('/api/quizzes/{chapter}/attempts')
    async def quiz_attempt(request: Request, chapter: int, body: QuizAnswers):
        return await current(request).submit_quiz(request, chapter, body.version, body.answers)

    @app.post('/api/certificate/name')
    async def certificate_name(request: Request, body: CertificateName):
        return await current(request).name_certificate(request, body.name)

    @app.post('/api/items/{item_id}/views')
    async def create_view(request: Request, item_id: str, body: Attempt):
        return await current(request).create_view(request, item_id, body.attempt_id)

    async def serve_page(request, view_id, page):
        service = current(request)
        body, context = await service.image(request, view_id=view_id, page=page)
        # The open is recorded only once the body has actually been handed to the client.
        return Response(body, media_type='image/jpeg', headers=timing_headers(context),
                        background=BackgroundTask(service.confirm_served, view_id, page, context))

    @app.get('/api/views/{view_id}/page/{page}.jpg')
    async def page(request: Request, view_id: str, page: int):
        return await serve_page(request, view_id, page)

    @app.post('/api/views/{view_id}/video')
    async def video_player(request: Request, view_id: str):
        return await video.prepare(current(request), request, view_id)

    @app.post('/api/views/{view_id}/video-progress')
    async def video_progress(request: Request, view_id: str, body: Playback):
        return await video.progress(current(request), request, view_id, body.ranges)

    @app.get('/api/video/{ticket}')
    async def video_content(request: Request, ticket: str):
        body, first, last, size = await video.content(current(request), request, ticket)
        return Response(body, status_code=206, media_type='video/mp4', headers={
            'Accept-Ranges': 'bytes', 'Content-Range': f'bytes {first}-{last}/{size}', **PRIVATE})

    @app.get('/api/views/{view_id}/photo.jpg')
    async def photo(request: Request, view_id: str):
        return await serve_page(request, view_id, 0)

    @app.get('/api/posts/{post_id}/thumb.jpg')
    async def thumbnail(request: Request, post_id: str):
        # Grid thumbnails are stamped too, and never count as an open.
        body, context = await current(request).image(request, post_id=post_id, thumbnail=True)
        return Response(body, media_type='image/jpeg', headers=timing_headers(context))

    @app.get('/api/admin/posts')
    async def admin_posts(request: Request):
        return await current(request).posts(request, admin=True)

    @app.post('/api/admin/categories/rename')
    async def rename_category(request: Request, body: CategoryRename):
        return await current(request).rename_category(request, body.category, body.name)

    @app.post('/api/admin/posts')
    async def create_post(request: Request, body: Draft):
        return await current(request).create_post(request, body.title, body.body, body.category)

    @app.post('/api/admin/posts/{post_id}/rename')
    async def rename_post(request: Request, post_id: str, body: PostRename):
        return await current(request).rename_post(request, post_id, body.title)

    @app.post('/api/admin/posts/{post_id}/items')
    async def attach(request: Request, post_id: str, file: UploadFile):
        return await current(request).attach(request, post_id, file)

    @app.post('/api/admin/posts/{post_id}/videos')
    async def attach_video(request: Request, post_id: str, file: UploadFile):
        return await current(request).attach(request, post_id, file, video_only=True)

    @app.post('/api/admin/posts/{post_id}/publish')
    async def publish(request: Request, post_id: str):
        # Publication never blocks on Google: the admin screen polls the status URL (ENG-D4).
        result = await current(request).publish(request, post_id)
        return JSONResponse({**result, 'status_url': f"/api/admin/operations/{result['operation_id']}"},
                            status_code=202, headers=PRIVATE)

    @app.get('/api/admin/operations/{operation_id}')
    async def operation(request: Request, operation_id: str):
        return await current(request).operation(request, operation_id)

    @app.get('/api/admin/signin-denials')
    async def denials(request: Request):
        return await current(request).denied_signins(request)

    @app.post('/api/admin/posts/{post_id}/archive')
    async def archive(request: Request, post_id: str):
        return await current(request).archive(request, post_id)

    @app.get('/api/admin/posts/{post_id}/report')
    async def report(request: Request, post_id: str):
        return await current(request).report(request, post_id)

    @app.get('/api/admin/progress')
    async def training_progress(request: Request):
        return await current(request).training_progress(request)

    @app.get('/api/admin/health')
    async def admin_health(request: Request):
        service = current(request)
        await service.execute(request, admin=True, event_name=None)
        return {**await service.health(private=True), 'export_error': service.export_error}

    if config.static_dir:
        root = Path(config.static_dir).expanduser().resolve()
        app.mount('/assets', StaticFiles(directory=root / 'assets'), name='assets')

        @app.get('/{path:path}')
        async def spa(path: str):
            # One page application: every unknown path is a client route, never a file probe.
            candidate = (root / path).resolve()
            if path and candidate.is_file() and root in candidate.parents:
                return FileResponse(candidate, headers=PRIVATE)
            return FileResponse(root / 'index.html', headers=PRIVATE)

    return app
