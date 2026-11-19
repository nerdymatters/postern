"""Native bytes authority uses real PostgreSQL and its restricted runtime role.

Object I/O and HTTP are covered separately; these tests exercise the publication,
scope, transaction and audit boundary without replacing permission decisions.
"""
import copy
import hashlib
import json
import secrets
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import psycopg

from postern.pg import MigrationError, Postgres, literal, restore_quarantined


def sql(value):
    if isinstance(value, (dict, list)):
        return literal(json.dumps(value))+'::jsonb'
    return literal(value)


def call(db, name, *args):
    uuid_args = {0, 1} if name == 'publish_content' else {1, 3}
    parameters = [sql(value)+('::uuid' if index in uuid_args else '') for index, value in enumerate(args)]
    try:
        return db.scalar('SELECT postern.'+name+'('+','.join(parameters)+');')
    except MigrationError as error:
        # These clusters contain only the synthetic rows constructed below.
        error.add_note(error.details)
        raise


@pytest.fixture(scope='module')
def content_runtime(db):
    db.execute('CREATE ROLE content_schema_test LOGIN IN ROLE postern_resource_service;')
    return Postgres({**db.env, 'PGUSER': 'content_schema_test'})


@pytest.fixture
def content(model, content_runtime):
    m = model
    m.runtime = content_runtime
    m.audience = 'urn:fixture:content'

    def session(actor='b', space=None):
        space = space or m.s
        login, assertion = str(uuid.uuid4()), str(uuid.uuid4())
        token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        principal = getattr(m, actor)
        m.db.execute(f"SELECT postern.register_login('{login}','{principal}',floor(extract(epoch FROM now()+interval '1 hour'))::bigint);")
        m.db.execute(f"INSERT INTO postern.resource_sessions(token_hash,login_id,space_id,membership_id,generation,audience,assertion_id,expires_at) "
                     f"SELECT '{token_hash}','{login}','{space}',id,'{m.generation}',{sql(m.audience)},'{assertion}',now()+interval '10 minutes' "
                     f"FROM postern.memberships WHERE space_id='{space}' AND principal_id='{principal}' AND state='active';")
        return token_hash

    def source(resource=None, space=None, kind='document', revision=1):
        resource, space = resource or m.r, space or m.s
        m.db.execute(f"UPDATE postern.resources SET adapter='native-content-v1',kind={sql(kind)},content_revision={revision} WHERE space_id='{space}' AND id='{resource}';")
        digest = hashlib.sha256(b'original').hexdigest()
        key = f'{space}/{resource}/{revision}/{digest}'
        m.db.execute(f"INSERT INTO postern.objects VALUES('{space}','{resource}',{revision},{sql(key)},'{digest}',8,now());")

    def publish(metadata=None, objects=None, kind='document', resource=None, space=None, revision=1):
        return call(m.db, 'publish_content', space or m.s, resource or m.r, revision, kind,
                    metadata if metadata is not None else {'page_count': 2},
                    objects if objects is not None else m.pages)

    def invoke(name, *tail, token=None, space=None, resource=None, revision=1):
        return json.loads(call(m.runtime, name, token or m.token, space or m.s,
                               m.audience, resource or m.r, revision, *tail))

    m.session, m.source, m.publish, m.invoke = session, source, publish, invoke
    m.pages = [{'representation': 'page', 'index': i, 'sha256': hashlib.sha256(f'page{i}'.encode()).hexdigest(), 'size': 5} for i in range(2)]
    m.token = session()
    source()
    return m


def test_native_manifest_and_committed_disclosure(content):
    m = content
    m.publish()
    response = m.invoke('resource_content_manifest')
    manifest = response['manifest']
    assert manifest == {'resource_id': m.r, 'revision': 1, 'kind': 'document', 'metadata': {'page_count': 2}}
    assert 'sha256' not in json.dumps(manifest) and 'session_hash' not in manifest
    assert response['session_hash'] == m.token and response['context']['membership_id'] == m.mb
    admitted = m.invoke('resource_content_admit', 'page', 0)
    assert admitted['object'] == {**m.pages[0], 'space_id': m.s, 'resource_id': m.r, 'revision': 1}
    assert admitted['session_hash'] == m.token
    assert admitted['context']['membership_id'] == m.mb
    assert m.invoke('resource_content_disclose', 'page', 0, admitted) == admitted
    assert m.db.scalar(f"SELECT string_agg(action,',' ORDER BY occurred_at) FROM postern.activity WHERE space_id='{m.s}';") == 'content.manifest,content.requested,content.disclosed'


