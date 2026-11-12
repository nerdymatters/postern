-- Catalog pages read only the grant paths that reach the caller; can_resource still decides each returned row.
SET LOCAL ROLE postern_owner;
-- can_resource looks up the active grants of each returned resource.
CREATE INDEX grant_resource ON postern.grants(space_id,resource_id) WHERE state='active';
-- Plan each call with its own cursor and search terms, so both can bound the index scans.
CREATE OR REPLACE FUNCTION postern.resource_catalog(h text,s uuid,aud text,q text,after_id uuid,expected jsonb) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern SET plan_cache_mode=force_custom_plan AS $$
DECLARE ctx jsonb; m uuid; terms tsquery; items jsonb='[]'; allowed boolean;
BEGIN
 ctx=postern.open_resource_request(h,s,aud);
 IF length(q)>200 THEN RAISE EXCEPTION 'invalid query' USING ERRCODE='22023'; END IF;
 IF expected IS NOT NULL AND expected<>ctx THEN RAISE EXCEPTION 'stale cursor' USING ERRCODE='22023'; END IF;
 IF q<>'' THEN terms=plainto_tsquery('simple',q); END IF;
 -- Owners/admins page every resource by ID. Members read their all-member, direct and audience
 -- grants as separate paths, each in resource order and stopping at 51 visible resources, so
 -- resources granted only to others are never read. Every returned row must still pass
 -- can_resource; false or NULL fails the page closed instead of skipping that row.
 IF ctx->>'role' IN ('owner','admin') THEN
  SELECT coalesce(jsonb_agg(x ORDER BY x.id),'[]'),bool_and(postern.can_resource(s,x.id,'discover') IS TRUE) INTO items,allowed FROM (
  SELECT id,kind,title,state,content_revision FROM postern.resources
  WHERE space_id=s AND 'discover'=ANY(supported_actions) AND (after_id IS NULL OR id>after_id)
  AND (q='' OR search @@ terms) ORDER BY id LIMIT 51) x;
 ELSIF ctx->>'role'='member' THEN
  m=(ctx->>'membership_id')::uuid;
  WITH valid AS NOT MATERIALIZED (SELECT resource_id,membership_id,audience_id,all_members FROM postern.grants
   WHERE space_id=s AND state='active' AND 'discover'=ANY(actions) AND (expires_at IS NULL OR expires_at>statement_timestamp())
   AND (after_id IS NULL OR resource_id>after_id)),
  visible AS NOT MATERIALIZED (SELECT id,kind,title,state,content_revision FROM postern.resources
   WHERE space_id=s AND state='published' AND 'discover'=ANY(supported_actions) AND (q='' OR search @@ terms))
  SELECT coalesce(jsonb_agg(x ORDER BY x.id),'[]'),bool_and(postern.can_resource(s,x.id,'discover') IS TRUE) INTO items,allowed
  FROM (SELECT * FROM visible WHERE id IN (SELECT DISTINCT id FROM (
   (SELECT DISTINCT v.id FROM valid g JOIN visible v ON v.id=g.resource_id WHERE g.all_members ORDER BY 1 LIMIT 51)
   UNION ALL (SELECT DISTINCT v.id FROM valid g JOIN visible v ON v.id=g.resource_id WHERE g.membership_id=m ORDER BY 1 LIMIT 51)
   UNION ALL SELECT p.id FROM postern.audience_members a CROSS JOIN LATERAL (SELECT DISTINCT v.id FROM valid g JOIN visible v
    ON v.id=g.resource_id WHERE g.audience_id=a.audience_id ORDER BY 1 LIMIT 51) p WHERE a.space_id=s AND a.membership_id=m) c
   ORDER BY id LIMIT 51) ORDER BY id LIMIT 51) x;
 END IF;
 IF allowed IS FALSE THEN RAISE EXCEPTION 'catalog authorization mismatch'; END IF;
 PERFORM postern.record_disclosure(s,'catalog.read');
 RETURN jsonb_build_object('context',ctx,'items',items);
END $$;
RESET ROLE;
