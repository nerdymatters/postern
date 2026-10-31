# Private-space membership management

**October 30, 2026: implemented and verified in isolation; not deployed.** This
extends [space onboarding](space-onboarding.md) without moving legacy content or
changing the default account/reading-room entrypoints.

## Permissions and identity

Owners and administrators can list active memberships in their own Postern.
Ordinary members cannot read this directory. Rows contain the immutable account
authority/ID, membership ID, current role and permitted actions, not global account
email, profile or identity-provider details. The interface identifies people by
the invitation address they shared. Names/profile sharing need a separate explicit
projection; a name or email never identifies the target of a mutation.

Only owners can switch other active memberships between `member` and `admin`.
Owner roles cannot be assigned or demoted here; ownership transfer remains a
separate, accepting-recipient workflow. Owners can remove other members, including
another owner when an active owner remains. Administrators can remove ordinary
members only. Self-removal goes through **Leave Postern** for every role.

A role change revokes the target's current resource sessions and pending
invitations issued by that membership. The next handoff uses its current role.
Explicit resource grants remain when an administrator becomes an ordinary member.
Removal/departure revokes the exact membership, its direct grants, group links,
resource sessions, pending invitations addressed to it and invitations it issued.
The global account, memberships elsewhere and historical activity remain.
Readmission creates a new membership ID and does not revive old grants/sessions.

## Browser controls

With the account and native space API variables configured, an owner/admin can
open **Manage members** from `/s/{space_id}`. The directory at
`/s/{space_id}/members` has paginated rows and an exact invitation-address filter.
The filter applies to loaded rows; **Load more members** continues the search.
**Show everyone** clears the filter.

Role changes and removal show the exact account identifier, current role and
consequences before confirmation. Double clicks share one operation ID. An
uncertain response retains the original request for **Retry change**; automatic
focus refresh is paused until the user retries or chooses **Reload members**.
A changed policy revision rejects stale decisions and requires a fresh list.
Account changes and permission refusals discard private directory/confirmation
state; late responses cannot restore it.

**Leave Postern**, at `/s/{space_id}/leave`, shows the space name and consequences
before confirmation. The last owner sees an explanation with no submit action.
Another active owner must exist before an owner can leave; concurrent departures
cannot leave an active space ownerless. [Ownership transfer](ownership-transfer.md) now lets the sole owner appoint an
explicitly accepting successor before leaving.

Leave retries use the live account session and the original membership ID, even
when the successful first request has already revoked resource access. A lost
response can therefore be retried without rejoining. If readmission happens before
a retry, the old receipt is returned without removing the new membership. Reloading
or navigating away discards the in-memory operation; check My Posterns before
starting another action. Live account/login, Terms, space and generation checks
still apply; revoked authentication cannot recover receipts.

## API and transaction contract

All routes return `Cache-Control: no-store`. Native resource routes require a
space-bound `ps_` token; the account departure route requires its own `pa_` token.
Neither token can substitute for the other.

| Request | Contract |
| --- | --- |
| `GET /v1/spaces/{s}/memberships?cursor=…` | Owner/admin directory, at most 50 rows, `next_cursor`, acting membership/role and policy revision. Signed cursor is bound to space, actor, membership, generation and policy revision. Audit failure prevents disclosure. |
| `POST /v1/spaces/{s}/memberships/{m}/role` | `{operation_id, expected_revision, role}`; role must be `member` or `admin`. |
| `POST /v1/spaces/{s}/memberships/{m}/remove` | `{operation_id, expected_revision}`; checks current caller and target roles. |
| `POST /v1/spaces/{s}/leave` on the account service | `{operation_id, membership_id}`; identity/login resolved server-side; returns `{space_id, membership_id, left:true}`. |
| `GET /v1/spaces/{s}/session` | Also returns `can_leave`, derived from current ownership. No member directory is exposed to ordinary members. |

The existing `/memberships/{m}/revoke` resource endpoint remains compatible for
previous clients. New browser removal uses `/remove` and its required revision
check. Both enforce live space roles and last-owner constraints.

Mutations lock relevant principals before the authority/login/space records and
recheck after waiting. Role/removal operations compare the rendered list revision
under the exclusive space lock. An existing, exactly matching operation returns
its receipt without applying the change again, after rechecking current authority.
Reusing a key with a different request fails. A stale/new request gets 409;
invalid input gets 422; wrong-space/unknown membership gets 404; insufficient
current role gets 403. Resource authentication failure gets 401.

Membership, grants, session invalidation, invitation changes, operation receipt,
policy revision, audit and outbox commit together. Audit/outbox failure rolls them
back. The existing deferred owner constraint remains a final invariant in addition
to the explicit leave guard. Restored generation invalidates old leave receipts.

## Migration and verification

Apply core `005_membership_management.sql` after core `004`, then private
`005_membership_management.sql` after private `004`. Earlier migration bytes remain
unchanged. New definer functions keep fixed search paths and expose only the
scoped functions to resource/onboarding capabilities. The private wrapper resolves
the bearer hash to a live login; the resource role cannot invoke self-departure or
inspect the account store. The default D1 Worker does not expose the leave route.

Run the [isolated service rehearsal](service-integration.md) and the frontend suite:

```sh
.venv/bin/python -m scripts.service_rehearsal \
  --accounts-source /absolute/path/to/private/postern-accounts \
  --output .context/membership-check
bun run --cwd dashboard/web test --maxWorkers=1
bun run --cwd dashboard/web build
```

Checks cover scoped directory/paging, wrong actors and credentials, stale revision
and queued authority changes, rollback, role changes, old-session denial, pending
invitation invalidation, last-owner races, exact-membership retry after readmission,
other-space/account preservation, confirmations and late UI responses. Browser
checks use owned loopback services and synthetic identities, not production data.

M2 remains open for hosted operator controls,
legacy content/private-object integration and real-data cutover. This implementation
does not establish a production launch or a new account/tier quota.
