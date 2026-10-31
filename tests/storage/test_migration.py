import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import uuid

import pytest

from postern.db import SCHEMA
from postern.honors import certificate, name_history
from postern.migration_bundle import canonical,export_legacy,import_legacy,identity,load_manifest
from postern.pg import MigrationError,Postgres,restore_quarantined
from postern.recover import import_events


@pytest.fixture
def legacy(tmp_path):
    path=tmp_path/'source.sqlite3'; conn=sqlite3.connect(path);conn.row_factory=sqlite3.Row;conn.executescript(SCHEMA)
    content=b'synthetic original\x00\xff\n';sha=hashlib.sha256(content).hexdigest();obj=tmp_path/'original';obj.write_bytes(content)
    conn.executemany('INSERT INTO members VALUES(?,?,?)',[('reader@example.invalid','Roster Name','reader'),('unbound@example.invalid','Unlinked','manager')])
    conn.execute("INSERT INTO meta VALUES('notice_version','\"legacy-terms\"')")
    conn.execute("INSERT INTO sessions VALUES('session',?,'subject','reader@example.invalid','Original Name',1,9999999999,0,'v1','web')",('c'*64,))
    conn.execute("INSERT INTO session_ip_agreements VALUES('session','ip-v1',2)")
    conn.execute("INSERT INTO posts VALUES('post',?,'Body: คู่มือ','Training','published',4,1,'admin@example.invalid',2,'publish',NULL)",("Quote ' and \\q\nUnicode คู่มือ",))
    conn.execute("INSERT INTO posts VALUES('archive','Archived','Old','Training','archived',2,1,'admin@example.invalid',2,NULL,NULL)")
    conn.execute("INSERT INTO items VALUES('item','post','manual.pdf','pdf',?, ?,3,'drive-synthetic','ready',NULL,0,2)",(sha,len(content)))
    conn.execute("INSERT INTO views VALUES('view','session','reader@example.invalid','item','post','attempt',1,2,3,3,'[1,2,3]',4)")
    conn.execute("INSERT INTO video_metadata VALUES('item',120)")
    conn.execute("INSERT INTO video_tickets VALUES(?,'view',9999999999)",('d'*64,))
    conn.execute("INSERT INTO video_checkpoints VALUES('view',3)")
    conn.execute("INSERT INTO video_coverage VALUES('view','[[0,120]]')")
    conn.execute("INSERT INTO session_devices VALUES('session','device-hash')")
    conn.execute("INSERT INTO selfies VALUES('selfie','reader@example.invalid','device-hash','session',1,2,3,'retired','private-drive-reference',?,'v1','enrollment',0,0,NULL)",('e'*64,))
    conn.execute("INSERT INTO selfie_reservations VALUES('reservation','reader@example.invalid','photo-attempt',1,'enrollment','completed','release')")
    conn.execute("INSERT INTO operations VALUES('publish','post',4,'publish','admin@example.invalid','confirmed',1,0,1,NULL)")
    records=[{'event_id':'pass','event':'quiz_attempt','ts':1700000000,'email':'reader@example.invalid','name':'Original Name','detail':{'chapter':0,'score':20,'total':20,'passed':True}},
      {'event_id':'name-a','event':'certificate_name','ts':1700000001,'email':'reader@example.invalid','detail':{'name':'Asha Rivera'}},
      {'event_id':'name-z','event':'certificate_name','ts':1700000001,'email':'reader@example.invalid','detail':{'name':'อาชา Santos'}},
      {'event_id':'chapter','event':'quiz_attempt','ts':1700000002,'email':'reader@example.invalid','detail':{'chapter':1,'score':4,'total':5,'passed':True,'questions':['q1','q2']}}]
    import_events(conn,records)
    conn.execute("INSERT INTO quiz_draws VALUES('reader@example.invalid',2,'quiz-v1','[\"q3\"]',4)")
    conn.execute("INSERT INTO batches VALUES('batch','drive-batch',?,1,4,1,2,1)",('f'*64,))
    expected=certificate(conn,'reader@example.invalid');assert expected is not None
    conn.commit();conn.close()
    installation=str(uuid.uuid4());bundle=tmp_path/'bundle';digest=export_legacy(path,installation,{sha:obj},bundle)
    return {'db':path,'bundle':bundle,'digest':digest,'installation':installation,'object':obj,'sha':sha,'certificate':expected}


def prepare(model):
    model.db.execute(f"UPDATE postern.authority SET state='quarantined'; INSERT INTO postern.spaces(id,owner_id,title) VALUES('{uuid.uuid4()}','{model.owner}','Unused');")
    space=str(uuid.uuid4());model.db.execute(f"INSERT INTO postern.spaces(id,owner_id,title) VALUES('{space}','{model.owner}','Migrated');")
    return space


