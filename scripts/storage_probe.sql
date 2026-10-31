-- Synthetic M2 storage experiment. Not an application migration or complete ACL schema.
CREATE TABLE spaces(id integer PRIMARY KEY, generation uuid NOT NULL DEFAULT gen_random_uuid(),
                    revision bigint NOT NULL DEFAULT 0, active boolean NOT NULL DEFAULT true);
CREATE TABLE accounts(id integer PRIMARY KEY);
CREATE TABLE memberships(space_id integer REFERENCES spaces, id bigint, account_id integer REFERENCES accounts,
                         role text NOT NULL CHECK(role IN ('owner','admin','member')), active boolean NOT NULL DEFAULT true,
                         PRIMARY KEY(space_id,id));
CREATE UNIQUE INDEX active_membership ON memberships(space_id,account_id) WHERE active;
CREATE TABLE audiences(space_id integer REFERENCES spaces,id integer,PRIMARY KEY(space_id,id));
CREATE TABLE audience_members(space_id integer,audience_id integer,membership_id bigint,
                             PRIMARY KEY(space_id,audience_id,membership_id),
                             FOREIGN KEY(space_id,audience_id) REFERENCES audiences,
                             FOREIGN KEY(space_id,membership_id) REFERENCES memberships);
CREATE INDEX member_audiences ON audience_members(space_id,membership_id,audience_id);
CREATE TABLE resources(space_id integer REFERENCES spaces,id integer,title text NOT NULL,
                       published boolean NOT NULL DEFAULT true,
                       search tsvector GENERATED ALWAYS AS (to_tsvector('simple',title)) STORED,
                       PRIMARY KEY(space_id,id));
CREATE INDEX resource_search ON resources USING gin(search);
CREATE TABLE grants(space_id integer,resource_id integer,membership_id bigint,audience_id integer,
                    all_members boolean NOT NULL DEFAULT false, expires_at timestamptz,
                    CHECK(num_nonnulls(membership_id,audience_id)+all_members::integer=1),
                    FOREIGN KEY(space_id,resource_id) REFERENCES resources,
                    FOREIGN KEY(space_id,membership_id) REFERENCES memberships,
                    FOREIGN KEY(space_id,audience_id) REFERENCES audiences);
CREATE INDEX direct_grants ON grants(space_id,membership_id,resource_id) WHERE membership_id IS NOT NULL;
CREATE INDEX audience_grants ON grants(space_id,audience_id,resource_id) WHERE audience_id IS NOT NULL;
CREATE INDEX common_grants ON grants(space_id,resource_id) WHERE all_members;
CREATE TABLE events(space_id integer REFERENCES spaces,id bigint,actor integer REFERENCES accounts,
                    occurred_at bigint NOT NULL, payload jsonb NOT NULL,PRIMARY KEY(space_id,id));
CREATE INDEX actor_history ON events(space_id,actor,occurred_at DESC,id DESC);
CREATE TABLE audit(space_id integer REFERENCES spaces,id uuid DEFAULT gen_random_uuid(),action text NOT NULL,
                   PRIMARY KEY(space_id,id));
CREATE TABLE operations(space_id integer REFERENCES spaces,actor integer REFERENCES accounts,key text,
                        digest text NOT NULL,result_revision bigint NOT NULL,PRIMARY KEY(space_id,actor,key));
CREATE TABLE legacy_events(installation text,event_id text,original bytea NOT NULL,
                          PRIMARY KEY(installation,event_id));

INSERT INTO spaces SELECT generate_series(1,100);
INSERT INTO accounts SELECT generate_series(1,10000);
INSERT INTO memberships(space_id,id,account_id,role)
 SELECT (i-1)%100+1,i,i,CASE WHEN i IN (1,101) THEN 'owner' WHEN i=301 THEN 'admin' ELSE 'member' END
 FROM generate_series(1,10000) i;
INSERT INTO memberships VALUES(2,10001,1,'admin',true);
INSERT INTO audiences SELECT s,g FROM generate_series(1,100) s CROSS JOIN generate_series(1,10) g;
INSERT INTO audience_members SELECT space_id,(account_id-1)/100%10+1,id FROM memberships;
-- Half the resources deliberately belong to one space; uniform fixtures hide hot-tenant costs.
INSERT INTO resources(space_id,id,title)
 SELECT CASE WHEN i<=50000 THEN 1 ELSE (i-50001)%99+2 END,i,
        CASE WHEN i=50001 THEN 'foreign secret' ELSE 'handbook resource '||i END
 FROM generate_series(1,100000) i;
UPDATE resources SET title=title||' คู่มือพนักงาน' WHERE id IN (11,12,50001);
INSERT INTO grants(space_id,resource_id,audience_id,all_members)
 SELECT space_id,id,CASE WHEN id%10<>0 THEN id%10+1 END,id%10=0 FROM resources;
INSERT INTO grants(space_id,resource_id,membership_id) SELECT 1,i,201 FROM generate_series(1,5) i;
-- The runner inserts history in bounded batches under the same statement deadline.

-- A role without ownership/BYPASSRLS proves space filtering is enforced by this table.
-- This deliberately tests resource scope only, not a full production RLS policy suite.
CREATE ROLE probe_reader NOLOGIN NOSUPERUSER NOBYPASSRLS;
GRANT SELECT ON resources TO probe_reader;
ALTER TABLE resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE resources FORCE ROW LEVEL SECURITY;
CREATE POLICY resource_scope ON resources TO probe_reader
 USING (space_id=NULLIF(current_setting('postern.space_id',true),'')::integer);

-- Permission mutations serialize on one space, not on the entire installation.
CREATE FUNCTION probe_mutate(s integer,a integer,k text,d text) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE prior operations; answer bigint;
BEGIN
 PERFORM 1 FROM spaces WHERE id=s AND active FOR UPDATE;
 IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM memberships WHERE space_id=s AND account_id=a AND active AND role IN ('owner','admin'))
 THEN RAISE EXCEPTION 'forbidden'; END IF;
 SELECT * INTO prior FROM operations WHERE space_id=s AND actor=a AND key=k;
 IF FOUND THEN
   IF prior.digest<>d THEN RAISE EXCEPTION 'operation mismatch'; END IF;
   RETURN prior.result_revision;
 END IF;
 UPDATE spaces SET revision=revision+1 WHERE id=s RETURNING revision INTO answer;
 INSERT INTO operations VALUES(s,a,k,d,answer);
 INSERT INTO audit(space_id,action) VALUES(s,'probe_mutation');
 RETURN answer;
END $$;
ANALYZE;
