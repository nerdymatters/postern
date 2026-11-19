"""Explicit dual-authority bridge; never a general proxy into a legacy installation.

PostgreSQL holds authority locks while the existing SQLite training transaction runs.
A durable admission audit precedes mutations. The SQLite effect and retry result commit
together: an uncertain PostgreSQL commit can be retried without another quiz attempt.
There is deliberately no claim of a distributed atomic commit between the two stores.
"""
from __future__ import annotations

import asyncio
import anyio
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
import time
from types import MappingProxyType
from typing import Annotated, Mapping

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout, TooManyRequests

from . import exam, honors, quiz
from .auth import Refusal
from .db import AuditUnavailable, event
from .service import Service
from .spaces import SpaceError, identifier, invalid_session


@dataclass(frozen=True)
class LegacyTrainingSource:
    installation: str
    service: Service

    def __post_init__(self):
        identifier(self.installation)
        if not isinstance(self.service, Service):
            raise TypeError('an explicitly registered legacy Service is required')


def _missing():
    return SpaceError(404, 'not_found', 'This training is not available.')


def _locked():
    # A legacy refusal names the blocking chapter, which might be hidden here.
    return Refusal(423, 'chapter_locked', 'Required training is not available or not yet complete.')


def _legacy_request(request):
    value = request.headers.get('x-postern-legacy-authorization', '')
    if not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43}', value):
        raise Refusal(401, 'legacy_session_required', 'Sign in to this training installation.')
    scope = dict(request.scope)
    scope['headers'] = [(k, v) for k, v in scope.get('headers', [])
                        if k.lower() not in (b'authorization', b'x-postern-legacy-authorization')]
    scope['headers'].append((b'authorization', value.encode('ascii')))
    return Request(scope)


class _BoundService:
    """Reuse Service methods while checking identity inside each SQLite transaction.

    Only the methods called below use this facade. It exposes no arbitrary HTTP
    dispatch. The final mutation adds its replay journal in the *same* transaction.
    """
    def __init__(self, source, snapshot, operation, digest):
        self.source, self.snapshot = source, snapshot
        self.operation, self.digest = operation, digest

    def __getattr__(self, name):
        if name in ('permissions', 'final_exam', 'config', 'db', 'read_progress'):
            return getattr(self.source.service, name)
        raise AttributeError(name)

    async def completed(self, request):
        return await Service.completed(self, request)

    async def execute(self, request, operation=None, *, event_name='api_access', **policy):
        service, snap = self.source.service, self.snapshot
        token, request_context = service.bearer(request), service.context(request)

        def transaction(db):
            ctx = service.permissions.authorize_content_request(db, token, request_context=request_context, **policy)
            if ctx['email'] != snap['legacy_email']:
                raise _missing()
            honors.history_ready(db, ctx['email'])
            if operation is None:
                return ctx
            if snap['mode'] == 'quiz':
                if snap['chapter'] == exam.KEY:
                    exam.available(db, ctx['email'], ctx['role'])
                else:
                    quiz.available(db, service.permissions.quizzes, ctx, snap['chapter'])
            elif snap['mode'] == 'name' and honors.awarded(db, ctx['email']) is None:
                raise Refusal(404, 'certificate_not_found', 'No certificate is available in this training history.')
            if self.operation:
                prior = db.execute('SELECT digest,result FROM space_training_operations '
                    'WHERE space_id=? AND principal_id=? AND operation_id=?',
                    (snap['context']['space_id'], snap['context']['principal_id'], self.operation)).fetchone()
                if prior:
                    if prior['digest'] != self.digest:
                        raise SpaceError(409, 'operation_conflict', 'Use the original request for this operation.')
                    return json.loads(prior['result'])
                # JSONL/summary recovery restores events, not this adapter's
                # response journal. Its durable tombstone must forbid rerunning
                # a known committed effect whose response was not recovered.
                recovered = db.execute("SELECT 1 FROM events WHERE type='space_training_operation' AND json_valid(payload) "
                    "AND email=? AND json_extract(payload,'$.detail.space_id')=? "
                    "AND json_extract(payload,'$.detail.principal_id')=? "
                    "AND json_extract(payload,'$.detail.operation_id')=? LIMIT 1",
                    (ctx['email'], snap['context']['space_id'], snap['context']['principal_id'], self.operation)).fetchone()
                if recovered:
                    raise SpaceError(409, 'operation_recovery_required',
                                     'This operation was already recorded. Its response needs recovery; it will not be repeated.')
            result = operation(db, ctx)
            if event_name:
                event(db, event_name, ctx, summary=False)
            if self.operation:
                event(db, 'space_training_operation', ctx, detail={'space_id': snap['context']['space_id'],
                    'principal_id': snap['context']['principal_id'], 'operation_id': self.operation,
                    'resource_id': snap['resource_id'], 'digest': self.digest}, summary=True)
                db.execute('INSERT INTO space_training_operations VALUES (?,?,?,?,?)',
                    (snap['context']['space_id'], snap['context']['principal_id'], self.operation,
                     self.digest, json.dumps(result, ensure_ascii=False, separators=(',', ':'))))
            return result

        # Service.write's response deadline can expire after the worker has started.
        # Drain that actual transaction, also on client cancellation, before releasing
        # the native authority locks; a queued effect must never outlive its lease.
        def bounded_transaction(db):
            deadline = time.monotonic() + 1.25
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                result = transaction(db)
                if time.monotonic() >= deadline:
                    raise AuditUnavailable('Training transaction deadline')
                return result
            finally:
                db.set_progress_handler(None, 0)

        worker = service.db.submit(bounded_transaction)
        task = asyncio.wrap_future(worker)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The writer calls set_running_or_notify_cancel before BEGIN. Cancel
            # queued work immediately; never wait for unrelated preceding jobs
            # while retaining the native authority lease.
            worker.cancel()
            # ASGI/AnyIO cancellation is level-triggered: asyncio.shield alone
            # immediately raises again at the next await in a cancelled scope.
            with anyio.CancelScope(shield=True):
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                if task.done() and not task.cancelled():
                    task.exception()  # Drain a failed worker as well as success.
            raise


