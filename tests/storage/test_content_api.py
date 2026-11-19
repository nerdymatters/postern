"""Real authority + immutable local objects + HTTP delivery and revocation races."""
import asyncio
from dataclasses import asdict, replace
import hashlib
import io
import json
import secrets
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
import jwt
from PIL import Image
import pytest
import uvicorn

from postern.app import create_app
from postern.config import Config
from postern.content_delivery import ContentService, NativeReadingPolicy, DenyContent
from postern.content_objects import ContentObject, LocalContentObjects, LocalContentPublisher
from postern.content_prepare import prepare_content
from postern.content_render import render_page
from postern.pg import literal, restore_quarantined
from postern.spaces import SpaceError, SpaceService, SpaceTrust
from postern.storage import FakeStore


@pytest.fixture(scope='module')
def http_runtime(db):
    db.execute('CREATE ROLE content_http_test LOGIN IN ROLE postern_resource_service;')
    return dict(host=db.env['PGHOST'], port=int(db.env['PGPORT']), user='content_http_test', dbname='postgres')


@pytest.fixture
def api(model, http_runtime, tmp_path):
    m = model
    root = (tmp_path/'objects').resolve(); root.mkdir(mode=0o700)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    trust = SpaceTrust('urn:content:fixture', 'urn:content:resources', 'test-key', public, secrets.token_bytes(32))
    spaces = SpaceService(http_runtime, trust, max_size=2)
    objects = LocalContentObjects(root)
    content = ContentService(spaces, objects, policy=NativeReadingPolicy([m.s, m.t]))
    store = FakeStore(tmp_path/'cloud'); store.write_allowlist([['email', 'name', 'role']])
    f = SimpleNamespace(model=m, root=root, spaces=spaces, content=content, objects=objects)

    def register(source, kind, meta, inventory):
        space, resource, revision = source.space_id, source.resource_id, source.revision
        m.db.execute(f"BEGIN; UPDATE postern.resources SET adapter='native-content-v1',kind={literal(kind)},content_revision={revision} WHERE space_id='{space}' AND id='{resource}';"
                     f"INSERT INTO postern.objects VALUES('{space}','{resource}',{revision},{literal(source.key)},'{source.sha256}',{source.size},now());"
                     f"SELECT postern.publish_content('{space}','{resource}',{revision},{literal(kind)},{literal(json.dumps(meta))}::jsonb,{literal(json.dumps(inventory))}::jsonb); COMMIT;")
    f.register = register

    def publish(resource=None, space=None, kind='document', data=None, chunk_bytes=4):
        resource, space = resource or m.r, space or m.s
        if data is None:
            image = Image.new('RGB', (640, 480), 'white')
            out = io.BytesIO(); image.save(out, 'JPEG'); data = out.getvalue()
        original = b'%PDF-1.4\nsynthetic preserved source\n'
        descriptors = []
        with LocalContentPublisher(root) as writer:
            source = ContentObject(space, resource, 1, 'original', 0, hashlib.sha256(original).hexdigest(), len(original))
            writer.publish(source, original)
            parts = [('page', 0, data), ('thumbnail', 0, data)] if kind == 'document' else [
                ('video', i, data[offset:offset+chunk_bytes]) for i, offset in enumerate(range(0, len(data), chunk_bytes))]
            for representation, index, value in parts:
                descriptor = ContentObject(space, resource, 1, representation, index, hashlib.sha256(value).hexdigest(), len(value))
                writer.publish(descriptor, value); descriptors.append(descriptor)
        meta = {'page_count': 1} if kind == 'document' else {
            'duration_ms': 1500, 'total_bytes': len(data), 'chunk_bytes': chunk_bytes, 'chunk_count': len(descriptors)}
        inventory = [{k: v for k, v in asdict(x).items() if k not in {'space_id', 'resource_id', 'revision'}} for x in descriptors]
        register(source, kind, meta, inventory)
        return descriptors
    f.publish = publish
    f.pages = publish()
    f.url = f'/v1/spaces/{m.s}/resources/{m.r}'
    f.app = create_app(Config(data_dir=tmp_path/'data', mode='demo'), store=store, spaces=spaces, content=content)
    with TestClient(f.app) as client:
        f.client = client
        def session(actor='b', space=None, target=None):
            space = space or m.s
            login = str(uuid.uuid4()); now = int(time.time())
            m.db.execute(f"SELECT postern.register_login('{login}','{getattr(m,actor)}',{now+3600});")
            membership = m.db.scalar(f"SELECT id FROM postern.memberships WHERE space_id='{space}' AND principal_id='{getattr(m,actor)}' AND state='active';")
            claims = dict(iss=trust.issuer, aud=trust.audience, sub=getattr(m, actor), iat=now, nbf=now, exp=now+60,
                          jti=str(uuid.uuid4()), login_id=login, space_id=space, generation=m.generation,
                          account_authority='fixture', account_id=getattr(m, actor), membership_id=membership)
            assertion = jwt.encode(claims, key, algorithm='EdDSA', headers={'kid': trust.kid, 'typ': 'postern-space+jwt'})
            response = (target or client).post(f'/v1/spaces/{space}/session/exchange', json={'assertion': assertion})
            assert response.status_code == 200, response.text
            return {'Authorization': 'Bearer '+response.json()['token']}
        f.session = session
        yield f
    objects.close()


