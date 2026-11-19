"""Real PostgreSQL authority plus two actual legacy SQLite training services."""
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
import jwt
import pytest

from postern import quiz
from postern.app import create_app
from postern.config import Config
from postern.db import event, set_meta
from postern.pg import Postgres, restore_quarantined
from postern.recover import import_events
from postern.service import Service
from postern.spaces import SpaceService, SpaceTrust
from postern.space_training import LegacyTrainingSource, SpaceTraining
from postern.storage import FakeStore
from starlette.requests import Request

EMAIL = 'reader@example.invalid'
OTHER = 'other@example.invalid'


@pytest.mark.parametrize('chunked', [False, True])
def test_native_training_body_is_bounded_before_authentication(training, chunked):
    f = training
    body = b'{"answers":{"one":"' + b'x' * 40000 + b'"}}'
    content = iter([body[:20000], body[20000:]]) if chunked else body
    response = f.client.post(f.url()+'/quiz', content=content, headers={'Content-Type': 'application/json'})
    assert response.status_code == 413
    assert response.json()['error'] == 'request_too_large'
    assert response.headers['cache-control'] == 'no-store'
    assert f.model.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{f.model.s}' AND action LIKE 'training.%';") == '0'


@pytest.mark.parametrize('answers', [{'k'*257: 'a'}, {'question': 'a'*129}])
def test_native_training_answer_strings_are_bounded(training, answers):
    f = training
    response = f.client.post(f.url()+'/quiz', headers=f.headers(), json={
        'operation_id': str(uuid.uuid4()), 'version': 'fixture', 'answers': answers})
    assert response.status_code == 422
    assert f.model.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{f.model.s}' AND action LIKE 'training.%';") == '0'


@pytest.fixture(scope='module')
def runtime(db):
    db.execute('CREATE ROLE training_api_test LOGIN IN ROLE postern_resource_service;')
    return dict(host=db.env['PGHOST'], port=int(db.env['PGPORT']), user='training_api_test', dbname='postgres')


