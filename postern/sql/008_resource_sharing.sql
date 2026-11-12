-- Flat audiences and explicit additive grants. All writes use the same exclusive
-- space lock, revision, durable operation receipt, audit and outbox transaction.
SET LOCAL ROLE postern_owner;
ALTER TABLE postern.audiences ADD COLUMN state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','archived'));
CREATE INDEX grant_management ON postern.grants(space_id,resource_id,id) WHERE state='active';
CREATE INDEX grant_cleanup ON postern.grants(space_id,id) WHERE state='active';

CREATE FUNCTION postern.grant_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF OLD.state='revoked' AND NEW.state<>'revoked' THEN RAISE EXCEPTION 'revoked grant cannot reactivate'; END IF;
 IF (NEW.space_id,NEW.id,NEW.resource_id,NEW.membership_id,NEW.audience_id,NEW.all_members,NEW.actions,NEW.expires_at)
 IS DISTINCT FROM (OLD.space_id,OLD.id,OLD.resource_id,OLD.membership_id,OLD.audience_id,OLD.all_members,OLD.actions,OLD.expires_at)
 THEN RAISE EXCEPTION 'grant identity and terms are immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER grant_guard BEFORE UPDATE ON postern.grants FOR EACH ROW EXECUTE FUNCTION postern.grant_guard();

-- Shared predicate for preview and retirement. An empty audience is intentional,
-- not stale: future members may be added. Publication again needs a new grant.
CREATE FUNCTION postern.stale_grant_reason(g postern.grants) RETURNS text
 LANGUAGE sql STABLE SET search_path=pg_catalog,postern AS $$
 SELECT CASE WHEN g.expires_at<=statement_timestamp() THEN 'expired'
 WHEN NOT ('discover'=ANY(g.actions)) THEN 'no_discover'
 WHEN NOT EXISTS(SELECT 1 FROM postern.resources r WHERE r.space_id=g.space_id AND r.id=g.resource_id AND r.state='published') THEN 'unpublished'
 WHEN g.membership_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM postern.memberships m JOIN postern.principals p ON p.id=m.principal_id
   WHERE m.space_id=g.space_id AND m.id=g.membership_id AND m.state='active' AND p.state='active') THEN 'membership_ended'
 WHEN g.audience_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM postern.audiences a WHERE a.space_id=g.space_id AND a.id=g.audience_id AND a.state='active') THEN 'audience_archived' END
$$;

CREATE FUNCTION postern.resource_sharing(h text,s uuid,aud text,kind text,target uuid,after_id uuid,expected jsonb) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; items jsonb;
BEGIN
 ctx=postern.open_resource_request(h,s,aud);
 IF ctx->>'role' NOT IN ('owner','admin') THEN RAISE EXCEPTION 'forbidden'; END IF;
 IF expected IS NOT NULL AND expected<>ctx THEN RAISE EXCEPTION 'stale cursor' USING ERRCODE='22023'; END IF;
 IF kind='audiences' THEN
  SELECT coalesce(jsonb_agg(x ORDER BY x.id),'[]') INTO items FROM (
   SELECT a.id,a.label,(SELECT count(*) FROM postern.audience_members am WHERE am.space_id=s AND am.audience_id=a.id) AS member_count
   FROM postern.audiences a WHERE a.space_id=s AND a.state='active' AND (after_id IS NULL OR a.id>after_id) ORDER BY a.id LIMIT 51) x;
 ELSIF kind='audience_members' THEN
  IF NOT EXISTS(SELECT 1 FROM postern.audiences WHERE space_id=s AND id=target AND state='active') THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
  SELECT coalesce(jsonb_agg(x ORDER BY x.id),'[]') INTO items FROM (
   SELECT m.id,m.role,p.authority,p.account_id FROM postern.audience_members am
   JOIN postern.memberships m ON m.space_id=am.space_id AND m.id=am.membership_id JOIN postern.principals p ON p.id=m.principal_id
   WHERE am.space_id=s AND am.audience_id=target AND m.state='active' AND p.state='active'
   AND (after_id IS NULL OR m.id>after_id) ORDER BY m.id LIMIT 51) x;
 ELSIF kind='grants' THEN
  IF NOT EXISTS(SELECT 1 FROM postern.resources WHERE space_id=s AND id=target) THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
  SELECT coalesce(jsonb_agg(x ORDER BY x.id),'[]') INTO items FROM (
   SELECT g.id,g.membership_id,g.audience_id,g.all_members,g.actions,extract(epoch FROM g.expires_at) AS expires_at,
   a.label AS audience_label,p.authority,p.account_id,postern.stale_grant_reason(g) AS stale_reason
   FROM postern.grants g LEFT JOIN postern.audiences a ON a.space_id=s AND a.id=g.audience_id
   LEFT JOIN postern.memberships m ON m.space_id=s AND m.id=g.membership_id LEFT JOIN postern.principals p ON p.id=m.principal_id
   WHERE g.space_id=s AND g.resource_id=target AND g.state='active' AND (after_id IS NULL OR g.id>after_id) ORDER BY g.id LIMIT 51) x;
 ELSIF kind='cleanup' THEN
  SELECT coalesce(jsonb_agg(x ORDER BY x.id),'[]') INTO items FROM (
   SELECT g.id,g.resource_id,postern.stale_grant_reason(g) AS reason FROM postern.grants g
   WHERE g.space_id=s AND g.state='active' AND postern.stale_grant_reason(g) IS NOT NULL
   AND (after_id IS NULL OR g.id>after_id) ORDER BY g.id LIMIT 51) x;
 ELSE RAISE EXCEPTION 'invalid sharing request' USING ERRCODE='PT422'; END IF;
 PERFORM postern.record_disclosure(s,'sharing.'||kind||'.listed',target);
 RETURN jsonb_build_object('context',ctx,'items',items);