@pytest.mark.parametrize('corruption', ['missing', 'duplicate', 'wrong_hash', 'oversize', 'negative', 'extra_key', 'wrong_representation', 'partial_thumbnails'])
def test_publication_is_complete_and_atomic(content, corruption):
    m = content
    objects = copy.deepcopy(m.pages)
    if corruption == 'missing': objects.pop()
    elif corruption == 'duplicate': objects[1] = objects[0]
    elif corruption == 'wrong_hash': objects[0]['sha256'] = '../bad'
    elif corruption == 'oversize': objects[0]['size'] = 8388609
    elif corruption == 'negative': objects[0]['index'] = -1
    elif corruption == 'extra_key': objects[0]['path'] = '/private/example'
    elif corruption == 'wrong_representation': objects[0]['representation'] = 'original'
    else: objects.append({**objects[0], 'representation': 'thumbnail'})
    with pytest.raises(MigrationError): m.publish(objects=objects)
    assert m.db.scalar(f"SELECT count(*) FROM postern.content_presentations WHERE space_id='{m.s}';") == '0'
    assert m.db.scalar(f"SELECT count(*) FROM postern.content_objects WHERE space_id='{m.s}';") == '0'


@pytest.mark.parametrize('metadata', [None, {}, {'page_count': 0}, {'page_count': '2'}, {'page_count': 10001}, {'page_count': 2, 'url': 'https://example.invalid'}])
def test_bounded_typed_metadata(content, metadata):
    m = content
    with pytest.raises(MigrationError):
        call(m.db, 'publish_content', m.s, m.r, 1, 'document', metadata, m.pages)


def test_video_chunks_and_thumbnail_publication(content):
    m = content
    m.db.execute(f"UPDATE postern.resources SET kind='video' WHERE space_id='{m.s}' AND id='{m.r}';")
    metadata = {'duration_ms': 1500, 'total_bytes': 7, 'chunk_bytes': 4, 'chunk_count': 2}
    objects = [{**x, 'representation': 'video', 'size': 4-i} for i, x in enumerate(m.pages)]
    objects.append({**m.pages[0], 'representation': 'thumbnail'})
    bad = copy.deepcopy(objects); bad[1]['size'] = 4
    with pytest.raises(MigrationError): m.publish(metadata, bad, kind='video')
    m.publish(metadata, objects, kind='video')
    assert m.invoke('resource_content_manifest')['manifest']['metadata'] == metadata
    assert m.invoke('resource_content_admit', 'video', 1)['object']['size'] == 3
    assert m.invoke('resource_content_admit', 'thumbnail', 0)['object']['representation'] == 'thumbnail'
    for representation, index in [('video', 2), ('page', 0), ('thumbnail', 1), ('original', 0)]:
        with pytest.raises(MigrationError): m.invoke('resource_content_admit', representation, index)


def test_original_inventory_and_exact_scope_required(content):
    m = content
    with pytest.raises(MigrationError): m.publish(resource=m.hidden)
    with pytest.raises(MigrationError): m.publish(space=m.t)
    with pytest.raises(MigrationError): m.publish(revision=2)
    with pytest.raises(MigrationError): m.publish(kind='video')
    m.publish()
    with pytest.raises(MigrationError):
        m.db.execute(f"INSERT INTO postern.content_objects VALUES('{m.t}','{m.r}',1,'page',0,'{'a'*64}',1);")


