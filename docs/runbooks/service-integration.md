# Isolated PostgreSQL service integration

**October 29, 2026: implemented and verified locally; not deployed.** The account
and resource services now have native PostgreSQL adapters against the versioned
schema. The private account implementation stays in its private checkout. Default
entrypoints still use D1 accounts and the SQLite resource service.

This round connects account creation, Terms acceptance, profiles, login/logout
and deletion to the private account schema; it connects space sessions, catalog
listing/search, resource metadata and membership revocation to the core schema. The next
[onboarding round](space-onboarding.md) adds operator-approved creation and
account-bound invitations.
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

Apply core migrations in order (currently `001`–`003`), then private account
migrations in order (currently `001`–`003`), using the migration
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

## Verification and remaining gates

The tests cover native login privileges; valid and forged assertions; replay;
cross-service tokens; scoped metadata/search and pagination; clean pool reuse
after commit/rollback; required audit failure; idempotent revocation; last-owner
protection; waiting reads versus revocation; readmission; generation changes;
account signup races/caps, email non-merging, Terms, profile publication/handle
conflicts, logout, suspension and atomic deletion. The original migration,
quarantine/import/restore checks remain part of the public suite.

[Browser onboarding](space-onboarding.md), [membership controls](membership-management.md)
and [ownership transfer](ownership-transfer.md) are now verified in isolation.
This is an integration rehearsal, not a production or capacity claim. Remaining
work includes full legacy resource and
private object adapters, durable outbox/cleanup scheduling, operational metrics,
private account migration bookkeeping, hosted TLS/pool/key rotation and recovery,
and an authorized cutover with real-data reconciliation. Any future grant,
audience or resource-policy write must follow the same space-lock/revision
protocol; unrestricted operator SQL is not a supported live mutation path.
