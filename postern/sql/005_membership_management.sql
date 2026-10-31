-- Scoped directory, optimistic role/removal changes, and retryable self-departure.
SET LOCAL ROLE postern_owner;
CREATE FUNCTION postern.resource_members(h text,s uuid,aud text,after_id uuid,expected jsonb) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; items jsonb; owners bigint;
BEGIN
 ctx=postern.open_resource_request(h,s,aud);
 IF ctx->>'role' NOT IN ('owner','admin') THEN RAISE EXCEPTION 'forbidden'; END IF;
 IF expected IS NOT NULL AND expected<>ctx THEN RAISE EXCEPTION 'stale cursor' USING ERRCODE='22023'; END IF;
 SELECT count(*) INTO owners FROM postern.memberships m JOIN postern.principals p ON p.id=m.principal_id
 WHERE m.space_id=s AND m.state='active' AND m.role='owner' AND p.state='active';
 SELECT coalesce(jsonb_agg(x ORDER BY x.membership_id),'[]') INTO items FROM (
 SELECT m.id AS membership_id,m.role,p.authority,p.account_id,m.principal_id=postern.actor() AS is_self,
 ctx->>'role'='owner' AND m.role<>'owner' AND m.principal_id<>postern.actor() AS can_change_role,
 m.principal_id<>postern.actor() AND ((ctx->>'role'='owner' AND (m.role<>'owner' OR owners>1))
 OR (ctx->>'role'='admin' AND m.role='member')) AS can_remove
 FROM postern.memberships m JOIN postern.principals p ON p.id=m.principal_id
 WHERE m.space_id=s AND m.state='active' AND p.state='active' AND (after_id IS NULL OR m.id>after_id)
 ORDER BY m.id LIMIT 51) x;
 PERFORM postern.record_disclosure(s,'memberships.listed');
 RETURN jsonb_build_object('context',ctx,'items',items);
