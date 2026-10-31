"""Offline, quarantined SQLite checkpoint import. No source writes or live cutover.

Bundles omit reusable legacy token hashes. Raw history stays private and immutable;
only explicitly bound roster members become principals' memberships. Files use a
local private object adapter here; a hosted S3 adapter/cutover is separate work.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from .db import SCHEMA
from .pg import literal, MigrationError

TABLES=set(re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)',SCHEMA))
OMITTED={'video_tickets'}


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def digest_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): digest.update(chunk)
    return digest.hexdigest()


def identity(installation,kind,old):
    return str(uuid.uuid5(uuid.UUID(installation),kind+':'+old))


def export_legacy(database,installation,objects,output):
    """Take a consistent read-only SQL snapshot; verify supplied original-file bytes."""
    installation=str(uuid.UUID(installation)); output=Path(output)
    output.mkdir(parents=True,mode=0o700,exist_ok=False)
    (output/'objects').mkdir(mode=0o700)
    counts={}; required={}
    conn=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('BEGIN')
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables!=TABLES: raise ValueError('source schema table set differs; reconcile before export')
        with (output/'rows.jsonl').open('xb') as stream:
            os.chmod(stream.name,0o600)
            for table in sorted(tables-OMITTED):
                columns=conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                keys=[r['name'] for r in sorted(columns,key=lambda r:r['pk']) if r['pk']]
                names=[r['name'] for r in columns if not(table=='sessions' and r['name']=='token_hash')]
                quoted=','.join('"'+n.replace('"','""')+'"' for n in names)
                order=','.join('"'+n.replace('"','""')+'"' for n in keys)
                counts[table]=0
                for row in conn.execute(f'SELECT {quoted} FROM "{table}" ORDER BY {order}'):
                    data=dict(row); raw=canonical(data)
                    record={'table':table,'key':canonical([data[k] for k in keys]).decode(),
                            'raw':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest()}
                    stream.write(canonical(record)+b'\n'); counts[table]+=1
                    if table=='items': required[data['sha256']]=data['bytes']
        conn.rollback()
        for sha,size in required.items():
            if not re.fullmatch('[0-9a-f]{64}',sha): raise ValueError('invalid object digest')
            source=Path(objects[sha])
            if source.is_symlink() or source.stat().st_size!=size: raise ValueError('invalid original file')
            target=output/'objects'/sha
            shutil.copyfile(source,target); target.chmod(0o600)
            if digest_file(target)!=sha: raise ValueError('original file digest mismatch')
        manifest={'format':1,'installation':installation,'counts':counts,'rows_sha256':digest_file(output/'rows.jsonl'),
                  'objects':required,'omitted':['video_tickets','sessions.token_hash']}
        data=canonical(manifest); (output/'manifest.json').write_bytes(data); (output/'manifest.json').chmod(0o600)
        return hashlib.sha256(data).hexdigest()
    finally:
        conn.close()


def load_manifest(bundle,expected_digest):
    bundle=Path(bundle)
    if bundle.is_symlink() or any((bundle/p).is_symlink() for p in ('manifest.json','rows.jsonl','objects')):
        raise ValueError('bundle symlinks forbidden')
    data=(bundle/'manifest.json').read_bytes()
    if hashlib.sha256(data).hexdigest()!=expected_digest: raise ValueError('checkpoint digest mismatch')
    manifest=json.loads(data)
    if manifest['format']!=1 or set(manifest['counts'])!=TABLES-OMITTED: raise ValueError('unsupported bundle')
    uuid.UUID(manifest['installation'])
    if digest_file(bundle/'rows.jsonl')!=manifest['rows_sha256']: raise ValueError('history digest mismatch')
    for sha,size in manifest['objects'].items():
        if not re.fullmatch('[0-9a-f]{64}',sha): raise ValueError('invalid object digest')
        path=bundle/'objects'/sha
        if path.is_symlink() or path.stat().st_size!=size or digest_file(path)!=sha: raise ValueError('object digest mismatch')
    return manifest


def import_legacy(db,bundle,expected_digest,space,bindings,object_root):
    """Import one final checkpoint into an empty suspended space, with atomic SQL replay.

    bindings maps each verified legacy email to {principal_id, evidence_ref}.
    The caller must establish that evidence independently; matching email is not proof.
    Repeated imports require exactly the same checkpoint and bindings. New checkpoints
    require a fresh target/rehearsal: this function is not a live delta replicator.
    """
    bundle=Path(bundle); space=str(uuid.UUID(space)); object_root=Path(object_root)
    manifest=load_manifest(bundle,expected_digest); installation=manifest['installation']
    if any(not isinstance(b,dict) or set(b)!={'principal_id','evidence_ref'} for b in bindings.values()):
        raise ValueError('explicit principal and evidence reference required')
    bindings={email:{'principal_id':str(uuid.UUID(binding['principal_id'])),
                     'evidence_ref':binding['evidence_ref']} for email,binding in bindings.items()}
    if any(not isinstance(b['evidence_ref'],str) or not 1<=len(b['evidence_ref'])<=512 for b in bindings.values()):
        raise ValueError('verified binding evidence reference required')
    if len({b['principal_id'] for b in bindings.values()})!=len(bindings): raise ValueError('ambiguous legacy identity binding')
    if object_root.is_symlink(): raise ValueError('object destination symlink')
    object_root.mkdir(parents=True,mode=0o700,exist_ok=True)
    if object_root.stat().st_mode & 0o077: raise ValueError('object root must be private to its OS owner')
    bound_digest=hashlib.sha256(canonical({'checkpoint':expected_digest,'bindings':bindings})).hexdigest()
    posts=[];items=[];members=[]; counts={k:0 for k in manifest['counts']}
    # A private spool bounds memory by the catalog/roster, not the number of history rows.
    with tempfile.TemporaryDirectory(prefix='postern-import-') as folder:
        script=Path(folder)/'import.sql'
        with script.open('w') as sql:
            script.chmod(0o600)
            sql.write("BEGIN; SET LOCAL ROLE postern_owner;\nSELECT id FROM postern.spaces WHERE id="+literal(space)+"::uuid FOR UPDATE;\n")
            sql.write("DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM postern.spaces WHERE id="+literal(space)+"::uuid AND state='suspended') OR NOT EXISTS(SELECT 1 FROM postern.authority WHERE state='quarantined') THEN RAISE EXCEPTION 'import requires quarantine'; END IF;\n")
            sql.write("IF EXISTS(SELECT 1 FROM postern.imports WHERE installation="+literal(installation)+"::uuid AND (space_id<>"+literal(space)+"::uuid OR digest<>"+literal(bound_digest)+")) THEN RAISE EXCEPTION 'checkpoint or binding conflict'; END IF;\n")
            sql.write("IF NOT EXISTS(SELECT 1 FROM postern.imports WHERE space_id="+literal(space)+"::uuid) AND EXISTS(SELECT 1 FROM postern.resources WHERE space_id="+literal(space)+"::uuid) THEN RAISE EXCEPTION 'target catalog not empty'; END IF; END $$;\n")
            sql.write("INSERT INTO postern.imports(space_id,installation,digest) VALUES("+literal(space)+"::uuid,"+literal(installation)+"::uuid,"+literal(bound_digest)+") ON CONFLICT DO NOTHING;\n")
            sql.write('CREATE TEMP TABLE incoming (LIKE postern.legacy_rows INCLUDING ALL) ON COMMIT DROP;\n')
            row_hash=hashlib.sha256(); batch=0
            with (bundle/'rows.jsonl').open('rb') as stream:
                for line in stream:
                    row_hash.update(line); record=json.loads(line); table=record['table']
                    if table not in counts: raise ValueError('unknown legacy table')
                    raw=base64.b64decode(record['raw'],validate=True)
                    if hashlib.sha256(raw).hexdigest()!=record['sha256']: raise ValueError('row digest mismatch')
                    value=json.loads(raw); counts[table]+=1
                    if table=='sessions' and 'token_hash' in value: raise ValueError('credential in legacy bundle')
                    if table=='posts': posts.append(value)
                    elif table=='items': items.append(value)
                    elif table=='members': members.append(value)
                    if batch==0: sql.write('COPY incoming FROM STDIN WITH (FORMAT csv);\n')
                    buffer=io.StringIO(); csv.writer(buffer,lineterminator='\n').writerow([space,installation,table,record['key'],'\\x'+raw.hex(),record['sha256']])
                    sql.write(buffer.getvalue()); batch+=1
                    if batch==1000: sql.write('\\.\n');batch=0
            if batch: sql.write('\\.\n')
            if row_hash.hexdigest()!=manifest['rows_sha256'] or counts!=manifest['counts']: raise ValueError('history changed or count mismatch')
            # SQLite INTEGER affinity can still contain text or floats. Validate
            # every revision before staging any files, not only during SQL replay.
            if any(type(row.get('revision')) is not int or not 1<=row['revision']<=9223372036854775807 for row in [*posts,*items]):
                raise ValueError('legacy revision must be a positive bigint')
            sql.write('INSERT INTO postern.legacy_rows SELECT * FROM incoming ON CONFLICT DO NOTHING;\n')
            by_post={p['id']:p for p in posts}; required={i['sha256']:i['bytes'] for i in items}
            if required!=manifest['objects']: raise ValueError('object inventory differs from catalog')
            if set(bindings)-{m['email'] for m in members}: raise ValueError('binding has no roster entry')
            for member in members:
                if member['email'] not in bindings: continue
                mid=identity(installation,'membership',member['email'])
                role='admin' if member['role']=='admin' else 'member'
                binding=bindings[member['email']]
                sql.write('INSERT INTO postern.legacy_bindings VALUES('+','.join([literal(space)+'::uuid',literal(installation)+'::uuid',literal(member['email']),literal(binding['principal_id'])+'::uuid',literal(binding['evidence_ref'])])+') ON CONFLICT DO NOTHING;\n')
                sql.write("INSERT INTO postern.memberships(space_id,id,principal_id,role) VALUES("+literal(space)+"::uuid,"+literal(mid)+"::uuid,"+literal(binding['principal_id'])+"::uuid,"+literal(role)+") ON CONFLICT DO NOTHING;\n")
            for kind,rows in [('post',posts),('item',items)]:
                for row in rows:
                    rid=identity(installation,kind,row['id']); parent=by_post[row['post_id']] if kind=='item' else row
                    state=('archived' if parent['status']=='archived' else 'published'
                           if parent['status']=='published' and (kind=='post' or row['derive_status']=='ready') else 'draft')
                    title=row['filename'] if kind=='item' else row['title']
                    binding=canonical({'installation':installation,'kind':kind,'old_id':row['id']}).decode()
                    values=[literal(space)+'::uuid',literal(rid)+'::uuid',literal(kind),literal(title),literal(state),literal(str(row['revision']))+'::bigint',"'legacy'",literal(binding)+'::jsonb']
                    sql.write('INSERT INTO postern.resources(space_id,id,kind,title,state,content_revision,adapter,binding) VALUES('+','.join(values)+') ON CONFLICT DO NOTHING;\n')
                    sql.write('INSERT INTO postern.legacy_map VALUES('+','.join([literal(space)+'::uuid',literal(installation)+'::uuid',literal(kind),literal(row['id']),literal(rid)+'::uuid'])+') ON CONFLICT DO NOTHING;\n')
                    gid=identity(installation,'grant',kind+':'+row['id'])
                    sql.write("INSERT INTO postern.grants(space_id,id,resource_id,all_members,actions) VALUES("+literal(space)+"::uuid,"+literal(gid)+"::uuid,"+literal(rid)+"::uuid,true,ARRAY['discover','view']) ON CONFLICT DO NOTHING;\n")
                    if kind=='item':
                        parent_id=identity(installation,'post',row['post_id'])
                        sql.write('INSERT INTO postern.resource_children VALUES('+','.join([literal(space)+'::uuid',literal(parent_id)+'::uuid',literal(rid)+'::uuid',literal(row['sort'])+'::integer'])+') ON CONFLICT DO NOTHING;\n')
                        key=f"{space}/{rid}/{row['revision']}/{row['sha256']}"
                        target=object_root/key
                        if not target.resolve().is_relative_to(object_root.resolve()): raise ValueError('object destination escapes root')
                        # UUID/digest keys are generated here, never taken from source filenames.
                        if any(p.is_symlink() for p in [target,*target.parents]): raise ValueError('object destination symlink')
                        target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
                        if target.exists():
                            if digest_file(target)!=row['sha256']: raise ValueError('existing object differs')
                        else:
                            temporary=None
                            try:
                                with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as dest, (bundle/'objects'/row['sha256']).open('rb') as source:
                                    temporary=Path(dest.name);shutil.copyfileobj(source,dest)
                                    dest.flush();os.fsync(dest.fileno())
                                if digest_file(temporary)!=row['sha256']: raise ValueError('staged object differs')
                                try: os.link(temporary,target)  # create-only publication, never overwrite
                                except FileExistsError: pass  # a racing identical import is checked below
                            finally:
                                if temporary is not None: temporary.unlink(missing_ok=True)
                        if digest_file(target)!=row['sha256'] or target.stat().st_size!=row['bytes']: raise ValueError('staged object differs')
                        sql.write('INSERT INTO postern.objects VALUES('+','.join([literal(space)+'::uuid',literal(rid)+'::uuid',literal(row['revision'])+'::bigint',literal(key),literal(row['sha256']),literal(row['bytes'])+'::bigint','now()'])+') ON CONFLICT DO NOTHING;\n')
            ev=identity(installation,'activity','import')
            sql.write("INSERT INTO postern.activity(space_id,id,action,policy_revision,detail) VALUES("+literal(space)+"::uuid,"+literal(ev)+"::uuid,'legacy.imported',0,"+literal(canonical({'checkpoint':expected_digest,'unbound':len(members)-len(bindings)}).decode())+"::jsonb) ON CONFLICT DO NOTHING;\nCOMMIT;\n")
        with script.open() as source:
            result=subprocess.run(['psql','-X','-q','-A','-t','-v','ON_ERROR_STOP=1','-d',db.database],env=db.env,stdin=source,text=True,capture_output=True,timeout=180)
        if result.returncode: raise MigrationError('Legacy import rolled back',result.stderr)
    return {'counts':counts,'unbound_members':len(members)-len(bindings),'checkpoint':expected_digest}
