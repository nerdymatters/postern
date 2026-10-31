"""Owned, disposable PostgreSQL cluster for tests/rehearsals. Never reuses a server."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from postern.pg import Postgres


@contextmanager
def local_postgres(log):
    env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
    version=subprocess.check_output(['postgres','--version'],text=True,env=env)
    if int(re.search(r'(\d+)\.',version).group(1))<17:
        raise RuntimeError('PostgreSQL 17+ CLI tools required')
    home=Path(tempfile.mkdtemp(prefix='postern-schema-'))
    cluster=home/'db'
    env.update(PGHOST=str(home),PGPORT='54873',PGUSER='postern_rehearsal',PGCONNECT_TIMEOUT='3',
               PGOPTIONS='-c statement_timeout=15000 -c lock_timeout=10000',PGAPPNAME='postern_schema_rehearsal')
    log=Path(log).resolve(); log.parent.mkdir(parents=True,exist_ok=True)
    try:
        subprocess.run(['initdb','-D',str(cluster),'-U',env['PGUSER'],'--auth=trust','--encoding=UTF8','--locale=C'],
                       env=env,check=True,capture_output=True,timeout=30)
        with (cluster/'postgresql.conf').open('a') as f:
            # tempfile's path is quoted for PostgreSQL config, not evaluated by a shell.
            socket=str(home).replace("'","''")
            f.write(f"\nlisten_addresses=''\nunix_socket_directories='{socket}'\nunix_socket_permissions=0700\nport=54873\nmax_connections=16\nshared_buffers='64MB'\n")
        subprocess.run(['pg_ctl','-D',str(cluster),'-l',str(log),'-w','start'],env=env,check=True,capture_output=True,timeout=30)
        yield Postgres(env)
    finally:
        if (cluster/'postmaster.pid').exists():
            stop=subprocess.run(['pg_ctl','-D',str(cluster),'-m','fast','-w','stop'],env=env,capture_output=True,timeout=20)
            if stop.returncode:
                raise RuntimeError(f'Owned cluster did not stop; retained at {home}')
        shutil.rmtree(home)
