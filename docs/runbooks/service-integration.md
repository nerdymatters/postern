# Isolated PostgreSQL service integration

**October 29, 2026: implemented and verified locally; not deployed.** The account
and resource services now have native PostgreSQL adapters against the versioned
schema. The private account implementation stays in its private checkout. Default
entrypoints still use D1 accounts and the SQLite resource service.

This round connects account creation, Terms acceptance, profiles, login/logout
and deletion to the private account schema; it connects space sessions, catalog
listing/search, resource metadata and membership revocation to the core schema.
[Onboarding](space-onboarding.md) now adds operator-approved creation and
account-bound invitations, verified in isolation.
[Groups and sharing](resource-sharing.md) adds audience management, explicit
resource grants, effective-access inspection and audited stale-grant cleanup.
It does **not** expose document/video bytes, imported legacy content, object keys,
training, exports or background processing through the new API. Those require the
legacy agreement/enrollment/training gates and private object adapter first.

## Reproduce

Use PostgreSQL 17+ CLI tools, the resource checkout's Python environment and,
for the private account checks, Bun and that checkout's installed dependencies:

```sh
.venv/bin/python -m pip install -e '.[test,postgres]'
.venv/bin/python -m scripts.service_rehearsal --output .context/service-check
```

The public command tests real PostgreSQL and the actual FastAPI routes with a
synthetic signing authority. To add the private account service:

```sh
.venv/bin/python -m scripts.service_rehearsal \
  --accounts-source /absolute/path/to/private/postern-accounts \
  --output .context/service-check-with-accounts
```

Each output directory must be new. The command never accepts a database URL or
real data. It clears inherited PostgreSQL settings and creates disposable clusters
with private Unix sockets and no database TCP listener. The private suite starts
Bun and Uvicorn on ephemeral `127.0.0.1` ports, signs synthetic Google credentials
with a temporary RSA key, and exercises both HTTP services. No Google account or
Google key endpoint is used. Fixture credentials/configuration are private to the
OS user; the signing configuration is removed, processes stopped, and clusters
removed on exit. Keep all evidence directories private because they contain test
configuration and fixture artifacts. Bun 1.4.3 needs the full Unix socket `path`;
the launcher does not rely on `hostname` alone.

`report.json` records consumed source hashes, installed tool versions, stage
results and JUnit counts. It checks that sources did not change during the run and
refuses a passing report with skipped tests. Private checks also run the existing
D1 suite, TypeScript checks and a Worker deployment **dry run**. The runner does
not publish either service or enable registration outside its synthetic fixture.

## Configuration and trust

The resource API is opt-in by code: `create_app(config, ..., spaces=service)`,
where `service` is a `postern.spaces.SpaceService`. Invitation creation additionally
requires a separate stable `invitation_key`; see the onboarding runbook. Its constructor requires an
explicit PostgreSQL connection and a `SpaceTrust` with a fixed issuer, audience,
key ID, Ed25519 public key and an independent 256-bit cursor key. The default
application does not import PostgreSQL/JWT packages, read a PostgreSQL DSN or
mount these routes. Install the `postgres` extra for the optional adapter.

The pool opens/closes with the application lifespan, has bounded connections,
queue length, connection/statement/lock deadlines, and commits or rolls back
before returning a connection. The login must be a restricted member of
`postern_resource_service`, without owner, identity-writer, reader or writer
capabilities. No browser, client or agent receives database access.

The private account adapter uses Bun SQL parameterized queries and transactions.
Its runtime login has only `postern_account_service`. Private triggers allocate
namespaced principals and synchronize account logins and lifecycle changes with
the core schema. Signup count checks and duplicate-subject creation serialize in
PostgreSQL; session counts and rate-limit buckets are bounded. Profile fields,
Terms validation and public profile disclosure reuse the existing account helpers.
The production Worker still constructs its D1 store. The synthetic verifier is
injected only by a separate rehearsal entrypoint.

