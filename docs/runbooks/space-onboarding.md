# Space creation and invitation acceptance

**October 30, 2026: isolated service APIs and browser flow implemented; not deployed.** This extends
[the native PostgreSQL integration](service-integration.md). Space creation,
invitations and acceptance now run through the real account/resource HTTP services
in the synthetic rehearsal. The opt-in browser flow is verified against these
services; message delivery and hosted rollout remain separate work.

## Creation policy

The owner selected **operator-approved creators with a per-account limit**.
Registration, accepted Terms and a freemium label do not grant creation rights.
No account receives an entitlement by default. An operator assigns a namespaced
principal a `postern.creation_entitlements.max_spaces`; zero disables new creation.
The application roles cannot read or write these entitlement rows directly.

The limit counts successful creations through this API, in all space states.
Archiving, suspension or deleting an account does not silently free slots. Joined
spaces do not consume creation slots. Imported spaces need explicit entitlement
reconciliation at cutover; they are not automatically added to this creation
ledger. These are configurable permissions, not published tier quotas. The
rehearsal uses limits of one and two to exercise exhaustion and races.

With a privileged operator connection, approval can use named psql variables:

```sql
BEGIN;
SET LOCAL ROLE postern_owner;
INSERT INTO postern.creation_entitlements(principal_id,max_spaces)
SELECT id, :'max_spaces'::integer FROM postern.principals
WHERE authority=:'authority' AND account_id=:'account_id' AND state='active'
ON CONFLICT(principal_id) DO UPDATE SET max_spaces=excluded.max_spaces;
COMMIT;
```

Check the affected-row count: an unknown/inactive account is not approved.
Production approval tools, their operator audit trail and hosted rollout are not
provided by this isolated fixture. The creation and membership operations themselves
are audited in the space transaction.

## Account API

These routes exist only for the native PostgreSQL account adapter. They require
a live `pa_` account token and acceptance of the current hosted Terms. The default
D1 Worker does not expose them. Caller-supplied principal, owner, role, email or
login IDs cannot identify the acting account.

| Request | Body / result |
| --- | --- |
| `GET /v1/spaces` | Own immutable `account_reference`, authorized active spaces and creation availability/limit/usage |
| `GET /v1/spaces?after={space_id}` | Next page, at most 50 own spaces; `next_after` is null at the end |
| `POST /v1/spaces` | `{ "operation_id": "UUID", "title": "My Postern" }` → space/owner/membership IDs, title and owner role |
| `POST /v1/invitations/preview` | `{ space_id, invitation_id, secret }`; verified recipient gets title, effective role, state and expiry, without joining |
| `POST /v1/invitations/accept` | `{ "space_id": "UUID", "invitation_id": "UUID", "secret": "pi_…" }` → space/membership IDs and actual role |

Creation normalizes the title to NFC, trims it and accepts 1–200 Unicode code
points without control characters. It serializes on the principal, checks the
operator limit, and commits a personal owner record (reusing one for the same
principal when available), active space, explicit owner membership, policy revision,
audit, outbox and idempotency result together. The deferred last-owner constraint
still applies. Organization creation remains separate; [ownership transfer](ownership-transfer.md)
uses its own proposal and explicit acceptance flow.

Retry with the same operation ID and normalized title to recover a lost response.
Different contents or an old authority generation conflict. A replay still requires
active ownership; it does not consume another slot or create another event. Disabling
future creation does not prevent the current owner recovering an existing result.

My Posterns uses fresh authorization on every page. The `after` UUID is only a
position in the current caller's filtered set, not a credential or cached permission.
Pages are not a historical snapshot; refresh to see newly joined spaces before an
already-used position. If a selected membership changes while a page waits for its
space locks, the request returns 409 `spaces_changed` and must be retried. Private
space metadata is audited before returning; another account's spaces are excluded.
Account profiles remain usable independently of this list.

## Resource API

An owner/admin first gets the existing signed space handoff and a scoped `ps_`
resource token. Invitation controls use that token, never the global account token.