@pytest.fixture
def training(model, runtime, tmp_path, monkeypatch):
    async def paused(_):
        await asyncio.Event().wait()
    monkeypatch.setattr(Service, 'background_loop', paused)
    m = model
    # One member owns the same explicit legacy email in both independent spaces.
    # A different native principal must never acquire that identity by guessing it.
    m.db.execute(f"INSERT INTO postern.memberships VALUES('{m.t}','{uuid.uuid4()}','{m.b}','member','active');")
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    trust = SpaceTrust('urn:training:accounts', 'urn:training:resources', 'training-key', public, secrets.token_bytes(32))
    spaces = SpaceService(runtime, trust, max_size=4)
    sources, clients, legacy_headers, ids, installations = {}, {}, {}, {}, {}
    with ExitStack() as stack:
        for space in (m.s, m.t):
            installation = str(uuid.uuid4()); installations[space] = installation
            store = FakeStore(tmp_path / space / 'cloud')
            store.write_allowlist([['email', 'name', 'role'], [EMAIL, 'Reader Person', 'reader'], [OTHER, 'Other Person', 'reader']])
            config = Config(data_dir=tmp_path / space / 'data', mode='demo', notice_version='1')
            service = Service(config, store)
            legacy_client = stack.enter_context(TestClient(create_app(config, service=service)))
            clients[space] = legacy_client
            sources[space] = LegacyTrainingSource(installation, service)
            response = legacy_client.post('/api/auth/demo', json={'email': EMAIL, 'name': 'Reader Person'})
            assert response.status_code == 200
            legacy_headers[space] = {'Authorization': 'Bearer ' + response.json()['token']}
            consent = {'version': '1', 'ip_version': 'ip-1', 'monitoring_accepted': True, 'ip_accepted': True}
            assert legacy_client.post('/api/consent', json=consent, headers=legacy_headers[space]).status_code == 200
            ids[space] = {chapter: str(uuid.uuid4()) for chapter in (0, 1, 2, 9)}
            m.db.execute(f"INSERT INTO postern.imports(space_id,installation,digest) VALUES('{space}','{installation}','{'a'*64}');"
                f"INSERT INTO postern.legacy_bindings VALUES('{space}','{installation}','{EMAIL}','{m.b}','reserved fixture principal');")
            for chapter, resource in ids[space].items():
                m.db.execute(f"INSERT INTO postern.resources(space_id,id,kind,title,state,adapter) VALUES('{space}','{resource}','training','Training {chapter}','published','legacy-training-v1');"
                    f"INSERT INTO postern.training_presentations VALUES('{space}','{resource}',1,'{installation}',{chapter});"
                    f"INSERT INTO postern.grants(space_id,resource_id,all_members,actions) VALUES('{space}','{resource}',true,ARRAY['discover','view']);")
                if chapter:
                    service.db.submit(lambda db, chapter=chapter: db.execute(
                        "INSERT INTO posts(id,title,body,category,status,created_at,created_by,published_at) VALUES(?,?,'',?,'published',1,?,2)",
                        (str(uuid.uuid4()), f'Secret chapter {chapter}', f'SOP Chapter {chapter}: Fixture', EMAIL))).result()
        bridge = SpaceTraining(spaces, sources)
        app_config = Config(data_dir=tmp_path / 'native-data', mode='demo')
        app_store = FakeStore(tmp_path / 'native-cloud'); app_store.write_allowlist([['email', 'name', 'role']])
        client = stack.enter_context(TestClient(create_app(app_config, store=app_store, spaces=spaces, training=bridge)))
        f = SimpleNamespace(model=m, spaces=spaces, bridge=bridge, sources=sources, client=client, legacy_clients=clients, key=key, trust=trust,
                            legacy_headers=legacy_headers, ids=ids, installations=installations)

        def headers(space=None, actor='b', legacy_space=None):
            space = space or m.s
            login = str(uuid.uuid4()); now = int(time.time())
            m.db.execute(f"SELECT postern.register_login('{login}','{getattr(m, actor)}',{now+3600});")
            membership = m.db.scalar(f"SELECT id FROM postern.memberships WHERE space_id='{space}' AND principal_id='{getattr(m,actor)}' AND state='active';")
            claims = dict(iss=trust.issuer, aud=trust.audience, sub=getattr(m, actor), iat=now, nbf=now, exp=now+60,
                jti=str(uuid.uuid4()), login_id=login, space_id=space, generation=m.generation, membership_id=membership,
                account_authority='fixture', account_id=getattr(m, actor))
            assertion = jwt.encode(claims, key, algorithm='EdDSA', headers={'kid': trust.kid, 'typ': 'postern-space+jwt'})
            response = client.post(f'/v1/spaces/{space}/session/exchange', json={'assertion': assertion})
            assert response.status_code == 200, response.text
            return {'Authorization': 'Bearer ' + response.json()['token'],
                    'X-Postern-Legacy-Authorization': legacy_headers[legacy_space or space]['Authorization']}

        f.headers = headers
        f.url = lambda chapter=1, space=None: f'/v1/spaces/{space or m.s}/resources/{ids[space or m.s][chapter]}/revisions/1/training'
        yield f


def answers(f, opened, space=None):
    bank = f.sources[space or f.model.s].service.permissions.quizzes
    pool = {q['id']: q['answer'] for q in bank[opened['chapter']]['questions']}
    return {q['id']: pool[q['id']] for q in opened['questions']}


def submit(f, headers, chapter=1, space=None, operation=None):
    opened = f.client.get(f.url(chapter, space) + '/quiz', headers=headers)
    assert opened.status_code == 200, opened.text
    body = dict(operation_id=operation or str(uuid.uuid4()), version=opened.json()['version'], answers=answers(f, opened.json(), space))
    return f.client.post(f.url(chapter, space) + '/quiz', headers=headers, json=body), body


def count(service, kind):
    return service.db.read(lambda db: db.execute('SELECT count(*) FROM events WHERE type=?', (kind,)).fetchone()[0])


