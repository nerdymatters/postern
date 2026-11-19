-- Narrow bridge to an explicitly registered legacy training installation.
-- Binding evidence is provisioned offline, never inferred from a matching email.
SET LOCAL ROLE postern_owner;

CREATE TABLE postern.training_presentations (
 space_id uuid NOT NULL, resource_id uuid NOT NULL, revision bigint NOT NULL CHECK(revision>0),
 installation uuid NOT NULL, chapter integer NOT NULL CHECK(chapter BETWEEN 0 AND 10000),
 PRIMARY KEY(space_id,resource_id,revision),
 UNIQUE(space_id,installation,chapter,revision),
 FOREIGN KEY(space_id,resource_id) REFERENCES postern.resources,
 FOREIGN KEY(space_id,installation) REFERENCES postern.imports
);
ALTER TABLE postern.training_presentations ENABLE ROW LEVEL SECURITY;
ALTER TABLE postern.training_presentations FORCE ROW LEVEL SECURITY;
CREATE POLICY internal ON postern.training_presentations TO postern_owner USING(true) WITH CHECK(true);
CREATE TRIGGER immutable_training_presentation BEFORE UPDATE OR DELETE ON postern.training_presentations
 FOR EACH ROW EXECUTE FUNCTION postern.immutable_row();

CREATE FUNCTION postern.training_snapshot(h text,s uuid,aud text,r uuid,rev bigint,mode text) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE ctx jsonb; p postern.training_presentations; email text; reachable jsonb; items jsonb; current_view boolean;
BEGIN
 ctx=postern.open_resource_request(h,s,aud);
 IF mode IS NULL OR mode NOT IN ('history','quiz','name') THEN RAISE EXCEPTION 'denied' USING ERRCODE='42501'; END IF;
 SELECT x.state='published' AND postern.content_view_current(s,r) IS TRUE INTO current_view
  FROM postern.resources x WHERE x.space_id=s AND x.id=r AND x.content_revision=rev
  AND x.kind='training' AND x.adapter='legacy-training-v1'
  AND (mode='history' OR (x.state='published' AND postern.content_view_current(s,r) IS TRUE)) FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'denied' USING ERRCODE='42501'; END IF;
 SELECT * INTO p FROM postern.training_presentations WHERE space_id=s AND resource_id=r AND revision=rev;
 IF NOT FOUND OR (mode='name' AND p.chapter<>0) THEN RAISE EXCEPTION 'denied' USING ERRCODE='42501'; END IF;
 SELECT legacy_email INTO email FROM postern.legacy_bindings WHERE space_id=s AND installation=p.installation
  AND principal_id=(ctx->>'principal_id')::uuid;
 IF NOT FOUND THEN RAISE EXCEPTION 'denied' USING ERRCODE='42501'; END IF;
 SELECT coalesce(jsonb_agg(t.chapter ORDER BY t.chapter),'[]'::jsonb) INTO reachable
 FROM postern.training_presentations t JOIN postern.resources x ON x.space_id=t.space_id AND x.id=t.resource_id
  AND x.content_revision=t.revision WHERE t.space_id=s AND t.installation=p.installation
  AND x.kind='training' AND x.adapter='legacy-training-v1' AND x.state='published'
  AND postern.content_view_current(s,x.id) IS TRUE;
 SELECT coalesce(jsonb_object_agg(m.old_id,m.resource_id),'{}'::jsonb) INTO items
 FROM postern.legacy_map m JOIN postern.resources x ON x.space_id=m.space_id AND x.id=m.resource_id
 WHERE m.space_id=s AND m.installation=p.installation AND m.kind='item' AND x.state='published'
  AND postern.content_view_current(s,x.id) IS TRUE;
 RETURN jsonb_build_object('session_hash',h,'context',ctx,'resource_id',r,'revision',rev,'mode',mode,
  'installation',p.installation,'legacy_email',email,'chapter',p.chapter,'current_view_allowed',current_view,
  'reachable_chapters',reachable,'reachable_items',items);
END $$;

CREATE FUNCTION postern.resource_training_admit(h text,s uuid,aud text,r uuid,rev bigint,mode text,operation uuid) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE result jsonb;
BEGIN
 result=postern.training_snapshot(h,s,aud,r,rev,mode);
 INSERT INTO postern.activity(space_id,actor_id,action,target_id,policy_revision,detail)
 VALUES(s,postern.actor(),'training.requested',r,(result->'context'->>'policy_revision')::bigint,
  jsonb_build_object('operation_id',operation,'mode',mode));
 RETURN result;
END $$;
CREATE FUNCTION postern.resource_training_disclose(h text,s uuid,aud text,r uuid,rev bigint,mode text,operation uuid,expected jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,postern AS $$
DECLARE result jsonb;
BEGIN
 result=postern.training_snapshot(h,s,aud,r,rev,mode);
 IF result IS DISTINCT FROM expected THEN RAISE EXCEPTION 'denied' USING ERRCODE='42501'; END IF;
 INSERT INTO postern.activity(space_id,actor_id,action,target_id,policy_revision,detail)
 VALUES(s,postern.actor(),'training.disclosed',r,(result->'context'->>'policy_revision')::bigint,
  jsonb_build_object('operation_id',operation,'mode',mode));
END $$;
REVOKE ALL ON FUNCTION postern.training_snapshot(text,uuid,text,uuid,bigint,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION postern.resource_training_admit(text,uuid,text,uuid,bigint,text,uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION postern.resource_training_disclose(text,uuid,text,uuid,bigint,text,uuid,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION postern.resource_training_admit(text,uuid,text,uuid,bigint,text,uuid),
 postern.resource_training_disclose(text,uuid,text,uuid,bigint,text,uuid,jsonb) TO postern_resource_service;
RESET ROLE;