Apply core migrations in order (currently `001`–`007`), then private account
migrations in order (currently `001`–`006`), using the migration
operator. Core `apply_schema` verifies each applied version's SHA-256, holds one
installer lock/transaction and rejects unknown versions. Version 1 is unchanged.
The private SQL is not bundled into this public project; its deployment migration
ledger and hosted connection/key provisioning remain cutover work.

## Request flow

1. The client signs into the account service and receives a `pa_` account token.
   An account can exist without belonging to a space.
2. With accepted current Terms, `POST /v1/space-authorizations` accepts only
   `space_id`. The account service checks a live login and active membership and
   signs an Ed25519 assertion, valid for at most 60 seconds. Issuer, audience and
   signing key are server configuration, never request choices.
3. `POST /v1/spaces/{space}/session/exchange` validates the fixed algorithm/key,
   issuer, audience, expiry, identity, space and membership. PostgreSQL rechecks
   the account login, current authority generation and active membership. A
   single-use assertion ID prevents replay. Success returns a separate opaque
   `ps_` token, valid for at most 15 minutes or the account login's remaining life.
4. Each resource request rechecks the token hash, account/login state, space,
   exact membership ID and authority generation. It locks against supported
   revocation/lifecycle operations, authorizes metadata and commits required
   audit before returning. No permission cache substitutes for this check.

The account service holds the private signing key; the resource service holds
only its public key. Tokens are not interchangeable with each other or with
legacy SQLite sessions. Database rows contain token hashes, never raw tokens.
Sessions bind to the exact membership: revocation followed by readmission cannot
revive an old token or pending assertion. Account suspension permanently revokes
its old logins; resumption requires a new login and explicit readmission.

| Resource endpoint | Behavior |
| --- | --- |
| `GET /v1/spaces/{space}/session` | Audited current scoped identity/role/revision |
| `POST /v1/spaces/{space}/session/signout` | Revoke that resource token; account login remains usable |
| `GET /v1/spaces/{space}/resources?query=...&cursor=...` | Up to 50 authorized metadata records; PostgreSQL title search |
| `GET /v1/spaces/{space}/resources/{resource}` | Authorized discovery metadata; no binding, object location or content |
| `POST /v1/spaces/{space}/memberships/{membership}/revoke` | Owner/admin permissions; body contains a UUID `operation_id`; atomic/idempotent result |

Search uses the schema's `simple` text-search configuration; relevance across
languages is not established. Signed cursors bind the actor, space, membership,
generation, policy revision and search query. Another actor or stale revision
must restart pagination. Counts and cursors contain only authorized results.
All responses use `no-store`. Audit failure returns a generic 503 without data.
Forbidden/missing metadata has the same 404 shape. Invalid, expired or revoked
space credentials receive 401. Last-owner and operation conflicts receive 409.

Account logout immediately disables dependent resource sessions on their next
request. Membership revocation commits grants/groups, policy revision, operation,
audit and outbox together. Suspension/deletion also revokes account logins and
suspends orphaned spaces. Requests waiting on a policy/account lock recheck after
it releases. A request authorized before revocation can finish its transaction;
this round does not establish a deadline for streaming bytes already in flight.
Restoring the authority with a new generation invalidates old account/resource
sessions and assertions, including when old rows are present in a backup.

## Catalog listing at scale

**October 31, 2026: verified locally; not deployed.** Core migration
`007_catalog_listing.sql` keeps catalog pages well inside the five-second
deadline when a member can see little of a large space. It changes how pages
find rows, not who may see them:

- Owners and admins page every resource in the space by ID, as before.
- Members read only their own grant paths: grants to all members, direct grants
  and grants to each of their audiences. Each path is read in resource order and
  stops after 51 visible resources. Resources granted only to other people are
  never read.
- `can_resource` still decides every returned row. If it refuses a row the page
  selected, or returns no decision, the request fails with the generic 503
  rather than returning or silently skipping the row. Session checks,
  revocation locks and the required audit row are unchanged.