| Request | Behavior |
| --- | --- |
| `POST /v1/spaces/{space}/invitations` | Create an invitation with `operation_id`, `recipient: { authority, account_id }`, `role` and `expires_in_seconds` |
| `GET /v1/spaces/{space}/invitations` | Pending, unexpired invitations for this generation; no secret or secret hash |
| `POST /v1/spaces/{space}/invitations/{invitation}/revoke` | Body `{ "operation_id": "UUID" }`; audited, idempotent cancellation |

Only owners may offer `admin`; owners and administrators may offer `member`.
Ordinary invitations cannot offer `owner`. Administrators cannot create or cancel
administrator invitations, and their pending list contains only member invitations.
Existing active members use the explicit [membership role controls](membership-management.md);
invitations cannot promote or demote them.

The recipient shares their own `account_reference` from My Posterns. The issuer
chooses that exact existing, active account. **Email invitations, address aliasing,
account lookup by email, public invitation links and automatic email delivery are
not implemented.** This avoids choosing an implicit email-matching policy.

Expiry is 60 seconds to seven days, checked by the database clock. The operator's
`onboarding_policy.max_pending_invitations` defaults to 100 per space (configurable
up to 1,000). Only live pending invitations count. This bounds the pending set; it
is not a customer tier or a hosted traffic/abuse-control claim.

The create response includes an opaque `pi_` secret. Transfer the invitation tuple
through an appropriate private channel; the service does not send a message. Submit
the secret only in preview/acceptance POST bodies, not a query string. The secret is not
a resource credential and cannot join a different account. Lists, audit, operation
results and outbox records never contain it; the database stores its SHA-256 only.

Configure a separate, stable, at-least-32-byte `invitation_key` on `SpaceService`.
Creation fails closed when this key is absent. HMAC derivation binds the secret to
the actor, authority generation, space, operation ID and full normalized request,
allowing an authorized retry to recover it without storing plaintext. A key change
prevents recovery of an old create response; revoke/reissue when necessary. Key
rotation alone does not invalidate already delivered secrets: use invitation
revocation or the authority-generation recovery protocol for that purpose.

## Browser flow

Build with both `VITE_POSTERN_ACCOUNTS_API` and `VITE_POSTERN_SPACES_API` pointing
at the corresponding native services or same-origin proxies. Leave the spaces
variable unset for the existing single-pilot account home. The default D1 Worker
cannot serve these native routes. API origins need the deployment's exact allowed
origin and HTTPS; do not enable this on a hosted frontend before configuring and
verifying those services. The local test harness is not a deployment launcher.

- `/home` lists live memberships and creator availability. Copy an invitation
  address here to identify the exact recipient account to an owner.
- `/spaces/new` creates a personal Postern only when approved and below the limit.
  A failed response keeps the original operation and name for safe retry. After
  leaving/reloading an uncertain request, check My Posterns before creating again.
- `/s/{uuid}` opens an independently checked resource session and a metadata-only
  catalog. Owners/admins can create and revoke permitted invitation roles. Share
  the resulting code privately; no email or message is sent. A failed clipboard
  operation shows a selectable text field.
- `/invitations/join` accepts a pasted code. Review checks the live recipient and
  shows the Postern name, actual role, signed-in account and expiry. Join is a
  separate action. Existing members can open the Postern without changing role.

`pr1.` invitation addresses encode the immutable authority/account ID pair; they
are identifiers, not authentication. `pi1.` codes encode the space/invitation ID
and opaque secret. They are not public links. Codes are kept only in component
memory while the screen is open and are not put in URL, history, localStorage or
sessionStorage. Switching accounts or routes abandons pending responses. Resource
`ps_` credentials stay in memory, separate from account and legacy sessions, with
best-effort revocation on SPA route/account changes. A full reload discards the
local session; any remaining server session expires or is invalidated by account
revocation. Every request still rechecks server authorization.

## Acceptance and revocation

