import subprocess
import uuid

import pytest

from postern.pg import MigrationError,Postgres,apply_schema,literal
from scripts.storage_probe import Control, wait_locked


def u(value): return literal(value)+'::uuid'


def revoke(m,actor='a',target='mb',key=None,hash='a'*64):
    return m.context(actor=actor,role='postern_writer')+'SELECT postern.revoke_member('+u(m.s)+','+u(getattr(m,target))+','+u(key or str(uuid.uuid4()))+','+literal(hash)+');'


def test_schema_replay_and_checksum(db):
    checksum=apply_schema(db)
    assert db.scalar('SELECT count(*) FROM postern.schema_migrations;')=='6'
    db.execute("UPDATE postern.schema_migrations SET sha256='changed';")
    try:
        with pytest.raises(MigrationError) as e: apply_schema(db)
        assert 'checksum mismatch' in e.value.details
    finally:
        for version, digest in checksum.items():
            db.execute(f'UPDATE postern.schema_migrations SET sha256={literal(digest)} WHERE version={version};')
    name='fresh_'+uuid.uuid4().hex
    db.execute(f'CREATE DATABASE {name};')
    another=Postgres(db.env,name)
    assert apply_schema(another)==checksum
    assert another.scalar('SELECT state FROM postern.authority;')=='quarantined'
    unsafe='unsafe_'+uuid.uuid4().hex
    db.execute(f'CREATE DATABASE {unsafe}; ALTER ROLE postern_reader CREATEDB;')
    try:
        with pytest.raises(MigrationError) as e: apply_schema(Postgres(db.env,unsafe))
        assert 'unsafe preexisting' in e.value.details
    finally: db.execute('ALTER ROLE postern_reader NOCREATEDB;')


def test_scoped_catalog_and_actions(model):
    m=model
    visible=m.db.scalar(m.context()+"SELECT title FROM postern.resources ORDER BY title;").splitlines()
    assert visible==['Audience','Visible คู่มือพนักงาน']
    assert m.db.scalar(m.context()+"SELECT count(*) FROM postern.resources WHERE title LIKE '%พนักงาน%';")=='1'
    assert m.db.scalar(m.context()+f"SELECT postern.can_resource('{m.s}','{m.group_resource}','view');")=='f'
    assert m.db.scalar(m.context(actor='a')+f"SELECT postern.can_resource('{m.s}','{m.r}','export');")=='f'
    assert m.db.scalar(m.context(actor='c')+'SELECT count(*) FROM postern.resources;')=='0'
    # Owner in A is an ordinary member in B, with no inherited grants.
    assert m.db.scalar(m.context(actor='a',space='t')+'SELECT count(*) FROM postern.resources;')=='0'
    assert m.db.scalar(m.context(actor='a')+'SELECT count(*) FROM postern.resources;')=='5'


@pytest.mark.parametrize('query',[
 'SELECT binding FROM postern.resources', 'SELECT * FROM postern.objects',
 'SELECT * FROM postern.invitations', 'SELECT * FROM postern.legacy_rows',
 "UPDATE postern.memberships SET role='owner'", 'SET ROLE postern_owner',
 "SELECT postern.disable_principal(gen_random_uuid(),'deleted')",
])
def test_runtime_privileges(model,query):
    runtime=Postgres({**model.db.env,'PGUSER':'postern_test_reader'})
    result=runtime.execute(model.context()+query+';',check=False)
    assert result.returncode and 'permission denied' in result.stderr


def test_missing_and_reset_context(model):
    m=model
    assert m.db.scalar('SET ROLE postern_reader; SELECT count(*) FROM postern.resources;')=='0'
    query='SET ROLE postern_reader; BEGIN;'+m.context().replace('SET postern.','SET LOCAL postern.')+'SELECT count(*) FROM postern.resources; COMMIT; SELECT count(*) FROM postern.resources;'
    assert m.db.scalar(query).splitlines()==['2','0']
    assert m.db.scalar(m.context()+"SET postern.generation='00000000-0000-0000-0000-000000000000'; SELECT count(*) FROM postern.resources;")=='0'


