-- Runtime integration. Keep 001 byte-for-byte immutable; provision login roles separately.
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='postern_resource_service') THEN
  CREATE ROLE postern_resource_service NOLOGIN NOSUPERUSER NOBYPASSRLS;
 ELSIF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='postern_resource_service' AND
  (rolcanlogin OR rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole OR rolreplication))
 THEN RAISE EXCEPTION 'unsafe preexisting Postern role'; END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname IN ('postern_owner','postern_reader','postern_writer','postern_identity_writer')
 AND pg_has_role('postern_resource_service',oid,'MEMBER')) THEN RAISE EXCEPTION 'unsafe runtime inheritance'; END IF;
END $$;
SET LOCAL ROLE postern_owner;
CREATE TABLE postern.account_logins (
 id uuid PRIMARY KEY, principal_id uuid NOT NULL REFERENCES postern.principals,
 generation uuid NOT NULL, expires_at timestamptz NOT NULL, revoked boolean NOT NULL DEFAULT false
);
CREATE INDEX account_login_expiry ON postern.account_logins(expires_at);
CREATE TABLE postern.resource_sessions (
 token_hash text PRIMARY KEY CHECK(token_hash ~ '^[0-9a-f]{64}$'),
 login_id uuid NOT NULL REFERENCES postern.account_logins ON DELETE CASCADE,
 space_id uuid NOT NULL REFERENCES postern.spaces, membership_id uuid NOT NULL, generation uuid NOT NULL,
 FOREIGN KEY(space_id,membership_id) REFERENCES postern.memberships,
 audience text NOT NULL CHECK(length(audience) BETWEEN 1 AND 255),
 assertion_id uuid NOT NULL UNIQUE, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL, revoked boolean NOT NULL DEFAULT false
);
CREATE INDEX resource_session_login ON postern.resource_sessions(login_id,created_at DESC);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['account_logins','resource_sessions'] LOOP
  EXECUTE format('ALTER TABLE postern.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE postern.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY internal ON postern.%I TO postern_owner USING(true) WITH CHECK(true)',t);
 END LOOP;
END $$;
CREATE FUNCTION postern.register_principal(a text,i text) RETURNS uuid
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE p uuid;
BEGIN
 INSERT INTO postern.principals(authority,account_id) VALUES(a,i) RETURNING id INTO p;
 RETURN p;
END $$;
CREATE FUNCTION postern.register_login(i uuid,p uuid,e bigint) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
BEGIN
 PERFORM 1 FROM postern.principals WHERE id=p AND state='active' FOR SHARE;
 IF NOT FOUND OR to_timestamp(e)<=clock_timestamp() THEN RAISE EXCEPTION 'invalid login'; END IF;
 INSERT INTO postern.account_logins(id,principal_id,generation,expires_at)
 SELECT i,p,generation,to_timestamp(e) FROM postern.authority;
END $$;
CREATE FUNCTION postern.revoke_login(i uuid) RETURNS void
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
 UPDATE postern.account_logins SET revoked=true WHERE id=i
$$;
CREATE FUNCTION postern.login_context(i uuid,s uuid DEFAULT NULL) RETURNS jsonb
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
 SELECT jsonb_build_object('principal_id',p.id,'authority',p.authority,'account_id',p.account_id,
 'generation',a.generation,'login_id',l.id,'expires_at',floor(extract(epoch FROM l.expires_at)),
 'membership_id',(SELECT id FROM postern.memberships WHERE space_id=s AND principal_id=p.id AND state='active'))
 FROM postern.account_logins l JOIN postern.principals p ON p.id=l.principal_id CROSS JOIN postern.authority a
 WHERE l.id=i AND NOT l.revoked AND l.expires_at>clock_timestamp() AND l.generation=a.generation AND p.state='active'
 AND (s IS NULL OR (a.state='active' AND EXISTS(SELECT 1 FROM postern.memberships m JOIN postern.spaces x ON x.id=m.space_id
 WHERE m.space_id=s AND m.principal_id=p.id AND m.state='active' AND x.state='active')))
$$;
CREATE FUNCTION postern.invalidate_logins() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
BEGIN
 IF NEW.state<>'active' THEN UPDATE postern.account_logins SET revoked=true WHERE principal_id=NEW.id; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER principal_login_revocation AFTER UPDATE OF state ON postern.principals FOR EACH ROW EXECUTE FUNCTION postern.invalidate_logins();
