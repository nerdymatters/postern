# Why Posterns need a separate permission boundary

**M2 foundation, October 29, 2026. Design only.** The owner selected private-space
ownership, membership and permissions as the next round. The
[contract](../private-spaces.md) defines the proposed model; this document explains
how it fits the existing application and what must be verified before rollout.

## The problem in the current system

Today a resource host has one roster, catalog, course configuration and recovery
gate. Roster locations change report grouping, not who may read a document.
Adding another tab or a `/s/...` route cannot isolate another owner's material.
Accounts are already separate from this host, so extending the account profile
with a global `admin` flag would introduce the wrong authority boundary.

Current source evidence:

| Area | Source | Consequence for M2 |
| --- | --- | --- |
| Membership/roles | [`Permissions.apply` and `authorize_content_request`](../../postern/auth.py); [roster regressions](../../tests/postern/test_restaurant_roster.py) | Membership is email-keyed and replaced on roster refresh. Groups are metadata; a person has one role across that host. |
| Storage scope | [`SCHEMA`](../../postern/db.py), [`Config`](../../postern/config.py) | Posts, items, quiz standing, events and selfies have no space key. A second tenant must not share this schema as-is. |
| Account bridge | [Account shell](../../dashboard/web/src/accounts.tsx), [routes](../../dashboard/web/src/routes.tsx), [account contract](../accounts.md) | Verified identities are compared across separate sessions; `/s/pilot` is a compatibility route for one host, not a space registry. |
| Disclosure | [`Service.execute` and snapshot reports](../../postern/service.py) | Authorization and durable audit are coupled. Keep the second authorization check after a long snapshot read. |
| Media/cache | [Video ranges/tickets](../../postern/video.py), [composite cache](../../postern/cache.py) | Current tickets resolve their owning session; cache hits remain the caller's authorization responsibility. Ticket lifetime is not a revocation guarantee. |
| Attribution/recovery | [Certificates](../../postern/honors.py), [recovery](../../postern/recover.py), [recovery runbook](../runbooks/recovery.md) | Historical event IDs and the first retained exam pass determine certificate identity. Missing history must stay explicit. |

These observations describe the working tree, including the local certificate
checks round. They do not establish deployed versions or multi-space isolation.

## Chosen model

Keep identity separate from access. Reuse the account/profile boundary, then add
explicit memberships and resource grants scoped to one Postern. One responsible
owner can have several Posterns, each with independently assigned human owners,
administrators and members. Flat audience groups organize sharing within a space.

```text
Account authority                 Space/catalog authority
  account + profile                 owner record
  verified identity                       |
        |                              Postern
        +--- active membership ----------+--- policies + audit
                  |                      |
             audience groups        resource + explicit grants
                                         |
                               resource adapter/host
                               rechecks before delivery
```

The authority boundaries are logical. They do not require one service or database
per box. Keep a modular implementation; the [M2-06 evaluation](storage.md)
recommends PostgreSQL plus private object storage after a bounded local probe.
Provider selection and migration verification remain open. The serving host must verify policy
from a configured trusted authority. A browser comparison, unsigned identity JSON
or possession of an account bearer token is not delegated resource authority.
The exact authenticated host-to-authority protocol belongs to M2-04 and must be
settled before connecting a new host; do not teach legacy endpoints to accept
account tokens as a shortcut.

Use the open-source core for portable space/resource types, permission evaluation,
adapter interfaces, isolation tests and web UI. Keep managed account provisioning,
hosting orchestration, commercial entitlements and secrets in the private service.
Self-hosting must be able to implement the same authority interfaces. A commercial
tier may limit creation or capacity, but can never replace a resource grant.

## Trade-offs

Three roles and flat, additive groups are easier to explain than custom role
builders, nested groups and allow/deny precedence. Removing one grant may leave
another path, so the admin UI must explain effective access. Space revocation
remains the decisive way to remove all of a person's access.

Administrators see the whole Postern and manage its audiences. This preserves a
clear administrative trust boundary, but does not support secrets hidden from
that Postern's administrators. Separate spaces provide that isolation. Ordinary
members receive no content merely because they joined; publication names its
audience explicitly. For legacy migration, an explicit all-members grant can
preserve today's access without making it the default for new content.

Explicit child grants avoid surprising inheritance when a file moves between
collections. They require transactional updates for bulk sharing and a clear
preview of which children change. M2-06 must prove those operations fit the chosen
store; do not approximate a bulk share with partially visible independent writes.

Per-request authorization costs more than trusting long-lived media tickets or
cached permission decisions. It makes revocation testable and prevents a cached
thumbnail from bypassing a new policy. Long transfers also need bounded lifetimes;
the contract's 30-second target must be measured before enabling shared hosting.

## Legacy migration boundary

Start by assigning the existing deployment one immutable legacy space identity.
This is a migration mapping, not permission to add another space to its database.
Keep its original URLs and `/s/pilot` alias routed to that one configured space.
Do not derive a space from a caller-supplied host, label or query parameter.

Preserve original post, attachment, view and event IDs in a mapping keyed by
legacy installation + record kind + old ID. Allocate new catalog IDs as needed
without rewriting historical payloads. Preserve first-pass exam event IDs,
timestamps, certificate prefixes, saved names, quiz results, agreements and
enrollment retention. A missing first pass still has the documented recovery
limitation; migration must not invent it.

Link historical actors only through verified account identity and an explicit
legacy binding. A matching current email is a candidate, not sufficient evidence
that a new Google subject owns the old history. Keep ambiguous records unlinked
and unavailable to that account until resolved. Preserve original email/name
snapshots for audit attribution; a public profile rename never rewrites them.