def test_dual_identity_isolation_and_real_quiz_history(training):
    f = training; m = f.model; h = f.headers()
    response, body = submit(f, h)
    assert response.status_code == 200, response.text
    assert response.json()['passed'] is True
    assert f.client.get(f.url() + '/progress', headers=h).json()['standing']['attempts'] == 1
    other = f.headers(m.t)
    assert f.client.get(f.url(space=m.t) + '/progress', headers=other).json()['standing'] is None
    for url, headers in [(f.url(space=m.t), h), (f.url(), other), (f.url(), f.headers(legacy_space=m.t)),
                         (f.url(), f.headers(actor='a'))]:
        assert f.client.get(url + '/quiz', headers=headers).status_code in (401, 404)
    # A valid session in this installation for a different email is also insufficient.
    token = f.legacy_clients[m.s].post('/api/auth/demo', json={'email': OTHER, 'name': 'Other'}).json()['token']
    wrong = {**h, 'X-Postern-Legacy-Authorization': 'Bearer ' + token}
    f.legacy_clients[m.s].post('/api/consent', headers={'Authorization': 'Bearer ' + token}, json={
        'version': '1', 'ip_version': 'ip-1', 'monitoring_accepted': True, 'ip_accepted': True})
    assert f.client.get(f.url() + '/quiz', headers=wrong).status_code == 404
    for value in ('', h['Authorization']):
        assert f.client.get(f.url() + '/quiz', headers={**h, 'X-Postern-Legacy-Authorization': value}).status_code == 401
    assert count(f.sources[m.s].service, 'quiz_attempt') == 1
    assert count(f.sources[m.t].service, 'quiz_attempt') == 0
    raw = Postgres({**m.db.env, 'PGUSER': 'training_api_test'})
    for sql in ('SELECT * FROM postern.training_presentations', 'SELECT * FROM postern.legacy_bindings',
                "SELECT postern.training_snapshot(NULL,NULL,NULL,NULL,1,'history')"):
        assert raw.execute(sql, check=False).returncode != 0


def test_hidden_prerequisite_and_unfinished_item_never_leak(training):
    f = training; m = f.model; h = f.headers()
    m.db.execute(f"UPDATE postern.grants SET state='revoked' WHERE space_id='{m.s}' AND resource_id='{f.ids[m.s][1]}';")
    denied = f.client.get(f.url(2) + '/quiz', headers=h)
    assert denied.status_code == 423
    assert 'Chapter 1' not in denied.text and f.ids[m.s][1] not in denied.text
    hidden_history = f.client.get(f.url(1) + '/progress', headers=h)
    assert hidden_history.status_code == 404 and 'chapter' not in hidden_history.text
    m.db.execute(f"INSERT INTO postern.grants(space_id,resource_id,all_members,actions) VALUES('{m.s}','{f.ids[m.s][1]}',true,ARRAY['discover','view']);")
    service = f.sources[m.s].service; item = str(uuid.uuid4())
    service.db.submit(lambda db: db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) "
        "SELECT ?,id,'hidden.pdf','pdf',?,1,1,'ready',0 FROM posts WHERE category LIKE 'SOP Chapter 1:%'", (item, 'a'*64))).result()
    opened = f.client.get(f.url() + '/quiz', headers=h)
    assert opened.status_code == 200, opened.text
    assert opened.json()['ready'] is False and opened.json()['questions'] == []
    assert opened.json()['unavailable_requirements'] is True
    assert item not in opened.text and 'hidden.pdf' not in opened.text
    refused = f.client.post(f.url() + '/quiz', headers=h, json={'operation_id': str(uuid.uuid4()), 'version': opened.json()['version'], 'answers': {}})
    assert refused.status_code == 409 and count(service, 'quiz_attempt') == 0


def test_unearned_hidden_certificate_and_oversize_revision_do_not_disclose(training):
    f = training; m = f.model; h = f.headers()
    m.db.execute(f"UPDATE postern.grants SET state='revoked' WHERE space_id='{m.s}' AND resource_id='{f.ids[m.s][0]}';")
    for route in ('certificate', 'progress'):
        result = f.client.get(f.url(0) + '/' + route, headers=h)
        assert result.status_code == 404 and 'chapter' not in result.text
    url = f.url().replace('/revisions/1/', f'/revisions/{2**63}/')
    assert f.client.get(url + '/progress', headers=h).status_code == 404