CREATE FUNCTION postern.expire_logins() RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
 DELETE FROM postern.account_logins WHERE expires_at<=statement_timestamp()
$$;
GRANT EXECUTE ON FUNCTION postern.register_principal(text,text),postern.register_login(uuid,uuid,bigint),
 postern.revoke_login(uuid),postern.login_context(uuid,uuid),postern.expire_logins() TO postern_identity_writer;

-- Only these definer functions are exposed to the resource login. It cannot SET a
-- forged actor and read tables, invoke identity helpers, or see profile/session data.
CREATE FUNCTION postern.open_resource_request(h text,s uuid,aud text,target uuid DEFAULT NULL) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE login uuid; p uuid; ctx jsonb; role_name text; rev bigint;
BEGIN
 SELECT l.id,l.principal_id INTO login,p FROM postern.resource_sessions r JOIN postern.account_logins l ON l.id=r.login_id
 WHERE r.token_hash=h AND r.space_id=s AND r.audience=aud;
 IF NOT FOUND THEN RAISE EXCEPTION 'invalid session' USING ERRCODE='28000'; END IF;
 -- Use the same principal-before-space order as account lifecycle and membership
 -- writes. Include the mutation target before acquiring any space lock.
 PERFORM 1 FROM postern.principals WHERE id=p OR id=(SELECT principal_id FROM postern.memberships WHERE space_id=s AND id=target) ORDER BY id FOR SHARE;
 PERFORM 1 FROM postern.authority FOR SHARE;
 PERFORM 1 FROM postern.account_logins WHERE id=login FOR SHARE;
 IF target IS NULL THEN PERFORM 1 FROM postern.spaces WHERE id=s FOR SHARE;
 ELSE PERFORM 1 FROM postern.spaces WHERE id=s FOR UPDATE; END IF;
 PERFORM 1 FROM postern.resource_sessions WHERE token_hash=h FOR SHARE;
 ctx=postern.login_context(login,s);
 IF ctx IS NULL OR NOT EXISTS(SELECT 1 FROM postern.resource_sessions WHERE token_hash=h AND NOT revoked
 AND expires_at>clock_timestamp() AND generation=(ctx->>'generation')::uuid
 AND membership_id=(ctx->>'membership_id')::uuid)
 THEN RAISE EXCEPTION 'invalid session' USING ERRCODE='28000'; END IF;
 PERFORM set_config('postern.actor_id',p::text,true);
 PERFORM set_config('postern.space_id',s::text,true);
 PERFORM set_config('postern.generation',ctx->>'generation',true);
 role_name=postern.current_role(s);
 SELECT policy_revision INTO rev FROM postern.spaces WHERE id=s;
 RETURN jsonb_build_object('principal_id',p,'space_id',s,'membership_id',ctx->>'membership_id','role',role_name,'policy_revision',rev,'generation',ctx->>'generation');
END $$;
CREATE FUNCTION postern.record_disclosure(s uuid,act text,target uuid DEFAULT NULL) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
BEGIN
 IF postern.current_role(s) IS NULL THEN RAISE EXCEPTION 'denied' USING ERRCODE='42501'; END IF;
 INSERT INTO postern.activity(space_id,actor_id,action,target_id,policy_revision)
 SELECT s,postern.actor(),act,target,policy_revision FROM postern.spaces WHERE id=s;
