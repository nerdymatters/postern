-- Versioned M2 foundation. Applied transactionally by postern.pg.apply_schema.
-- Roles are NOLOGIN capabilities; a deployment provisions its own login identities.
DO $$ DECLARE r text; BEGIN
 FOREACH r IN ARRAY ARRAY['postern_owner','postern_reader','postern_writer','postern_identity_writer'] LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
   EXECUTE format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOBYPASSRLS',r);
  ELSIF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r AND (rolcanlogin OR rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication)) THEN
   RAISE EXCEPTION 'unsafe preexisting Postern role';
  END IF;
 END LOOP;
 IF pg_has_role('postern_reader','postern_owner','MEMBER') OR pg_has_role('postern_reader','postern_writer','MEMBER')
 OR pg_has_role('postern_reader','postern_identity_writer','MEMBER') OR pg_has_role('postern_writer','postern_owner','MEMBER')
 OR pg_has_role('postern_writer','postern_identity_writer','MEMBER') OR pg_has_role('postern_identity_writer','postern_owner','MEMBER')
 THEN RAISE EXCEPTION 'unsafe Postern role inheritance'; END IF;
END $$;
GRANT postern_reader TO postern_writer;
CREATE SCHEMA postern AUTHORIZATION postern_owner;
REVOKE ALL ON SCHEMA postern FROM PUBLIC;
SET LOCAL ROLE postern_owner;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
CREATE TABLE postern.schema_migrations(version integer PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE postern.authority (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 generation uuid NOT NULL DEFAULT gen_random_uuid(),
 state text NOT NULL DEFAULT 'quarantined' CHECK(state IN ('quarantined','active'))
);
INSERT INTO postern.authority DEFAULT VALUES;
-- This is an identity/status interface, not the managed profile/session store.
CREATE TABLE postern.principals (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), authority text NOT NULL CHECK(length(authority) BETWEEN 1 AND 255),
 account_id text NOT NULL CHECK(length(account_id) BETWEEN 1 AND 255),
 state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','deleted')),
 UNIQUE(authority,account_id), UNIQUE(id,account_id)
);
CREATE TABLE postern.owners (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), kind text NOT NULL CHECK(kind IN ('person','organization')),
 principal_id uuid REFERENCES postern.principals, label text NOT NULL CHECK(length(label) BETWEEN 1 AND 200),
 CHECK((kind='person')=(principal_id IS NOT NULL))
);
CREATE TABLE postern.spaces (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid NOT NULL REFERENCES postern.owners,
 title text NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
 state text NOT NULL DEFAULT 'suspended' CHECK(state IN ('active','suspended','archived')),
 policy_revision bigint NOT NULL DEFAULT 0 CHECK(policy_revision>=0)
);
CREATE TABLE postern.memberships (
 space_id uuid NOT NULL REFERENCES postern.spaces, id uuid NOT NULL DEFAULT gen_random_uuid(),
 principal_id uuid NOT NULL REFERENCES postern.principals,
 role text NOT NULL CHECK(role IN ('owner','admin','member')),
 state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked')),
 PRIMARY KEY(space_id,id)
);
CREATE UNIQUE INDEX membership_active ON postern.memberships(space_id,principal_id) WHERE state='active';
CREATE INDEX membership_principal ON postern.memberships(principal_id,space_id);
CREATE TABLE postern.invitations (
 space_id uuid NOT NULL REFERENCES postern.spaces, id uuid NOT NULL DEFAULT gen_random_uuid(),
 inviter_id uuid NOT NULL, recipient_id uuid REFERENCES postern.principals,
 recipient_email text CHECK(length(recipient_email) BETWEEN 3 AND 320),
 role text NOT NULL CHECK(role IN ('admin','member')), secret_hash bytea UNIQUE NOT NULL CHECK(octet_length(secret_hash)=32),
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','accepted','revoked','expired')),
 accepted_membership_id uuid, PRIMARY KEY(space_id,id),
 FOREIGN KEY(space_id,inviter_id) REFERENCES postern.memberships,
 FOREIGN KEY(space_id,accepted_membership_id) REFERENCES postern.memberships,
 CHECK(num_nonnulls(recipient_id,recipient_email)=1), CHECK(expires_at>created_at),
 CHECK((state='accepted')=(accepted_membership_id IS NOT NULL))
);
CREATE TABLE postern.audiences (
 space_id uuid NOT NULL REFERENCES postern.spaces, id uuid NOT NULL DEFAULT gen_random_uuid(),
 label text NOT NULL CHECK(length(label) BETWEEN 1 AND 200), PRIMARY KEY(space_id,id)
);
CREATE TABLE postern.audience_members (
 space_id uuid NOT NULL, audience_id uuid NOT NULL, membership_id uuid NOT NULL,
 PRIMARY KEY(space_id,audience_id,membership_id),
 FOREIGN KEY(space_id,audience_id) REFERENCES postern.audiences,
 FOREIGN KEY(space_id,membership_id) REFERENCES postern.memberships
);
CREATE INDEX audience_member_lookup ON postern.audience_members(space_id,membership_id,audience_id);
CREATE TABLE postern.resources (
 space_id uuid NOT NULL REFERENCES postern.spaces, id uuid NOT NULL DEFAULT gen_random_uuid(),
 kind text NOT NULL CHECK(length(kind) BETWEEN 1 AND 64), title text NOT NULL CHECK(length(title)<=1000),
 state text NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','published','archived')),
 content_revision bigint NOT NULL DEFAULT 1 CHECK(content_revision>0),
 adapter text NOT NULL CHECK(length(adapter) BETWEEN 1 AND 64), binding jsonb NOT NULL DEFAULT '{}',
 supported_actions text[] NOT NULL DEFAULT ARRAY['discover','view'],
 search tsvector GENERATED ALWAYS AS(to_tsvector('simple',title)) STORED,
 PRIMARY KEY(space_id,id),
 CHECK(supported_actions <@ ARRAY['discover','view','download','export','control','execute']::text[]),
 CHECK('discover'=ANY(supported_actions) AND cardinality(supported_actions)<=6), CHECK(jsonb_typeof(binding)='object')
);
CREATE INDEX resource_search ON postern.resources USING gin(search);
CREATE TABLE postern.resource_children (
 space_id uuid NOT NULL, parent_id uuid NOT NULL, child_id uuid NOT NULL, ordinal integer NOT NULL CHECK(ordinal>=0),
 PRIMARY KEY(space_id,parent_id,child_id), CHECK(parent_id<>child_id),
 FOREIGN KEY(space_id,parent_id) REFERENCES postern.resources,
 FOREIGN KEY(space_id,child_id) REFERENCES postern.resources
);
CREATE TABLE postern.grants (
 space_id uuid NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(), resource_id uuid NOT NULL,
 membership_id uuid, audience_id uuid, all_members boolean NOT NULL DEFAULT false,
 actions text[] NOT NULL, expires_at timestamptz,
 state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked')), PRIMARY KEY(space_id,id),
 FOREIGN KEY(space_id,resource_id) REFERENCES postern.resources,
 FOREIGN KEY(space_id,membership_id) REFERENCES postern.memberships,
 FOREIGN KEY(space_id,audience_id) REFERENCES postern.audiences,
 CHECK(num_nonnulls(membership_id,audience_id)+all_members::integer=1),
 CHECK(cardinality(actions) BETWEEN 1 AND 6 AND actions <@ ARRAY['discover','view','download','export','control','execute']::text[]),
 CHECK(NOT ('view'=ANY(actions)) OR 'discover'=ANY(actions))
);
CREATE INDEX grant_direct ON postern.grants(space_id,membership_id,resource_id) WHERE state='active';
CREATE INDEX grant_audience ON postern.grants(space_id,audience_id,resource_id) WHERE state='active';
CREATE INDEX grant_all ON postern.grants(space_id,resource_id) WHERE state='active' AND all_members;
CREATE TABLE postern.objects (
 space_id uuid NOT NULL, resource_id uuid NOT NULL, revision bigint NOT NULL CHECK(revision>0),
 object_key text NOT NULL UNIQUE, sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
 bytes bigint NOT NULL CHECK(bytes>=0), verified_at timestamptz NOT NULL,
 PRIMARY KEY(space_id,resource_id,revision), FOREIGN KEY(space_id,resource_id) REFERENCES postern.resources,
 CHECK(object_key=space_id::text||'/'||resource_id::text||'/'||revision::text||'/'||sha256)
);
CREATE TABLE postern.activity (
 space_id uuid NOT NULL REFERENCES postern.spaces, id uuid NOT NULL DEFAULT gen_random_uuid(),
 actor_id uuid REFERENCES postern.principals, action text NOT NULL, target_id uuid,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(), policy_revision bigint NOT NULL,
 detail jsonb NOT NULL DEFAULT '{}', PRIMARY KEY(space_id,id)
);
CREATE INDEX activity_actor ON postern.activity(space_id,actor_id,occurred_at DESC,id DESC);
CREATE TABLE postern.operations (
 space_id uuid NOT NULL REFERENCES postern.spaces, actor_id uuid NOT NULL REFERENCES postern.principals,
 key uuid NOT NULL, request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'), result jsonb NOT NULL,
 PRIMARY KEY(space_id,actor_id,key)
);
CREATE TABLE postern.outbox (
 space_id uuid NOT NULL, id uuid NOT NULL DEFAULT gen_random_uuid(), event_id uuid NOT NULL,
 kind text NOT NULL, payload jsonb NOT NULL, available_at timestamptz NOT NULL DEFAULT now(),
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0), lease_owner uuid, lease_until timestamptz, delivered_at timestamptz,
 PRIMARY KEY(space_id,id), FOREIGN KEY(space_id,event_id) REFERENCES postern.activity,
 CHECK((lease_owner IS NULL)=(lease_until IS NULL))
);
CREATE INDEX outbox_pending ON postern.outbox(available_at) WHERE delivered_at IS NULL;
CREATE TABLE postern.imports (
 space_id uuid NOT NULL REFERENCES postern.spaces, installation uuid NOT NULL,
 digest text NOT NULL CHECK(digest ~ '^[0-9a-f]{64}$'), imported_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(space_id,installation), UNIQUE(installation), UNIQUE(space_id)
);
CREATE TABLE postern.legacy_bindings (
 space_id uuid NOT NULL, installation uuid NOT NULL, legacy_email text NOT NULL,
 principal_id uuid NOT NULL REFERENCES postern.principals,
 evidence_ref text NOT NULL CHECK(length(evidence_ref) BETWEEN 1 AND 512),
 PRIMARY KEY(space_id,installation,legacy_email), UNIQUE(space_id,installation,principal_id),
 FOREIGN KEY(space_id,installation) REFERENCES postern.imports
);
CREATE TABLE postern.legacy_rows (
 space_id uuid NOT NULL, installation uuid NOT NULL, table_name text NOT NULL, row_key text NOT NULL,
 original bytea NOT NULL, sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
 PRIMARY KEY(space_id,installation,table_name,row_key),
 FOREIGN KEY(space_id,installation) REFERENCES postern.imports
);
CREATE TABLE postern.legacy_map (
 space_id uuid NOT NULL, installation uuid NOT NULL, kind text NOT NULL, old_id text NOT NULL, resource_id uuid NOT NULL,
 PRIMARY KEY(space_id,installation,kind,old_id), UNIQUE(space_id,resource_id),
 FOREIGN KEY(space_id,installation) REFERENCES postern.imports,
 FOREIGN KEY(space_id,resource_id) REFERENCES postern.resources
);
-- Every scoped relation gets fail-closed RLS. Runtime has no direct write privileges.
DO $$ DECLARE t text; BEGIN
 FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='postern' LOOP
  EXECUTE format('ALTER TABLE postern.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE postern.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY internal ON postern.%I TO postern_owner USING(true) WITH CHECK(true)',t);
 END LOOP;