Preview and acceptance share one live database guard. Preview is audited before
returning its narrow metadata, creates no membership, and grants no resource
access. Failed audit means no preview. Acceptance always rechecks, so previewing
before revocation, expiry or an inviter role change cannot authorize joining.

Acceptance authenticates the recipient at the account service, checks current
Terms, and passes only the server-resolved login reference into the core function.
It locks the relevant principals, authority, login and space, then rechecks the
invitation, exact recipient, hash, generation, expiry, active space and inviter's
current authority. A forwarded secret, suspended/deleted recipient, revoked login,
revoked invitation or invalid inviter cannot create membership.

An invitation is consumed with its membership, audit, policy revision and outbox in
one transaction. Concurrent acceptance returns one membership. A second previously
pending invitation cannot upgrade/downgrade an already-active membership: it binds
to that existing membership and returns its actual role. A successful acceptance
can be retried only by that recipient while the accepted membership and space remain
active. The normal account-to-space handoff is still required for a resource token.
Joining does not bypass resource grants or legacy agreement/training requirements.

Revoking a membership also invalidates pending invitations addressed to that account
in the space, and pending invitations issued by that membership. Any inviter role
change invalidates their pending invitations. Restoring a role or readmitting a
person cannot revive those invitations. Readmission requires a fresh invitation,
acceptance and membership ID; old direct/group grants and resource sessions remain
invalid. Accepted invitations do not become a second admission path after removal.

Invitation revocation and acceptance serialize on the space. Requests waiting behind
a revocation recheck after the lock releases. Mutation audit/outbox failure rolls
back the membership, invitation state, quota use and operation result together.
Unknown, wrong-space and inaccessible invitations return the same 404 through the
account API. Invalid account authentication returns 401, while an explicitly
suspended account returns the account service's existing 403 response.

## Migrations and verification

Apply core `003_space_onboarding.sql` after the unchanged core `001`/`002`, followed
by private `003_onboarding.sql` after private `001`/`002`. The core migration records
creation entitlements/operations and invitation generation, and adds scoped functions
and a narrow `postern_onboarding_service` capability. The private account owner gets
that capability; the account runtime sees only wrappers that resolve its bearer-token
hash. The resource login cannot grant creation rights or call the onboarding functions.

Then apply core `004_invitation_preview.sql` and private
`004_invitation_preview.sql`. These add the shared preview/acceptance guard and
the recipient-only account wrapper, and add the authorized title to the resource
session context. Existing migrations `001`–`003` remain unchanged.

Previously stored pending invitations are explicitly revoked by the upgrade because
they were not issued by this protocol. Existing ownership, memberships, accepted
history and authority generation are preserved. Restore rotates generation and
quarantines spaces, invalidating old invitations as well as credentials.

Run the existing owned-environment rehearsal with a new evidence directory:

```sh
.venv/bin/python -m scripts.service_rehearsal \
  --accounts-source /absolute/path/to/private/postern-accounts \
  --output .context/onboarding-check
```

The public suite covers migration upgrades, entitlement races, retry/mismatch,
transaction rollback, role/recipient isolation, hashed secrets, acceptance races,
readmission, expiry, generation changes, inviter demotion, pending-set bounds,
My Posterns paging and waiting acceptance versus revocation. The private suite
creates spaces and joins accounts using both loopback HTTP services; only entitlement
approval uses an operator fixture. Existing account/D1, resource, import and restore
regressions remain in the rehearsal. The report records source hashes and tool versions.

[Membership management](membership-management.md) now supplies scoped directory,
role changes, removal and retryable self-departure through core/private migration
`005`, preserving all earlier migrations.

M2 remains open. [Ownership transfer](ownership-transfer.md) now supplies explicit recipient
acceptance, cancellation and decline. Next work includes any approved email delivery
policy, legacy content/private-object adapters, operational cleanup/outbox delivery,
hosted controls and real-data cutover. Creation entitlements need reconciliation
with imported ownership before exposing this path outside isolation.