END $$;
CREATE FUNCTION postern.resource_membership_change(h text,s uuid,aud text,m uuid,k uuid,hash text,expected bigint,new_role text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; target postern.memberships; prior postern.operations; answer jsonb; rev bigint;
BEGIN
 -- Principal locks (including target) precede the exclusive space lock. Recheck
 -- current authority after waiting behind role/revocation/account mutations.
 ctx=postern.open_resource_request(h,s,aud,m);
 IF ctx->>'role' NOT IN ('owner','admin') THEN RAISE EXCEPTION 'forbidden'; END IF;
 SELECT * INTO target FROM postern.memberships WHERE space_id=s AND id=m;
 IF NOT FOUND THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
 IF target.principal_id=postern.actor() OR (ctx->>'role'='admin' AND (new_role IS NOT NULL OR target.role<>'member'))
 OR (new_role IS NOT NULL AND (new_role NOT IN ('member','admin') OR target.role='owner'))
 THEN RAISE EXCEPTION 'forbidden'; END IF;
 SELECT * INTO prior FROM postern.operations WHERE space_id=s AND actor_id=postern.actor() AND key=k;
 IF FOUND THEN
  IF prior.request_hash<>hash OR prior.result->>'target'<>m::text THEN RAISE EXCEPTION 'operation mismatch'; END IF;
  RETURN prior.result;
 END IF;
 IF expected IS NULL OR expected<>(ctx->>'policy_revision')::bigint OR target.state<>'active'
 THEN RAISE EXCEPTION 'membership changed' USING ERRCODE='PT412'; END IF;
 IF new_role IS NULL THEN
  answer=postern.revoke_member(s,m,k,hash);
  UPDATE postern.resource_sessions SET revoked=true WHERE space_id=s AND membership_id=m;
  RETURN answer;
 END IF;
 IF target.role=new_role THEN RAISE EXCEPTION 'membership changed' USING ERRCODE='PT412'; END IF;
 UPDATE postern.memberships SET role=new_role WHERE space_id=s AND id=m;
 UPDATE postern.resource_sessions SET revoked=true WHERE space_id=s AND membership_id=m;
 rev=postern.onboarding_event(s,'membership.role_changed',m,jsonb_build_object('previous_role',target.role,'role',new_role));
 answer=jsonb_build_object('target',m,'role',new_role,'revision',rev);
 INSERT INTO postern.operations VALUES(s,postern.actor(),k,hash,answer);
 RETURN answer;
END $$;
CREATE FUNCTION postern.leave_space(login uuid,s uuid,m uuid,k uuid,hash text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; target postern.memberships; prior postern.operations; rev bigint; ev uuid; answer jsonb;
BEGIN
 -- A live account login remains valid after its space membership ends. Exact
 -- membership binding makes lost-response retry safe even after readmission.
 ctx=postern.onboarding_context(login,s);
 SELECT * INTO target FROM postern.memberships WHERE space_id=s AND id=m AND principal_id=postern.actor();
 IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM postern.spaces WHERE id=s AND state='active')
 THEN RAISE EXCEPTION 'membership unavailable' USING ERRCODE='42501'; END IF;
 SELECT * INTO prior FROM postern.operations WHERE space_id=s AND actor_id=postern.actor() AND key=k;
 IF FOUND THEN
  IF prior.request_hash<>hash OR prior.result->>'target'<>m::text OR prior.result->>'action'<>'membership.left'
  THEN RAISE EXCEPTION 'operation mismatch'; END IF;
  IF prior.result->>'generation'<>ctx->>'generation' THEN RAISE EXCEPTION 'membership unavailable' USING ERRCODE='42501'; END IF;
  RETURN prior.result;
 END IF;
 IF target.state<>'active' THEN RAISE EXCEPTION 'membership unavailable' USING ERRCODE='42501'; END IF;
 IF target.role='owner' AND NOT EXISTS(SELECT 1 FROM postern.memberships x JOIN postern.principals p ON p.id=x.principal_id
 WHERE x.space_id=s AND x.id<>m AND x.role='owner' AND x.state='active' AND p.state='active')
 THEN RAISE EXCEPTION 'active space requires an active owner'; END IF;
 UPDATE postern.memberships SET state='revoked' WHERE space_id=s AND id=m;
 DELETE FROM postern.audience_members WHERE space_id=s AND membership_id=m;
 UPDATE postern.grants SET state='revoked' WHERE space_id=s AND membership_id=m;
 UPDATE postern.resource_sessions SET revoked=true WHERE space_id=s AND membership_id=m;
 UPDATE postern.spaces SET policy_revision=policy_revision+1 WHERE id=s RETURNING policy_revision INTO rev;
 INSERT INTO postern.activity(space_id,actor_id,action,target_id,policy_revision)
 VALUES(s,postern.actor(),'membership.left',m,rev) RETURNING id INTO ev;
 INSERT INTO postern.outbox(space_id,event_id,kind,payload) VALUES(s,ev,'policy.changed',jsonb_build_object('revision',rev));
 answer=jsonb_build_object('target',m,'space_id',s,'action','membership.left','revision',rev,'generation',ctx->>'generation');
 INSERT INTO postern.operations VALUES(s,postern.actor(),k,hash,answer);
 RETURN answer;
END $$;
CREATE OR REPLACE FUNCTION postern.resource_context(h text,s uuid,aud text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb;
BEGIN
 ctx=postern.open_resource_request(h,s,aud);
 ctx=ctx||jsonb_build_object('title',(SELECT title FROM postern.spaces WHERE id=s),
 'can_leave',ctx->>'role'<>'owner' OR EXISTS(SELECT 1 FROM postern.memberships m JOIN postern.principals p ON p.id=m.principal_id
 WHERE m.space_id=s AND m.state='active' AND m.role='owner' AND p.state='active' AND m.principal_id<>postern.actor()));
 PERFORM postern.record_disclosure(s,'session.read');
 RETURN ctx;
END $$;
GRANT EXECUTE ON FUNCTION postern.resource_members(text,uuid,text,uuid,jsonb),
 postern.resource_membership_change(text,uuid,text,uuid,uuid,text,bigint,text) TO postern_resource_service;
GRANT EXECUTE ON FUNCTION postern.leave_space(uuid,uuid,uuid,uuid,text) TO postern_onboarding_service;
RESET ROLE;
