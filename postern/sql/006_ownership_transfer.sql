-- Explicit, account-bound acceptance. Existing migrations stay immutable.
SET LOCAL ROLE postern_owner;
CREATE TABLE postern.ownership_transfers (
 space_id uuid NOT NULL REFERENCES postern.spaces, id uuid NOT NULL DEFAULT gen_random_uuid(),
 proposer_id uuid NOT NULL, recipient_id uuid NOT NULL, previous_owner_id uuid NOT NULL REFERENCES postern.owners,
 former_role text NOT NULL CHECK(former_role IN ('admin','member')),
 generation uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), expires_at timestamptz NOT NULL,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','accepted','cancelled','declined','invalidated','expired')),
 PRIMARY KEY(space_id,id), FOREIGN KEY(space_id,proposer_id) REFERENCES postern.memberships,
 FOREIGN KEY(space_id,recipient_id) REFERENCES postern.memberships,
 CHECK(proposer_id<>recipient_id), CHECK(expires_at>created_at)
);
CREATE UNIQUE INDEX one_pending_transfer ON postern.ownership_transfers(space_id) WHERE state='pending';
ALTER TABLE postern.ownership_transfers ENABLE ROW LEVEL SECURITY;
ALTER TABLE postern.ownership_transfers FORCE ROW LEVEL SECURITY;
CREATE POLICY internal ON postern.ownership_transfers TO postern_owner USING(true) WITH CHECK(true);

CREATE FUNCTION postern.invalidate_membership_transfers() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
BEGIN
 IF NEW.role<>OLD.role OR NEW.state<>OLD.state THEN
  UPDATE postern.ownership_transfers SET state='invalidated'
  WHERE space_id=NEW.space_id AND state='pending' AND NEW.id IN (proposer_id,recipient_id);
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER membership_pending_transfers AFTER UPDATE ON postern.memberships
 FOR EACH ROW EXECUTE FUNCTION postern.invalidate_membership_transfers();
CREATE FUNCTION postern.invalidate_space_transfers() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
BEGIN
 IF NEW.owner_id<>OLD.owner_id OR NEW.state<>OLD.state THEN
  UPDATE postern.ownership_transfers SET state='invalidated' WHERE space_id=NEW.id AND state='pending';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER space_pending_transfers AFTER UPDATE ON postern.spaces
 FOR EACH ROW EXECUTE FUNCTION postern.invalidate_space_transfers();

-- Internal only. Callers first hold the space lock and live login context.
CREATE FUNCTION postern.transfer_available(t postern.ownership_transfers) RETURNS boolean
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
 SELECT t.state='pending' AND t.expires_at>clock_timestamp()
 AND t.generation=nullif(current_setting('postern.generation',true),'')::uuid
 AND EXISTS(SELECT 1 FROM postern.spaces WHERE id=t.space_id AND state='active' AND owner_id=t.previous_owner_id)
 AND EXISTS(SELECT 1 FROM postern.memberships m JOIN postern.principals p ON p.id=m.principal_id
 WHERE m.space_id=t.space_id AND m.id=t.proposer_id AND m.state='active' AND m.role='owner' AND p.state='active')
 AND EXISTS(SELECT 1 FROM postern.memberships m JOIN postern.principals p ON p.id=m.principal_id
 WHERE m.space_id=t.space_id AND m.id=t.recipient_id AND m.state='active' AND p.state='active')
$$;
CREATE FUNCTION postern.ownership_status(login uuid,s uuid) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; m postern.memberships; t postern.ownership_transfers; proposal jsonb=NULL;
BEGIN
 ctx=postern.onboarding_context(login,s);
 IF postern.current_role(s) IS NULL THEN RAISE EXCEPTION 'space unavailable' USING ERRCODE='42501'; END IF;
 SELECT * INTO m FROM postern.memberships WHERE space_id=s AND principal_id=postern.actor() AND state='active';
 SELECT * INTO t FROM postern.ownership_transfers WHERE space_id=s AND state='pending';
 IF FOUND AND (m.role='owner' OR m.id=t.recipient_id) AND postern.transfer_available(t) THEN
  SELECT jsonb_build_object('transfer_id',t.id,'former_role',t.former_role,'expires_at',floor(extract(epoch FROM t.expires_at)),
   'is_recipient',m.id=t.recipient_id,'can_cancel',m.role='owner',
   'proposer',jsonb_build_object('authority',p.authority,'account_id',p.account_id),
   'recipient',jsonb_build_object('authority',r.authority,'account_id',r.account_id)) INTO proposal
  FROM postern.memberships a JOIN postern.principals p ON p.id=a.principal_id,
       postern.memberships b JOIN postern.principals r ON r.id=b.principal_id
  WHERE a.space_id=s AND a.id=t.proposer_id AND b.space_id=s AND b.id=t.recipient_id;
 END IF;
 PERFORM postern.record_disclosure(s,'ownership.viewed');
 RETURN jsonb_build_object('title',(SELECT title FROM postern.spaces WHERE id=s),'role',m.role,'membership_id',m.id,
  'policy_revision',(SELECT policy_revision FROM postern.spaces WHERE id=s),'transfer',proposal);