def test_document_stamp_thumbnail_and_no_key_or_original_disclosure(api):
    f = api; h = f.session()
    assert f.client.get(f.url+'/document?revision=1', headers=h).json() == {
        'resource_id': f.model.r, 'revision': 1, 'page_count': 1}
    for suffix in ('/document/pages/0.jpg', '/thumbnails/0.jpg'):
        response = f.client.get(f.url+suffix+'?revision=1', headers=h)
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert 'etag' not in response.headers
        image = Image.open(io.BytesIO(response.content))
        assert image.format == 'JPEG'
        assert response.content != f.objects.read(f.pages[0])
    for suffix in ('/original', '/download', '/export', '/objects/'+f.pages[0].sha256):
        response = f.client.get(f.url+suffix, headers=f.session('a'))
        assert response.status_code == 404
    assert f.model.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{f.model.s}' AND action='content.disclosed';") == '2'


@pytest.mark.parametrize('suffix', ['/document', '/document/pages/0.jpg', '/thumbnails/0.jpg'])
def test_warmed_content_rechecks_membership_and_no_conditionals(api, suffix):
    f = api; m = f.model; h = f.session(); path = f.url+suffix+'?revision=1'
    assert f.client.get(path, headers=h).status_code == 200
    m.db.execute(f"UPDATE postern.memberships SET state='revoked' WHERE space_id='{m.s}' AND id='{m.mb}';")
    for headers in (h, {**h, 'If-None-Match': '*'}, {**h, 'If-Modified-Since': 'Wed, 18 Nov 2026 23:00:00 GMT'}):
        response = f.client.get(path, headers=headers)
        assert response.status_code in {400, 401, 404}
        assert response.headers['cache-control'] == 'no-store'
        assert response.status_code != 304 and not response.content.startswith(b'\xff\xd8')


def test_default_deny_policy_and_live_policy_recheck(api):
    f = api; h = f.session()
    f.content.policy = DenyContent()
    assert f.client.get(f.url+'/document?revision=1', headers=h).status_code == 404
    assert f.client.get(f.url+'/document/pages/0.jpg?revision=1', headers=h).status_code == 404
    class Policy:
        calls = 0
        async def authorize(self, request, snapshot):
            self.calls += 1
            if self.calls > 1:
                raise SpaceError(403, 'consent_required', 'Consent is required.')
    policy = Policy(); f.content.policy = policy
    response = f.client.get(f.url+'/document/pages/0.jpg?revision=1', headers=h)
    assert response.status_code == 403 and policy.calls == 2


@pytest.mark.parametrize('change', ['membership', 'grant', 'revision', 'archive', 'generation', 'audit'])
def test_change_during_render_releases_no_pixels(api, change):
    f = api; m = f.model; h = f.session()
    async def racing_renderer(*args, **kwargs):
        data = await render_page(*args, **kwargs)
        sql = {
            'membership': f"UPDATE postern.memberships SET state='revoked' WHERE id='{m.mb}';",
            'grant': f"UPDATE postern.grants SET state='revoked' WHERE space_id='{m.s}' AND resource_id='{m.r}';",
            'revision': f"UPDATE postern.resources SET content_revision=2 WHERE space_id='{m.s}' AND id='{m.r}';",
            'archive': f"UPDATE postern.resources SET state='archived' WHERE space_id='{m.s}' AND id='{m.r}';",
            'generation': 'UPDATE postern.authority SET generation=gen_random_uuid();',
            'audit': "CREATE FUNCTION postern.fail_content_http_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected audit failure'; END $$; CREATE TRIGGER fail_content_http_audit BEFORE INSERT ON postern.activity FOR EACH ROW EXECUTE FUNCTION postern.fail_content_http_audit();",
        }[change]
        await asyncio.to_thread(m.db.execute, sql)
        return data
    f.content.renderer = racing_renderer
    try:
        response = f.client.get(f.url+'/document/pages/0.jpg?revision=1', headers=h)
        assert response.status_code in {401, 404, 503}, response.text
        assert not response.content.startswith(b'\xff\xd8')
        assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='content.disclosed';") == '0'
    finally:
        if change == 'audit':
            m.db.execute('DROP TRIGGER fail_content_http_audit ON postern.activity; DROP FUNCTION postern.fail_content_http_audit();')