@pytest.mark.parametrize('boundary', ['consent', 'legacy_revoke', 'native_revoke', 'membership', 'recovery', 'stale_roster', 'enrollment'])
def test_current_authority_and_legacy_gates(training, boundary):
    f = training; m = f.model; h = f.headers(); service = f.sources[m.s].service
    if boundary == 'consent':
        service.db.submit(lambda db: db.execute("UPDATE sessions SET consent_version='old'")).result()
    elif boundary == 'legacy_revoke':
        service.db.submit(lambda db: db.execute('UPDATE sessions SET revoked=1')).result()
    elif boundary == 'native_revoke':
        digest = hashlib.sha256(h['Authorization'][7:].encode()).hexdigest()
        m.db.execute(f"UPDATE postern.resource_sessions SET revoked=true WHERE token_hash='{digest}';")
    elif boundary == 'membership':
        m.db.execute(f"UPDATE postern.memberships SET state='revoked' WHERE space_id='{m.s}' AND principal_id='{m.b}';")
    elif boundary == 'recovery':
        service.db.submit(lambda db: set_meta(db, 'recovery_mode', True)).result()
    elif boundary == 'stale_roster':
        service.db.submit(lambda db: set_meta(db, 'allowlist_freshness', {})).result()
    else:
        service.permissions.selfie_enabled = True
    response = f.client.get(f.url() + '/quiz', headers=h)
    assert response.status_code in (401, 403, 409, 503), response.text
    assert count(service, 'quiz_attempt') == 0


def test_archived_own_progress_and_restored_certificate_identity(training):
    f = training; m = f.model; h = f.headers(); service = f.sources[m.s].service
    rows = [dict(event_id='original-final-pass', event='quiz_attempt', ts=1700000000, email=EMAIL, name='Original Reader',
                 detail=dict(chapter=0, category='Final Exam', version='old-bank', score=16, total=20, pass_mark=16, passed=True, answers={})),
            dict(event_id='saved-certificate-name', event='certificate_name', ts=1700000001, email=EMAIL,
                 detail=dict(name='Preserved Reader', saved_at=1700000001))]
    service.db.submit(lambda db: import_events(db, rows)).result()
    before = f.client.get(f.url(0) + '/certificate', headers=h)
    assert before.status_code == 200, before.text
    certificate = before.json()['certificate']; assert certificate['name'] == 'Preserved Reader'
    assert certificate['awarded_at'] == 1700000000
    # Another independently restored installation can have identical event IDs and
    # certificate number. Neither is a bearer capability or global lookup key.
    other_service = f.sources[m.t].service
    other_service.db.submit(lambda db: import_events(db, rows)).result()
    other = f.headers(m.t)
    assert f.client.get(f.url(0, m.t) + '/certificate', headers=other).json()['certificate'] == certificate
    assert f.client.get(f.url(0, m.t) + '/certificate', headers=h).status_code == 401
    m.db.execute(f"UPDATE postern.resources SET state='archived' WHERE space_id='{m.s}' AND id='{f.ids[m.s][0]}';")
    assert f.client.get(f.url(0) + '/certificate', headers=h).json()['certificate'] == certificate
    assert f.client.get(f.url(0) + '/progress', headers=h).json()['standing']['passed'] is True
    assert f.client.get(f.url(0) + '/quiz', headers=h).status_code == 404
    m.db.execute("UPDATE postern.authority SET state='quarantined',generation=gen_random_uuid();")
    assert f.client.get(f.url(0) + '/certificate', headers=h).status_code == 401


def test_idempotent_mutation_and_payload_conflict(training):
    f = training; h = f.headers(); service = f.sources[f.model.s].service
    response, body = submit(f, h)
    assert response.status_code == 200
    assert f.client.post(f.url() + '/quiz', headers=h, json=body).json() == response.json()
    assert count(service, 'quiz_attempt') == 1
    changed = {**body, 'version': 'changed'}
    assert f.client.post(f.url() + '/quiz', headers=h, json=changed).status_code == 409
    service.db.submit(lambda db: db.execute("UPDATE posts SET status='archived' WHERE category LIKE 'SOP Chapter 1:%'")).result()
    assert f.client.post(f.url() + '/quiz', headers=h, json=body).status_code == 404
    service.db.submit(lambda db: db.execute("UPDATE posts SET status='published' WHERE category LIKE 'SOP Chapter 1:%'")).result()
    service.db.submit(lambda db: db.execute('UPDATE sessions SET revoked=1')).result()
    assert f.client.post(f.url() + '/quiz', headers=h, json=body).status_code == 403
    assert count(service, 'quiz_attempt') == 1