END $$;
CREATE FUNCTION postern.propose_ownership(login uuid,s uuid,recipient uuid,former text,ttl integer,expected bigint,k uuid,hash text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; m postern.memberships; target postern.memberships; t postern.ownership_transfers; prior postern.operations; answer jsonb; owner uuid;
BEGIN
 SELECT * INTO target FROM postern.memberships WHERE space_id=s AND id=recipient;
 ctx=postern.onboarding_context(login,s,target.principal_id);
 IF postern.current_role(s) IS NULL THEN RAISE EXCEPTION 'space unavailable' USING ERRCODE='42501'; END IF;
 SELECT * INTO m FROM postern.memberships WHERE space_id=s AND principal_id=postern.actor() AND state='active';
 SELECT * INTO prior FROM postern.operations WHERE space_id=s AND actor_id=postern.actor() AND key=k;
 IF FOUND THEN
  IF prior.request_hash<>hash OR prior.result->>'action'<>'ownership.proposed' THEN RAISE EXCEPTION 'operation mismatch'; END IF;
  IF prior.result->>'generation'<>ctx->>'generation' OR prior.result->>'membership_id'<>m.id::text
  THEN RAISE EXCEPTION 'transfer unavailable' USING ERRCODE='42501'; END IF;
  SELECT * INTO t FROM postern.ownership_transfers WHERE space_id=s AND id=(prior.result->>'transfer_id')::uuid;
  RETURN jsonb_build_object('transfer_id',t.id,'state',CASE WHEN t.state='pending' AND NOT postern.transfer_available(t) THEN 'invalidated' ELSE t.state END);
 END IF;
 IF m.role<>'owner' THEN RAISE EXCEPTION 'forbidden'; END IF;
 SELECT * INTO target FROM postern.memberships WHERE space_id=s AND id=recipient AND state='active';
 IF NOT FOUND OR target.id=m.id OR NOT EXISTS(SELECT 1 FROM postern.principals WHERE id=target.principal_id AND state='active')
 THEN RAISE EXCEPTION 'recipient unavailable' USING ERRCODE='42501'; END IF;
 IF former IS NULL OR former NOT IN ('admin','member') OR ttl IS NULL OR ttl NOT BETWEEN 60 AND 604800
 THEN RAISE EXCEPTION 'invalid transfer' USING ERRCODE='22023'; END IF;
 SELECT owner_id INTO owner FROM postern.spaces WHERE id=s AND policy_revision=expected;
 IF NOT FOUND THEN RAISE EXCEPTION 'ownership changed' USING ERRCODE='PT412'; END IF;
 UPDATE postern.ownership_transfers SET state=CASE WHEN expires_at<=clock_timestamp() THEN 'expired' ELSE 'invalidated' END
 WHERE space_id=s AND state='pending' AND NOT postern.transfer_available(ownership_transfers);
 IF EXISTS(SELECT 1 FROM postern.ownership_transfers WHERE space_id=s AND state='pending')
 THEN RAISE EXCEPTION 'transfer already pending' USING ERRCODE='PT409'; END IF;
 INSERT INTO postern.ownership_transfers(space_id,proposer_id,recipient_id,previous_owner_id,former_role,generation,expires_at)
 VALUES(s,m.id,target.id,owner,former,(ctx->>'generation')::uuid,clock_timestamp()+make_interval(secs=>ttl)) RETURNING * INTO t;
 PERFORM postern.onboarding_event(s,'ownership.proposed',t.id,jsonb_build_object('proposer_membership',m.id,'recipient_membership',target.id,'former_role',former));
 answer=jsonb_build_object('transfer_id',t.id,'state','pending','action','ownership.proposed','membership_id',m.id,'generation',ctx->>'generation');
 INSERT INTO postern.operations VALUES(s,postern.actor(),k,hash,answer);
 RETURN jsonb_build_object('transfer_id',t.id,'state','pending');
END $$;
CREATE FUNCTION postern.decide_ownership(login uuid,s uuid,id uuid,decision text,k uuid,hash text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; t postern.ownership_transfers; m postern.memberships; prior postern.operations; proposer uuid; recipient uuid; owner uuid; answer jsonb;
BEGIN
 SELECT x.principal_id INTO proposer FROM postern.ownership_transfers t0 JOIN postern.memberships x
 ON x.space_id=t0.space_id AND x.id=t0.proposer_id WHERE t0.space_id=s AND t0.id=decide_ownership.id;
 -- Acceptance locks both affected principals before authority/login/space.
 ctx=postern.onboarding_context(login,s,proposer);
 IF postern.current_role(s) IS NULL THEN RAISE EXCEPTION 'space unavailable' USING ERRCODE='42501'; END IF;
 SELECT * INTO m FROM postern.memberships WHERE space_id=s AND principal_id=postern.actor() AND state='active';
 IF decision IS NULL OR decision NOT IN ('accept','decline','cancel') THEN RAISE EXCEPTION 'invalid decision' USING ERRCODE='22023'; END IF;
 SELECT * INTO prior FROM postern.operations WHERE space_id=s AND actor_id=postern.actor() AND key=k;
 IF FOUND THEN
  IF prior.request_hash<>hash OR prior.result->>'transfer_id'<>id::text OR prior.result->>'action'<>'ownership.'||decision THEN RAISE EXCEPTION 'operation mismatch'; END IF;
  IF prior.result->>'generation'<>ctx->>'generation' OR prior.result->>'membership_id'<>m.id::text
  THEN RAISE EXCEPTION 'transfer unavailable' USING ERRCODE='42501'; END IF;
  RETURN prior.result - 'action' - 'membership_id' - 'generation';
 END IF;
 SELECT * INTO t FROM postern.ownership_transfers WHERE space_id=s AND ownership_transfers.id=decide_ownership.id;
 IF NOT FOUND OR NOT postern.transfer_available(t) THEN RAISE EXCEPTION 'transfer unavailable' USING ERRCODE='42501'; END IF;
 IF (decision IN ('accept','decline') AND m.id<>t.recipient_id) OR (decision='cancel' AND m.role<>'owner')
 THEN RAISE EXCEPTION 'transfer unavailable' USING ERRCODE='42501'; END IF;
 -- Mark accepted before role/owner triggers so this transaction cannot invalidate itself.
 UPDATE postern.ownership_transfers SET state=CASE decision WHEN 'accept' THEN 'accepted' WHEN 'decline' THEN 'declined' ELSE 'cancelled' END
 WHERE space_id=s AND ownership_transfers.id=t.id RETURNING state INTO decision;
 IF decision='accepted' THEN
  recipient=m.principal_id;
  SELECT o.id INTO owner FROM postern.owners o WHERE kind='person' AND principal_id=recipient ORDER BY o.id LIMIT 1;
  IF owner IS NULL THEN INSERT INTO postern.owners(kind,principal_id,label) VALUES('person',recipient,'Personal owner') RETURNING owners.id INTO owner; END IF;
  UPDATE postern.memberships SET role='owner' WHERE space_id=s AND memberships.id=t.recipient_id;
  UPDATE postern.memberships SET role=t.former_role WHERE space_id=s AND memberships.id=t.proposer_id;
  UPDATE postern.spaces SET owner_id=owner WHERE spaces.id=s;
  UPDATE postern.resource_sessions SET revoked=true WHERE space_id=s AND membership_id IN(t.proposer_id,t.recipient_id);
  UPDATE postern.invitations SET state='revoked' WHERE space_id=s AND state='pending' AND inviter_id IN(t.proposer_id,t.recipient_id);
 END IF;
 PERFORM postern.onboarding_event(s,'ownership.'||decision,t.id,jsonb_build_object('previous_owner_id',t.previous_owner_id,
  'owner_id',coalesce(owner,t.previous_owner_id),'proposer_membership',t.proposer_id,'recipient_membership',t.recipient_id,'former_role',t.former_role));
 answer=jsonb_build_object('transfer_id',t.id,'state',decision,'action','ownership.'||CASE decision WHEN 'accepted' THEN 'accept' WHEN 'declined' THEN 'decline' ELSE 'cancel' END,
  'membership_id',m.id,'generation',ctx->>'generation');
 INSERT INTO postern.operations VALUES(s,postern.actor(),k,hash,answer);
 RETURN answer - 'action' - 'membership_id' - 'generation';
END $$;
GRANT EXECUTE ON FUNCTION postern.ownership_status(uuid,uuid),postern.propose_ownership(uuid,uuid,uuid,text,integer,bigint,uuid,text),
 postern.decide_ownership(uuid,uuid,uuid,text,uuid,text) TO postern_onboarding_service;
RESET ROLE;