def test_two_spaces_identical_hashes_do_not_share_authority(api):
    f = api; m = f.model
    f.publish(resource=m.foreign, space=m.t)
    h = f.session(); foreign = f'/v1/spaces/{m.t}/resources/{m.foreign}'
    assert f.client.get(f.url+'/document/pages/0.jpg?revision=1', headers=h).status_code == 200
    for base, headers in [(foreign, h), (f'/v1/spaces/{m.s}/resources/{m.foreign}', h),
                          (f.url, f.session('c', m.t))]:
        response = f.client.get(base+'/document/pages/0.jpg?revision=1', headers=headers)
        assert response.status_code in {401, 404}
    assert f.client.get(foreign+'/document/pages/0.jpg?revision=1', headers=f.session('c', m.t)).status_code == 200


def test_corrupt_prepared_bytes_never_fall_back_to_original(api):
    f = api; h = f.session(); descriptor = f.pages[0]
    path = f.root/descriptor.key
    path.chmod(0o600)  # Simulate offline corruption past the create-only publisher.
    path.write_bytes(b'x' * descriptor.size)
    response = f.client.get(f.url+'/document/pages/0.jpg?revision=1', headers=h)
    assert response.status_code == 503
    assert 'PDF' not in response.text and str(f.root) not in response.text


def test_video_ranges_tickets_and_revocation(api):
    f = api; m = f.model; data = b'abcdefghijklmnopqrstuv'
    f.publish(resource=m.hidden, kind='video', data=data)
    h = f.session('a'); base = f'/v1/spaces/{m.s}/resources/{m.hidden}/video'
    response = f.client.get(base+'/content.mp4?revision=1', headers={**h, 'Range': 'bytes=3-13'})
    assert response.status_code == 206 and response.content == data[3:14]
    assert response.headers['content-range'] == f'bytes 3-13/{len(data)}'
    token = f.client.post(base+'/ticket?revision=1', headers=h).json()['ticket']
    path = base+'/content.mp4?revision=1&ticket='+token
    assert f.client.get(path).content == data
    assert f.client.get(path.replace(m.hidden, m.r)).status_code == 401
    assert f.client.get(path.replace('revision=1', 'revision=2')).status_code == 401
    assert f.client.get(path, headers={'If-None-Match': '*'}).status_code == 400
    m.db.execute('UPDATE postern.authority SET generation=gen_random_uuid();')
    assert f.client.get(path).status_code == 401
    assert f.client.get(base+'/content.mp4?revision=1', headers=h).status_code == 401


def test_media_ticket_expiry_during_first_object_read_releases_no_bytes(api):
    f = api; m = f.model
    f.publish(resource=m.hidden, kind='video', data=b'abcdefghijkl')
    base = f'/v1/spaces/{m.s}/resources/{m.hidden}/video'
    token = f.client.post(base+'/ticket?revision=1', headers=f.session('a')).json()['ticket']
    original = f.content._read
    async def delayed(descriptor):
        data = await original(descriptor)
        key = hashlib.sha256(token.encode()).hexdigest()
        f.content._tickets[key] = replace(f.content._tickets[key], expires=time.monotonic()-1)
        return data
    f.content._read = delayed
    response = f.client.get(base+'/content.mp4?revision=1&ticket='+token)
    assert response.status_code == 401 and b'abcdefghijkl' not in response.content