END $$;
CREATE FUNCTION postern.actor() RETURNS uuid LANGUAGE sql STABLE SET search_path=pg_catalog
 AS $$ SELECT nullif(current_setting('postern.actor_id',true),'')::uuid $$;
CREATE FUNCTION postern.scope() RETURNS uuid LANGUAGE sql STABLE SET search_path=pg_catalog
 AS $$ SELECT nullif(current_setting('postern.space_id',true),'')::uuid $$;
CREATE FUNCTION postern.current_role(s uuid) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,postern
 AS $$ SELECT m.role FROM postern.memberships m JOIN postern.principals p ON p.id=m.principal_id
 JOIN postern.spaces s0 ON s0.id=m.space_id CROSS JOIN postern.authority a
 WHERE m.space_id=s AND s=postern.scope() AND m.principal_id=postern.actor()
 AND m.state='active' AND p.state='active' AND s0.state='active' AND a.state='active'
 AND a.generation=nullif(current_setting('postern.generation',true),'')::uuid $$;
CREATE FUNCTION postern.can_resource(s uuid,r uuid,act text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,postern
 AS $$ SELECT EXISTS(SELECT 1 FROM postern.resources x WHERE x.space_id=s AND x.id=r
 AND act IN ('discover','view') AND act=ANY(x.supported_actions) AND postern.current_role(s) IS NOT NULL
 AND (postern.current_role(s) IN ('owner','admin') OR (x.state='published' AND EXISTS(
 SELECT 1 FROM postern.grants g JOIN postern.memberships m ON m.space_id=g.space_id
 AND m.principal_id=postern.actor() AND m.state='active'
 WHERE g.space_id=s AND g.resource_id=r AND g.state='active' AND act=ANY(g.actions)
 AND (g.expires_at IS NULL OR g.expires_at>statement_timestamp())
 AND (g.all_members OR g.membership_id=m.id OR EXISTS(SELECT 1 FROM postern.audience_members am
 WHERE am.space_id=s AND am.audience_id=g.audience_id AND am.membership_id=m.id)))))) $$;
CREATE POLICY catalog_read ON postern.resources TO postern_reader USING(postern.can_resource(space_id,id,'discover'));
CREATE POLICY space_read ON postern.spaces TO postern_reader USING(postern.current_role(id) IS NOT NULL);
CREATE POLICY membership_read ON postern.memberships TO postern_reader USING(
 postern.current_role(space_id) IN ('owner','admin') OR (postern.current_role(space_id) IS NOT NULL AND principal_id=postern.actor()));
CREATE POLICY history_read ON postern.activity TO postern_reader USING(
 postern.current_role(space_id) IN ('owner','admin') OR (postern.current_role(space_id) IS NOT NULL AND actor_id=postern.actor()));
-- No grants on source bindings, object locations, invitation secrets or raw legacy records.
GRANT USAGE ON SCHEMA postern TO postern_reader,postern_identity_writer;
GRANT SELECT(id,space_id,kind,title,state,content_revision) ON postern.resources TO postern_reader;
GRANT SELECT ON postern.spaces,postern.memberships,postern.activity TO postern_reader;
GRANT EXECUTE ON FUNCTION postern.actor(),postern.scope(),postern.current_role(uuid),postern.can_resource(uuid,uuid,text) TO postern_reader;

CREATE FUNCTION postern.immutable_row() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog
 AS $$ BEGIN RAISE EXCEPTION 'immutable history or identity'; END $$;
CREATE TRIGGER immutable_activity BEFORE UPDATE OR DELETE ON postern.activity FOR EACH ROW EXECUTE FUNCTION postern.immutable_row();
CREATE TRIGGER immutable_legacy BEFORE UPDATE OR DELETE ON postern.legacy_rows FOR EACH ROW EXECUTE FUNCTION postern.immutable_row();
CREATE TRIGGER immutable_mapping BEFORE UPDATE OR DELETE ON postern.legacy_map FOR EACH ROW EXECUTE FUNCTION postern.immutable_row();
CREATE TRIGGER immutable_binding BEFORE UPDATE OR DELETE ON postern.legacy_bindings FOR EACH ROW EXECUTE FUNCTION postern.immutable_row();
CREATE TRIGGER immutable_object BEFORE UPDATE OR DELETE ON postern.objects FOR EACH ROW EXECUTE FUNCTION postern.immutable_row();
CREATE TRIGGER immutable_resource_scope BEFORE UPDATE OF space_id,id ON postern.resources FOR EACH ROW EXECUTE FUNCTION postern.immutable_row();
CREATE TRIGGER immutable_principal_identity BEFORE UPDATE OF id,authority,account_id ON postern.principals FOR EACH ROW EXECUTE FUNCTION postern.immutable_row();
CREATE TRIGGER immutable_owner_identity BEFORE UPDATE OF id,kind,principal_id ON postern.owners FOR EACH ROW EXECUTE FUNCTION postern.immutable_row();
CREATE FUNCTION postern.principal_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF OLD.state='deleted' AND NEW.state<>'deleted' THEN RAISE EXCEPTION 'deleted principal cannot reactivate'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER principal_guard BEFORE UPDATE OF state ON postern.principals FOR EACH ROW EXECUTE FUNCTION postern.principal_guard();
CREATE FUNCTION postern.membership_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,postern AS $$
BEGIN
 IF TG_OP='UPDATE' AND (NEW.space_id<>OLD.space_id OR NEW.id<>OLD.id OR NEW.principal_id<>OLD.principal_id
 OR (OLD.state='revoked' AND NEW.state<>'revoked')) THEN RAISE EXCEPTION 'membership identity or revoked state is immutable'; END IF;
 PERFORM 1 FROM postern.spaces WHERE id=COALESCE(NEW.space_id,OLD.space_id) FOR UPDATE;
 RETURN COALESCE(NEW,OLD);
END $$;
CREATE TRIGGER membership_guard BEFORE INSERT OR UPDATE OR DELETE ON postern.memberships FOR EACH ROW EXECUTE FUNCTION postern.membership_guard();
CREATE FUNCTION postern.owner_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE s uuid;
BEGIN
 IF TG_TABLE_NAME='spaces' THEN s=COALESCE(NEW.id,OLD.id); ELSE s=COALESCE(NEW.space_id,OLD.space_id); END IF;
 IF EXISTS(SELECT 1 FROM postern.spaces WHERE id=s AND state='active') AND NOT EXISTS(
 SELECT 1 FROM postern.memberships m JOIN postern.principals p ON p.id=m.principal_id
 WHERE m.space_id=s AND m.role='owner' AND m.state='active' AND p.state='active')
 THEN RAISE EXCEPTION 'active space requires an active owner'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER space_owner AFTER INSERT OR UPDATE ON postern.spaces DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION postern.owner_guard();
CREATE CONSTRAINT TRIGGER membership_owner AFTER INSERT OR UPDATE OR DELETE ON postern.memberships DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION postern.owner_guard();

CREATE FUNCTION postern.revoke_member(s uuid,m uuid,k uuid,h text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE a uuid=postern.actor(); target postern.memberships; caller_role text; prior postern.operations; rev bigint; ev uuid; answer jsonb;
BEGIN
 -- Account locks precede space locks; suspension uses exclusive locks on the same rows.
 PERFORM 1 FROM postern.principals WHERE id=a OR id=(SELECT principal_id FROM postern.memberships WHERE space_id=s AND id=m) ORDER BY id FOR SHARE;
 PERFORM 1 FROM postern.spaces WHERE id=s FOR UPDATE;
 caller_role=postern.current_role(s);
 IF caller_role IS NULL OR caller_role NOT IN ('owner','admin') THEN RAISE EXCEPTION 'forbidden'; END IF;
 SELECT * INTO prior FROM postern.operations WHERE space_id=s AND actor_id=a AND key=k;
 IF FOUND THEN IF prior.request_hash<>h OR prior.result->>'target'<>m::text THEN RAISE EXCEPTION 'operation mismatch'; END IF; RETURN prior.result; END IF;
 SELECT * INTO target FROM postern.memberships WHERE space_id=s AND id=m AND state='active';
 IF NOT FOUND OR (caller_role='admin' AND target.role<>'member') THEN RAISE EXCEPTION 'forbidden'; END IF;
 UPDATE postern.memberships SET state='revoked' WHERE space_id=s AND id=m;
 DELETE FROM postern.audience_members WHERE space_id=s AND membership_id=m;
 UPDATE postern.grants SET state='revoked' WHERE space_id=s AND membership_id=m;
 UPDATE postern.spaces SET policy_revision=policy_revision+1 WHERE id=s RETURNING policy_revision INTO rev;
 INSERT INTO postern.activity(space_id,actor_id,action,target_id,policy_revision) VALUES(s,a,'membership.revoked',m,rev) RETURNING id INTO ev;
 INSERT INTO postern.outbox(space_id,event_id,kind,payload) VALUES(s,ev,'policy.changed',jsonb_build_object('revision',rev));
 answer=jsonb_build_object('target',m,'revision',rev);
 INSERT INTO postern.operations VALUES(s,a,k,h,answer);
 RETURN answer;
END $$;
GRANT EXECUTE ON FUNCTION postern.revoke_member(uuid,uuid,uuid,text) TO postern_writer;
-- Only a trusted account service can call this; its profile/session deletion joins this transaction.
CREATE FUNCTION postern.disable_principal(p uuid,new_state text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE s uuid; rev bigint; ev uuid; old_state text;
BEGIN
 IF new_state NOT IN ('suspended','deleted') THEN RAISE EXCEPTION 'invalid lifecycle transition'; END IF;
 SELECT state INTO old_state FROM postern.principals WHERE id=p FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'unknown principal'; END IF;
 IF old_state=new_state OR old_state='deleted' THEN RETURN; END IF;
 PERFORM 1 FROM postern.spaces WHERE id IN(SELECT space_id FROM postern.memberships WHERE principal_id=p AND state='active') ORDER BY id FOR UPDATE;
 UPDATE postern.principals SET state=new_state WHERE id=p;
 FOR s IN SELECT DISTINCT space_id FROM postern.memberships WHERE principal_id=p AND state='active' ORDER BY space_id LOOP
  UPDATE postern.memberships SET state='revoked' WHERE space_id=s AND principal_id=p AND state='active';
  DELETE FROM postern.audience_members am USING postern.memberships m WHERE am.space_id=s AND m.space_id=s AND am.membership_id=m.id AND m.principal_id=p;
  UPDATE postern.grants g SET state='revoked' FROM postern.memberships m WHERE g.space_id=s AND m.space_id=s AND g.membership_id=m.id AND m.principal_id=p;
  UPDATE postern.spaces SET policy_revision=policy_revision+1,state=CASE WHEN state='active' AND NOT EXISTS(
   SELECT 1 FROM postern.memberships m JOIN postern.principals a ON a.id=m.principal_id WHERE m.space_id=s AND m.state='active' AND m.role='owner' AND a.state='active') THEN 'suspended' ELSE state END
   WHERE id=s RETURNING policy_revision INTO rev;
  INSERT INTO postern.activity(space_id,action,target_id,policy_revision,detail)
   VALUES(s,'account.'||new_state,p,rev,jsonb_build_object('service','account-authority','subject_id',p)) RETURNING id INTO ev;
  INSERT INTO postern.outbox(space_id,event_id,kind,payload) VALUES(s,ev,'policy.changed',jsonb_build_object('revision',rev));
 END LOOP;
END $$;
GRANT EXECUTE ON FUNCTION postern.disable_principal(uuid,text) TO postern_identity_writer;
CREATE FUNCTION postern.resume_principal(p uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
BEGIN
 PERFORM 1 FROM postern.principals WHERE id=p AND state IN ('active','suspended') FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'principal cannot resume'; END IF;
 -- Membership IDs stay revoked. Account resumption never readmits a former member.
 UPDATE postern.principals SET state='active' WHERE id=p;
END $$;
GRANT EXECUTE ON FUNCTION postern.resume_principal(uuid) TO postern_identity_writer;
RESET ROLE;