END $$;

CREATE FUNCTION postern.resource_sharing_change(h text,s uuid,aud text,k uuid,hash text,expected bigint,act text,target uuid,data jsonb) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; prior postern.operations; answer jsonb; rev bigint; changed uuid=target; member uuid;
 group_id uuid; resource uuid; terms text[]; expiry timestamptz; label text; n bigint=0; detail jsonb=data; retired jsonb;
BEGIN
 IF act IN ('audience.add','audience.remove','grant.create') THEN member=(data->>'membership_id')::uuid; END IF;
 -- Non-null lock target selects the exclusive space lock. Never upgrade a read
 -- lock or acquire another principal lock after the space lock.
 ctx=postern.open_resource_request(h,s,aud,coalesce(member,'00000000-0000-0000-0000-000000000000'::uuid));
 IF ctx->>'role' NOT IN ('owner','admin') THEN RAISE EXCEPTION 'forbidden'; END IF;
 SELECT * INTO prior FROM postern.operations WHERE space_id=s AND actor_id=postern.actor() AND key=k;
 IF FOUND THEN
  IF prior.request_hash<>hash OR prior.result->>'action' IS DISTINCT FROM act OR prior.result->>'generation' IS DISTINCT FROM ctx->>'generation'
  THEN RAISE EXCEPTION 'operation mismatch'; END IF;
  RETURN prior.result;
 END IF;
 IF expected IS NULL OR expected<>(ctx->>'policy_revision')::bigint THEN RAISE EXCEPTION 'sharing changed' USING ERRCODE='PT413'; END IF;
 IF act IN ('audience.create','audience.rename') THEN
  label=data->>'label';
  IF label IS NULL OR length(btrim(label)) NOT BETWEEN 1 AND 200 OR label ~ '[[:cntrl:]]' THEN RAISE EXCEPTION 'invalid sharing request' USING ERRCODE='PT422'; END IF;
 END IF;
 IF act LIKE 'audience.%' AND act<>'audience.create' THEN
  IF NOT EXISTS(SELECT 1 FROM postern.audiences WHERE space_id=s AND id=target AND state='active') THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
 END IF;
 CASE act
 WHEN 'audience.create' THEN
  INSERT INTO postern.audiences(space_id,label) VALUES(s,label) RETURNING id INTO changed;
 WHEN 'audience.rename' THEN
  detail=data||jsonb_build_object('previous_label',(SELECT a.label FROM postern.audiences a WHERE a.space_id=s AND a.id=target));
  UPDATE postern.audiences SET label=data->>'label' WHERE space_id=s AND id=target;
 WHEN 'audience.archive' THEN
  UPDATE postern.audiences SET state='archived' WHERE space_id=s AND id=target;
  UPDATE postern.grants SET state='revoked' WHERE space_id=s AND audience_id=target AND state='active';
  GET DIAGNOSTICS n=ROW_COUNT;
  DELETE FROM postern.audience_members WHERE space_id=s AND audience_id=target;
  detail=jsonb_build_object('grants_revoked',n);
 WHEN 'audience.add','audience.remove' THEN
  IF NOT EXISTS(SELECT 1 FROM postern.memberships m JOIN postern.principals p ON p.id=m.principal_id
   WHERE m.space_id=s AND m.id=member AND m.state='active' AND p.state='active') THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
  IF act='audience.add' THEN
   INSERT INTO postern.audience_members VALUES(s,target,member) ON CONFLICT DO NOTHING;
  ELSE DELETE FROM postern.audience_members WHERE space_id=s AND audience_id=target AND membership_id=member;
  END IF;
 WHEN 'grant.create' THEN
  resource=target; group_id=(data->>'audience_id')::uuid;
  IF NOT EXISTS(SELECT 1 FROM postern.resources WHERE space_id=s AND id=resource) THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
  SELECT array_agg(v ORDER BY v) INTO terms FROM jsonb_array_elements_text(data->'actions') v;
  IF terms IS NULL OR cardinality(terms) NOT BETWEEN 1 AND 2 OR NOT terms <@ ARRAY['discover','view']::text[] OR NOT 'discover'=ANY(terms)
   OR num_nonnulls(member,group_id)+(coalesce((data->>'all_members')::boolean,false))::integer<>1
   OR (SELECT count(DISTINCT x) FROM unnest(terms) x)<>cardinality(terms)
  THEN RAISE EXCEPTION 'invalid sharing request' USING ERRCODE='PT422'; END IF;
  IF NOT EXISTS(SELECT 1 FROM postern.resources WHERE space_id=s AND id=resource AND state='published' AND terms <@ supported_actions)
  THEN RAISE EXCEPTION 'resource cannot be shared' USING ERRCODE='PT413'; END IF;
  IF member IS NOT NULL AND NOT EXISTS(SELECT 1 FROM postern.memberships m JOIN postern.principals p ON p.id=m.principal_id
   WHERE m.space_id=s AND m.id=member AND m.state='active' AND p.state='active') THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
  IF group_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM postern.audiences WHERE space_id=s AND id=group_id AND state='active') THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
  expiry=to_timestamp((data->>'expires_at')::bigint);
  IF expiry IS NOT NULL AND expiry<=clock_timestamp() THEN RAISE EXCEPTION 'invalid sharing request' USING ERRCODE='PT422'; END IF;
  INSERT INTO postern.grants(space_id,resource_id,membership_id,audience_id,all_members,actions,expires_at)
   VALUES(s,resource,member,group_id,(data->>'all_members')::boolean,terms,expiry) RETURNING id INTO changed;
  detail=data||jsonb_build_object('resource_id',resource);
 WHEN 'grant.revoke' THEN
  UPDATE postern.grants SET state='revoked' WHERE space_id=s AND id=target AND state='active';
  IF NOT FOUND THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
 WHEN 'grant.cleanup' THEN
  -- Bound writes and audit payload. Repeated calls drain the queue; retries of
  -- the same operation return the same batch, even as more grants expire.
  WITH candidates AS MATERIALIZED (
   SELECT g.id,postern.stale_grant_reason(g) AS reason FROM postern.grants g
   WHERE g.space_id=s AND g.state='active' AND postern.stale_grant_reason(g) IS NOT NULL ORDER BY g.id LIMIT 500
  ), updated AS (
   UPDATE postern.grants g SET state='revoked' FROM candidates c WHERE g.space_id=s AND g.id=c.id RETURNING g.id,c.reason
  ) SELECT count(*),coalesce(jsonb_agg(updated ORDER BY id),'[]') INTO n,retired FROM updated;
  detail=jsonb_build_object('retired',retired);
 ELSE RAISE EXCEPTION 'invalid sharing request' USING ERRCODE='PT422';
 END CASE;
 rev=postern.onboarding_event(s,act,changed,detail);
 answer=jsonb_build_object('action',act,'target',changed,'revision',rev,'retired_count',n,'generation',ctx->>'generation');
 INSERT INTO postern.operations VALUES(s,postern.actor(),k,hash,answer);
 RETURN answer;