def test_real_final_exam_and_saved_name_keep_existing_rules(training):
    f = training; m = f.model; h = f.headers(); service = f.sources[m.s].service
    assert f.client.get(f.url(0) + '/quiz', headers=h).status_code == 423
    service.db.submit(lambda db: import_events(db, [dict(event_id='chapter-nine-pass', event='quiz_attempt',
        ts=1700000000, email=EMAIL, detail=dict(chapter=9, score=4, total=5, passed=True))])).result()
    opened = f.client.get(f.url(0) + '/quiz', headers=h)
    assert opened.status_code == 200, opened.text
    assert opened.json()['total'] == 20 and opened.json()['pass_mark'] == 16
    choices = {q['id']: q['answer'] for q in service.final_exam[0]['questions']}
    body = dict(operation_id=str(uuid.uuid4()), version=opened.json()['version'], answers=choices)
    passed = f.client.post(f.url(0) + '/quiz', headers=h, json=body)
    assert passed.status_code == 200 and passed.json()['passed'] is True
    certificate = f.client.get(f.url(0) + '/certificate', headers=h).json()['certificate']
    rename = dict(operation_id=str(uuid.uuid4()), name='Verified Reader')
    named = f.client.post(f.url(0) + '/certificate/name', headers=h, json=rename)
    assert named.status_code == 200, named.text
    assert named.json()['number'] == certificate['number'] and named.json()['awarded_at'] == certificate['awarded_at']
    assert named.json()['name'] == 'Verified Reader'
    assert f.client.post(f.url(0) + '/certificate/name', headers=h, json=rename).json() == named.json()
    assert count(service, 'certificate_name') == 1
    assert f.client.get(f.url(0, m.t) + '/certificate', headers=f.headers(m.t)).json()['certificate'] is None


def test_actual_legacy_page_delivery_opens_native_chapter_quiz(training):
    f = training; m = f.model; h = f.headers(); service = f.sources[m.s].service
    item, resource = str(uuid.uuid4()), str(uuid.uuid4())
    service.db.submit(lambda db: db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) "
        "SELECT ?,id,'lesson.pdf','pdf',?,1,1,'ready',0 FROM posts WHERE category LIKE 'SOP Chapter 1:%'", (item, 'a'*64))).result()
    directory = service.config.data_dir / 'derived' / item; directory.mkdir()
    (directory / 'page-0.jpg').write_bytes((Path(__file__).parents[1] / 'fixtures' / 'page.jpg').read_bytes())
    (directory / 'manifest.json').write_text(json.dumps({'pages': 1, 'bytes': 1}))
    m.db.execute(f"INSERT INTO postern.resources(space_id,id,kind,title,state,adapter) VALUES('{m.s}','{resource}','document','Lesson','published','legacy');"
        f"INSERT INTO postern.legacy_map VALUES('{m.s}','{f.installations[m.s]}','item','{item}','{resource}');"
        f"INSERT INTO postern.grants(space_id,resource_id,all_members,actions) VALUES('{m.s}','{resource}',true,ARRAY['discover','view']);")
    opened = f.client.get(f.url() + '/quiz', headers=h)
    assert opened.status_code == 200 and opened.json()['ready'] is False
    assert opened.json()['unfinished'] == [resource] and item not in opened.text
    legacy = f.legacy_clients[m.s]; legacy_headers = f.legacy_headers[m.s]
    view = legacy.post(f'/api/items/{item}/views', headers=legacy_headers, json={'attempt_id': str(uuid.uuid4())})
    assert view.status_code == 200, view.text
    page = legacy.get(f"/api/views/{view.json()['id']}/page/0.jpg", headers=legacy_headers)
    assert page.status_code == 200 and page.headers['content-type'] == 'image/jpeg' and len(page.content) > 100
    assert count(service, 'page_served') == 1
    passed, _ = submit(f, h)
    assert passed.status_code == 200 and passed.json()['passed'] is True
    assert f.client.get(f.url() + '/progress', headers=h).json()['standing']['passed'] is True
    assert f.client.get(f.url(space=m.t) + '/progress', headers=f.headers(m.t)).json()['standing'] is None