def migrate(model,legacy,space,objects,bindings=None):
    bindings={'reader@example.invalid':model.b} if bindings is None else bindings
    return import_legacy(model.db,legacy['bundle'],legacy['digest'],space,
                         {email:{'principal_id':principal,'evidence_ref':'synthetic-verified-subject-binding'} for email,principal in bindings.items()},objects)


def test_complete_checkpoint_preserved_and_replay(model,legacy,tmp_path):
    m=model;space=prepare(m);objects=tmp_path/'objects';before=hashlib.sha256(legacy['db'].read_bytes()).hexdigest()
    result=migrate(m,legacy,space,objects)
    assert objects.stat().st_mode & 0o077==0
    assert result['unbound_members']==1
    expected=[]
    for line in (legacy['bundle']/'rows.jsonl').read_bytes().splitlines():
        row=json.loads(line);expected.append((row['table'],row['key'],base64.b64decode(row['raw']).hex()))
    actual=m.db.rows(f"SELECT table_name,row_key,encode(original,'hex') AS raw FROM postern.legacy_rows WHERE space_id='{space}'")
    assert sorted((r['table_name'],r['row_key'],r['raw']) for r in actual)==sorted(expected)
    assert migrate(m,legacy,space,objects)==result
    assert m.db.scalar(f"SELECT count(*) FROM postern.activity WHERE space_id='{space}';")=='1'
    assert m.db.scalar(f"SELECT count(*) FROM postern.memberships WHERE space_id='{space}';")=='1'
    assert m.db.scalar(f"SELECT evidence_ref FROM postern.legacy_bindings WHERE space_id='{space}';")=='synthetic-verified-subject-binding'
    assert m.db.scalar(f"SELECT state FROM postern.resources WHERE space_id='{space}' AND id='{identity(legacy['installation'],'post','archive')}';")=='archived'
    record=m.db.rows(f"SELECT * FROM postern.objects WHERE space_id='{space}'")[0]
    assert (objects/record['object_key']).read_bytes()==legacy['object'].read_bytes()
    assert hashlib.sha256(legacy['db'].read_bytes()).hexdigest()==before
    assert all('token_hash' not in json.loads(bytes.fromhex(r['raw'])) for r in actual if r['table_name']=='sessions')
    assert all(r['table_name']!='video_tickets' for r in actual)
    # Replay actual preserved event payloads in reverse import order through legacy code.
    restored=sqlite3.connect(tmp_path/'replay.sqlite3');restored.row_factory=sqlite3.Row;restored.executescript(SCHEMA)
    restored.execute("INSERT INTO members VALUES('reader@example.invalid','Roster Name','reader')")
    events=[json.loads(json.loads(bytes.fromhex(r['raw']))['payload']) for r in actual if r['table_name']=='events']
    import_events(restored,list(reversed(events)))
    assert certificate(restored,'reader@example.invalid')==legacy['certificate']
    source=sqlite3.connect(legacy['db']);source.row_factory=sqlite3.Row
    assert name_history(restored,'reader@example.invalid')==name_history(source,'reader@example.invalid')
    columns='email,chapter,attempts,best_score,last_score,last_ts,passed_at'
    assert [tuple(r) for r in restored.execute('SELECT '+columns+' FROM quiz_standing ORDER BY chapter')]==[tuple(r) for r in source.execute('SELECT '+columns+' FROM quiz_standing ORDER BY chapter')]
    restored.close();source.close()
    assert m.db.scalar(m.context()+f"SET postern.space_id='{space}'; SELECT count(*) FROM postern.resources;")=='0'
    assert m.db.execute(f"UPDATE postern.spaces SET state='active' WHERE id='{space}';",check=False).returncode
    m.db.execute(f"BEGIN; INSERT INTO postern.memberships(space_id,principal_id,role) VALUES('{space}','{m.a}','owner'); UPDATE postern.spaces SET state='active' WHERE id='{space}'; UPDATE postern.authority SET state='active'; COMMIT;")
    assert m.db.scalar(m.context()+f"SET postern.space_id='{space}'; SELECT count(*) FROM postern.resources;")=='2'
    assert m.db.scalar(m.context()+f"SET postern.space_id='{space}'; SELECT title FROM postern.resources WHERE kind='post';")=="Quote ' and \\q\nUnicode คู่มือ"


def test_import_failure_rollback_and_retry(model,legacy,tmp_path):
    m=model;space=prepare(m);objects=tmp_path/'objects'
    with pytest.raises(MigrationError): migrate(m,legacy,space,objects,{'reader@example.invalid':str(uuid.uuid4())})
    for table in ['imports','legacy_rows','resources','memberships','activity']:
        assert m.db.scalar(f"SELECT count(*) FROM postern.{table} WHERE space_id='{space}';")=='0'
    migrate(m,legacy,space,objects)
    with pytest.raises(MigrationError) as e: migrate(m,legacy,space,objects,{'reader@example.invalid':m.c})
    assert 'checkpoint or binding conflict' in e.value.details
    assert m.db.scalar(f"SELECT principal_id FROM postern.memberships WHERE space_id='{space}';")==m.b


