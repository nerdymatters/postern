# Audience groups and resource grants

**November 1, 2026: implemented and verified locally; not deployed.** This extends
the opt-in native Postern metadata service and browser interface. It does not
enable native Posterns, migrate legacy content, or add content playback.

## Manage sharing

Open a native Postern and choose **Groups and sharing**. Owners and administrators
can create and rename flat audience groups, add or remove active memberships,
and archive groups. Members cannot inspect the directory or sharing policy.
Groups never nest or cross spaces. Membership IDs identify recipients;
account/profile names and invitation addresses do not supply authority.

Select a published resource, then share it with one active membership, one active
group, or **all current and future members**. Choose discover only or discover
and view, and an optional expiry in local time. The service stores an absolute
expiry, so retrying a lost response does not extend it. Other reserved actions
such as download and execute remain unavailable. The resource must support every
requested action. Each child resource needs its own grant.

Review each change before confirming it. A lost response leaves the exact request
available to retry; reload to check the outcome before starting a different change.
Concurrent policy changes require a reload. Every successful write that changes
access commits its revision, operation receipt, audit event and outbox entry
together. Adding a current member, removing an absent one, or a cleanup that finds
nothing commits only the receipt, so open lists stay valid. An audit or outbox
failure rolls back the mutation. Group/member/grant lists contain at most
50 rows per page; cursors bind the actor, space, list, target and policy revision.

Select a member and resource to inspect effective discover/view access. The
summary includes all matching grants and the member's space role; up to 50 grant
paths are shown, with a notice when more exist. Removing a group or revoking one
grant does not remove access through other paths. Owners and administrators
retain role access. To remove all of a person's access to this Postern, use
[membership removal](membership-management.md).

Archiving a group revokes all its grants and removes its membership links in one
transaction. Its record and audit history remain. Recreating its name creates a
new ID. Rejoining a space creates a new membership ID; old group assignments and
direct grants do not return. Grant terms are immutable: revoke and create a new
grant to change its recipients, actions or expiry. Revoked grants cannot reactivate.

## Retire stale grants

**Review cleanup** previews whether stale grants exist. Confirming retires at most
500 across the current Postern. Repeat until none remain. Candidates are active
grants that have expired, lack discover, point to a draft or archived resource,
or name an ended membership or archived group. Empty active groups are retained
because their future members may need the existing grants.

Cleanup marks rows revoked rather than deleting them. Its audit event records
every retired grant ID and reason; the original terms stay in the grant table.
It uses the same exclusive space lock and revision protocol as sharing changes.
Retries return the original batch receipt rather than consuming another batch.
Cleanup does not change the currently visible catalog. It **does** remove dormant
sharing on drafts and archives: publishing those resources again requires new
grants. This is stated in the confirmation.

Cleanup is an explicit administrative action, not a task run on every catalog
read. There is no automatic expiry worker in this slice. Expiry still denies
access immediately, even before cleanup. Operators can use the authenticated
cleanup API for maintenance, fetching the latest revision before each batch and
retaining its operation ID for retries. Do not give a maintenance job table-write
privileges or bypass the live session checks. Future publication adapters must
use the space lock/revision protocol and retire grants when unpublishing.

## API

All paths below are relative to `/v1/spaces/{space}` and require a live resource
session. Mutations require `operation_id` (canonical UUID) and `expected_revision`
from a current sharing list. Bodies reject missing or extra fields.

| Method and path | Additional body or result |
| --- | --- |
| `GET /audiences` | Active groups and member counts; optional `cursor` |
| `POST /audiences` | `label`: 1–200 normalized characters, no control characters |
| `POST /audiences/{audience}/rename` | `label` |
| `POST /audiences/{audience}/archive` | No additional fields |
| `GET /audiences/{audience}/members` | Active memberships; optional `cursor` |
| `POST /audiences/{audience}/add` or `/remove` | `membership_id` |
| `GET /resources/{resource}/grants` | Active terms, recipients and stale reasons; optional `cursor` |
| `POST /resources/{resource}/grants` | `membership_id`, `audience_id`, `all_members`, `actions`, `expires_at` |
| `POST /grants/{grant}/revoke` | No additional fields |
| `GET /resources/{resource}/access/{membership}` | Effective actions, role and bounded grant paths |
| `GET /grants/cleanup` | Stale IDs/reasons and revision; optional `cursor` |
| `POST /grants/cleanup` | Retires up to 500; returns `retired_count` |

A grant must specify exactly one recipient: one UUID and the other field `null`,
or both UUID fields `null` with `all_members: true`. `actions` is `["discover"]` or
`["discover","view"]`. `expires_at` is a future Unix timestamp in whole seconds,
or `null` for no expiry. Receipts prove a committed operation; they do not promise
that the resulting grant is still active.

## Verification

[`008_resource_sharing.sql`](../../postern/sql/008_resource_sharing.sql) is additive;
older migrations keep their checksums. Like `007`, it builds its grant index inside
the migration transaction, so grant changes wait until it commits. Once spaces
hold data, apply it in a quiet window. The runtime retains only function execution
privileges, with no direct reads/writes of grants or audiences. The
[real PostgreSQL tests](../../tests/storage/test_sharing.py) exercise additive
access against `can_resource`, same-space targets, role restrictions,
publication/actions, cleanup, receipts, readmission, pagination,
audit/outbox rollback and queued mutations. The
[browser tests](../../dashboard/web/src/sharing.test.tsx) cover confirmation,
lost responses, overlapping access, cleanup, stale revisions and account changes.

Run from the repository root with PostgreSQL 17+ on `PATH`:

```sh
.venv/bin/python -m pytest tests/storage -q
.venv/bin/python -m scripts.grant_cleanup_benchmark --output .context/grant-cleanup-check
cd dashboard/web
bun run test --maxWorkers=1
bun run build
```

The benchmark requires a new output directory and creates an owned disposable
cluster. It seeds 10,000 stale grants and one usable grant, measures catalog
latency and grant-row reads before/after authenticated cleanup, and verifies that
the exact same resource remains visible and all retired rows remain stored. It then
adds 100,000 usable grants and one stale grant and times the stale-grant preview and
one cleanup: both find stale grants through their own indexed paths, so the usable
grants are never examined one by one (in one local run: 45 ms preview and 58 ms
cleanup, against about 1 s for a scan of every active grant). Its report includes
input hashes. Timings describe the local machine, not hosted
capacity or quotas; compare row work as well as elapsed time.

Provider selection, hosted cleanup scheduling, content adapters and full media
revocation verification remain part of the existing M2 rollout gates.