@pytest.mark.parametrize('role', ['a', 'b', 'd'])
@pytest.mark.parametrize('state', ['draft', 'archived'])
def test_publication_gate_applies_to_every_role(content, role, state):
    m = content
    m.publish()
    token = m.session(role)
    m.db.execute(f"UPDATE postern.resources SET state='{state}' WHERE space_id='{m.s}' AND id='{m.r}';")
    with pytest.raises(MigrationError): m.invoke('resource_content_manifest', token=token)
    with pytest.raises(MigrationError): m.invoke('resource_content_admit', 'page', 0, token=token)


def test_view_permission_session_and_space_boundaries(content):
    m = content
    m.publish()
    with pytest.raises(MigrationError): m.invoke('resource_content_admit', 'page', 0, space=m.t)
    with pytest.raises(MigrationError): m.invoke('resource_content_admit', 'page', 0, resource=m.foreign)
    with pytest.raises(MigrationError): m.invoke('resource_content_admit', 'page', 0, revision=2)
    with pytest.raises(MigrationError): m.invoke('resource_content_admit', 'original', 0)
    with pytest.raises(MigrationError): m.invoke('resource_content_admit', None, None)
    m.db.execute(f"UPDATE postern.grants SET state='revoked' WHERE space_id='{m.s}' AND resource_id='{m.r}'; "
                 f"INSERT INTO postern.grants(space_id,resource_id,membership_id,actions) VALUES('{m.s}','{m.r}','{m.mb}',ARRAY['discover']);")
    with pytest.raises(MigrationError): m.invoke('resource_content_manifest')
    with pytest.raises(MigrationError): m.invoke('resource_content_admit', 'page', 0)


def test_same_bytes_in_two_spaces_keep_independent_authority(content):
    m = content
    m.publish()
    m.source(resource=m.foreign, space=m.t)
    m.publish(resource=m.foreign, space=m.t)
    first = m.invoke('resource_content_admit', 'page', 0)
    second = m.invoke('resource_content_admit', 'page', 0, token=m.session('c', m.t), space=m.t, resource=m.foreign)
    assert first['object']['sha256'] == second['object']['sha256']
    assert first['object']['space_id'] != second['object']['space_id']
    # A's owner is only a member of B, with no implicit B resource grants.
    with pytest.raises(MigrationError):
        m.invoke('resource_content_admit', 'page', 0, token=m.session('a', m.t), space=m.t, resource=m.foreign)
    with pytest.raises(MigrationError): m.invoke('resource_content_disclose', 'page', 0, second)


def test_another_live_grant_preserves_access(content):
    m = content
    m.publish()
    m.db.execute(f"INSERT INTO postern.grants(space_id,resource_id,all_members,actions) VALUES('{m.s}','{m.r}',true,ARRAY['discover','view']); "
                 f"UPDATE postern.grants SET state='revoked' WHERE space_id='{m.s}' AND resource_id='{m.r}' AND membership_id='{m.mb}'; "
                 f"UPDATE postern.spaces SET policy_revision=policy_revision+1 WHERE id='{m.s}';")
    admitted = m.invoke('resource_content_admit', 'page', 0)
    assert m.invoke('resource_content_disclose', 'page', 0, admitted) == admitted


def test_runtime_cannot_publish_inspect_or_bypass(content):
    m = content
    m.publish()
    denied = [
        'SELECT * FROM postern.content_presentations', 'SELECT * FROM postern.content_objects',
        'SET ROLE postern_owner',
        f"SELECT postern.publish_content('{m.s}','{m.r}',1,'document','{{}}','[]')",
        f"SELECT postern.content_snapshot('{m.token}','{m.s}',{sql(m.audience)},'{m.r}',1,'page',0)",
        'UPDATE postern.content_objects SET bytes=1', 'DELETE FROM postern.content_presentations',
    ]
    for query in denied:
        result = m.runtime.execute(query+';', check=False)
        assert result.returncode and 'permission denied' in result.stderr
    for table in ('content_objects', 'content_presentations'):
        with pytest.raises(MigrationError): m.db.execute(f"DELETE FROM postern.{table} WHERE space_id='{m.s}';")
    with pytest.raises(MigrationError): m.db.execute(f"UPDATE postern.content_objects SET bytes=1 WHERE space_id='{m.s}';")