- Each call is planned with its own cursor and search terms. A new
  `grant_resource` index serves the per-row `can_resource` check.

Reproduce on an owned, disposable cluster:

```sh
.venv/bin/python -m scripts.catalog_benchmark --output .context/catalog-benchmark/my-run --large 100000
```

The output directory must be new. The benchmark applies the real migrations and
seeds 230,000 synthetic resources in 104 spaces. It then calls
`postern.resource_catalog` through a restricted resource login with the
service's five-second statement and three-second lock deadlines. Each page is
compared with an independent query, and authorization calls and grant rows read
are counted. A run takes about 25 seconds after `007`, and about four minutes
before it, because those calls time out.

Results from Oct 31, 2026: PostgreSQL 17.11 on the shared office Mac (macOS
arm64, load average 7–12). Each figure is the median of five calls after one
warm-up, and includes committing the call's audit row.

| Space and caller | Before `007` | After `007` |
| --- | ---: | ---: |
| 10,000 resources, each granted to another member; caller has none | timed out at 5 s | 5 ms |
| Same space; caller has one direct grant | timed out | 3 ms |
| Same space; caller's audience has 25 grants | timed out | 17 ms |
| 100,000 resources granted to others; caller has none or one | timed out | 3 ms |
| 10,000 resources shared with all members: first / last page | 28 / 50 ms | 27 / 28 ms |
| Same space, title search with 10 matches | 13 ms | 15 ms |
| 10,000 all-member grants that expired or point at drafts | timed out | 22 ms |
| Admin, first / last page of 10,000 resources | 34 / 25 ms | 27 / 24 ms |
| Admin, last page of 100,000 resources | 21 ms | 21 ms |
| 100 spaces of 1,000 resources; caller has none | 542 ms | 3 ms |

For a member who sees nothing among 10,000 resources, a page previously made
10,000 `can_resource` calls and read 20,050 grant rows; it now makes neither.
`tests/storage/test_catalog.py` compares every page with `can_resource` for 13
callers and six searches in a randomized space. It also checks that authorization
calls and grant reads stay constant when resources granted to others quadruple.

These figures come from one local client, without network, object transfer or
concurrent writes. They are not quotas or a hosted capacity claim. A page's work
grows with the grants that reach the caller, plus up to 51 `can_resource` checks
(about 20 ms for a full page). Grants that reach a member but cannot show anything
are still read until an administrator runs [stale-grant cleanup](resource-sharing.md#retire-stale-grants).
Cleanup now retires them in audited batches without changing the visible catalog. An admin's
selective title search can read many rows in a large space. `007` builds its index
inside the migration transaction, so grant changes wait until it commits. Once
spaces hold data, apply it in a quiet window.

## Verification and remaining gates

The tests cover native login privileges; valid and forged assertions; replay;
cross-service tokens; scoped metadata/search and pagination; clean pool reuse
after commit/rollback; required audit failure; idempotent revocation; last-owner
protection; waiting reads versus revocation; readmission; generation changes;
account signup races/caps, email non-merging, Terms, profile publication/handle
conflicts, logout, suspension and atomic deletion. The original migration,
quarantine/import/restore checks remain part of the public suite.

[Browser onboarding](space-onboarding.md), [membership controls](membership-management.md)
and [ownership transfer](ownership-transfer.md), together with
[groups and explicit grants](resource-sharing.md), are now verified in isolation.
This is an integration rehearsal, not a production or capacity claim. Remaining
work includes full legacy resource and
private object adapters, durable outbox/cleanup scheduling, operational metrics,
private account migration bookkeeping, hosted TLS/pool/key rotation and recovery,
and an authorized cutover with real-data reconciliation. Any future grant,
audience or resource-policy write must follow the same space-lock/revision
protocol; unrestricted operator SQL is not a supported live mutation path.