END $$;
CREATE FUNCTION postern.exchange_session(h text,s uuid,aud text,j uuid,login uuid,p uuid,a text,account text,g uuid,m uuid,e bigint) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; expiry timestamptz;
BEGIN
 PERFORM 1 FROM postern.principals WHERE id=p FOR SHARE;
 PERFORM 1 FROM postern.authority FOR SHARE;
 PERFORM 1 FROM postern.account_logins WHERE id=login FOR UPDATE;
 PERFORM 1 FROM postern.spaces WHERE id=s FOR SHARE;
 ctx=postern.login_context(login,s);
 IF ctx IS NULL OR ctx->>'principal_id'<>p::text OR ctx->>'authority'<>a OR ctx->>'account_id'<>account
 OR ctx->>'generation'<>g::text OR ctx->>'membership_id'<>m::text OR to_timestamp(e)<=clock_timestamp() OR to_timestamp(e)>clock_timestamp()+interval '60 seconds'
 OR e>(ctx->>'expires_at')::bigint
 THEN RAISE EXCEPTION 'invalid assertion' USING ERRCODE='28000'; END IF;
 expiry=least(to_timestamp((ctx->>'expires_at')::bigint),clock_timestamp()+interval '15 minutes');
 DELETE FROM postern.resource_sessions WHERE login_id=login AND expires_at<=clock_timestamp();
 INSERT INTO postern.resource_sessions(token_hash,login_id,space_id,membership_id,generation,audience,assertion_id,expires_at)
 VALUES(h,login,s,m,g,aud,j,expiry);
 -- Keep replay evidence for the lifetime of the assertion, even when evicting a session.
 UPDATE postern.resource_sessions SET revoked=true WHERE login_id=login AND NOT revoked AND token_hash NOT IN
 (SELECT token_hash FROM postern.resource_sessions WHERE login_id=login AND NOT revoked
 ORDER BY created_at DESC,token_hash LIMIT 8);
 PERFORM set_config('postern.actor_id',p::text,true); PERFORM set_config('postern.space_id',s::text,true);
 PERFORM set_config('postern.generation',g::text,true);
 PERFORM postern.record_disclosure(s,'session.exchanged');
 RETURN jsonb_build_object('expires_at',floor(extract(epoch FROM expiry)),'space_id',s);
EXCEPTION WHEN unique_violation THEN RAISE EXCEPTION 'invalid assertion' USING ERRCODE='28000';
END $$;
CREATE FUNCTION postern.resource_context(h text,s uuid,aud text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb;
BEGIN
 ctx=postern.open_resource_request(h,s,aud);
 PERFORM postern.record_disclosure(s,'session.read');
 RETURN ctx;
END $$;
CREATE FUNCTION postern.resource_catalog(h text,s uuid,aud text,q text,after_id uuid,expected jsonb) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; items jsonb;
BEGIN
 ctx=postern.open_resource_request(h,s,aud);
 IF length(q)>200 THEN RAISE EXCEPTION 'invalid query' USING ERRCODE='22023'; END IF;
 IF expected IS NOT NULL AND expected<>ctx THEN RAISE EXCEPTION 'stale cursor' USING ERRCODE='22023'; END IF;
 SELECT coalesce(jsonb_agg(x ORDER BY x.id),'[]') INTO items FROM (
 SELECT id,kind,title,state,content_revision FROM postern.resources
 WHERE space_id=s AND postern.can_resource(s,id,'discover') AND (after_id IS NULL OR id>after_id)
 AND (q='' OR search @@ plainto_tsquery('simple',q)) ORDER BY id LIMIT 51) x;
 PERFORM postern.record_disclosure(s,'catalog.read');
 RETURN jsonb_build_object('context',ctx,'items',items);
END $$;
CREATE FUNCTION postern.resource_detail(h text,s uuid,aud text,r uuid) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE item jsonb;
BEGIN
 PERFORM postern.open_resource_request(h,s,aud);
 SELECT jsonb_build_object('id',id,'kind',kind,'title',title,'state',state,'content_revision',content_revision) INTO item
 FROM postern.resources WHERE space_id=s AND id=r AND postern.can_resource(s,r,'discover');
 IF item IS NULL THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
 PERFORM postern.record_disclosure(s,'resource.metadata',r);
 RETURN item;
END $$;
CREATE FUNCTION postern.resource_revoke(h text,s uuid,aud text,m uuid,k uuid,hash text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
BEGIN
 PERFORM postern.open_resource_request(h,s,aud,m);
 RETURN postern.revoke_member(s,m,k,hash);
END $$;
CREATE FUNCTION postern.end_resource_session(h text,s uuid,aud text) RETURNS void
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
 UPDATE postern.resource_sessions SET revoked=true WHERE token_hash=h AND space_id=s AND audience=aud
$$;
GRANT USAGE ON SCHEMA postern TO postern_resource_service;
GRANT EXECUTE ON FUNCTION postern.exchange_session(text,uuid,text,uuid,uuid,uuid,text,text,uuid,uuid,bigint),
 postern.resource_context(text,uuid,text),postern.resource_catalog(text,uuid,text,text,uuid,jsonb),
 postern.resource_detail(text,uuid,text,uuid),postern.resource_revoke(text,uuid,text,uuid,uuid,text),
 postern.end_resource_session(text,uuid,text) TO postern_resource_service;
RESET ROLE;