def test_foreign_refs_nulls_and_immutable_scope(model):
    m=model
    failures=[
      f"INSERT INTO postern.grants(space_id,resource_id,membership_id,actions) VALUES('{m.t}','{m.foreign}','{m.mb}',ARRAY['discover'])",
      f"INSERT INTO postern.grants(space_id,resource_id,all_members,actions) VALUES(NULL,'{m.r}',true,ARRAY['discover'])",
      f"UPDATE postern.resources SET space_id='{m.t}' WHERE id='{m.r}'",
      f"INSERT INTO postern.grants(space_id,resource_id,all_members,actions) VALUES('{m.s}','{m.r}',true,ARRAY['view'])",
    ]
    for query in failures: assert m.db.execute(query+';',check=False).returncode


def test_revoke_is_atomic_idempotent_and_revokes_all_paths(model):
    m=model;key=str(uuid.uuid4())
    first=m.db.scalar(revoke(m,key=key)); assert first==m.db.scalar(revoke(m,key=key))
    assert m.db.execute(revoke(m,key=key,hash='b'*64),check=False).returncode
    assert m.db.scalar(m.context()+'SELECT count(*) FROM postern.resources;')=='0'
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}';")=='1'
    assert m.db.scalar(f"SELECT count(*) FROM postern.outbox WHERE space_id='{m.s}';")=='1'
    assert m.db.scalar(f"SELECT policy_revision FROM postern.spaces WHERE id='{m.s}';")=='1'
    assert m.db.execute(f"UPDATE postern.memberships SET state='active' WHERE space_id='{m.s}' AND id='{m.mb}';",check=False).returncode
    # Readmission has a new ID; the old direct/group grants do not return.
    m.db.execute(f"INSERT INTO postern.memberships(space_id,principal_id,role) VALUES('{m.s}','{m.b}','member');")
    assert m.db.scalar(m.context()+'SELECT count(*) FROM postern.resources;')=='0'


def test_last_owner_admin_escalation_and_outbox_failure(model):
    m=model
    assert m.db.execute(revoke(m,actor='d',target='ma'),check=False).returncode
    assert m.db.execute(revoke(m,target='ma'),check=False).returncode
    m.db.execute("CREATE FUNCTION postern.fail_outbox() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected outbox failure'; END $$; CREATE TRIGGER fail_outbox BEFORE INSERT ON postern.outbox FOR EACH ROW EXECUTE FUNCTION postern.fail_outbox();")
    try:
        assert m.db.execute(revoke(m),check=False).returncode
        assert m.db.scalar(f"SELECT state FROM postern.memberships WHERE space_id='{m.s}' AND id='{m.mb}';")=='active'
        assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{m.s}';")=='0'
        assert m.db.scalar(f"SELECT policy_revision FROM postern.spaces WHERE id='{m.s}';")=='0'
    finally: m.db.execute('DROP TRIGGER fail_outbox ON postern.outbox; DROP FUNCTION postern.fail_outbox();')


def test_account_disable_orphan_history_and_rollback(model):
    m=model
    assert m.db.execute(f"BEGIN; SET ROLE postern_identity_writer; SELECT postern.disable_principal('{m.a}','deleted'); SELECT 1/0; COMMIT;",check=False).returncode
    assert m.db.scalar(f"SELECT state FROM postern.principals WHERE id='{m.a}';")=='active'
    m.db.execute(f"SET ROLE postern_identity_writer; SELECT postern.disable_principal('{m.a}','deleted');")
    assert m.db.scalar(f"SELECT state FROM postern.spaces WHERE id='{m.s}';")=='suspended'
    assert m.db.scalar(f"SELECT state FROM postern.spaces WHERE id='{m.t}';")=='active'
    assert m.db.scalar(m.context()+'SELECT count(*) FROM postern.resources;')=='0'
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE target_id='{m.a}' AND actor_id IS NULL;")=='2'
    assert m.db.execute(f"UPDATE postern.principals SET state='active' WHERE id='{m.a}';",check=False).returncode
    assert m.db.execute(f"SET ROLE postern_identity_writer; SELECT postern.resume_principal('{m.a}');",check=False).returncode
    m.db.execute(f"SET ROLE postern_identity_writer; SELECT postern.disable_principal('{m.a}','deleted');")
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE target_id='{m.a}' AND actor_id IS NULL;")=='2'