class SpaceTraining:
    def __init__(self, spaces, registrations: Mapping[str, LegacyTrainingSource]):
        self.spaces = spaces
        checked = {identifier(space): source for space, source in registrations.items()}
        if not all(isinstance(source, LegacyTrainingSource) for source in checked.values()):
            raise TypeError('explicit training registrations required')
        if len({source.installation for source in checked.values()}) != len(checked):
            raise ValueError('one legacy installation cannot serve two spaces')
        if len({source.service.config.data_dir.resolve() for source in checked.values()}) != len(checked):
            raise ValueError('one legacy database cannot serve two spaces')
        self.sources = MappingProxyType(checked)

    async def start(self):
        # Services are owned/started by the caller; registering one does not change
        # its auth, roster, enrollment, consent, quiz bank, or backup policy.
        for source in self.sources.values():
            def prepare(db):
                db.execute('''CREATE TABLE IF NOT EXISTS space_training_operations (
                    space_id TEXT NOT NULL, principal_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    digest TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(space_id,principal_id,operation_id))''')
                db.execute("CREATE INDEX IF NOT EXISTS space_training_operation_lookup ON events "
                    "(email,json_extract(payload,'$.detail.space_id'),json_extract(payload,'$.detail.principal_id'),"
                    "json_extract(payload,'$.detail.operation_id')) WHERE type='space_training_operation' AND json_valid(payload)")
            await source.service.write(prepare)

    async def _admit(self, conn, args):
        return (await (await conn.execute('SELECT postern.resource_training_admit(%s,%s,%s,%s,%s,%s,%s)', args)).fetchone())[0]

    @staticmethod
    def _sanitize(result, snap):
        """Convert reachable legacy item references, and hide unrelated training."""
        result = dict(result)
        allowed, items = set(snap['reachable_chapters']), snap['reachable_items']

        def standing(value):
            value = dict(value)
            value['locked'] = value.pop('locked_by', None) is not None
            unfinished = value.get('unfinished', [])
            value['unfinished'] = [items[item] for item in unfinished if item in items]
            value['unavailable_requirements'] = any(item not in items for item in unfinished)
            return value

        if 'standing' in result:
            result['standing'] = standing(result['standing'])
        if 'unfinished' in result:
            unfinished = result['unfinished']
            result['unfinished'] = [items[item] for item in unfinished if item in items]
            result['unavailable_requirements'] = any(item not in items for item in unfinished)
        if 'quizzes' in result:
            result['quizzes'] = [standing(value) for value in result['quizzes'] if value['chapter'] in allowed]
        if 'final_exam' in result:
            result['final_exam'] = standing(result['final_exam']) if result['final_exam'] and 0 in allowed else None
        if result.get('next_chapter') and result['next_chapter']['chapter'] not in allowed:
            result['next_chapter'] = None
        return result

    async def run(self, request, space, resource, revision, action, *, operation_id=None, payload=None):
        try:
            space, resource = identifier(space), identifier(resource)
            if operation_id is not None:
                operation_id = identifier(operation_id)
        except ValueError:
            raise _missing() from None
        if type(revision) is not int or not 1 <= revision <= 2**63-1 or action not in ('progress', 'quiz', 'submit', 'certificate', 'name'):
            raise _missing()
        mutation = action in ('submit', 'name')
        if mutation and operation_id is None:
            raise SpaceError(422, 'operation_required', 'A request operation ID is required.')
        if not mutation and operation_id is not None:
            raise SpaceError(422, 'invalid_operation', 'This read does not accept an operation ID.')
        source = self.sources.get(space)
        if source is None:
            raise _missing()
        legacy = _legacy_request(request)
        mode = 'quiz' if action in ('quiz', 'submit') else 'name' if action == 'name' else 'history'
        args = (self.spaces.token_hash(request), space, self.spaces.trust.audience, resource, revision, mode, operation_id)
        digest = hashlib.sha256(json.dumps([space, resource, revision, action, payload],
            sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        try:
            if mutation:
                # Committed intent survives an uncertain final audit commit.
                async with self.spaces.pool.connection() as conn:
                    await self._admit(conn, args)
            async with asyncio.timeout(10), self.spaces.pool.connection() as conn:
                snap = await self._admit(conn, args)
                if snap['installation'] != source.installation:
                    raise _missing()
                bound = _BoundService(source, snap, operation_id, digest)
                chapter = snap['chapter']
                if action == 'quiz':
                    result = await Service.quiz(bound, legacy, chapter)
                elif action == 'submit':
                    result = await Service.submit_quiz(bound, legacy, chapter, payload['version'], payload['answers'])
                elif action == 'name':
                    result = await Service.name_certificate(bound, legacy, payload['name'])
                else:
                    if action == 'certificate' and chapter != 0:
                        raise _missing()
                    def own(db, ctx):
                        if action == 'certificate':
                            certificate = honors.certificate(db, ctx['email'], prefix=source.service.config.branding.certificate_prefix)
                            if certificate is None and not snap['current_view_allowed']:
                                raise _missing()
                            return {'certificate': certificate}
                        standing = quiz.records(db, ctx['email']).get((ctx['email'], chapter))
                        if standing is None and not snap['current_view_allowed']:
                            raise _missing()
                        return {'chapter': chapter, 'standing': standing}
                    result = await bound.execute(legacy, own, event_name='training_own_history_viewed')
                # Independently recheck legacy session/roster/consent/enrollment
                # after a slow progress read, with no mutation/replay side effect.
                await bound.execute(legacy, event_name=None)
                await conn.execute('SELECT postern.resource_training_disclose(%s,%s,%s,%s,%s,%s,%s,%s)', (*args, Jsonb(snap)))
            # Both commits precede any response. Native binding details never leave.
            return self._sanitize(result, snap) if action in ('quiz', 'submit') else result
        except Refusal as exc:
            if exc.code == quiz.LOCKED:
                raise _locked() from None
            raise
        except psycopg.Error as exc:
            if exc.sqlstate == '28000':
                raise invalid_session() from None
            if exc.sqlstate == '42501':
                raise _missing() from None
            raise SpaceError(503, 'training_unavailable', 'Training is temporarily unavailable. Retry the same operation ID.') from None
        except (PoolTimeout, TooManyRequests):
            raise SpaceError(503, 'training_unavailable', 'Training is temporarily unavailable.') from None
        except (TimeoutError, AuditUnavailable, sqlite3.Error):
            raise SpaceError(503, 'training_unavailable', 'Training is temporarily unavailable. Retry the same operation ID.') from None


class TrainingSubmission(BaseModel):
    operation_id: str = Field(max_length=36)
    version: str = Field(max_length=64)
    answers: dict[Annotated[str, Field(min_length=1, max_length=256)],
                  Annotated[str, Field(min_length=1, max_length=128)]] = Field(max_length=quiz.LIMIT)


class TrainingName(BaseModel):
    operation_id: str = Field(max_length=36)
    name: str = Field(max_length=200)


def install(app, training: SpaceTraining):
    router = APIRouter(prefix='/v1/spaces/{space}/resources/{resource}/revisions/{revision}/training')

    @router.get('/progress')
    async def progress(request: Request, space: str, resource: str, revision: int):
        return await training.run(request, space, resource, revision, 'progress')

    @router.get('/quiz')
    async def open_quiz(request: Request, space: str, resource: str, revision: int):
        return await training.run(request, space, resource, revision, 'quiz')

    @router.post('/quiz')
    async def submit(request: Request, space: str, resource: str, revision: int, body: TrainingSubmission):
        return await training.run(request, space, resource, revision, 'submit', operation_id=body.operation_id,
                                  payload=body.model_dump(exclude={'operation_id'}))

    @router.get('/certificate')
    async def certificate(request: Request, space: str, resource: str, revision: int):
        return await training.run(request, space, resource, revision, 'certificate')

    @router.post('/certificate/name')
    async def name(request: Request, space: str, resource: str, revision: int, body: TrainingName):
        return await training.run(request, space, resource, revision, 'name', operation_id=body.operation_id,
                                  payload=body.model_dump(exclude={'operation_id'}))

    app.include_router(router)