def test_disclosure_snapshot_rejects_null_tampering_and_different_session(content):
    m = content
    m.publish()
    admitted = m.invoke('resource_content_admit', 'page', 0)
    tampered = copy.deepcopy(admitted); tampered['object']['sha256'] = '0'*64
    for expected in (None, {}, tampered):
        with pytest.raises(MigrationError): m.invoke('resource_content_disclose', 'page', 0, expected)
    with pytest.raises(MigrationError): m.invoke('resource_content_disclose', 'page', 1, admitted)
    with pytest.raises(MigrationError): m.invoke('resource_content_disclose', 'page', 0, admitted, token=m.session())
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='content.disclosed';") == '0'


@pytest.mark.parametrize('mutation', ['grant', 'membership', 'account', 'space', 'resource', 'revision', 'policy', 'session', 'login', 'expiry', 'generation'])
def test_live_changes_between_admission_and_disclosure_refuse(content, mutation):
    m = content
    m.publish()
    admitted = m.invoke('resource_content_admit', 'page', 0)
    changes = {
        'grant': f"UPDATE postern.grants SET state='revoked' WHERE space_id='{m.s}' AND resource_id='{m.r}'",
        'membership': m.context(actor='a', role='postern_writer')+f"SELECT postern.revoke_member('{m.s}','{m.mb}','{uuid.uuid4()}','{'a'*64}')",
        'account': f"SELECT postern.disable_principal('{m.b}','suspended')",
        'space': f"UPDATE postern.spaces SET state='suspended' WHERE id='{m.s}'",
        'resource': f"UPDATE postern.resources SET state='archived' WHERE space_id='{m.s}' AND id='{m.r}'",
        'revision': f"UPDATE postern.resources SET content_revision=2 WHERE space_id='{m.s}' AND id='{m.r}'",
        'policy': f"UPDATE postern.spaces SET policy_revision=policy_revision+1 WHERE id='{m.s}'",
        'session': f"UPDATE postern.resource_sessions SET revoked=true WHERE token_hash='{m.token}'",
        'login': f"UPDATE postern.account_logins SET revoked=true WHERE id=(SELECT login_id FROM postern.resource_sessions WHERE token_hash='{m.token}')",
        'expiry': f"UPDATE postern.resource_sessions SET expires_at=now()-interval '1 second' WHERE token_hash='{m.token}'",
        'generation': "UPDATE postern.authority SET generation=gen_random_uuid(),state='quarantined'",
    }
    m.db.execute(changes[mutation]+';')
    with pytest.raises(MigrationError): m.invoke('resource_content_disclose', 'page', 0, admitted)
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='content.disclosed';") == '0'


@pytest.mark.parametrize('action', ['content.requested', 'content.disclosed', 'content.manifest'])
def test_required_audit_failures_return_no_result(content, action):
    m = content
    m.publish()
    admitted = m.invoke('resource_content_admit', 'page', 0) if action == 'content.disclosed' else None
    m.db.execute("CREATE FUNCTION postern.content_audit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                 f"IF NEW.action='{action}' THEN RAISE EXCEPTION 'injected audit failure'; END IF; RETURN NEW; END $$; "
                 "CREATE TRIGGER content_audit_failure BEFORE INSERT ON postern.activity FOR EACH ROW EXECUTE FUNCTION postern.content_audit_failure();")
    try:
        with pytest.raises(MigrationError):
            if action == 'content.disclosed': m.invoke('resource_content_disclose', 'page', 0, admitted)
            elif action == 'content.requested': m.invoke('resource_content_admit', 'page', 0)
            else: m.invoke('resource_content_manifest')
        assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='{action}';") == '0'
    finally:
        m.db.execute('DROP TRIGGER content_audit_failure ON postern.activity; DROP FUNCTION postern.content_audit_failure();')


