-- Immutable native presentations. Publication is an offline owner capability;
-- the runtime receives only audited, current-authority read functions.
SET LOCAL ROLE postern_owner;

CREATE FUNCTION postern.content_metadata_valid(k text,m jsonb) RETURNS boolean
 LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE v text;
BEGIN
 IF k IS NULL OR m IS NULL OR jsonb_typeof(m)<>'object' THEN RETURN false; END IF;
 IF k='document' THEN
  IF NOT m ? 'page_count' OR m-ARRAY['page_count']<>'{}'::jsonb THEN RETURN false; END IF;
  IF jsonb_typeof(m->'page_count')<>'number' OR m->>'page_count' !~ '^[1-9][0-9]{0,4}$' THEN RETURN false; END IF;
  RETURN (m->>'page_count')::bigint<=10000;
 ELSIF k='video' THEN
  IF NOT m ?& ARRAY['duration_ms','total_bytes','chunk_bytes','chunk_count']
   OR m-ARRAY['duration_ms','total_bytes','chunk_bytes','chunk_count']<>'{}'::jsonb THEN RETURN false; END IF;
  FOREACH v IN ARRAY ARRAY['duration_ms','total_bytes','chunk_bytes','chunk_count'] LOOP
   IF jsonb_typeof(m->v)<>'number' OR m->>v !~ '^[1-9][0-9]{0,9}$' THEN RETURN false; END IF;
  END LOOP;
  RETURN (m->>'duration_ms')::bigint<=86400000 AND (m->>'total_bytes')::bigint<=2147483648
   AND (m->>'chunk_bytes')::bigint<=8388608 AND (m->>'chunk_count')::bigint<=65536
   AND (m->>'chunk_count')::bigint=((m->>'total_bytes')::bigint-1)/(m->>'chunk_bytes')::bigint+1;
 END IF;
 RETURN false;
END $$;

