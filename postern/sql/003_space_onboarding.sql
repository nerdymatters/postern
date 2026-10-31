-- Operator-approved creation and account-bound invitations. 001/002 stay immutable.
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='postern_onboarding_service') THEN
  CREATE ROLE postern_onboarding_service NOLOGIN NOSUPERUSER NOBYPASSRLS;
 ELSIF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='postern_onboarding_service' AND
 (rolcanlogin OR rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication))
 THEN RAISE EXCEPTION 'unsafe preexisting Postern role'; END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname IN ('postern_owner','postern_reader','postern_writer','postern_identity_writer','postern_resource_service')
 AND pg_has_role('postern_onboarding_service',oid,'MEMBER')) THEN RAISE EXCEPTION 'unsafe onboarding inheritance'; END IF;
END $$;
SET LOCAL ROLE postern_owner;
CREATE TABLE postern.creation_entitlements (
 principal_id uuid PRIMARY KEY REFERENCES postern.principals,
 max_spaces integer NOT NULL CHECK(max_spaces BETWEEN 0 AND 100000)
);
CREATE TABLE postern.space_creations (
 principal_id uuid NOT NULL REFERENCES postern.principals, operation_id uuid NOT NULL,
 request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'), generation uuid NOT NULL,
 space_id uuid NOT NULL UNIQUE REFERENCES postern.spaces, result jsonb NOT NULL,
 PRIMARY KEY(principal_id,operation_id)
);
CREATE TABLE postern.onboarding_policy (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 max_pending_invitations integer NOT NULL DEFAULT 100 CHECK(max_pending_invitations BETWEEN 1 AND 1000)
);
INSERT INTO postern.onboarding_policy DEFAULT VALUES;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['creation_entitlements','space_creations','onboarding_policy'] LOOP
  EXECUTE format('ALTER TABLE postern.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE postern.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY internal ON postern.%I TO postern_owner USING(true) WITH CHECK(true)',t);
 END LOOP;
END $$;
CREATE TRIGGER immutable_creation BEFORE UPDATE OR DELETE ON postern.space_creations FOR EACH ROW EXECUTE FUNCTION postern.immutable_row();
ALTER TABLE postern.invitations ADD COLUMN generation uuid;
ALTER TABLE postern.invitations ADD COLUMN accepted_at timestamptz;
-- Old pending rows were never issued by this protocol: do not silently activate them.
UPDATE postern.invitations SET generation=(SELECT generation FROM postern.authority),state=CASE WHEN state='pending' THEN 'revoked' ELSE state END;
ALTER TABLE postern.invitations ALTER COLUMN generation SET NOT NULL;
CREATE INDEX invitation_pending ON postern.invitations(space_id,expires_at) WHERE state='pending';

CREATE FUNCTION postern.onboarding_context(login uuid,s uuid DEFAULT NULL,other uuid DEFAULT NULL,exclusive_actor boolean DEFAULT false) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE p uuid; ctx jsonb;
BEGIN
 SELECT principal_id INTO p FROM postern.account_logins WHERE id=login;
 IF NOT FOUND THEN RAISE EXCEPTION 'invalid login' USING ERRCODE='28000'; END IF;
 IF exclusive_actor THEN PERFORM 1 FROM postern.principals WHERE id=p FOR UPDATE;
 ELSE PERFORM 1 FROM postern.principals WHERE id IN(p,other) ORDER BY id FOR SHARE; END IF;
 PERFORM 1 FROM postern.authority FOR SHARE;
 PERFORM 1 FROM postern.account_logins WHERE id=login FOR SHARE;
 IF s IS NOT NULL THEN PERFORM 1 FROM postern.spaces WHERE id=s FOR UPDATE; END IF;
 ctx=postern.login_context(login,NULL);
 IF ctx IS NULL THEN RAISE EXCEPTION 'invalid login' USING ERRCODE='28000'; END IF;
 IF NOT EXISTS(SELECT 1 FROM postern.authority WHERE state='active') THEN RAISE EXCEPTION 'authority unavailable' USING ERRCODE='55000'; END IF;
 PERFORM set_config('postern.actor_id',p::text,true);
 PERFORM set_config('postern.generation',ctx->>'generation',true);
 PERFORM set_config('postern.space_id',coalesce(s::text,''),true);
 RETURN ctx;
