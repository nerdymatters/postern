# Storage for multiple private Posterns

**M2-06 evaluation, October 29, 2026. Local experiment and owner-approved storage
direction; no production migration.** The owner approved PostgreSQL for authoritative
space, membership, catalog and audit records, with private object storage for hosted
files. Use an S3-compatible adapter. Keep policy evaluation in the portable resource core and managed
account orchestration in the private service. Provider, region, budget and recovery
service levels are still open. M2-06 remains incomplete until migration is verified.

The owner selected **100 spaces, 10,000 accounts and 100,000 resources** as the
first synthetic evaluation workload. These are not customer quotas, plan limits,
concurrent-user counts or a capacity guarantee. Freemium and other tier entitlements
remain M2-02 decisions. The [permission contract](../private-spaces.md) and
[implementation order](private-spaces.md#implementation-order-and-exit-evidence)
remain the behavioral requirements.

## Options and decision

| Option | Fit | Cost or limitation to resolve |
| --- | --- | --- |
| PostgreSQL + private object storage | Composite foreign keys, per-space row locking, transactional policy/audit/idempotency records, scoped SQL search and reporting | Operate a database and connection pool; choose and rehearse backups; measure hosted latency and contention |
| D1 + private object storage | Existing managed account implementation; SQL and transactional batches; platform recovery | One database executes queries serially; paid database limit is 10 GB. Partitioning by space complicates account-wide lifecycle changes and cross-space operations |
| Current SQLite + Drive + Sheets | Working single-host compatibility baseline and existing recovery path | Present schema has no space keys; roster is one authority and resource metadata comes from separate systems. It is not a multi-space permission store |

D1 **does support transactions**: a failing statement rolls back its batch.
Its documented paid limit is 10 GB per database and queries within one database
execute one at a time. The local fixture is well below that size; this experiment
does not show that D1 would fail the pilot. PostgreSQL is recommended for the
transaction and recovery model, not on an unmeasured speed or price comparison.
No hosted D1 benchmark or price estimate was run.
Sources: [D1 batch API](https://developers.cloudflare.com/d1/worker-api/d1-database/#batch),
[D1 limits](https://developers.cloudflare.com/d1/platform/limits/).

Use one PostgreSQL authority initially, with explicit space keys and indexes;
do not introduce one database per space or an independent search service yet.
File bytes belong in object storage. Device folders and live resources remain
adapter-managed sources, not mandatory cloud copies. SQLite remains useful for
the existing host and disposable recovery comparisons until cutover.

## Tenancy: shared by default, a dedicated copy on request

**Owner decision, November 10, 2026.** Every Postern lives on one shared
platform with one PostgreSQL authority. After signing in once, a person sees
every Postern they can access. A customer who needs their data kept apart can
later be given a dedicated deployment of the same software. Files and sensitive
records are to be encrypted with a key per Postern that is kept outside the
database. The line above stands: no database per space.

Two shapes were compared: the shared one, and a separate copy of Postern per
customer behind a shared sign-in.

| What goes wrong | One shared database | A separate copy per customer |
| --- | --- | --- |
| A copy of one database is stolen | Every customer's records | One customer's records; the shared sign-in list still exposes every member's email and which Posterns they belong to |
| A coding mistake shows one customer's data to another | Possible; every query must filter by space, backed by row-level security and tests | Cannot cross copies |
| An attacker can change a database | Can add themselves to any Postern | Only inside that one copy |
| The server itself is taken over | Everything on it | Everything on it |
| One person must be cut off everywhere | One change, instant everywhere | Must reach every copy; a missed copy keeps letting them in |
| A security hole needs fixing | Patch once | Patch every copy |
| A bad update or operator mistake | Hits everyone; restoring one customer is hard | Hits one customer; one copy can be updated first |

A separate copy contains damage better after a breach. The shared database is
easier to keep patched and consistent, costs less to run, and gives one sign-in
over every Postern. A takeover of the server is the same in both.

Reasons for the shared shape:

- Account suspension and membership changes must commit together (next
  section). One database gives that; separate copies do not.
- A security fix lands once.
- The My Posterns home, creating a Postern, invitations and membership are
  already built on it.
- A separate copy stays available as a deployment choice, because the software
  is open source. It needs no second architecture.

This follows a common pattern. GitHub.com splits its databases by function, not
by customer, and offers GitHub Enterprise Server, "a self-hosted version of the
GitHub platform", to customers who need their own. AWS's SaaS guidance names
the shapes pool (shared), silo (dedicated) and bridge (a mix), and notes that
silo gives the strongest isolation at the most cost and complexity.
Sources: [GitHub's database partitioning](https://github.blog/engineering/infrastructure/partitioning-githubs-relational-databases-scale/),
[GitHub Enterprise Server](https://docs.github.com/en/enterprise-server@latest/admin/overview/about-github-enterprise-server),
[AWS silo, pool and bridge models](https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/silo-pool-and-bridge-models.html),
[AWS tenant isolation strategies](https://docs.aws.amazon.com/whitepapers/latest/saas-tenant-isolation-strategies/the-bridge-model.html).

### What a breach of the shared database would expose

1. **A copy is read.** For every customer: who the members are, which Posterns
   exist, resource titles, who can see what, and the activity and training
   history. Not the files, which are stored apart. No passwords, because
   sign-in is through Google. Resource sessions and invitation secrets are
   stored only as hashes (`postern/sql/002_service_sessions.sql`,
   `postern/sql/003_space_onboarding.sql`) and cannot be reused.
2. **The database can be changed.** The worst case. The database decides who
   may see what, so an attacker could add themselves to any Postern and open
   its files through the normal application, and could alter audit and
   training records.
3. **The server is taken over.** Everything, files included.

Files that stay on a connected device (roadmap M4) are not exposed by a breach
of the hosted database; that device's member list and activity are.

### Safeguards

- In the design today: file bytes outside the database; hashed session and
  invitation secrets; a restricted runtime login per service; a new authority
  generation that invalidates every session after an incident
  ([service integration](../runbooks/service-integration.md)).
- Decided November 10: a key per Postern kept outside the database, and the
  dedicated-deployment option.
- Proposed, to settle in the content design: private, TLS-verified access to
  the hosted database (hosted TLS is still listed as remaining work in the
  service integration guide); sealed copies of the audit log outside the
  database, so tampering shows; alerts on bulk reads and exports; a written
  breach procedure (replace keys, start a new generation, notify customers).

These are design requirements. None is implemented by this section.

## Account authority and transaction boundary

The current managed account service uses D1. For the first managed multi-space
implementation, the recommended destination is a separately privileged account
schema in the same PostgreSQL database as space policy. That allows account
suspension/deletion, membership revocation and orphaned-space suspension to commit
together. Do this as a rehearsed private-service migration before native multi-space
writes; do not copy account status asynchronously into a second access authority.
This is a proposed destination, not a change to the deployed M1 service.

Keep account/profile and resource APIs, bearer tokens and package boundaries
separate. Resource services receive only the identity/status interface they need,
not access to profile, identity-provider or session-secret tables. The public
profile service receives no membership/content role. Isolate pools and statement
budgets so resource reports cannot monopolize account connections. Co-location
creates a shared database failure domain; it must not introduce a dependency on a
resource host or roster to render an account profile.

Preserve immutable account IDs, verified issuer/subject bindings, handles, profile
edits and exact Terms version/hash/timestamps. Account deletion still removes
account-owned data while leaving separately owned historical attribution intact,
as specified in the [account contract](../accounts.md). Historical actors need
stable tombstone references, not cascading deletion of audit/history. A retained
actor reference is never an active account. Self-hosted authorities remain
namespaced and must implement the same status/revocation contract. Their trusted
protocol is an M2-04 gate; the experiment does not implement it.

For production permission mutations:

1. Use an explicit transaction. Lock relevant account-status rows first in stable
   ID order, then affected space rows in stable ID order. All lifecycle and policy
   writers must use this ordering. Ordinary writes in different spaces should not
   acquire a global exclusive lock. Derive the principal from authenticated server
   context and recheck active account, space, membership and role after waiting.
2. Validate same-space references, grant actions, expected generation/revision,
   last-owner rules and current invitation/expiry state. Never read a count outside
   the transaction and use it later to authorize a destructive role change.
3. Commit the change, policy revision, audit entry, operation result and durable
   outbox together. Scope idempotency by authority/space/actor/operation key and
   bind it to the request digest. Recheck authorization even when returning a prior
   result. Roll back the whole bounded bulk share if any target is invalid.
4. Dispatch external work from the outbox after commit. Retried consumers must
   be idempotent and reauthorize any later disclosure. Retry deadlocks/serialization
   failures within bounded deadlines; never silently retry only half a change.

PostgreSQL row locks support this model, but the complete account-lock order,
invitation handling, outbox and privilege layout are **not implemented by this
probe**. See [explicit locking](https://www.postgresql.org/docs/17/explicit-locking.html).

Authorization reads use the primary. A sequentially consistent replica session
does not by itself establish that a different actor's latest revocation is visible.
If D1 is retained for any authority reads during transition, its documented
`first-primary` session mode is relevant; the bridge still needs a proved protocol.
See [D1 read replication](https://developers.cloudflare.com/d1/best-practices/read-replication/).

Composite keys include `space_id` on memberships, audiences, resources, grants,
history and derived tables. Add row-level security as defense against accidentally
omitting scope. Runtime roles must neither own the tables nor have `BYPASSRLS`;
use transaction-local trusted context and verify connection-pool reset. A custom
SQL setting is not proof of identity. Every application query still needs the
member/action predicate; backups and administrative connections are a separate
trust boundary. PostgreSQL documents the owner/superuser bypass rules in
[row security](https://www.postgresql.org/docs/17/ddl-rowsecurity.html).

## Search, retrieval and reporting

Build an authorized resource set inside the query from current active membership,
direct/group/all-member grants, expiration and the role in that space. Join it
before producing results, counts, suggestions or snippets. Use bounded keyset
pagination; cursor validation must bind account, space, generation, filters and
ordering. Recheck permission before sending a slow query's result or bytes.

Start with title/metadata search in PostgreSQL. A GIN index is available for
full-text search, but the experiment's `simple` configuration does not segment
Thai compound words. `พนักงาน` misses `คู่มือพนักงาน`. The tested title-substring
fallback returns only the visible matching resource, excluding both a hidden
same-space title and a foreign-space title. Use parameterized SQL, bounded input
and statement deadlines in the implementation; test representative Thai/English
queries before choosing ranking or tokenizer extensions. This is not evidence of
good multilingual relevance. See [PostgreSQL text-search indexes](https://www.postgresql.org/docs/17/textsearch-indexes.html).

Content extraction, body search, agent retrieval and vector indexes remain later
work. Any derived index must carry exact space/resource/content revision and can
only propose candidates; it cannot grant access. Personal history uses a
space/actor/time/event-ID index. Space-wide analytics requires that space's admin
role. Large reports need bounded jobs and reauthorization before delivery, not an
unbounded aggregate competing with sign-in.

## Private objects and restore behavior

Use immutable, space-scoped object keys and a manifest containing resource ID,
content revision, length and SHA-256. Upload to a pending key, verify the completed
bytes, then publish the manifest and grants in one database transaction. No
transaction spans PostgreSQL and object storage. A committed outbox and delayed,
reference-checked garbage collection handle failed publication, retries and
orphaned uploads; an ambiguous upload response is not proof of failure.

R2 is a candidate S3-compatible backend, not a selected provider. Buckets are
private by default, and direct object operations are strongly consistent, while
custom-domain caches can keep deleted bytes. Keep public bucket access disabled.
An authenticated adapter must recheck policy for downloads, thumbnails and range
requests; a reusable public/presigned URL must not bypass the proposed 30-second
ongoing-transfer revocation bound. Provider IAM-key revocation is not an
application permission mechanism. Test real transfers before promising the bound.
Sources: [R2 private/public access](https://developers.cloudflare.com/r2/buckets/public-buckets/),
[R2 consistency and caching](https://developers.cloudflare.com/r2/reference/consistency/).

Require database base backups plus WAL/PITR, and an independently recoverable
private object inventory retained for compatible recovery points. Set RPO, RTO,
retention, deletion behavior and operator access before provider selection. A
`pg_dump`/restore check is useful but is not PITR or a test of object recovery.
See [PostgreSQL continuous archiving](https://www.postgresql.org/docs/17/continuous-archiving.html).

Restore into a quarantined authority with spaces inactive and a new authority
generation. Invalidate old sessions/tickets/jobs, verify object hashes and history,
then reconcile later revocations, account deletions and ownership changes against
the surviving external recovery record before reopening anything. If that record
is unavailable, remain closed and require explicit reconciliation. Restoring older
grants must never reopen access automatically.

## Local experiment and reproduction

[`scripts/storage_probe.py`](../../scripts/storage_probe.py) creates its own
temporary PostgreSQL cluster and private Unix socket, clears inherited `PG*`
connection settings, listens on no TCP address and stops/removes its cluster on
exit. It never connects to an existing database. The
[`SQL fixture`](../../scripts/storage_probe.sql) is a disposable experiment, not
a production migration or complete ACL schema. Run from the repository root
with the project's Python dependencies installed and PostgreSQL 17+ CLI tools on
`PATH`, without Python optimization:

```sh
.venv/bin/python -m scripts.storage_probe --output .context/storage-evaluation/my-run
```

Use a new output directory. It retains only synthetic data: JSON results and
query plans, a server log, a logical dump and two comparison SQLite files. The
fixture adds one million small activity events, ten audiences per space, overlapping
direct/group/all-member grants and deliberately concentrates 50,000 resources in
one space. SQL statements have a 15-second deadline. The one-million-row seed
exceeded that deadline in one statement; ten 100,000-row batches completed without
raising the limit. Durability settings remain on.

The final run passed on PostgreSQL 17.11, macOS arm64, in 55.3 seconds including
seeding, checks and cleanup. The synthetic database occupied 253,474,483 bytes
(about 242 MiB), excluding the separate restored database and dump. Settings:
64 MiB shared buffers, 4 MiB work memory, 12 connections, `fsync`,
`synchronous_commit` and `full_page_writes` on. Each latency is PostgreSQL
server execution time from 20 warm samples after one warm-up, using
`EXPLAIN (ANALYZE, BUFFERS)`. They exclude network, API, object transfer and
production concurrency. Percentiles from this small sample are descriptive only.

| Query | Median ms | p95 ms | Maximum ms | Returned rows |
| --- | ---: | ---: | ---: | ---: |
| Authorized title search, first page | 27.81 | 34.05 | 38.47 | 51 |
| Authorized title search, after resource 40,000 | 28.09 | 30.92 | 32.90 | 51 |
| Authorized Thai title substring | 44.36 | 51.81 | 51.86 | 1 |
| Authorized count (10,004 visible resources) | 47.50 | 52.67 | 55.31 | 1 |
| One actor's history page | 0.17 | 0.21 | 0.39 | 51 |
| Activity grouped by actor in one space | 54.63 | 77.33 | 171.27 | 100 |

The activity maximum illustrates why the warm median is not a request deadline
or concurrency guarantee. The input digests for this run are:

```text
storage_probe.py  867ae3c4cb7242198d7667e78fd60556a993fd9dbf32a04129fce9e3d9f7ecbc
storage_probe.sql 3694105d414d14ab25a0e85c32d70eae9a5c8e26cca4864ca6c67d34d14929ef
```

Executed checks cover exact permitted counts; hidden/foreign/nonmember and
expired-grant exclusion; same-space foreign keys; resource-table RLS and local
context reset; failed write and bulk rollback; duplicate-operation/audit behavior;
competing last-owner removals; queued-admin revocation; revoked-actor retry denial;
and an independent-space write completing while another space is locked.

Three synthetic legacy event payloads survive PostgreSQL byte-for-byte. Importing
them in reverse order through the actual legacy recovery/certificate code preserves
the expected certificate number, timestamp and Thai saved name. A logical restore
compares resource/event/membership/audit counts and those raw event bytes, then
checks inactive-space denial and a new generation. These checks do not prove a
complete legacy migration, account deletion handling, full-table RLS, pool safety,
production race handling, media revocation, object recovery or PITR.

## One migration and the next implementation gate

The [versioned schema and isolated migration rehearsal](../runbooks/postgres-rehearsal.md)
are now implemented locally. They add executable constraints, privileged
transactions and quarantined checkpoint import beyond the experiment above;
they do not activate a new account or resource backend. Keep M2-06 open through
the remaining integration and deployment gates:

- The [native account/resource adapters](../runbooks/service-integration.md) now
  verify lifecycle transactions, pooled metadata requests and disclosure audit
  in isolation. [Onboarding](../runbooks/space-onboarding.md) and
  [ownership transfer](../runbooks/ownership-transfer.md) are also verified in isolation.
  Complete legacy content/object adapters, outbox/cleanup scheduling and hosted connection-pool checks. The new
  migration uses opaque identities; the integer-ID experiment stays separate.
- Catalog pages for a member who sees little of a space holding 10,000 or
  100,000 resources granted to others now take 3–17 ms locally; they previously
  exceeded the five-second deadline. See
  [catalog listing at scale](../runbooks/service-integration.md#catalog-listing-at-scale).
- Choose hosted PostgreSQL/object services, region, operational owner and budget;
  verify connection pooling, cold/warm latency, mixed reads/writes, contention,
  object size/transfer limits and recovery objectives at this workload. Local row
  counts alone cannot size production infrastructure.
- Rehearse D1 account export/import privately, preserving identities, handles,
  Terms acceptance and session behavior. Reconcile suspended/deleted accounts.
  Cut over one account writer with a checkpoint and read-only comparison. Keep
  profile publication, registration closure and independent session semantics.
- Map the legacy installation and every old resource/event ID once. Preserve raw
  event bytes, original attribution, agreement/enrollment retention, first exam
  passes, certificate prefixes/names and URLs. Ambiguous identity links remain
  quarantined. Inventory object hashes and compare complete history/progress,
  not just record counts or the probe's three-event sample.
- Freeze legacy writes for a bounded final checkpoint, replay and compare the
  final delta, then hand off the roster and resource writers exactly once. Keep
  original stores read-only for the chosen rollback window. Before new writes,
  rollback may return to that checkpoint; afterward prefer forward repair or a
  rehearsed reverse replay preserving new activity. Never roll back by blindly
  restoring an older database or by merging two membership authorities.
- Rehearse database plus object restore in quarantine, including unavailable
  recovery evidence and later revocations/deletions. Run the full
  [isolation scenarios](private-spaces.md#isolation-scenarios-required-before-rollout)
  against actual adapters and pooled application connections before the second
  owner's pilot. Existing M0 launch gates remain independent.
