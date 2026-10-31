"""Opt-in PostgreSQL metadata API. No legacy enrollment, media or object access.

The resource login can execute only scoped functions. Each function checks live
account/session/membership state and writes required audit in the same transaction.
The pool commits before a result leaves this module. All identity GUCs are LOCAL.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import base64
import hmac
import json
import re
import secrets
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, PoolTimeout, TooManyRequests
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse


class SpaceError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def invalid_session():
    return SpaceError(401, 'space_session_required', 'Please open this space again.')


def identifier(value):
    if not isinstance(value, str):
        raise ValueError('UUID required')
    if str(uuid.UUID(value)) != value:
        raise ValueError('canonical UUID required')
    return value


@dataclass(frozen=True)
class SpaceTrust:
    issuer: str
    audience: str
    kid: str
    public_key: str
    cursor_key: bytes

    def __post_init__(self):
        if not all((self.issuer, self.audience, self.kid)) or len(self.cursor_key) < 32:
            raise ValueError('explicit trust configuration and 256-bit cursor key required')
        # Parse at construction, never retrieve a key from an assertion header/URL.
        key = jwt.algorithms.get_default_algorithms()['EdDSA'].prepare_key(self.public_key)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError('resource service requires an Ed25519 public key')


class SpaceService:
    def __init__(self, connection: dict, trust: SpaceTrust, *, max_size=4, invitation_key: bytes | None = None):
        if not all(connection.get(k) for k in ('host', 'port', 'user', 'dbname')):
            raise ValueError('explicit PostgreSQL connection required')
        self.trust = trust
        if invitation_key is not None and (not isinstance(invitation_key, bytes) or len(invitation_key) < 32):
            raise ValueError('256-bit invitation key required')
        self.invitation_key = invitation_key
        self.pool = AsyncConnectionPool(open=False, min_size=1, max_size=max_size,
            timeout=3, max_waiting=16, kwargs={**connection, 'connect_timeout': 3,
            'options': '-c statement_timeout=5000 -c lock_timeout=3000', 'autocommit': False})

    async def start(self):
        await self.pool.open(wait=True, timeout=5)
        try:
            async with self.pool.connection() as conn:
                row = await (await conn.execute("""SELECT NOT (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication)
                  AND pg_has_role(current_user,'postern_resource_service','MEMBER')
                  AND NOT pg_has_role(current_user,'postern_owner','MEMBER')
                  AND NOT pg_has_role(current_user,'postern_identity_writer','MEMBER')
                  AND NOT pg_has_role(current_user,'postern_onboarding_service','MEMBER')
                  AND NOT pg_has_role(current_user,'postern_reader','MEMBER')
                  AND NOT pg_has_role(current_user,'postern_writer','MEMBER')
                  AND NOT has_table_privilege(current_user,'postern.principals','SELECT')
                  FROM pg_roles WHERE rolname=current_user""")).fetchone()
                if not row or not row[0]:
                    raise ValueError('resource runtime requires a restricted login')
        except BaseException:
            await self.pool.close()
            raise

    async def close(self):
        await self.pool.close()

    async def call(self, function, params):
        # Function names come solely from the fixed calls below, never request data.
        allowed = {'exchange_session', 'resource_context', 'resource_catalog',
                   'resource_detail', 'resource_revoke', 'end_resource_session',
                   'create_invitation', 'list_invitations', 'revoke_invitation',
                   'resource_members', 'resource_membership_change'}
        if function not in allowed:
            raise ValueError('unknown operation')
        try:
            async with self.pool.connection() as conn:
                row = await (await conn.execute('SELECT postern.' + function + '(' +
                    ','.join(['%s'] * len(params)) + ')', params)).fetchone()
            # COMMIT (including deferred owner constraints) completed before returning.
            return row[0]
        except psycopg.Error as exc:
            if exc.sqlstate == '28000':
                raise invalid_session() from None
            if exc.sqlstate == '42501':
                raise SpaceError(404, 'not_found', 'This resource is not available.') from None
            if exc.sqlstate == 'PT409':
                raise SpaceError(409, 'invitation_conflict', 'The invitation conflicts with current membership or the pending invitation limit.') from None
            if exc.sqlstate == 'PT412':
                raise SpaceError(409, 'membership_changed', 'Memberships changed. Reload the member list before trying again.') from None
            if exc.sqlstate == '22023':
                raise SpaceError(422, 'invalid_cursor', 'This list changed. Reload it before continuing.') from None
            if exc.sqlstate == 'P0001':
                message = exc.diag.message_primary
                if message == 'forbidden':
                    raise SpaceError(403, 'forbidden', 'This action is not permitted.') from None
                if message == 'operation mismatch':
                    raise SpaceError(409, 'operation_conflict', 'Use the original request for this operation.') from None
                if message == 'active space requires an active owner':
                    raise SpaceError(409, 'membership_conflict', 'This change conflicts with the current membership.') from None
            raise SpaceError(503, 'space_unavailable', 'This space is temporarily unavailable.') from None
        except (PoolTimeout, TooManyRequests):
            raise SpaceError(503, 'space_unavailable', 'This space is temporarily unavailable.') from None

    @staticmethod
    def token_hash(request):
        match = re.fullmatch(r'Bearer (ps_[A-Za-z0-9_-]{43})', request.headers.get('authorization', ''))
        if not match:
            raise invalid_session()
        return hashlib.sha256(match[1].encode()).hexdigest()

    async def exchange(self, space, assertion):
        try:
            if not isinstance(assertion, str) or len(assertion) > 4096:
                raise ValueError('assertion size')
            header = jwt.get_unverified_header(assertion)
            if header != {'alg': 'EdDSA', 'typ': 'postern-space+jwt', 'kid': self.trust.kid}:
                raise ValueError('assertion header')
            claims = jwt.decode(assertion, self.trust.public_key, algorithms=['EdDSA'],
                issuer=self.trust.issuer, audience=self.trust.audience, options={'strict_aud': True,
                'require': ['iss', 'aud', 'sub', 'exp', 'iat', 'nbf', 'jti', 'login_id',
                            'space_id', 'generation', 'membership_id', 'account_authority', 'account_id']})
            for key in ('sub', 'jti', 'login_id', 'space_id', 'generation', 'membership_id'):
                identifier(claims[key])
            if (claims['space_id'] != space or any(type(claims[k]) is not int for k in ('iat', 'nbf', 'exp'))
                    or claims['nbf'] != claims['iat'] or not 0 < claims['exp']-claims['iat'] <= 60
                    or any(not isinstance(claims[k], str) or not 1 <= len(claims[k]) <= 255 for k in ('account_authority', 'account_id'))):
                raise ValueError('assertion claims')
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise invalid_session() from None
        value = 'ps_' + secrets.token_urlsafe(32)
        result = await self.call('exchange_session', (hashlib.sha256(value.encode()).hexdigest(),
            space, self.trust.audience, claims['jti'], claims['login_id'], claims['sub'],
            claims['account_authority'], claims['account_id'], claims['generation'], claims['membership_id'], claims['exp']))
        return {**result, 'token': value}

    async def catalog(self, hashed, space, query, cursor):
        after, expected = None, None
        if cursor:
            try:
                data = jwt.decode(cursor, self.trust.cursor_key, algorithms=['HS256'],
                    audience=self.trust.audience, options={'require': ['exp', 'aud', 'context', 'query', 'after']})
                if data['query'] != query or data['context']['space_id'] != space:
                    raise ValueError('cursor scope')
                after, expected = identifier(data['after']), data['context']
            except (jwt.PyJWTError, KeyError, TypeError, ValueError):
                raise SpaceError(422, 'invalid_cursor', 'Reload the resource list.') from None
        result = await self.call('resource_catalog', (hashed, space, self.trust.audience, query, after,
                                                     Jsonb(expected) if expected is not None else None))
        items, next_cursor = result['items'], None
        if len(items) > 50:
            items = items[:50]
            next_cursor = jwt.encode({'context': result['context'], 'query': query, 'after': items[-1]['id'],
                'aud': self.trust.audience, 'exp': int(time.time())+900}, self.trust.cursor_key, algorithm='HS256')
        return {'resources': items, 'next_cursor': next_cursor}


    async def invite(self, hashed, space, value):
        if self.invitation_key is None:
            raise SpaceError(503, 'invitations_unavailable', 'Invitations are not configured.')
        try:
            operation = identifier(value['operation_id'])
            recipient = value['recipient']
            if not isinstance(recipient, dict) or set(recipient) != {'authority', 'account_id'}:
                raise ValueError('recipient reference')
            if any(not isinstance(recipient[k], str) or not 1 <= len(recipient[k]) <= 255
                   or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in recipient[k]) for k in recipient):
                raise ValueError('recipient reference')
            role, ttl = value['role'], value['expires_in_seconds']
            if role not in ('member', 'admin') or type(ttl) is not int or not 60 <= ttl <= 604800:
                raise ValueError('role or expiry')
        except (KeyError, ValueError, TypeError):
            raise SpaceError(422, 'invalid_invitation', 'Choose an account, member or admin role, and an expiry between one minute and seven days.') from None
        fingerprint = hashlib.sha256(json.dumps(['invitation.create', space, recipient['authority'], recipient['account_id'], role, ttl],
                                                separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        ctx = await self.call('resource_context', (hashed, space, self.trust.audience))
        # Recover a lost successful response without persisting a plaintext secret.
        # Independent key; bound to actor, authority generation and complete request.
        seed = json.dumps(['invitation-v1', ctx['principal_id'], ctx['generation'], space, operation, fingerprint], separators=(',', ':')).encode()
        secret = 'pi_' + base64.urlsafe_b64encode(hmac.digest(self.invitation_key, seed, 'sha256')).decode().rstrip('=')
        result = await self.call('create_invitation', (hashed, space, self.trust.audience, operation, fingerprint,
            recipient['authority'], recipient['account_id'], role, ttl, hashlib.sha256(secret.encode()).digest()))
        return {**result, 'secret': secret}

    async def members(self, hashed, space, cursor):
        after, expected = None, None
        if cursor:
            try:
                data = jwt.decode(cursor, self.trust.cursor_key, algorithms=['HS256'], audience=self.trust.audience,
                    options={'require': ['exp', 'aud', 'kind', 'context', 'after']})
                if data['kind'] != 'memberships' or data['context']['space_id'] != space:
                    raise ValueError('cursor scope')
                after, expected = identifier(data['after']), data['context']
            except (jwt.PyJWTError, KeyError, TypeError, ValueError):
                raise SpaceError(422, 'invalid_cursor', 'Reload the member list.') from None
        result = await self.call('resource_members', (hashed, space, self.trust.audience, after,
            Jsonb(expected) if expected is not None else None))
        items, ctx, next_cursor = result['items'], result['context'], None
        if len(items) > 50:
            items = items[:50]
            next_cursor = jwt.encode({'context': ctx, 'after': items[-1]['membership_id'], 'kind': 'memberships',
                'aud': self.trust.audience, 'exp': int(time.time())+900}, self.trust.cursor_key, algorithm='HS256')
        return {'members': items, 'next_cursor': next_cursor, 'role': ctx['role'],
            'membership_id': ctx['membership_id'], 'policy_revision': ctx['policy_revision']}

    async def change_member(self, hashed, space, member, value, *, remove=False):
        try:
            operation = identifier(value['operation_id'])
            expected = value['expected_revision']
            role = None if remove else value['role']
            if type(expected) is not int or not 0 <= expected <= 9007199254740991 or (not remove and role not in ('member', 'admin')):
                raise ValueError('role or revision')
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SpaceError(422, 'invalid_membership', 'Choose a member or administrator role and a valid list revision.') from None
        fingerprint = hashlib.sha256(json.dumps(['membership.remove' if remove else 'membership.role', space, member, role, expected],
            separators=(',', ':')).encode()).hexdigest()
        return await self.call('resource_membership_change', (hashed, space, self.trust.audience, member, operation, fingerprint, expected, role))


def router(service):
    api = APIRouter(prefix='/v1/spaces')

    async def body(request, key):
        if request.headers.get('content-type', '').split(';')[0].lower() != 'application/json':
            raise SpaceError(415, 'json_required', 'Send an application/json request.')
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8192:
                raise SpaceError(413, 'request_too_large', 'This request is too large.')
        try:
            value = json.loads(data)
            keys = {key} if isinstance(key, str) else set(key)
            if not isinstance(value, dict) or set(value) != keys:
                raise ValueError('fields')
            return value[key] if isinstance(key, str) else value
        except (ValueError, UnicodeError):
            raise SpaceError(422, 'invalid_request', 'Send the required request fields.') from None

    def valid(value):
        try:
            return identifier(value)
        except (ValueError, TypeError, AttributeError):
            raise SpaceError(422, 'invalid_identifier', 'Choose a valid identifier.') from None

    @api.post('/{space}/session/exchange')
    async def exchange(space: str, request: Request):
        return await service.exchange(valid(space), await body(request, 'assertion'))

    @api.get('/{space}/session')
    async def context(space: str, request: Request):
        return await service.call('resource_context', (service.token_hash(request), valid(space), service.trust.audience))

    @api.post('/{space}/session/signout')
    async def signout(space: str, request: Request):
        await service.call('end_resource_session', (service.token_hash(request), valid(space), service.trust.audience))
        return {'signed_out': True}

    @api.get('/{space}/resources')
    async def catalog(space: str, request: Request, query: str = Query('', max_length=200), cursor: str = Query('', max_length=4096)):
        return await service.catalog(service.token_hash(request), valid(space), query, cursor)

    @api.get('/{space}/resources/{resource}')
    async def detail(space: str, resource: str, request: Request):
        return {'resource': await service.call('resource_detail', (service.token_hash(request), valid(space), service.trust.audience, valid(resource)))}

    @api.post('/{space}/memberships/{membership}/revoke')
    async def revoke(space: str, membership: str, request: Request):
        key = valid(await body(request, 'operation_id'))
        s, m = valid(space), valid(membership)
        fingerprint = hashlib.sha256(f'membership.revoke:{s}:{m}'.encode()).hexdigest()
        return await service.call('resource_revoke', (service.token_hash(request), s, service.trust.audience, m, key, fingerprint))

    @api.get('/{space}/memberships')
    async def members(space: str, request: Request, cursor: str = Query('', max_length=4096)):
        return await service.members(service.token_hash(request), valid(space), cursor)

    @api.post('/{space}/memberships/{membership}/role')
    async def role(space: str, membership: str, request: Request):
        value = await body(request, ('operation_id', 'expected_revision', 'role'))
        return await service.change_member(service.token_hash(request), valid(space), valid(membership), value)

    @api.post('/{space}/memberships/{membership}/remove')
    async def remove(space: str, membership: str, request: Request):
        value = await body(request, ('operation_id', 'expected_revision'))
        return await service.change_member(service.token_hash(request), valid(space), valid(membership), value, remove=True)

    @api.post('/{space}/invitations')
    async def invite(space: str, request: Request):
        value = await body(request, ('operation_id', 'recipient', 'role', 'expires_in_seconds'))
        return await service.invite(service.token_hash(request), valid(space), value)

    @api.get('/{space}/invitations')
    async def invitations(space: str, request: Request):
        return await service.call('list_invitations', (service.token_hash(request), valid(space), service.trust.audience))

    @api.post('/{space}/invitations/{invitation}/revoke')
    async def revoke_invitation(space: str, invitation: str, request: Request):
        key = valid(await body(request, 'operation_id'))
        s, i = valid(space), valid(invitation)
        fingerprint = hashlib.sha256(f'invitation.revoke:{s}:{i}'.encode()).hexdigest()
        return await service.call('revoke_invitation', (service.token_hash(request), s, service.trust.audience, i, key, fingerprint))

    @api.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'])
    async def missing(path: str):
        raise SpaceError(404, 'not_found', 'This endpoint is not available.')

    return api


def install(app, service):
    @app.exception_handler(SpaceError)
    async def refused(request, error):
        return JSONResponse({'error': error.code, 'message': error.message}, status_code=error.status)
    app.include_router(router(service))
