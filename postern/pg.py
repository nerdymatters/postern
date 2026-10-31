"""Explicit PostgreSQL CLI connection for offline migrations, not an HTTP query layer.

No connection defaults are accepted. The caller supplies a connection environment;
rehearsals construct one for their own temporary Unix-socket-only server.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path


def literal(value):
    """Encode data as UTF-8 hex: quotes, backslashes and psql metacommands stay data."""
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if type(value) in (int, float):
        if isinstance(value,float) and not math.isfinite(value):
            raise ValueError('non-finite SQL number')
        return str(value)
    if not isinstance(value, str) or '\x00' in value:
        raise ValueError('expected PostgreSQL text without NUL')
    return "convert_from(decode('"+value.encode().hex()+"','hex'),'UTF8')"


class Postgres:
    def __init__(self, env, database='postgres'):
        if not all(env.get(k) for k in ('PGHOST','PGPORT','PGUSER')):
            raise ValueError('explicit PostgreSQL host, port and user required')
        self.env, self.database = dict(env), database

    def execute(self, query, *, check=True):
        result=subprocess.run(['psql','-X','-q','-A','-t','-v','ON_ERROR_STOP=1','-d',self.database],
                              input=query, text=True, capture_output=True, env=self.env, timeout=60)
        if check and result.returncode:
            # Migration failures can contain private rows. The operator retains stderr,
            # but do not include it in exception text used by generic application logging.
            raise MigrationError('PostgreSQL command failed', result.stderr)
        return result

    def scalar(self, query):
        return self.execute(query).stdout.strip()

    def rows(self, query):
        return json.loads(self.scalar('SELECT coalesce(json_agg(q),\'[]\') FROM ('+query+') q;'))


class MigrationError(RuntimeError):
    def __init__(self, message, details=''):
        super().__init__(message)
        self.details=details


def apply_schema(db):
    """Install once, atomically; serialize installers and refuse changed migration bytes."""
    paths=sorted(Path(__file__).with_name('sql').glob('[0-9][0-9][0-9]_*.sql'))
    digests={int(p.name[:3]):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    script=[r"BEGIN; SELECT pg_advisory_xact_lock(7067,1);"]
    for path in paths:
        version=int(path.name[:3]); digest=digests[version]
        script.append(r"SELECT to_regclass('postern.schema_migrations') IS NOT NULL AS installed \gset")
        script.append(r"\if :installed")
        script.append(f"SELECT EXISTS(SELECT 1 FROM postern.schema_migrations WHERE version={version}) AS applied " + r"\gset")
        script.extend([r"\else", r"\set applied false", r"\endif", r"\if :applied"])
        script.append(f"SELECT sha256={literal(digest)} AS matches FROM postern.schema_migrations WHERE version={version} " + r"\gset")
        script.extend([r"\if :matches", r"\else", "DO $$ BEGIN RAISE EXCEPTION 'migration checksum mismatch'; END $$;", r"\endif", r"\else"])
        script.append(path.read_text())
        script.append(f"INSERT INTO postern.schema_migrations(version,sha256) VALUES({version},{literal(digest)});")
        script.append(r"\endif")
    versions=','.join(map(str,digests))
    script.append(f"DO $$ BEGIN IF EXISTS(SELECT 1 FROM postern.schema_migrations WHERE version NOT IN ({versions})) THEN RAISE EXCEPTION 'unknown migration version'; END IF; END $$; COMMIT;")
    db.execute('\n'.join(script))
    return digests


def restore_quarantined(db, dump, database):
    """Restore to a new database closed to application logins, then rotate generation.

    The privileged caller must separately reconcile later revocations/deletions and
    object availability. This function never grants CONNECT or activates a space.
    Cluster roles must already be provisioned (for example by apply_schema).
    """
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}',database) or database==db.database:
        raise ValueError('new, simple restore database name required')
    db.execute(f'CREATE DATABASE "{database}" ALLOW_CONNECTIONS false;\n'
               f'REVOKE CONNECT ON DATABASE "{database}" FROM PUBLIC;\n'
               f'ALTER DATABASE "{database}" ALLOW_CONNECTIONS true;')
    # A failed restore stays inaccessible to application logins for investigation.
    subprocess.run(['pg_restore','--exit-on-error','-d',database,str(dump)],env=db.env,
                   check=True,capture_output=True,timeout=180)
    restored=Postgres(db.env,database)
    restored.execute("BEGIN; UPDATE postern.authority SET state='quarantined',generation=gen_random_uuid(); "
                     "UPDATE postern.spaces SET state='suspended'; COMMIT;")
    return restored