def test_resuming_account_does_not_readmit_membership(model):
    m=model
    m.db.execute(f"SET ROLE postern_identity_writer; SELECT postern.disable_principal('{m.b}','suspended'); SELECT postern.resume_principal('{m.b}');")
    assert m.db.scalar(f"SELECT state FROM postern.principals WHERE id='{m.b}';")=='active'
    assert m.db.scalar(m.context()+'SELECT count(*) FROM postern.resources;')=='0'


def race(db,first,second):
    control=Control(db.env);children=[]
    try:
        control.send('DO $$ BEGIN PERFORM pg_advisory_lock(876543); END $$')
        for name,query in [('m2_probe_first',first),('m2_probe_second',second)]:
            child=subprocess.Popen(['psql','-X','-q','-A','-t','-v','ON_ERROR_STOP=1','-d','postgres'],env={**db.env,'PGAPPNAME':name},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            child.stdin.write(query); child.stdin.close();child.stdin=None;children.append(child)
            wait_locked(db.env,name)
        control.send('DO $$ BEGIN PERFORM pg_advisory_unlock(876543); END $$')
        outputs=[child.communicate(timeout=5) for child in children]
        return [(child.returncode,*output) for child,output in zip(children,outputs)]
    finally:
        control.close()
        for child in children:
            if child.poll() is None: child.kill();child.communicate()


def test_concurrent_last_owner_and_queued_revocation(model):
    m=model
    m.db.execute(f"UPDATE postern.memberships SET role='owner' WHERE space_id='{m.s}' AND id='{m.mb}';")
    first=f"BEGIN; SELECT id FROM postern.spaces WHERE id='{m.s}' FOR UPDATE; SELECT pg_advisory_xact_lock(876543);"+revoke(m)+'COMMIT;'
    second=revoke(m,actor='b',target='ma')
    outcomes=race(m.db,first,second)
    assert outcomes[0][0]==0 and outcomes[1][0]!=0 and 'forbidden' in outcomes[1][2],outcomes
    assert m.db.scalar(f"SELECT count(*) FROM postern.memberships WHERE space_id='{m.s}' AND state='active' AND role='owner';")=='1'


def test_account_disable_wins_queued_mutation(model):
    m=model
    first=f"BEGIN; SELECT id FROM postern.principals WHERE id='{m.d}' FOR UPDATE; SELECT pg_advisory_xact_lock(876543); SET ROLE postern_identity_writer; SELECT postern.disable_principal('{m.d}','suspended'); COMMIT;"
    outcomes=race(m.db,first,revoke(m,actor='d'))
    assert outcomes[0][0]==0 and outcomes[1][0]!=0 and 'forbidden' in outcomes[1][2],outcomes
    assert m.db.scalar(f"SELECT state FROM postern.memberships WHERE space_id='{m.s}' AND id='{m.mb}';")=='active'


def test_upgrade_from_version_one_preserves_data(db):
    import hashlib
    from pathlib import Path
    source=Path(__file__).resolve().parents[2]/'postern/sql/001_spaces.sql'
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    name='upgrade_'+uuid.uuid4().hex
    db.execute(f'CREATE DATABASE {name};')
    target=Postgres(db.env,name)
    target.execute('BEGIN;'+source.read_text()+f"INSERT INTO postern.schema_migrations(version,sha256) VALUES(1,'{digest}'); INSERT INTO postern.principals(authority,account_id) VALUES('before-upgrade','preserved'); COMMIT;")
    generation=target.scalar('SELECT generation FROM postern.authority;')
    apply_schema(target)
    assert target.scalar('SELECT count(*) FROM postern.schema_migrations;')=='6'
    assert target.scalar('SELECT account_id FROM postern.principals;')=='preserved'
    assert target.scalar('SELECT generation FROM postern.authority;')==generation
    assert target.scalar('SELECT sha256 FROM postern.schema_migrations WHERE version=1;')==digest