END $$;

CREATE FUNCTION postern.resource_access(h text,s uuid,aud text,r uuid,m uuid) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; role_name text; item postern.resources; paths jsonb; actions text[];
BEGIN
 ctx=postern.open_resource_request(h,s,aud);
 IF ctx->>'role' NOT IN ('owner','admin') THEN RAISE EXCEPTION 'forbidden'; END IF;
 SELECT mm.role INTO role_name FROM postern.memberships mm JOIN postern.principals p ON p.id=mm.principal_id
 WHERE mm.space_id=s AND mm.id=m AND mm.state='active' AND p.state='active';
 SELECT * INTO item FROM postern.resources WHERE space_id=s AND id=r;
 IF role_name IS NULL OR NOT FOUND THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
 WITH matching AS MATERIALIZED (
  SELECT g.id,g.actions,g.all_members,g.audience_id,a.label AS audience_label,extract(epoch FROM g.expires_at) AS expires_at
  FROM postern.grants g LEFT JOIN postern.audiences a ON a.space_id=s AND a.id=g.audience_id
  WHERE g.space_id=s AND g.resource_id=r AND g.state='active' AND item.state='published'
  AND (g.expires_at IS NULL OR g.expires_at>statement_timestamp())
  AND (g.all_members OR g.membership_id=m OR EXISTS(SELECT 1 FROM postern.audience_members am WHERE am.space_id=s AND am.audience_id=g.audience_id AND am.membership_id=m))
 ) SELECT ARRAY(SELECT v FROM unnest(ARRAY['discover','view']) v WHERE v=ANY(item.supported_actions)
  AND (role_name IN ('owner','admin') OR EXISTS(SELECT 1 FROM matching WHERE v=ANY(matching.actions)))),
  (SELECT coalesce(jsonb_agg(x ORDER BY x.id),'[]') FROM (SELECT * FROM matching ORDER BY id LIMIT 51) x) INTO actions,paths;
 PERFORM postern.record_disclosure(s,'resource.access_inspected',r);
 RETURN jsonb_build_object('membership_id',m,'resource_id',r,'role',role_name,'actions',actions,'sources',paths,'policy_revision',ctx->'policy_revision');
END $$;
GRANT EXECUTE ON FUNCTION postern.resource_sharing(text,uuid,text,text,uuid,uuid,jsonb),
 postern.resource_sharing_change(text,uuid,text,uuid,text,bigint,text,uuid,jsonb),
 postern.resource_access(text,uuid,text,uuid,uuid) TO postern_resource_service;
RESET ROLE;