@pytest.mark.parametrize('table,revision', [('items','../../../../escaped'), ('posts',0), ('items',1.5), ('posts','9223372036854775808')])
def test_invalid_sqlite_revision_is_rejected_before_object_staging(model,legacy,tmp_path,table,revision):
    # SQLite affinity permits text/floats in INTEGER columns. This is a fresh,
    # correctly digested checkpoint, so integrity checks alone cannot reject it.
    with sqlite3.connect(legacy['db']) as conn:
        conn.execute(f'UPDATE {table} SET revision=?', (revision,))
    bundle=tmp_path/'malformed-bundle'
    digest=export_legacy(legacy['db'],legacy['installation'],{legacy['sha']:legacy['object']},bundle)
    space=prepare(model);objects=tmp_path/'objects'
    before={p for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(ValueError,match='revision'):
        import_legacy(model.db,bundle,digest,space,{},objects)
    assert {p for p in tmp_path.rglob('*') if p.is_file()}==before
    assert model.db.scalar(f"SELECT count(*) FROM postern.imports WHERE space_id='{space}';")=='0'


@pytest.mark.parametrize('part',['manifest','history','object','symlink'])
def test_tampering_denied_before_import(model,legacy,tmp_path,part):
    m=model;space=prepare(m)
    file=legacy['bundle']/({'manifest':'manifest.json','history':'rows.jsonl'}.get(part,'objects/'+legacy['sha']))
    if part=='symlink': file.unlink();file.symlink_to(legacy['object'])
    else: file.write_bytes(file.read_bytes()+b'changed')
    with pytest.raises(ValueError): migrate(m,legacy,space,tmp_path/'objects')
    assert m.db.scalar(f"SELECT count(*) FROM postern.imports WHERE space_id='{space}';")=='0'


def test_ambiguous_identity_and_active_target_denied(model,legacy,tmp_path):
    m=model;space=prepare(m)
    with pytest.raises(ValueError,match='ambiguous'): migrate(m,legacy,space,tmp_path/'objects',{'reader@example.invalid':m.b,'unbound@example.invalid':m.b})
    m.db.execute(f"UPDATE postern.authority SET state='active';")
    with pytest.raises(MigrationError) as e: migrate(m,legacy,space,tmp_path/'objects')
    assert 'quarantine' in e.value.details


def test_binding_evidence_and_second_installation_required(model,legacy,tmp_path):
    m=model;space=prepare(m)
    with pytest.raises(ValueError,match='evidence'):
        import_legacy(m.db,legacy['bundle'],legacy['digest'],space,{'reader@example.invalid':{'principal_id':m.b,'evidence_ref':''}},tmp_path/'objects')
    migrate(m,legacy,space,tmp_path/'objects')
    second=tmp_path/'second'
    digest=export_legacy(legacy['db'],str(uuid.uuid4()),{legacy['sha']:legacy['object']},second)
    with pytest.raises(MigrationError):
        import_legacy(m.db,second,digest,space,{},tmp_path/'objects')
    assert m.db.scalar(f"SELECT count(*) FROM postern.imports WHERE space_id='{space}';")=='1'


def test_database_restore_quarantines_and_preserves_manifest(model,legacy,tmp_path):
    m=model;space=prepare(m);migrate(m,legacy,space,tmp_path/'objects')
    dump=tmp_path/'backup.dump';name='restored_'+uuid.uuid4().hex
    subprocess.run(['pg_dump','-Fc','-d','postgres','-f',str(dump)],env=m.db.env,check=True,capture_output=True,timeout=30)
    old=m.db.scalar('SELECT generation FROM postern.authority;')
    restored=restore_quarantined(m.db,dump,name)
    denied=Postgres({**m.db.env,'PGUSER':'postern_test_reader'},name).execute('SELECT 1;',check=False)
    assert denied.returncode and 'permission denied for database' in denied.stderr
    assert old!=restored.scalar('SELECT generation FROM postern.authority;')
    for table in ['legacy_rows','legacy_map','legacy_bindings','objects']:
        assert m.db.rows(f"SELECT * FROM postern.{table} WHERE space_id='{space}' ORDER BY 1,2,3")==restored.rows(f"SELECT * FROM postern.{table} WHERE space_id='{space}' ORDER BY 1,2,3")
    assert restored.scalar(m.context()+f"SET postern.space_id='{space}'; SELECT count(*) FROM postern.resources;")=='0'