def test_media_ticket_expiry_during_later_chunk_aborts_without_that_chunk(api):
    f = api; m = f.model
    f.publish(resource=m.hidden, kind='video', data=b'abcdefghijkl')
    base = f'/v1/spaces/{m.s}/resources/{m.hidden}/video'
    token = f.client.post(base+'/ticket?revision=1', headers=f.session('a')).json()['ticket']
    original = f.content._read
    async def delayed(descriptor):
        data = await original(descriptor)
        if descriptor.index == 1:
            key = hashlib.sha256(token.encode()).hexdigest()
            f.content._tickets[key] = replace(f.content._tickets[key], expires=time.monotonic()-1)
        return data
    f.content._read = delayed
    async def transfer():
        messages = []
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            messages.append(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                 'http_version': '1.1', 'method': 'GET', 'scheme': 'http',
                 'path': base+'/content.mp4', 'raw_path': (base+'/content.mp4').encode(),
                 'query_string': ('revision=1&ticket='+token).encode(), 'root_path': '',
                 'headers': [], 'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 80)}
        with pytest.raises(RuntimeError, match='incomplete content response|response already started'):
            await f.app(scope, receive, send)
        return messages
    messages = f.client.portal.call(transfer)
    assert messages[0]['status'] == 200
    assert b''.join(value.get('body', b'') for value in messages) == b'abcd'
    assert not any(value['type'] == 'http.response.body' and not value.get('more_body', False) for value in messages)


@pytest.mark.parametrize('value', ['bytes=0-1,3-4', 'bytes=-0', 'bytes=22-', 'bytes=5-2', 'nope'])
def test_bad_video_ranges_fail_closed(api, value):
    f = api; m = f.model
    f.publish(resource=m.hidden, kind='video', data=b'abcdefgh')
    response = f.client.get(f'/v1/spaces/{m.s}/resources/{m.hidden}/video/content.mp4?revision=1',
                            headers={**f.session('a'), 'Range': value})
    assert response.status_code == 416


def test_actual_slow_socket_stops_emission_and_releases_database(api):
    """Run the complete middleware stack on real TCP, beyond TestClient buffering."""
    f = api; m = f.model
    payload = b'v' * (48 * 1024 * 1024)
    f.publish(resource=m.hidden, kind='video', data=payload, chunk_bytes=8 * 1024 * 1024)
    m.db.execute(f"INSERT INTO postern.grants(space_id,resource_id,membership_id,actions) VALUES('{m.s}','{m.hidden}','{m.mb}',ARRAY['discover','view']);")
    h = f.session()
    finished = threading.Event()
    failures = []
    async def observed(scope, receive, send):
        try:
            await f.app(scope, receive, send)
        except BaseException as exc:
            failures.append(type(exc).__name__)
            raise
        finally:
            if scope['type'] == 'http': finished.set()
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(2)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(observed, lifespan='off', log_level='critical', access_log=False))
    future = f.client.portal.start_task_soon(server.serve, [listener])
    async def started():
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(.01)
    f.client.portal.call(started)
    slow = socket.socket()
    try:
        slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        slow.settimeout(5); slow.connect(('127.0.0.1', port))
        path = f'/v1/spaces/{m.s}/resources/{m.hidden}/video/content.mp4?revision=1'
        slow.sendall(f'GET {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: {h["Authorization"]}\r\nConnection: close\r\n\r\n'.encode())
        header = bytearray()
        while not header.endswith(b'\r\n\r\n'):
            header += slow.recv(1)
            assert len(header) < 16384
        assert header.startswith(b'HTTP/1.1 200')
        began = time.monotonic()
        # The HTTP writer must not hold a membership/space lock while blocked on TCP.
        m.db.execute(f"UPDATE postern.memberships SET state='revoked' WHERE id='{m.mb}';")
        assert time.monotonic() - began < 1
        assert finished.wait(5), 'slow socket kept content producer alive'
        assert time.monotonic() - began < 5
        assert failures, 'large incomplete response falsely completed successfully'
        assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='content.disclosed';") == '1'
        assert f.client.get(path, headers=h).status_code == 401
    finally:
        slow.close(); server.should_exit = True
        future.result(timeout=5); listener.close()


def test_real_pdf_and_video_from_converter_to_restricted_http(api, tmp_path):
    f = api; m = f.model
    source = io.BytesIO()
    with Image.new('RGB', (120, 180), 'blue') as image:
        image.save(source, format='PDF')
    with LocalContentPublisher(f.root) as publisher:
        document = prepare_content(publisher, space_id=m.s, resource_id=m.hidden, revision=1,
                                   kind='document', source=source.getvalue())
    f.register(document.original, 'document', document.manifest['metadata'], document.inventory)
    owner = f.session('a'); base = f'/v1/spaces/{m.s}/resources/{m.hidden}'
    response = f.client.get(base+'/document/pages/0.jpg?revision=1', headers=owner)
    assert response.status_code == 200, response.text
    assert Image.open(io.BytesIO(response.content)).format == 'JPEG'
    assert f.objects.read(document.original) == source.getvalue()
    assert f.client.get(base+'/document?revision=1', headers=owner).json()['page_count'] == 1
    assert f.client.get(base+'/original', headers=owner).status_code == 404

    clip = tmp_path/'input.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=12',
                    '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)],
                   check=True, capture_output=True, timeout=30)
    with LocalContentPublisher(f.root) as publisher:
        video = prepare_content(publisher, space_id=m.t, resource_id=m.foreign, revision=1,
                                kind='video', source=clip.read_bytes(), chunk_bytes=1024)
    f.register(video.original, 'video', video.manifest['metadata'], video.inventory)
    base = f'/v1/spaces/{m.t}/resources/{m.foreign}/video'
    response = f.client.get(base+'/content.mp4?revision=1', headers=f.session('c', m.t))
    assert response.status_code == 200
    delivered = tmp_path/'delivered.mp4'; delivered.write_bytes(response.content)
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', str(delivered)],
                           check=True, capture_output=True, timeout=15)
    assert probe.stdout.strip() == b'h264'
    assert f.client.get(base+'/content.mp4?revision=1', headers=owner).status_code == 401