def test_grant_expiring_while_disclosure_waits_for_lock_is_denied(content):
    m = content
    m.publish()
    # Grant properties are immutable: replace the original with a short-lived
    # equivalent. Observe expiry through DB time, not an assumed sleep duration.
    m.db.execute(f"UPDATE postern.grants SET state='revoked' WHERE space_id='{m.s}' AND resource_id='{m.r}'; "
                 f"INSERT INTO postern.grants(space_id,resource_id,membership_id,actions,expires_at) VALUES('{m.s}','{m.r}','{m.mb}',ARRAY['discover','view'],clock_timestamp()+interval '1.5 seconds');")
    admitted = m.invoke('resource_content_admit', 'page', 0)
    with psycopg.connect(host=m.db.env['PGHOST'], port=int(m.db.env['PGPORT']), user=m.db.env['PGUSER'], dbname='postgres') as control:
        control.execute('SELECT 1 FROM postern.spaces WHERE id=%s FOR UPDATE', (m.s,))
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(m.invoke, 'resource_content_disclose', 'page', 0, admitted)
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                if m.db.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename='content_schema_test' AND wait_event_type='Lock');") == 't': break
                time.sleep(.01)
            else: raise AssertionError('content disclosure never reached the deliberate lock')
            while time.monotonic()<deadline:
                if m.db.scalar(f"SELECT NOT EXISTS(SELECT 1 FROM postern.grants WHERE space_id='{m.s}' AND resource_id='{m.r}' AND state='active' AND expires_at>clock_timestamp());") == 't': break
                time.sleep(.02)
            else: raise AssertionError('fixture grant did not expire')
            control.commit()
            with pytest.raises(MigrationError): pending.result(timeout=5)
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}' AND action='content.disclosed';") == '0'


def test_quarantined_restore_preserves_manifests_without_reviving_authority(content, tmp_path):
    m = content
    m.publish()
    m.source(resource=m.foreign, space=m.t)
    m.publish(resource=m.foreign, space=m.t)
    admitted = m.invoke('resource_content_admit', 'page', 0)
    queries = {table: f"SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text),'[]') FROM postern.{table} x WHERE space_id IN ('{m.s}','{m.t}');"
               for table in ('objects', 'content_presentations', 'content_objects', 'activity')}
    inventories = {table: m.db.scalar(query) for table, query in queries.items()}
    dump = tmp_path/'content.dump'
    subprocess.run(['pg_dump', '-Fc', '-d', 'postgres', '-f', str(dump)], env=m.db.env, check=True, capture_output=True, timeout=30)
    database = 'content_restore_'+uuid.uuid4().hex
    restored = restore_quarantined(m.db, dump, database)
    assert {table: restored.scalar(query) for table, query in queries.items()} == inventories
    assert restored.scalar('SELECT state FROM postern.authority;') == 'quarantined'
    assert restored.scalar(f"SELECT count(*) FROM postern.spaces WHERE id IN ('{m.s}','{m.t}') AND state='suspended';") == '2'
    runtime = Postgres(m.runtime.env, database)
    with pytest.raises(MigrationError):
        call(runtime, 'resource_content_disclose', m.token, m.s, m.audience, m.r, 1, 'page', 0, admitted)
    # Only the owned synthetic fixture is reactivated after its full inventory is
    # compared. This is not an operational policy/revocation-reconciliation tool.
    restored.execute(f'GRANT CONNECT ON DATABASE {database} TO content_schema_test;')
    restored.execute(f"UPDATE postern.authority SET state='active'; UPDATE postern.spaces SET state='active' WHERE id='{m.s}';")
    with pytest.raises(MigrationError):
        call(runtime, 'resource_content_disclose', m.token, m.s, m.audience, m.r, 1, 'page', 0, admitted)
    m.db, m.runtime = restored, runtime
    m.generation = restored.scalar('SELECT generation FROM postern.authority;')
    new_token = m.session()
    current = m.invoke('resource_content_admit', 'page', 0, token=new_token)
    assert m.invoke('resource_content_disclose', 'page', 0, current, token=new_token) == current
    # B stays suspended after independently activating A.
    with pytest.raises(MigrationError):
        m.invoke('resource_content_admit', 'page', 0, token=m.session('c', m.t), space=m.t, resource=m.foreign)
