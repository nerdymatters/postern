"""Real PostgreSQL only: always an owned cluster, never a developer/production DSN."""
from types import SimpleNamespace
import uuid

import pytest

from postern.pg import apply_schema, literal
from scripts.local_postgres import local_postgres


@pytest.fixture(scope='module')
def db(tmp_path_factory):
    with local_postgres(tmp_path_factory.mktemp('postgres')/'server.log') as connection:
        apply_schema(connection)
        connection.execute('CREATE ROLE postern_test_reader LOGIN IN ROLE postern_reader;')
        yield connection


@pytest.fixture
def model(db):
    ids={k:str(uuid.uuid4()) for k in ['a','b','c','d','s','t','owner','owner2','ma','mb','mc','md','mt','mtb','r','hidden','draft','foreign','group','group_resource','expired']}
    f=SimpleNamespace(**ids,db=db)
    q=lambda k:literal(ids[k])+'::uuid'
    db.execute("UPDATE postern.authority SET state='active',generation=gen_random_uuid();")
    f.generation=db.scalar('SELECT generation FROM postern.authority;')
    def context(actor='b',space='s',role='postern_reader'):
        return (f'SET ROLE {role}; SET postern.actor_id='+"'"+ids[actor]+"'; SET postern.space_id='"+ids[space]+"'; SET postern.generation='"+f.generation+"';")
    f.context=context
    db.execute('BEGIN;'+''.join('INSERT INTO postern.principals(id,authority,account_id) VALUES('+q(k)+",'fixture',"+literal(ids[k])+');' for k in ['a','b','c','d'])+
        "INSERT INTO postern.owners VALUES("+q('owner')+",'person',"+q('a')+",'Owner A'),("+q('owner2')+",'person',"+q('c')+",'Owner B');"+
        "INSERT INTO postern.spaces(id,owner_id,title,state) VALUES("+q('s')+','+q('owner')+",'A','active'),("+q('t')+','+q('owner2')+",'B','active');"+
        ''.join('INSERT INTO postern.memberships VALUES('+q(s)+','+q(m)+','+q(p)+','+literal(role)+",'active');" for s,m,p,role in [('s','ma','a','owner'),('s','mb','b','member'),('s','md','d','admin'),('t','mt','c','owner'),('t','mtb','a','member')])+
        ''.join('INSERT INTO postern.resources(space_id,id,kind,title,state,adapter) VALUES('+q(s)+','+q(r)+",'document',"+literal(title)+','+literal(state)+",'test');" for s,r,title,state in [('s','r','Visible คู่มือพนักงาน','published'),('s','hidden','Hidden คู่มือพนักงาน','published'),('s','draft','Draft','draft'),('t','foreign','Foreign secret','published'),('s','group_resource','Audience','published'),('s','expired','Expired','published')])+
        'INSERT INTO postern.audiences VALUES('+q('s')+','+q('group')+",'Group');"+
        'INSERT INTO postern.audience_members VALUES('+q('s')+','+q('group')+','+q('mb')+');'+
        'INSERT INTO postern.grants(space_id,resource_id,membership_id,actions) VALUES('+q('s')+','+q('r')+','+q('mb')+",ARRAY['discover','view']);"+
        'INSERT INTO postern.grants(space_id,resource_id,audience_id,actions) VALUES('+q('s')+','+q('group_resource')+','+q('group')+",ARRAY['discover']);"+
        'INSERT INTO postern.grants(space_id,resource_id,all_members,actions,expires_at) VALUES('+q('s')+','+q('expired')+",true,ARRAY['discover','view'],now()-interval '1 second');COMMIT;")
    return f