def test_native_revocation_waits_for_admitted_mutation(training, monkeypatch):
    f = training; m = f.model; h = f.headers(); service = f.sources[m.s].service
    opened = f.client.get(f.url() + '/quiz', headers=h).json()
    body = dict(operation_id=str(uuid.uuid4()), version=opened['version'], answers=answers(f, opened))
    entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
    original = quiz.grade
    def blocked(*args, **kwargs):
        entered.set(); assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(quiz, 'grade', blocked)
    result = []
    worker = threading.Thread(target=lambda: result.append(f.client.post(f.url() + '/quiz', headers=h, json=body)))
    worker.start(); assert entered.wait(5)
    digest = hashlib.sha256(h['Authorization'][7:].encode()).hexdigest()
    def revoke():
        m.db.execute(f"UPDATE postern.resource_sessions SET revoked=true WHERE token_hash='{digest}';")
        revoked.set()
    revoker = threading.Thread(target=revoke); revoker.start()
    try:
        assert not revoked.wait(.15)
    finally:
        release.set(); worker.join(5); revoker.join(5)
    assert revoked.is_set() and result[0].status_code == 200
    assert f.client.get(f.url() + '/progress', headers=h).status_code == 401
    assert count(service, 'quiz_attempt') == 1


def test_audit_failure_recovers_same_operation_without_duplicate_attempt(training):
    f = training; m = f.model; h = f.headers(); service = f.sources[m.s].service
    opened = f.client.get(f.url() + '/quiz', headers=h).json()
    body = dict(operation_id=str(uuid.uuid4()), version=opened['version'], answers=answers(f, opened))
    m.db.execute("CREATE FUNCTION postern.training_test_audit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.action='training.disclosed' THEN RAISE EXCEPTION 'fixture audit failure'; END IF; RETURN NEW; END $$;"
        "CREATE TRIGGER training_test_failure BEFORE INSERT ON postern.activity FOR EACH ROW EXECUTE FUNCTION postern.training_test_audit_failure();")
    try:
        response = f.client.post(f.url() + '/quiz', headers=h, json=body)
        assert response.status_code == 503, response.text
        assert count(service, 'quiz_attempt') == 1
        assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='training.requested' AND detail->>'operation_id'='{body['operation_id']}';") == '1'
    finally:
        m.db.execute('DROP TRIGGER training_test_failure ON postern.activity; DROP FUNCTION postern.training_test_audit_failure();')
    retry = f.client.post(f.url() + '/quiz', headers=h, json=body)
    assert retry.status_code == 200 and retry.json()['passed'] is True
    assert count(service, 'quiz_attempt') == 1
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='training.disclosed' AND detail->>'operation_id'='{body['operation_id']}';") == '1'