Preserve existing administrator access within the mapped legacy space, but do
not infer responsible ownership from a roster Admin label. The operator must
identify its owner and verify initial owner memberships before migration.
Map staff/manager/C-suite to ordinary space membership with separate training
policy attributes, preserving their current quiz exemptions. Current Admin
exemptions also remain training policy, not an implied global role.

Use one membership authority at a time for the legacy space. Until an explicit
cutover, its fresh roster remains authoritative and removal/outage retains its
current effect. Native invitations must not bypass it or compete as a second
writer. Cutover needs a checked reconciliation, read-only comparison, write
handoff and rollback plan. Never union two conflicting membership sources to
avoid a refusal. Roster groups remain report labels unless an owner explicitly
maps them into real audiences after reviewing the resulting grants.

## Implementation order and exit evidence

| Slice | Deliverable | Required evidence before the next dependent slice |
| --- | --- | --- |
| M2-01, this round | Ownership, roles, membership, audiences, resource/action contract and legacy boundary | Source audit, scenario review and linked design; no runtime isolation claim |
| M2-06, storage decision | Catalog/policy/event model; transactional permission changes; scoped search; backup/recovery and one migration plan | Bounded queries on representative data, identity/history comparisons, restore and revocation design; select store before catalog code/migration |
| M2-02, membership | Creation entitlement/limits, owner bootstrap, invite delivery/acceptance, membership and transfer controls | Atomic accept/revoke races; recipient binding; last-owner handling; bounded secrets, quotas and expiry; no private content needed to render an empty account |
| M2-03, catalog migration | Legacy mappings and adapters for documents/images/video/training; compatible links | Exact attribution comparison and explicit audience preview; no loss of events, certificate identity or original access |
| M2-04, enforcement | Shared decisions on every surface; trusted authority protocol; cache/job/ticket scope | The isolation scenarios below pass against real stores and adapters, including authority failure and timed revocation |
| M2-05, pilot | Second independent owner and audience | Two-space end-to-end and restored-data checks; owner verifies correct grants, compatibility and rollback |

Membership interface design can proceed alongside M2-06. Persistent membership
implementation must use the selected policy transaction model; it must not create
a disposable second authority that the catalog later replaces. M0's security and
hosted Terms gates remain independent prerequisites for broad account registration.

The M2-02 product decisions still to make are who can create Posterns, count/member/
storage quotas by tier, invitation lifetime/delivery and verified-email matching,
and the operator process for an orphaned space. Recommended first pilot: operator-
approved creators and explicit finite caps, without inferring entitlements from
freemium. This is a recommendation, not an approved tier or limit. Storage/provider
selection and verified cutover stay with M2-06; delegated agents and device
enrollment stay with M3/M4. The selected storage workload is an experiment,
not an approved tier limit.

## Isolation scenarios required before rollout

These are future acceptance requirements, not tests run by this documentation round.

| ID | Scenario | Expected result |
| --- | --- | --- |
| ISO-01 | An account belongs to no spaces; guess space/resource IDs | Empty private list; no titles, members, counts, thumbnails or source locations |
| ISO-02 | Admin in A, member in B; use A context with B targets | Denied without metadata; B's administration remains denied |
| ISO-03 | Forged owner/role/group/account fields or foreign authority account ID | Ignored/refused; trusted principal and exact space govern |
| ISO-04 | Replay or forward an invite; race acceptance with revocation/demotion/expiry | At most one valid membership; no stale acceptance, role upgrade or wrong-recipient binding |
| ISO-05 | Remove a group grant while a direct grant remains, then revoke membership | Explain remaining access first; membership revocation denies every path |
| ISO-06 | Reinvite a previously revoked member | New membership ID; old grants, groups and tickets stay inactive |
| ISO-07 | Competing demotions/transfers remove the last owner | Serialize; no active ownerless space. Account deletion suspends orphaned spaces without deleting their history |
| ISO-08 | Member requests draft, archived, ungranted child or unsupported action | No private body; collection membership or stronger role in another space cannot help |
| ISO-09 | Cached page/thumbnail, old ticket, range request or slow report after revoke | Reauthorize before disclosure; no cached positive decision bypass; verify the ongoing-transfer deadline |
| ISO-10 | Permission authority fails during a read or cached response | No new access; ongoing transfer ends within the defined bound; account/profile remains independently usable |
| ISO-11 | Search, counts, cursors, suggestions and training lock explanations | Only authorized metadata; refuse cursor/context replay; no hidden prerequisite names |
| ISO-12 | Switch account/space while responses, uploads or reports are pending | No previous-space state or disclosure appears in the new context |
| ISO-13 | Audit write fails or permission changes while a mutation is queued | No unauthorized mutation or unaudited disclosure; retry does not duplicate committed work |
| ISO-14 | Restore older grants/memberships or partially missing owner records | Quarantine, invalidate sessions/tickets, reconcile later revocations/deletions; no access resurrection |
| ISO-15 | Rename profiles/roster groups or migrate ambiguous legacy emails | No authority expansion or silent identity merge; original progress/certificates remain attributable |
| ISO-16 | Cross-space background jobs, exports, analytics and personalized cache keys | No mixed targets, statistics or pixels; committed backups preserve scoped audit independently of departed users |

Runtime acceptance must exercise direct APIs and actual media/cache/restore paths,
not just navigation or pure policy functions. The current test suite protects
legacy behavior but cannot establish these new two-space guarantees.