def test_restored_objects_preserve_ids_history_and_require_current_authority(api, http_runtime, tmp_path):
    f = api; m = f.model; old = f.session()
    foreign = f.publish(resource=m.foreign, space=m.t)
    assert f.client.get(f.url+'/document/pages/0.jpg?revision=1', headers=old).status_code == 200
    queries = {table: f"SELECT jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text) FROM postern.{table} x WHERE space_id IN ('{m.s}','{m.t}');"
               for table in ('objects', 'content_presentations', 'content_objects', 'activity')}
    inventory = {table: m.db.scalar(query) for table, query in queries.items()}
    dump = tmp_path/'before-revoke.dump'
    subprocess.run(['pg_dump', '-Fc', '-d', 'postgres', '-f', str(dump)], env=m.db.env, check=True, capture_output=True, timeout=30)
    # A revoke after the snapshot must be reconciled into the closed restore.
    m.db.execute(f"UPDATE postern.memberships SET state='revoked' WHERE id='{m.mb}';")
    restored = restore_quarantined(m.db, dump, 'http_restore_'+uuid.uuid4().hex)
    assert {table: restored.scalar(query) for table, query in queries.items()} == inventory
    root = (tmp_path/'restored-objects').resolve(); shutil.copytree(f.root, root)
    with LocalContentObjects(root) as reader:
        for descriptor in [*f.pages, *foreign]:
            assert reader.read(descriptor) == f.objects.read(descriptor)
        assert restored.scalar('SELECT state FROM postern.authority;') == 'quarantined'
        restored.execute(f"UPDATE postern.memberships SET state='revoked' WHERE id='{m.mb}';")
        # Fixture-only readmission: validate complete copied inventory and apply
        # current revocation above before enabling A. B remains independently closed.
        restored.execute(f"GRANT CONNECT ON DATABASE {restored.database} TO content_http_test; UPDATE postern.authority SET state='active'; UPDATE postern.spaces SET state='active' WHERE id='{m.s}';")
        spaces = SpaceService({**http_runtime, 'dbname': restored.database}, f.spaces.trust)
        content = ContentService(spaces, reader, policy=NativeReadingPolicy([m.s, m.t]))
        store = FakeStore(tmp_path/'restored-cloud'); store.write_allowlist([['email', 'name', 'role']])
        app = create_app(Config(data_dir=tmp_path/'restored-data', mode='demo'), store=store, spaces=spaces, content=content)
        m.db = restored; m.generation = restored.scalar('SELECT generation FROM postern.authority;')
        with TestClient(app) as client:
            path = f.url+'/document/pages/0.jpg?revision=1'
            assert client.get(path, headers=old).status_code == 401
            owner = f.session('a', target=client)
            assert client.get(path, headers=owner).status_code == 200
            assert restored.scalar(f"SELECT state FROM postern.memberships WHERE id='{m.mb}';") == 'revoked'
            assert restored.scalar(f"SELECT state FROM postern.spaces WHERE id='{m.t}';") == 'suspended'
            wrong = f'/v1/spaces/{m.t}/resources/{m.foreign}/document/pages/0.jpg?revision=1'
            assert client.get(wrong, headers=owner).status_code == 401
            restored.execute(f"UPDATE postern.spaces SET state='active' WHERE id='{m.t}';")
            assert client.get(wrong, headers=f.session('c', m.t, target=client)).status_code == 200
            assert client.get(path, headers=f.session('c', m.t, target=client)).status_code == 401
            (root/f.pages[0].key).unlink()
            assert client.get(path, headers=owner).status_code == 503