@pytest.mark.parametrize('tier', ['events', 'summary'])
def test_event_only_recovery_never_reexecutes_lost_response(training, runtime, tmp_path, tier):
    f = training; m = f.model; h = f.headers(); service = f.sources[m.s].service
    passed, body = submit(f, h)
    assert passed.status_code == 200
    f.legacy_clients[m.s].portal.call(service.export)
    if tier == 'summary':
        rows = json.loads((service.store.root / 'summary.json').read_text())
    else:
        paths = list((service.config.data_dir / 'exports').glob('*.jsonl'))
        assert len(paths) == 1
        rows = [json.loads(line) for line in paths[0].read_text().splitlines()]
    assert any(row['event'] == 'space_training_operation' and row['detail']['operation_id'] == body['operation_id'] for row in rows)
    config = Config(data_dir=tmp_path / 'event-restored', mode='demo', notice_version='1')
    store = FakeStore(tmp_path / 'event-restored-cloud')
    store.write_allowlist([['email', 'name', 'role'], [EMAIL, 'Reader Person', 'reader']])
    restored = Service(config, store)
    def recover(db):
        for row in service.db.read(lambda source: [dict(row) for row in source.execute('SELECT * FROM posts')]):
            columns = ','.join(row)
            db.execute(f"INSERT INTO posts({columns}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
        import_events(db, rows)
    restored.db.submit(recover).result()
    spaces = SpaceService(runtime, f.trust)
    bridge = SpaceTraining(spaces, {m.s: LegacyTrainingSource(f.installations[m.s], restored)})
    with TestClient(create_app(config, service=restored, spaces=spaces, training=bridge)) as client:
        signin = client.post('/api/auth/demo', json={'email': EMAIL, 'name': 'Reader Person'})
        legacy = {'Authorization': 'Bearer ' + signin.json()['token']}
        assert client.post('/api/consent', headers=legacy, json={
            'version': '1', 'ip_version': 'ip-1', 'monitoring_accepted': True, 'ip_accepted': True}).status_code == 200
        fresh = {**f.headers(), 'X-Postern-Legacy-Authorization': legacy['Authorization']}
        response = client.post(f.url() + '/quiz', headers=fresh, json=body)
        assert response.status_code == 409 and response.json()['error'] == 'operation_recovery_required'
        assert count(restored, 'quiz_attempt') == 1
        assert restored.db.read(lambda db: db.execute('SELECT count(*) FROM space_training_operations').fetchone()[0]) == 0


def test_cancellation_drains_write_before_releasing_native_lease(training, monkeypatch):
    f = training; m = f.model; h = f.headers(); service = f.sources[m.s].service
    opened = f.client.get(f.url() + '/quiz', headers=h).json()
    body = dict(operation_id=str(uuid.uuid4()), version=opened['version'], answers=answers(f, opened))
    entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
    original = quiz.grade
    def blocked(*args, **kwargs):
        entered.set(); assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(quiz, 'grade', blocked)
    request = Request({'type': 'http', 'method': 'POST', 'path': f.url() + '/quiz',
                       'headers': [(k.lower().encode(), v.encode()) for k, v in h.items()], 'client': ('127.0.0.1', 1)})
    async def action():
        return await f.bridge.run(request, m.s, f.ids[m.s][1], 1, 'submit', operation_id=body['operation_id'],
                                  payload={k: v for k, v in body.items() if k != 'operation_id'})
    future = f.client.portal.start_task_soon(action)
    assert entered.wait(5)
    future.cancel()
    digest = hashlib.sha256(h['Authorization'][7:].encode()).hexdigest()
    def revoke():
        m.db.execute(f"UPDATE postern.resource_sessions SET revoked=true WHERE token_hash='{digest}';")
        revoked.set()
    revoker = threading.Thread(target=revoke); revoker.start()
    try:
        assert not revoked.wait(.15)
    finally:
        release.set(); revoker.join(5)
    assert revoked.is_set()
    assert count(service, 'quiz_attempt') == 1
    retry = f.client.post(f.url() + '/quiz', headers=f.headers(), json=body)
    assert retry.status_code == 200 and retry.json()['passed'] is True
    assert count(service, 'quiz_attempt') == 1


def test_cancelled_queued_mutation_never_starts_after_native_lease_release(training, monkeypatch):
    f = training; m = f.model; h = f.headers(); service = f.sources[m.s].service
    opened = f.client.get(f.url() + '/quiz', headers=h).json()
    body = dict(operation_id=str(uuid.uuid4()), version=opened['version'], answers=answers(f, opened))
    queued, release, started = threading.Event(), threading.Event(), threading.Event()
    submit_original = service.db.submit
    calls = 0; workers = []
    def intercept(operation):
        nonlocal calls
        if operation.__name__ == 'bounded_transaction':
            calls += 1
            # First call checks identity for completed(); second is the actual
            # submission transaction. Put unrelated work ahead of that mutation.
            if calls == 2:
                def occupy(db):
                    assert release.wait(5)
                submit_original(occupy)
                def observed(db):
                    started.set()
                    return operation(db)
                worker = submit_original(observed)
                workers.append(worker); queued.set()
                return worker
        return submit_original(operation)
    monkeypatch.setattr(service.db, 'submit', intercept)
    request = Request({'type': 'http', 'method': 'POST', 'path': f.url() + '/quiz',
                       'headers': [(k.lower().encode(), v.encode()) for k, v in h.items()], 'client': ('127.0.0.1', 1)})
    async def action():
        return await f.bridge.run(request, m.s, f.ids[m.s][1], 1, 'submit', operation_id=body['operation_id'],
                                  payload={k: v for k, v in body.items() if k != 'operation_id'})
    future = f.client.portal.start_task_soon(action)
    assert queued.wait(5)
    future.cancel()
    digest = hashlib.sha256(h['Authorization'][7:].encode()).hexdigest()
    try:
        # Lock acquisition must succeed while the unrelated SQLite work is still
        # blocked: cancelling an unstarted mutation releases its native lease.
        response = m.db.execute(f"SET lock_timeout='1s'; UPDATE postern.resource_sessions SET revoked=true WHERE token_hash='{digest}';", check=False)
        assert response.returncode == 0, response.stderr
        assert workers[0].cancelled() and not started.is_set()
    finally:
        release.set()
    # Drain the queue: its set_running_or_notify_cancel must skip the mutation.
    submit_original(lambda db: None).result(timeout=5)
    assert not started.is_set() and count(service, 'quiz_attempt') == 0
    retry = f.client.post(f.url() + '/quiz', headers=f.headers(), json=body)
    assert retry.status_code == 200 and count(service, 'quiz_attempt') == 1


def test_real_restored_databases_preserve_history_and_reject_old_authority(training, runtime, tmp_path):
    f = training; m = f.model; h = f.headers(); service = f.sources[m.s].service
    rows = [dict(event_id='stable-restored-exam', event='quiz_attempt', ts=1700000000, email=EMAIL, name='Original Person',
                 detail=dict(chapter=0, score=20, total=20, passed=True)),
            dict(event_id='stable-restored-name', event='certificate_name', ts=1700000001, email=EMAIL,
                 detail=dict(name='Saved Person', saved_at=1700000001))]
    service.db.submit(lambda db: import_events(db, rows)).result()
    certificate = f.client.get(f.url(0) + '/certificate', headers=h).json()['certificate']
    # SQLite online backup includes original events, sessions and retry journal.
    recovered_dir = tmp_path / 'restored-legacy'; recovered_dir.mkdir()
    destination = sqlite3.connect(recovered_dir / 'postern.sqlite3')
    try:
        service.db.read(lambda db: db.backup(destination))
    finally:
        destination.close()
    dump = tmp_path / 'training.dump'
    subprocess.run(['pg_dump', '-Fc', '-d', 'postgres', '-f', str(dump)], env=m.db.env, check=True, capture_output=True)
    name = 'training_restore_' + uuid.uuid4().hex[:12]
    recovered = restore_quarantined(m.db, dump, name)
    assert recovered.scalar(f"SELECT count(*) FROM postern.training_presentations WHERE space_id='{m.s}';") == '4'
    recovered.execute('GRANT CONNECT ON DATABASE "' + name + '" TO training_api_test;')
    spaces = SpaceService({**runtime, 'dbname': name}, f.trust)
    store = FakeStore(tmp_path / 'restored-cloud')
    store.write_allowlist([['email', 'name', 'role'], [EMAIL, 'Reader Person', 'reader']])
    config = Config(data_dir=recovered_dir, mode='demo', notice_version='1')
    restored_service = Service(config, store)
    bridge = SpaceTraining(spaces, {m.s: LegacyTrainingSource(f.installations[m.s], restored_service)})
    with TestClient(create_app(config, service=restored_service, spaces=spaces, training=bridge)) as client:
        assert client.get(f.url(0) + '/certificate', headers=h).status_code == 401
        # Explicit fixture reconciliation. This is never automatic restore behavior.
        recovered.execute("UPDATE postern.authority SET state='active'; UPDATE postern.spaces SET state='active';")
        assert client.get(f.url(0) + '/certificate', headers=h).status_code == 401
        now = int(time.time()); login = str(uuid.uuid4())
        recovered.execute(f"SELECT postern.register_login('{login}','{m.b}',{now+3600});")
        generation = recovered.scalar('SELECT generation FROM postern.authority;')
        claims = dict(iss=f.trust.issuer, aud=f.trust.audience, sub=m.b, iat=now, nbf=now, exp=now+60,
            jti=str(uuid.uuid4()), login_id=login, space_id=m.s, generation=generation, membership_id=m.mb,
            account_authority='fixture', account_id=m.b)
        assertion = jwt.encode(claims, f.key, algorithm='EdDSA', headers={'kid': f.trust.kid, 'typ': 'postern-space+jwt'})
        exchange = client.post(f'/v1/spaces/{m.s}/session/exchange', json={'assertion': assertion})
        assert exchange.status_code == 200, exchange.text
        fresh = {**h, 'Authorization': 'Bearer ' + exchange.json()['token']}
        response = client.get(f.url(0) + '/certificate', headers=fresh)
        assert response.status_code == 200, response.text
        assert response.json()['certificate'] == certificate
        assert client.get(f.url(0, m.t) + '/certificate', headers=fresh).status_code == 404