END $$;
CREATE FUNCTION postern.onboarding_event(s uuid,act text,target uuid,info jsonb DEFAULT '{}') RETURNS bigint
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE rev bigint; ev uuid;
BEGIN
 IF postern.current_role(s) IS NULL THEN RAISE EXCEPTION 'forbidden'; END IF;
 UPDATE postern.spaces SET policy_revision=policy_revision+1 WHERE id=s RETURNING policy_revision INTO rev;
 INSERT INTO postern.activity(space_id,actor_id,action,target_id,policy_revision,detail)
 VALUES(s,postern.actor(),act,target,rev,info) RETURNING id INTO ev;
 INSERT INTO postern.outbox(space_id,event_id,kind,payload) VALUES(s,ev,'policy.changed',jsonb_build_object('revision',rev));
 RETURN rev;
END $$;
CREATE FUNCTION postern.create_space(login uuid,k uuid,title text,h text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; p uuid; o uuid; s uuid; m uuid; lim integer; prior postern.space_creations; answer jsonb;
BEGIN
 ctx=postern.onboarding_context(login,NULL,NULL,true); p=(ctx->>'principal_id')::uuid;
 IF title IS NULL OR length(title) NOT BETWEEN 1 AND 200 OR title<>btrim(title) OR title ~ '[[:cntrl:]]'
 THEN RAISE EXCEPTION 'invalid title' USING ERRCODE='22023'; END IF;
 SELECT * INTO prior FROM postern.space_creations WHERE principal_id=p AND operation_id=k;
 IF FOUND THEN
  IF prior.request_hash<>h OR prior.result->>'title'<>title OR prior.generation<>(ctx->>'generation')::uuid THEN RAISE EXCEPTION 'operation mismatch'; END IF;
  PERFORM 1 FROM postern.spaces WHERE id=prior.space_id FOR SHARE;
  PERFORM set_config('postern.space_id',prior.space_id::text,true);
  IF postern.current_role(prior.space_id) IS DISTINCT FROM 'owner' THEN RAISE EXCEPTION 'forbidden'; END IF;
  RETURN prior.result;
 END IF;
 SELECT max_spaces INTO lim FROM postern.creation_entitlements WHERE principal_id=p FOR SHARE;
 IF lim IS NULL OR lim=0 THEN RAISE EXCEPTION 'creation not enabled' USING ERRCODE='PT403'; END IF;
 IF (SELECT count(*) FROM postern.space_creations WHERE principal_id=p)>=lim THEN RAISE EXCEPTION 'creation limit reached' USING ERRCODE='PT409'; END IF;
 SELECT id INTO o FROM postern.owners WHERE kind='person' AND principal_id=p ORDER BY id LIMIT 1;
 IF o IS NULL THEN INSERT INTO postern.owners(kind,principal_id,label) VALUES('person',p,'Personal owner') RETURNING id INTO o; END IF;
 INSERT INTO postern.spaces(owner_id,title,state) VALUES(o,title,'active') RETURNING id INTO s;
 INSERT INTO postern.memberships(space_id,principal_id,role) VALUES(s,p,'owner') RETURNING id INTO m;
 PERFORM set_config('postern.space_id',s::text,true);
 PERFORM postern.onboarding_event(s,'space.created',s);
 answer=jsonb_build_object('space_id',s,'owner_id',o,'membership_id',m,'role','owner','title',title);
 INSERT INTO postern.space_creations VALUES(p,k,h,(ctx->>'generation')::uuid,s,answer);
 RETURN answer;
END $$;

CREATE FUNCTION postern.my_spaces(login uuid,after_id uuid DEFAULT NULL) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; p uuid; ids uuid[]; items jsonb='[]'; row record; lim integer; used bigint; next_id uuid;
BEGIN
 ctx=postern.onboarding_context(login);p=(ctx->>'principal_id')::uuid;
 SELECT max_spaces INTO lim FROM postern.creation_entitlements WHERE principal_id=p;
 SELECT count(*) INTO used FROM postern.space_creations WHERE principal_id=p;
 SELECT array_agg(id ORDER BY id) INTO ids FROM (SELECT s.id FROM postern.spaces s JOIN postern.memberships m ON m.space_id=s.id
 WHERE m.principal_id=p AND m.state='active' AND s.state='active' AND (after_id IS NULL OR s.id>after_id) ORDER BY s.id LIMIT 51) q;
 PERFORM 1 FROM postern.spaces WHERE id=ANY(ids) ORDER BY id FOR SHARE;
 -- A revocation while waiting must not truncate pagination or return a stale ID.
 IF (SELECT count(*) FROM postern.spaces s JOIN postern.memberships m ON m.space_id=s.id
 WHERE s.id=ANY(ids) AND s.state='active' AND m.principal_id=p AND m.state='active')<>coalesce(cardinality(ids),0)
 THEN RAISE EXCEPTION 'space list changed' USING ERRCODE='PT412'; END IF;
 FOR row IN SELECT s.id,s.title,m.id AS membership_id,m.role,s.policy_revision FROM postern.spaces s JOIN postern.memberships m ON m.space_id=s.id
 WHERE s.id=ANY(ids) AND s.state='active' AND m.principal_id=p AND m.state='active' ORDER BY s.id LOOP
  IF jsonb_array_length(items)=50 THEN next_id=(items->49->>'space_id')::uuid; EXIT; END IF;
  PERFORM set_config('postern.space_id',row.id::text,true);
  PERFORM postern.record_disclosure(row.id,'space.listed');
  items=items||jsonb_build_array(jsonb_build_object('space_id',row.id,'title',row.title,'membership_id',row.membership_id,'role',row.role,'policy_revision',row.policy_revision));
 END LOOP;
 RETURN jsonb_build_object('account_reference',jsonb_build_object('authority',ctx->>'authority','account_id',ctx->>'account_id'),
 'spaces',items,'next_after',next_id,'creation',jsonb_build_object('allowed',coalesce(lim,0)>used,'limit',coalesce(lim,0),'used',used));
END $$;

CREATE FUNCTION postern.invitation_request(h text,s uuid,aud text,recipient uuid DEFAULT NULL) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE p uuid; ctx jsonb;
BEGIN
 SELECT l.principal_id INTO p FROM postern.resource_sessions r JOIN postern.account_logins l ON l.id=r.login_id
 WHERE r.token_hash=h AND r.space_id=s AND r.audience=aud;
 IF NOT FOUND THEN RAISE EXCEPTION 'invalid session' USING ERRCODE='28000'; END IF;
 PERFORM 1 FROM postern.principals WHERE id IN(p,recipient) ORDER BY id FOR SHARE;
 PERFORM 1 FROM postern.authority FOR SHARE;
 PERFORM 1 FROM postern.account_logins WHERE id=(SELECT login_id FROM postern.resource_sessions WHERE token_hash=h) FOR SHARE;
 -- Acquire UPDATE immediately, never upgrade a shared space lock under contention.
 PERFORM 1 FROM postern.spaces WHERE id=s FOR UPDATE;
 ctx=postern.open_resource_request(h,s,aud);
 IF ctx->>'role' NOT IN ('owner','admin') THEN RAISE EXCEPTION 'forbidden'; END IF;
 RETURN ctx;
END $$;
CREATE FUNCTION postern.create_invitation(h text,s uuid,aud text,k uuid,fingerprint text,recipient_authority text,recipient_account text,offered text,ttl integer,secret bytea) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; p uuid; r uuid; prior postern.operations; inv postern.invitations; answer jsonb; lim integer;
BEGIN
 SELECT id INTO r FROM postern.principals WHERE authority=recipient_authority AND account_id=recipient_account;
 ctx=postern.invitation_request(h,s,aud,r);p=(ctx->>'principal_id')::uuid;
 IF offered NOT IN ('member','admin') OR ttl NOT BETWEEN 60 AND 604800 OR octet_length(secret)<>32 THEN RAISE EXCEPTION 'invalid invitation' USING ERRCODE='22023'; END IF;
 IF offered='admin' AND ctx->>'role'<>'owner' THEN RAISE EXCEPTION 'forbidden'; END IF;
 SELECT * INTO prior FROM postern.operations WHERE space_id=s AND actor_id=p AND key=k;
 IF FOUND THEN
  IF prior.request_hash<>fingerprint THEN RAISE EXCEPTION 'operation mismatch'; END IF;
  SELECT * INTO inv FROM postern.invitations WHERE space_id=s AND id=(prior.result->>'invitation_id')::uuid;
  IF inv.secret_hash IS DISTINCT FROM secret THEN RAISE EXCEPTION 'operation mismatch'; END IF;
  IF inv.state<>'pending' OR inv.expires_at<=clock_timestamp() OR inv.generation<>(ctx->>'generation')::uuid
  OR inv.inviter_id<>(ctx->>'membership_id')::uuid OR NOT EXISTS(SELECT 1 FROM postern.principals WHERE id=r AND state='active') THEN RAISE EXCEPTION 'invitation unavailable' USING ERRCODE='42501'; END IF;
  RETURN prior.result;
 END IF;
 IF r IS NULL OR NOT EXISTS(SELECT 1 FROM postern.principals WHERE id=r AND state='active') THEN RAISE EXCEPTION 'recipient unavailable' USING ERRCODE='42501'; END IF;
 IF EXISTS(SELECT 1 FROM postern.memberships WHERE space_id=s AND principal_id=r AND state='active') THEN RAISE EXCEPTION 'already a member' USING ERRCODE='PT409'; END IF;
 SELECT max_pending_invitations INTO lim FROM postern.onboarding_policy FOR SHARE;
 IF (SELECT count(*) FROM postern.invitations WHERE space_id=s AND state='pending' AND expires_at>clock_timestamp() AND generation=(ctx->>'generation')::uuid)>=lim
 THEN RAISE EXCEPTION 'invitation limit reached' USING ERRCODE='PT409'; END IF;
 INSERT INTO postern.invitations(space_id,inviter_id,recipient_id,role,secret_hash,expires_at,generation)
 VALUES(s,(ctx->>'membership_id')::uuid,r,offered,secret,clock_timestamp()+make_interval(secs=>ttl),(ctx->>'generation')::uuid) RETURNING * INTO inv;
 PERFORM postern.onboarding_event(s,'invitation.created',inv.id,jsonb_build_object('role',offered));
 answer=jsonb_build_object('space_id',s,'invitation_id',inv.id,'role',offered,'expires_at',floor(extract(epoch FROM inv.expires_at)));
 INSERT INTO postern.operations VALUES(s,p,k,fingerprint,answer);
 RETURN answer;
END $$;
CREATE FUNCTION postern.list_invitations(h text,s uuid,aud text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; answer jsonb;
BEGIN
 ctx=postern.open_resource_request(h,s,aud);
 IF ctx->>'role' NOT IN ('owner','admin') THEN RAISE EXCEPTION 'forbidden'; END IF;
 SELECT coalesce(jsonb_agg(q ORDER BY q.invitation_id),'[]') INTO answer FROM (
 SELECT i.id AS invitation_id,i.role,floor(extract(epoch FROM i.expires_at)) AS expires_at,p.authority,p.account_id
 FROM postern.invitations i JOIN postern.principals p ON p.id=i.recipient_id WHERE i.space_id=s AND i.state='pending'
 AND i.expires_at>clock_timestamp() AND i.generation=(ctx->>'generation')::uuid AND (ctx->>'role'='owner' OR i.role='member')
 ORDER BY i.id LIMIT 1000) q;
 PERFORM postern.record_disclosure(s,'invitations.listed');
 RETURN jsonb_build_object('invitations',answer);
END $$;
CREATE FUNCTION postern.revoke_invitation(h text,s uuid,aud text,i uuid,k uuid,fingerprint text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; inv postern.invitations; prior postern.operations; answer jsonb;
BEGIN
 ctx=postern.invitation_request(h,s,aud);
 SELECT * INTO inv FROM postern.invitations WHERE space_id=s AND id=i;
 IF NOT FOUND OR inv.generation<>(ctx->>'generation')::uuid THEN RAISE EXCEPTION 'invitation unavailable' USING ERRCODE='42501'; END IF;
 IF inv.role='admin' AND ctx->>'role'<>'owner' THEN RAISE EXCEPTION 'forbidden'; END IF;
 SELECT * INTO prior FROM postern.operations WHERE space_id=s AND actor_id=(ctx->>'principal_id')::uuid AND key=k;
 IF FOUND THEN
  IF prior.request_hash<>fingerprint OR prior.result->>'invitation_id'<>i::text THEN RAISE EXCEPTION 'operation mismatch'; END IF;
  RETURN prior.result;
 END IF;
 IF inv.state<>'pending' THEN RAISE EXCEPTION 'invitation unavailable' USING ERRCODE='42501'; END IF;
 UPDATE postern.invitations SET state='revoked' WHERE space_id=s AND id=i;
 PERFORM postern.onboarding_event(s,'invitation.revoked',i);
 answer=jsonb_build_object('invitation_id',i,'revoked',true);
 INSERT INTO postern.operations VALUES(s,(ctx->>'principal_id')::uuid,k,fingerprint,answer);
 RETURN answer;
END $$;
CREATE FUNCTION postern.accept_invitation(login uuid,s uuid,i uuid,secret bytea) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE inv postern.invitations; ctx jsonb; inviter uuid; p uuid; m postern.memberships; inviter_role text; is_new boolean=false;
BEGIN
 SELECT * INTO inv FROM postern.invitations WHERE space_id=s AND id=i;
 SELECT principal_id INTO inviter FROM postern.memberships WHERE space_id=s AND id=inv.inviter_id;
 ctx=postern.onboarding_context(login,s,inviter);p=(ctx->>'principal_id')::uuid;
 SELECT * INTO inv FROM postern.invitations WHERE space_id=s AND id=i FOR UPDATE;
 IF NOT FOUND OR inv.recipient_id IS DISTINCT FROM p OR inv.secret_hash IS DISTINCT FROM secret
 OR inv.generation<>(ctx->>'generation')::uuid OR NOT EXISTS(SELECT 1 FROM postern.spaces WHERE id=s AND state='active')
 THEN RAISE EXCEPTION 'invitation unavailable' USING ERRCODE='42501'; END IF;
 IF inv.state='accepted' THEN
  SELECT * INTO m FROM postern.memberships WHERE space_id=s AND id=inv.accepted_membership_id AND principal_id=p AND state='active';
  IF NOT FOUND THEN RAISE EXCEPTION 'invitation unavailable' USING ERRCODE='42501'; END IF;
  RETURN jsonb_build_object('space_id',s,'membership_id',m.id,'role',m.role);
 END IF;
 SELECT x.role INTO inviter_role FROM postern.memberships x JOIN postern.principals a ON a.id=x.principal_id
 WHERE x.space_id=s AND x.id=inv.inviter_id AND x.state='active' AND a.state='active';
 IF inv.state<>'pending' OR inv.expires_at<=clock_timestamp() OR inviter_role IS NULL
 OR NOT (inviter_role='owner' OR (inviter_role='admin' AND inv.role='member'))
 THEN RAISE EXCEPTION 'invitation unavailable' USING ERRCODE='42501'; END IF;
 SELECT * INTO m FROM postern.memberships WHERE space_id=s AND principal_id=p AND state='active';
 IF NOT FOUND THEN
  INSERT INTO postern.memberships(space_id,principal_id,role) VALUES(s,p,inv.role) RETURNING * INTO m;
  is_new=true;
 END IF;
 -- A second pending invite cannot promote/demote an already-active membership.
 UPDATE postern.invitations SET state='accepted',accepted_membership_id=m.id,accepted_at=clock_timestamp() WHERE space_id=s AND id=i;
 PERFORM postern.onboarding_event(s,CASE WHEN is_new THEN 'membership.joined' ELSE 'invitation.accepted' END,m.id,jsonb_build_object('invitation_id',i));
 RETURN jsonb_build_object('space_id',s,'membership_id',m.id,'role',m.role);
END $$;
CREATE FUNCTION postern.invalidate_pending_invitations() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
BEGIN
 -- Runs inside the membership mutation's space lock/audit/outbox transaction.
 UPDATE postern.invitations SET state='revoked' WHERE space_id=NEW.space_id AND state='pending' AND
 ((OLD.state='active' AND NEW.state='revoked' AND recipient_id=NEW.principal_id)
 OR (inviter_id=NEW.id AND (NEW.state<>'active' OR NEW.role IS DISTINCT FROM OLD.role)));
 RETURN NEW;
END $$;
CREATE TRIGGER membership_pending_invitations AFTER UPDATE OF state,role ON postern.memberships FOR EACH ROW EXECUTE FUNCTION postern.invalidate_pending_invitations();
GRANT USAGE ON SCHEMA postern TO postern_onboarding_service;
GRANT EXECUTE ON FUNCTION postern.create_space(uuid,uuid,text,text),postern.my_spaces(uuid,uuid),postern.accept_invitation(uuid,uuid,uuid,bytea) TO postern_onboarding_service;
GRANT EXECUTE ON FUNCTION postern.create_invitation(text,uuid,text,uuid,text,text,text,text,integer,bytea),
 postern.list_invitations(text,uuid,text),postern.revoke_invitation(text,uuid,text,uuid,uuid,text) TO postern_resource_service;
RESET ROLE;
