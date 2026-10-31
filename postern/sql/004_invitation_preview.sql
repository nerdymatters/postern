-- Recipient-only preview and acceptance share the same live authorization guard.
SET LOCAL ROLE postern_owner;
CREATE FUNCTION postern.checked_invitation(login uuid,s uuid,i uuid,secret bytea) RETURNS postern.invitations
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE inv postern.invitations; ctx jsonb; inviter uuid; p uuid; m postern.memberships; inviter_role text;
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
  RETURN inv;
 END IF;
 SELECT x.role INTO inviter_role FROM postern.memberships x JOIN postern.principals a ON a.id=x.principal_id
 WHERE x.space_id=s AND x.id=inv.inviter_id AND x.state='active' AND a.state='active';
 IF inv.state<>'pending' OR inv.expires_at<=clock_timestamp() OR inviter_role IS NULL
 OR NOT (inviter_role='owner' OR (inviter_role='admin' AND inv.role='member'))
 THEN RAISE EXCEPTION 'invitation unavailable' USING ERRCODE='42501'; END IF;
 RETURN inv;
END $$;
CREATE OR REPLACE FUNCTION postern.accept_invitation(login uuid,s uuid,i uuid,secret bytea) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE inv postern.invitations; p uuid; m postern.memberships; is_new boolean=false;
BEGIN
 inv=postern.checked_invitation(login,s,i,secret); p=postern.actor();
 IF inv.state='accepted' THEN
  SELECT * INTO m FROM postern.memberships WHERE space_id=s AND id=inv.accepted_membership_id AND principal_id=p AND state='active';
  RETURN jsonb_build_object('space_id',s,'membership_id',m.id,'role',m.role);
 END IF;
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
CREATE FUNCTION postern.preview_invitation(login uuid,s uuid,i uuid,secret bytea) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE inv postern.invitations; title text; rev bigint; effective_role text;
BEGIN
 inv=postern.checked_invitation(login,s,i,secret);
 SELECT x.title,x.policy_revision INTO title,rev FROM postern.spaces x WHERE id=s;
 SELECT role INTO effective_role FROM postern.memberships WHERE space_id=s AND principal_id=postern.actor() AND state='active';
 -- The verified recipient may not be a member yet. Audit this narrow disclosure
 -- directly; do not grant general catalog/space access to an invitation holder.
 INSERT INTO postern.activity(space_id,actor_id,action,target_id,policy_revision)
 VALUES(s,postern.actor(),'invitation.previewed',i,rev);
 RETURN jsonb_build_object('space_id',s,'invitation_id',i,'title',title,'role',coalesce(effective_role,inv.role),
 'state',inv.state,'already_member',effective_role IS NOT NULL,'expires_at',floor(extract(epoch FROM inv.expires_at)));
END $$;
GRANT EXECUTE ON FUNCTION postern.preview_invitation(uuid,uuid,uuid,bytea) TO postern_onboarding_service;
CREATE OR REPLACE FUNCTION postern.resource_context(h text,s uuid,aud text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb;
BEGIN
 ctx=postern.open_resource_request(h,s,aud);
 ctx=ctx||jsonb_build_object('title',(SELECT title FROM postern.spaces WHERE id=s));
 PERFORM postern.record_disclosure(s,'session.read');
 RETURN ctx;
END $$;
RESET ROLE;
