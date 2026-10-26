from __future__ import annotations
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from functools import partial
from .db import meta, set_meta, event
from .config import IP_NOTICE_VERSION


class Refusal(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def canonical_email(value):
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", value):
        raise ValueError('Invalid email address')
    local, domain = value.rsplit('@', 1)
    if domain in ('gmail.com', 'googlemail.com'):
        local, domain = local.split('+', 1)[0].replace('.', ''), 'gmail.com'
    if not local:
        raise ValueError('Invalid email address')
    return f'{local}@{domain}'


# Managers and C-suite read like staff and may skip chapter quizzes; only admins manage content.
ROLES = {'': 'reader', 'reader': 'reader', 'staff': 'reader', 'admin': 'admin', 'manager': 'manager',
         'c-suite': 'c-suite', 'csuite': 'c-suite', 'c-level': 'c-suite', 'clevel': 'c-suite'}


def role_name(value):
    return ROLES.get(re.sub(r'[\s_-]+', '-', value.strip().lower()))


def parse_allowlist(values, email_header='email', *, warnings=None):
    """Validate the complete roster before applying any account or group changes.

    Legacy single-range rosters keep their strict grammar. Restaurant worksheets
    may contain pending contacts without an email; report those rows, never grant
    them access. A nonempty malformed address or conflicting role rejects all tabs.
    """
    if not isinstance(values, dict):
        return _parse_allowlist(values, email_header)
    if not values:
        raise ValueError('The allowlist needs at least one worksheet')
    result = {}
    for group, rows in values.items():
        missing = []
        try:
            members = _parse_allowlist(rows, email_header, missing=missing)
            for email, member in members.items():
                if email in result:
                    if result[email]['role'] != member['role']:
                        raise ValueError('Duplicate address has conflicting roles across worksheets')
                    result[email]['groups'].append(group)
                else:
                    result[email] = {**member, 'groups': [group]}
        except ValueError as error:
            raise ValueError(f'{group}: {error}') from error
        if warnings is not None:
            warnings.extend({'group': group, 'row': row, 'message': 'Missing email address'} for row in missing)
    return result


def _parse_allowlist(values, email_header, *, missing=None):
    if not values:
        raise ValueError('The allowlist needs a header row')
    headers = [str(v).strip().lower() for v in values[0]]
    label = email_header.strip().lower()
    if headers.count(label) != 1:
        raise ValueError(f'The allowlist needs exactly one {email_header!r} column')
    for optional in ('name', 'role'):
        if headers.count(optional) > 1:
            raise ValueError(f'The {optional} column appears more than once')
    email_index = headers.index(label)
    name_index = headers.index('name') if 'name' in headers else None
    role_index = headers.index('role') if 'role' in headers else None
    result = {}
    for number, row in enumerate(values[1:], 2):
        cells = [str(v).strip() for v in row]
        if not any(cells):
            continue
        def get(index):
            return cells[index] if index is not None and index < len(cells) else ''
        if missing is not None and not get(email_index):
            missing.append(number)
            continue
        try:
            email = canonical_email(get(email_index))
        except ValueError:
            raise ValueError(f'Row {number}: enter a valid email address')
        role = role_name(get(role_index))
        if role is None:
            raise ValueError(f'Row {number}: role must be blank (staff), Admin, Manager or C-Suite')
        if email in result and result[email]['role'] != role:
            raise ValueError(f'Row {number}: duplicate address has conflicting roles')
        result[email] = {'email': email, 'name': get(name_index), 'role': role}
    return result


def verify_google_identity(credential, client_ids):
    """Check a Google ID token's signature, issuer, expiry and audience, then return who it names."""
    from google.auth.transport import requests as transport
    from google.oauth2 import id_token
    from requests import Session
    if not credential or len(credential) > 8192:
        raise Refusal(401, 'sign_in_required', 'Please sign in with Google to continue.')
    try:
        # Audience is checked here so one deployment can accept the web and, later, the desktop client.
        # Bound certificate I/O so an unavailable Google endpoint cannot occupy a worker for minutes.
        with Session() as session:
            request = partial(transport.Request(session=session), timeout=5)
            claims = id_token.verify_oauth2_token(credential, request)
    except Exception:
        raise Refusal(401, 'sign_in_required', 'Google could not confirm that sign-in. Please try again.')
    if claims.get('iss') not in ('accounts.google.com', 'https://accounts.google.com') or claims.get('aud') not in client_ids:
        raise Refusal(401, 'sign_in_required', 'That sign-in was issued for a different application.')
    subject = claims.get('sub')
    if not isinstance(subject, str) or not 1 <= len(subject) <= 255 or not subject.isascii() or not subject.isprintable():
        raise Refusal(401, 'sign_in_required', 'Google could not confirm that account identity. Please try again.')
    if not isinstance(claims.get('email'), str) or not claims['email'] or claims.get('email_verified') is not True:
        raise Refusal(403, 'access_denied', 'Your Google account has no verified email address.')
    return {'sub': subject, 'email': claims['email'], 'name': claims.get('name', '')}


def session_identity(subject, mode):
    """Describe an authenticated session for account/space matching, never authorization.

    This is returned only to the session's holder. Local demo identities must not
    be mistaken for a verified Google account by an account-enabled frontend.
    """
    return {'issuer': 'https://accounts.google.com' if mode == 'google' else 'postern:demo',
            'subject': subject}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Permissions:
    def __init__(self, notice_version, clock=time.monotonic, *, ip_notice_version=IP_NOTICE_VERSION, default_group='Staff'):
        self.boot = str(uuid.uuid4())
        self.clock = clock
        self.notice_version = notice_version
        self.ip_notice_version = ip_notice_version
        self.default_group = default_group

    def fresh(self, db):
        state = meta(db, 'allowlist_freshness', {})
        return state.get('boot') == self.boot and self.clock() - state.get('at', 0) < 60

    def require_fresh(self, db):
        if not self.fresh(db):
            raise Refusal(503, 'authorization_source_unavailable', 'Access temporarily paused while we verify permissions.')

    def apply(self, db, snapshot, *, groups=None, warnings=()):
        previous = {r['email']: dict(r) for r in db.execute('SELECT * FROM members')}
        revoked = []
        for email in previous.keys() - snapshot.keys():
            revoked += [r[0] for r in db.execute('SELECT id FROM sessions WHERE email=? AND revoked=0', (email,))]
            db.execute('UPDATE sessions SET revoked=1 WHERE email=?', (email,))
            event(db, 'allowlist_removed', email=email)
        for email, member in snapshot.items():
            if email not in previous:
                event(db, 'allowlist_added', email=email)
            elif member['role'] != previous[email]['role']:
                event(db, 'role_changed', email=email, detail={'role': member['role']})
        db.execute('DELETE FROM members')
        db.executemany('INSERT INTO members(email,name,role) VALUES (:email,:name,:role)', snapshot.values())
        # These are derived roster metadata, replaced in the same transaction as
        # membership. Scores and viewing history remain keyed by the one email.
        membership = {email: member.get('groups', [self.default_group]) for email, member in snapshot.items()}
        set_meta(db, 'staff_groups', groups if groups is not None else
                 list(dict.fromkeys(group for names in membership.values() for group in names)) or [self.default_group])
        set_meta(db, 'member_groups', membership)
        set_meta(db, 'allowlist_warnings', list(warnings))
        set_meta(db, 'allowlist_freshness', {'boot': self.boot, 'at': self.clock(), 'wall': time.time()})
        set_meta(db, 'allowlist_error', None)
        if meta(db, 'allowlist_paused', True):
            event(db, 'allowlist_recovered')
        set_meta(db, 'allowlist_paused', False)
        return revoked

    def consent_current(self, db, session):
        if session['consent_version'] != self.notice_version:
            return False
        agreement = db.execute('SELECT version FROM session_ip_agreements WHERE session_id=?', (session['id'],)).fetchone()
        return agreement is not None and agreement['version'] == self.ip_notice_version

    def authorize_content_request(self, db, token, *, admin=False, consent=True, selfie=True, post_id=None, item_id=None, view_id=None, request_context=None, token_digest=None):
        session = db.execute('SELECT * FROM sessions WHERE token_hash=?', (token_digest or token_hash(token),)).fetchone() if token or token_digest else None
        if not session or session['expires_at'] <= time.time():
            raise Refusal(401, 'sign_in_required', 'Please sign in to continue.')
        self.require_fresh(db)
        member = db.execute('SELECT * FROM members WHERE email=?', (session['email'],)).fetchone()
        if session['revoked'] or not member:
            raise Refusal(403, 'access_denied', 'Your access is no longer available.')
        if admin and member['role'] != 'admin':
            raise Refusal(403, 'access_denied', 'Administrator access is required.')
        if consent and not self.consent_current(db, session):
            raise Refusal(403, 'consent_required', 'Please review and accept both agreements before entering.')
        context = {**dict(session), 'role': member['role'], **(request_context or {})}
        context.pop('token_hash', None)
        # Administrators can manage content without a camera. Reading routes
        # never set admin=True, so their enrollment requirement still applies.
        if getattr(self, 'selfie_enabled', False) and selfie and not admin:
            from .selfie import require_enrollment
            context['selfie_id'] = require_enrollment(db, context, self.selfie_notice_version)['id']
        if view_id:
            view = db.execute('SELECT * FROM views WHERE id=?', (view_id,)).fetchone()
            if not view or view['session_id'] != session['id']:
                raise Refusal(403, 'access_denied', 'This view is not available to your session.')
            item_id = view['item_id']
            context['view'] = dict(view)
        if item_id:
            item = db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
            if not item:
                raise Refusal(403, 'access_denied', 'This attachment is not available.')
            post_id = item['post_id']
            context['item'] = dict(item)
        if post_id:
            post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
            if not post or (post['status'] != 'published' and member['role'] != 'admin') or meta(db, 'recovery_mode', False):
                raise Refusal(403, 'access_denied', 'This post is not available.')
            if getattr(self, 'quizzes', None):
                # Staff open a chapter only after passing every earlier chapter's quiz.
                from .quiz import require_unlocked
                require_unlocked(db, self.quizzes, member['role'], member['email'], post['category'])
            context['post'] = dict(post)
        db.execute('UPDATE sessions SET expires_at=? WHERE id=?', (time.time()+30*86400, session['id']))
        return context