CREATE TABLE postern.content_presentations (
 space_id uuid NOT NULL, resource_id uuid NOT NULL, revision bigint NOT NULL CHECK(revision>0),
 kind text NOT NULL CHECK(kind IN ('document','video')), metadata jsonb NOT NULL,
 published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(space_id,resource_id,revision),
 FOREIGN KEY(space_id,resource_id,revision) REFERENCES postern.objects(space_id,resource_id,revision),
 CHECK(postern.content_metadata_valid(kind,metadata))
);
CREATE TABLE postern.content_objects (
 space_id uuid NOT NULL, resource_id uuid NOT NULL, revision bigint NOT NULL,
 representation text NOT NULL CHECK(representation IN ('page','thumbnail','video')),
 ordinal integer NOT NULL CHECK(ordinal BETWEEN 0 AND 65535),
 sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
 bytes bigint NOT NULL CHECK(bytes BETWEEN 1 AND 8388608),
 PRIMARY KEY(space_id,resource_id,revision,representation,ordinal),
 FOREIGN KEY(space_id,resource_id,revision) REFERENCES postern.content_presentations
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['content_presentations','content_objects'] LOOP
  EXECUTE format('ALTER TABLE postern.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE postern.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY internal ON postern.%I TO postern_owner USING(true) WITH CHECK(true)',t);
 END LOOP;
END $$;
CREATE FUNCTION postern.immutable_content() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 RAISE EXCEPTION 'immutable content inventory' USING ERRCODE='23514';
END $$;
CREATE TRIGGER immutable_content_presentation BEFORE UPDATE OR DELETE ON postern.content_presentations
 FOR EACH ROW EXECUTE FUNCTION postern.immutable_content();
CREATE TRIGGER immutable_content_object BEFORE UPDATE OR DELETE ON postern.content_objects
 FOR EACH ROW EXECUTE FUNCTION postern.immutable_content();

-- The trusted publisher verifies actual object bytes before calling this helper.
-- All rows are written by one statement: any invalid/missing/duplicate descriptor
-- rolls back the complete publication. No upload capability is granted to HTTP.
CREATE FUNCTION postern.publish_content(s uuid,r uuid,rev bigint,k text,m jsonb,inventory jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE item jsonb; rep text; idx integer; n bigint; count_needed bigint; nthumb bigint;
BEGIN
 IF postern.content_metadata_valid(k,m) IS NOT TRUE OR jsonb_typeof(inventory) IS DISTINCT FROM 'array'
 THEN RAISE EXCEPTION 'invalid content inventory' USING ERRCODE='22023'; END IF;
 IF jsonb_array_length(inventory) NOT BETWEEN 1 AND 65537 OR octet_length(inventory::text)>16777216
 THEN RAISE EXCEPTION 'invalid content inventory' USING ERRCODE='22023'; END IF;
 PERFORM 1 FROM postern.spaces WHERE id=s FOR UPDATE;
 PERFORM 1 FROM postern.resources x JOIN postern.objects o ON o.space_id=x.space_id AND o.resource_id=x.id AND o.revision=x.content_revision
 WHERE x.space_id=s AND x.id=r AND x.content_revision=rev AND x.kind=k AND x.adapter='native-content-v1'
 AND o.bytes>0 AND o.verified_at IS NOT NULL FOR SHARE OF x;
 IF NOT FOUND THEN RAISE EXCEPTION 'invalid content source' USING ERRCODE='22023'; END IF;
 INSERT INTO postern.content_presentations(space_id,resource_id,revision,kind,metadata) VALUES(s,r,rev,k,m);
 FOR item IN SELECT value FROM jsonb_array_elements(inventory) LOOP
  IF jsonb_typeof(item)<>'object' OR NOT item ?& ARRAY['representation','index','sha256','size']
   OR item-ARRAY['representation','index','sha256','size']<>'{}'::jsonb
   OR jsonb_typeof(item->'index')<>'number' OR item->>'index' !~ '^(0|[1-9][0-9]{0,4})$'
   OR jsonb_typeof(item->'size')<>'number' OR item->>'size' !~ '^[1-9][0-9]{0,7}$'
   OR jsonb_typeof(item->'sha256')<>'string' OR item->>'sha256' !~ '^[0-9a-f]{64}$'
  THEN RAISE EXCEPTION 'invalid content object' USING ERRCODE='22023'; END IF;
  rep=item->>'representation'; idx=(item->>'index')::integer; n=(item->>'size')::bigint;
  IF (k='document' AND rep NOT IN ('page','thumbnail')) OR (k='video' AND rep NOT IN ('video','thumbnail'))
   OR rep IS NULL OR n NOT BETWEEN 1 AND 8388608 OR idx NOT BETWEEN 0 AND 65535
  THEN RAISE EXCEPTION 'invalid content object' USING ERRCODE='22023'; END IF;
  IF k='document' AND idx>=(m->>'page_count')::integer
   OR k='video' AND ((rep='thumbnail' AND idx<>0) OR (rep='video' AND idx>=(m->>'chunk_count')::integer))
  THEN RAISE EXCEPTION 'invalid content index' USING ERRCODE='22023'; END IF;
  IF k='video' AND rep='video' AND n<>least((m->>'chunk_bytes')::bigint,(m->>'total_bytes')::bigint-idx::bigint*(m->>'chunk_bytes')::bigint)
  THEN RAISE EXCEPTION 'invalid video chunk' USING ERRCODE='22023'; END IF;
  INSERT INTO postern.content_objects VALUES(s,r,rev,rep,idx,item->>'sha256',n);
 END LOOP;
 count_needed=CASE WHEN k='document' THEN (m->>'page_count')::bigint ELSE (m->>'chunk_count')::bigint END;
 SELECT count(*) FILTER(WHERE representation=CASE WHEN k='document' THEN 'page' ELSE 'video' END),
  count(*) FILTER(WHERE representation='thumbnail') INTO n,nthumb
 FROM postern.content_objects WHERE space_id=s AND resource_id=r AND revision=rev;
 -- Unique, in-range indexes plus the exact count establish contiguity.
 IF n<>count_needed OR (k='document' AND nthumb NOT IN (0,count_needed)) OR (k='video' AND nthumb NOT IN (0,1))
 THEN RAISE EXCEPTION 'incomplete content inventory' USING ERRCODE='22023'; END IF;
END $$;

-- The established catalog predicate uses statement_timestamp(). A content
-- statement may have waited behind a revocation lock since that time; also
-- require a grant that is live at the current wall clock after acquiring locks.
-- Keep the canonical policy predicate mandatory, including its supported-action
-- checks. This helper does not waive publication or adapter-specific gates.
CREATE FUNCTION postern.content_view_current(s uuid,r uuid) RETURNS boolean
 LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
 SELECT postern.can_resource(s,r,'view') IS TRUE AND
 (postern.current_role(s) IN ('owner','admin') OR EXISTS(
  SELECT 1 FROM postern.grants g JOIN postern.memberships m ON m.space_id=g.space_id
   AND m.principal_id=postern.actor() AND m.state='active'
  WHERE g.space_id=s AND g.resource_id=r AND g.state='active' AND 'view'=ANY(g.actions)
   AND (g.expires_at IS NULL OR g.expires_at>clock_timestamp())
   AND (g.all_members OR g.membership_id=m.id OR EXISTS(
    SELECT 1 FROM postern.audience_members am WHERE am.space_id=s AND am.audience_id=g.audience_id AND am.membership_id=m.id))))
$$;

CREATE FUNCTION postern.content_snapshot(h text,s uuid,aud text,r uuid,rev bigint,rep text,idx integer) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; presentation postern.content_presentations; descriptor jsonb;
BEGIN
 ctx=postern.open_resource_request(h,s,aud);
 PERFORM 1 FROM postern.resources x WHERE x.space_id=s AND x.id=r AND x.content_revision=rev
  AND x.state='published' AND x.adapter='native-content-v1' AND x.kind IN ('document','video')
  AND postern.content_view_current(s,r) IS TRUE FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
 SELECT p.* INTO presentation FROM postern.content_presentations p JOIN postern.resources x ON x.space_id=p.space_id AND x.id=p.resource_id
 WHERE p.space_id=s AND p.resource_id=r AND p.revision=rev AND p.kind=x.kind;
 IF NOT FOUND THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
 IF rep IS NOT NULL OR idx IS NOT NULL THEN
  SELECT jsonb_build_object('space_id',s,'resource_id',r,'revision',rev,'representation',representation,
   'index',ordinal,'sha256',sha256,'size',bytes) INTO descriptor FROM postern.content_objects
  WHERE space_id=s AND resource_id=r AND revision=rev AND representation=rep AND ordinal=idx
   AND ((presentation.kind='document' AND rep IN ('page','thumbnail')) OR (presentation.kind='video' AND rep IN ('video','thumbnail')));
  IF NOT FOUND THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
 END IF;
 RETURN jsonb_build_object('session_hash',h,'context',ctx,'manifest',jsonb_build_object(
  'resource_id',r,'revision',rev,'kind',presentation.kind,'metadata',presentation.metadata),'object',descriptor);
END $$;

CREATE FUNCTION postern.resource_content_manifest(h text,s uuid,aud text,r uuid,rev bigint) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE snapshot jsonb;
BEGIN
 snapshot=postern.content_snapshot(h,s,aud,r,rev,NULL,NULL);
 PERFORM postern.record_disclosure(s,'content.manifest',r);
 -- The service applies its explicit content policy before returning only the
 -- public manifest. Session/context remain internal to that second boundary.
 RETURN snapshot-'object';
END $$;
CREATE FUNCTION postern.resource_content_admit(h text,s uuid,aud text,r uuid,rev bigint,rep text,idx integer) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE snapshot jsonb;
BEGIN
 IF rep IS NULL OR idx IS NULL THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
 snapshot=postern.content_snapshot(h,s,aud,r,rev,rep,idx);
 INSERT INTO postern.activity(space_id,actor_id,action,target_id,policy_revision,detail)
 VALUES(s,postern.actor(),'content.requested',r,(snapshot->'context'->>'policy_revision')::bigint,
  jsonb_build_object('revision',rev,'representation',rep,'index',idx));
 RETURN snapshot;
END $$;
CREATE FUNCTION postern.resource_content_disclose(h text,s uuid,aud text,r uuid,rev bigint,rep text,idx integer,expected jsonb) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE snapshot jsonb;
BEGIN
 IF rep IS NULL OR idx IS NULL THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
 snapshot=postern.content_snapshot(h,s,aud,r,rev,rep,idx);
 IF snapshot IS DISTINCT FROM expected THEN RAISE EXCEPTION 'not found' USING ERRCODE='42501'; END IF;
 INSERT INTO postern.activity(space_id,actor_id,action,target_id,policy_revision,detail)
 VALUES(s,postern.actor(),'content.disclosed',r,(snapshot->'context'->>'policy_revision')::bigint,
  jsonb_build_object('revision',rev,'representation',rep,'index',idx));
 RETURN snapshot;
END $$;
GRANT EXECUTE ON FUNCTION postern.resource_content_manifest(text,uuid,text,uuid,bigint),
 postern.resource_content_admit(text,uuid,text,uuid,bigint,text,integer),
 postern.resource_content_disclose(text,uuid,text,uuid,bigint,text,integer,jsonb) TO postern_resource_service;
RESET ROLE;
