# Private Posterns: ownership and permission contract

**M2-01 contract, October 29, 2026. Isolated metadata/onboarding APIs implemented; full content integration pending.** This defines the
model that M2 will build and verify. The current resource host still serves one
private space; its restaurant/location tabs only filter reports. This document
does not enable multi-space hosting or expand anyone's existing access.

A **Postern** is a private space with its own membership, resources and policies.
An account may belong to several Posterns with different roles in each. An
account with no memberships has a usable profile and an empty **My Posterns**.
See the [design rationale and implementation order](designs/private-spaces.md),
[current architecture](architecture.md) and [roadmap](../ROADMAP.md#m2-private-spaces-and-resource-catalog).

## Identity and records

These are logical records, not a database migration or a new HTTP API. The
[M2-06 storage evaluation](designs/storage.md) recommends PostgreSQL and private
object storage; provider selection and migration verification remain open. A
[versioned schema and synthetic rehearsal](runbooks/postgres-rehearsal.md) now
implement the storage foundation. [Isolated service APIs](runbooks/space-onboarding.md)
now exercise metadata access and onboarding; the default deployed entrypoints
remain unchanged.

| Record | Identity and required relationships | Constraints |
| --- | --- | --- |
| Account reference | Configured account authority + immutable account ID | Resolve server-side from verified identity. Email, handle and display name never grant access or merge accounts. Hosted and self-hosted authority namespaces cannot collide. |
| Owner | Opaque owner ID; kind `person` or `organization` | Names the responsible owner independently of a space. A person owner references one account; an organization has a private descriptive record. Neither organization affiliation nor a public profile grants a role. |
| Space | Opaque space ID; owner ID; state `active`, `suspended` or `archived`; policy version | Exactly one owner record. Every active space has at least one active human member with role `owner`. One owner may have several spaces; rights do not carry between them. |
| Membership | Opaque membership ID; space ID; account reference; role; state `active` or `revoked` | At most one active membership per account per space. A revoked membership ID is never reactivated; readmission creates a new ID so old grants cannot revive. |
| Invitation | Opaque invitation ID; space ID; intended recipient; offered role; expiry; hashed secret; state | States: `pending`, `accepted`, `revoked`, `expired`. An invitation is not a membership or a bearer content credential. Acceptance binds one authenticated account atomically. |
| Audience group | Opaque group ID; space ID; display label | Flat groups of active memberships in that space. No nesting, public groups or cross-space members in M2. Labels are editable and never authorize access. |
| Audience membership | Space ID + group ID + membership ID | All three records must refer to the same space. Revocation removes the effective relationship immediately. |
| Resource | Opaque resource ID; immutable space ID; kind; adapter binding; content revision; publication state | A resource belongs to exactly one space. Its adapter locates and serves the actual bytes. Provider IDs, filenames, paths and content hashes are not access credentials. |
| Grant | Opaque grant ID; space ID; resource ID; subject; explicit actions; optional expiry; state | Subject is one membership, one audience group, or `all_members` of this space. Target and subject must be in the same space. State is `active` or `revoked`; expiry uses server time. |
| Activity | Stable event ID; space ID; actor reference; action; target; time; policy version; outcome | Retain original attribution through rename, revocation, deletion and restore. Do not place raw tokens, invitation secrets or provider credentials in events. |

Use server-generated, opaque IDs for new records. Display slugs and names are
mutable aliases, never lookup keys for authorization. Every resource lookup and
relationship check includes the space ID even if IDs are globally unique.
Cross-space foreign-key relationships must be impossible in the selected store.
An unknown record and a record in another space must not disclose different
private metadata through errors.

An owner record is responsibility metadata; the space's explicit `owner`
memberships identify the people allowed to act for it. An organization controller,
account-service operator or billing contact gains no implicit content role.
Operational database access is a separate trust boundary, not an application role.

## Roles and administrative actions

The first model has three space roles. Extra job titles and training exemptions
are separate attributes, not additional administration levels.

| Action | Owner | Administrator | Member |
| --- | --- | --- | --- |
| See this space in My Posterns and its basic metadata | Yes | Yes | Yes |
| Discover/view published resources | All in this space | All in this space | Only resources with a matching grant |
| Create, update, publish or archive resources; manage drafts | Yes | Yes | No |
| Manage audience groups and resource grants | Yes | Yes | No |
| Invite, revoke or readmit ordinary members | Yes | Yes | No |
| Assign/demote administrators or change owner memberships | Yes | No | No |
| Read space-wide activity/training reports | Yes | Yes | No |
| Read one's own available progress/certificate | Yes | Yes | Yes, under the training policy |
| Change space policies, suspend/archive the space, transfer ownership | Yes | No | No |
| Leave the space | Yes, subject to the last-owner rule | Yes | Yes |

Administrators can access every resource within their space, including drafts
needed for management. They are not a way to create confidential subgroups
hidden from administrators. Use a separate Postern for that boundary. Members
do not receive a roster or group-member directory merely by joining; shared
directory features would need a separately defined disclosure policy.

Resource viewing still applies the adapter's agreements, enrollment and other
content gates to every role. Administration without a selfie does not exempt an
administrator's reading request. A role alone does not authorize original-file
download, agent export, remote control or computation.

Ordinary membership/grant changes commit with their audit event and a new policy
version. An administrator cannot modify owner/administrator memberships or
promote themselves. Only an owner can grant the administrator role. Creating or
accepting an owner role uses the ownership-transfer flow, never an ordinary invite.

The last active owner cannot leave or be demoted through ordinary membership
controls. Ownership transfer requires a current owner and an explicitly accepting,
active recipient, and atomically updates the responsible owner and role assignments.
Changing a person's email or an organization's name is not a transfer. If account
deletion/suspension removes the last available owner, suspend the affected space
and preserve its history for operator-assisted recovery; do not silently appoint
an administrator or prevent deletion of the person's hosted account. Recovery
must establish an authorized successor and record the handoff before reopening.

Transferring one Postern changes that space's owner reference and approved role
assignments only. It never rewrites a shared owner record to transfer sibling
spaces. Deleted account references may remain non-authenticating historical
references; recreating an account does not claim them.

## Membership and invitation rules

New hosted accounts start with no private memberships. Space creation eligibility
and quotas are separate from account registration and from the freemium label;
The owner selected operator-approved creators with configurable per-account limits.
The isolated API enforces explicit entitlements; no signup or tier label grants one.
The [onboarding runbook](runbooks/space-onboarding.md) defines the current counting
policy and remaining hosted/UI work.

M2-02 implements these invitation requirements. The current isolated protocol uses
account-bound invitations only; email binding and delivery remain undecided:

1. A current authorized inviter chooses one space and recipient. Email may be
   used for delivery, but acceptance resolves a verified account rather than
   trusting an email or account ID supplied in the acceptance body.
2. The invitation is bounded, expires, is stored only as a secret hash and is
   single-use. A forwarded link alone cannot accept it for a different recipient.
   Account-bound invitations require that account; email-bound invitations require
   a current verified address with an explicit matching policy. Do not import the
   legacy Gmail alias rules into new invitations without deciding that policy.
3. Acceptance rechecks the space, invitation and inviter's current authority.
   It atomically consumes the invitation and creates membership with an audit
   event. Revoked, expired or demoted-inviter requests cannot create membership.
   Retrying a successful acceptance returns the same result only to its accepting
   account; it cannot upgrade an existing role or create another membership.
4. Removing a member revokes membership, effective group relationships, direct
   grants and space sessions/tickets. It does not delete the global account,
   memberships in other spaces or historical activity. Reinvitation requires a
   new acceptance; old direct/group grants stay revoked.

Removing someone from an audience only removes that audience's grants. Another
direct, group or all-members grant may still allow access. The management UI must
show those remaining paths; **Remove from Postern** is the action that ends all
of that person's access to this space. M2 uses additive grants, without deny rules
or group nesting, so this behavior stays explainable.

## Resource actions and publication

| Action | Meaning | M2 support |
| --- | --- | --- |
| `discover` | See permitted title/type and approved metadata in lists, search or a direct metadata response | Required for catalog entries; no source locations or other audiences' counts |
| `view` | Open the protected human presentation, such as stamped pages or the video player | Existing document/image/video adapters, after their content gates |
| `download` | Receive original/source bytes | Reserved, denied by the current adapters |
| `export` | Receive a machine-readable copy for an external agent/service | Reserved for M3; requires an explicit owner choice and adapter support |
| `control`, `execute` | Operate a remote session or run a job | Reserved for M5/M6; denied in M2 |

An adapter declares supported actions. Unsupported or unknown actions are denied,
including for owners. Resource management uses the role matrix, not a grant that
a member can delegate. No read permission implies a stronger action. A valid
`view` grant also includes `discover`; metadata-only grants may contain just
`discover`. Discovery can show a gated resource to an authorized audience, but
does not bypass its agreements, enrollment, publication or training prerequisites.

New resources start as drafts with no member grants. Publishing requires explicit
audience selection and confirmed content preparation. Publication and the chosen
grants become visible together; failed preparation cannot expose a partial record
or source location. Archiving hides the resource from members and ends new views
while retaining its history. Owner/admin management access is still audited.

A collection (including a legacy post) and its children are separate catalog
records. A collection grant does not automatically reveal every child. List only
children the viewer may discover, and check each requested child before serving
it. Publishing a set may explicitly write matching child grants in the same
transaction; later changes never rely on undocumented inheritance. Resource
space IDs are immutable: moving content to another space is a separately audited
copy with new identity and fresh audience selection, not a field edit.

Training remains an adapter policy. Category names, job titles, restaurant tabs
and quiz exemptions grant no resource access. A training publication must check
that its intended audience can reach required course resources; hidden prerequisites
must not leak their titles or IDs through a lock explanation. Own historical
progress and certificates remain space-scoped even when content is archived.
Certificate number strings are not globally unique IDs or authorization tokens.

## Permission evaluation and revocation

The proposed shared decision interface is logical, not a callable function in
today's code: **verified principal + space + resource/action + current policy →
allow or refusal**. It receives trusted account/session context, never a role,
group list, owner ID or email asserted by browser JSON.

For each protected operation:

1. Verify the session's issuer/audience, expiry and revocation. Resolve the trusted
   account reference and a current active membership in this exact space. Account
   suspension/deletion and a suspended space deny access regardless of grants.
2. Resolve the target under that space; require the supported action, publication
   state and either a permitted management role or a matching, unexpired grant.
   Membership is required even when a stale direct grant still exists.
3. Apply account and space requirements independently: hosted Terms for the hosted
   account path; space agreements, enrollment and training policy where applicable.
   Terms acceptance alone grants no membership. Preserve the standalone host's
   existing operator policies; do not require it to contact the managed service.
4. Authorize and durably audit before returning private metadata/content or
   committing a mutation. A long read may run outside the writer, but must recheck
   current authorization before disclosure. Mutations recheck in their commit
   transaction. An unavailable authority or failed required audit yields no data.

Retried mutations use an operation key bound to the actor, space, action and
request contents. Reusing a key with different contents is refused. A lost
response can recover the committed result without duplicating membership, grants
or audit work, but returning that result still requires current authorization.

M2's online authority must reject new operations after revocation commits. Every
media range request, cache hit and background delivery reauthorizes; a ticket or
cached body is insufficient. Policy versions include an authority generation and
increasing revision; restore establishes a new generation, never reuses an old
version, and invalidates old credentials/cursors.
Caches may retain content for performance but cannot retain a positive access
decision across an unchecked policy change.

**Proposed M2-04 acceptance target:** an already-authorized, ongoing transfer stops
sending new private bytes within 30 seconds after revocation or loss of permission
authority. Bound response sizes, authorization lifetime and cancellation; a byte
cap by itself is not a time bound. Verify this under slow clients and lost
connections before claiming the target. Already delivered/buffered copies cannot
be recalled. This is a future target, not a measured guarantee of the current host.

For authenticated nonmembers, missing and inaccessible spaces/resources return
the same not-found response. Invalid authentication returns 401. A known member
may receive an action refusal or an unavailable-authority response without extra
private metadata. Lists, result counts, pagination cursors and suggestions are
computed from the authorized set. A cursor is bound to principal, space, query and
policy version and is refused/restarted after an incompatible permission change.

## Every surface carries the space boundary

| Surface | Required scope/check |
| --- | --- |
| Catalog and search | Authorize before returning names, snippets, counts or rankings. Scope indexes and cursor context to space and permitted resources. |
| Pages, thumbnails, portraits and video | Resolve resource/subject in the same space; bind tickets to space, resource, actor, session and supported action; check again on delivery. |
| Caches | Include space, resource/revision, representation and any actor/session watermark identity in keys; reauthorize even on a hit. Never share personalized pixels across principals. |
| Uploads and background jobs | Carry immutable space/actor/target context. Recheck grants before new disclosure or publication. Already committed internal backup work may finish under a scoped service identity; revocation must not discard its audit. |
| Audit, exports and analytics | Scope report readers and aggregates. Sealed backup exports use a scoped service identity, not a newly invented user download grant. |
| Recovery | Preserve space/resource/event IDs and historical actor references. Restore into quarantine; reconcile revocations/deletions and invalidate sessions/tickets before serving. Missing ownership or membership evidence denies access. |
| UI state | Key requests and cached state by account and space. Switching either clears prior private views and ignores late responses. A refusal in one space leaves the account and other authorized spaces usable. |

## Examples to preserve in implementation

- Asha is a member of Training and an administrator of Partners. Her Partners
  role cannot list Training's staff history or grant herself Training resources.
  Guessing a Training resource ID through a Partners URL yields no metadata.
- Ben is in Training's North audience and has a separate direct view grant.
  Removing North membership does not remove that direct access; the UI explains
  it. Revoking his Training membership denies both paths, while Partners access
  and his account remain usable. Readmission does not revive the old grant.
- A legacy certificate retains its event-derived number, date and saved-name
  history through migration. A later profile rename changes none of them.
  An unresolved legacy identity link is quarantined rather than assigned by a
  matching name or reused email.

The [implementation plan](designs/private-spaces.md#implementation-order-and-exit-evidence)
maps this contract to storage selection, invitations, migration and isolation
tests. No route, SQL migration, creation entitlement or billing behavior is
introduced by this document.
