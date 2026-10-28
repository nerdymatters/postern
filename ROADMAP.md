# Postern roadmap

Postern is becoming a resource hub where owners share access with people and AI
agents. Profiles identify participants; private spaces organize resources and
audiences; permissions govern what each participant may do. Resources can live on
a hosted service, an organization's server or a connected device.

This is the master-plan implementation checklist, created on 2026-10-26.
[TODOS.md](TODOS.md) remains the maintenance backlog. The current system is
described in [Architecture](docs/architecture.md).

## Direction and tracking

- **First release:** any supported, verified Google user can create a Postern
  account and profile. Existing private spaces remain invitation-only. A new
  account starts with no private access.
- **Hosting boundary:** managed cloud signup/account orchestration and future
  billing remain private and separate from the existing resource host. Define
  the reusable open-source interfaces and package boundaries before coding.
- **UI design and marketing:** the project owner will use [Ploy.ai](https://ploy.ai/)
  to develop Postern's interface direction and marketing. Selected designs and
  copy feed into implementation through the parallel track below.
- **Product vocabulary:** each private resource space is a **Postern**. The
  navigation label is **Posterns**, the dashboard is **My Posterns**, and
  **Profile** remains the separate place for a person's identity.
- **Current milestone:** M1, accounts and profiles. Implementation is prepared;
  hosted Google sign-in, controlled two-account switching and a read-only
  history comparison passed. Live member acceptance and production rollout
  remain pending. M0 launch preparation runs alongside it.
- **Later milestones:** ordered proposals, with implementation details to settle
  at each milestone. Their order is not a release-date commitment.
- **Tracking:** task IDs remain stable. Check a task only after its outcome is
  verified, and add the relevant PR, issue or validation reference beside it.
  Keep unresolved choices visible in the task; use the milestone's exit criteria
  to decide whether it is ready to release.

| Milestone | Status | Dependency |
|---|---|---|
| M0 Launch preparation | Planned; parallel with M1 | Current deployment baseline |
| M1 Accounts and profiles | Controlled Google pilot passed; production pending | M0 launch checks before release |
| UI design and marketing | Planned with Ploy.ai; parallel with M1 | Product direction and current user flows |
| M2 Private spaces and resource catalog | Planned | M1 identity and access boundaries |
| M3 Read-only agent access | Later | M2 resource API and permissions |
| M4 Rust folder client | Later | M2 resource API and permissions |
| M5 Screen sharing | Research, then implementation | M2 permissions and a validated integration |
| M6 Bounded compute | Research, then implementation | M2 permissions and a selected workload |

M3 and M4 can proceed independently after M2. Screen-sharing research can run
earlier; it does not require rewriting the upstream application in Rust.

## UI design and marketing

**Outcome:** a consistent Postern interface and clear messaging for the resource
hub. The project owner leads exploration in [Ploy.ai](https://ploy.ai/); selected
outputs guide implementation in this repository. This track runs alongside M1.

- [ ] **DM-01** Use Ploy.ai to explore the visual direction and key UI flows,
  starting with sign-in, profiles and My Posterns. Include mobile layouts and
  the invited-member and no-access states.
- [ ] **DM-02** Use Ploy.ai to develop positioning, landing-page concepts and
  launch copy for sharing resources with people and AI agents. Distinguish
  available capabilities from the roadmap's future milestones.
- [ ] **DM-03** Bring the selected designs and copy into Postern. Verify mobile
  and keyboard usability, profile visibility, private-space access behavior and
  the accuracy of public product claims before publishing.

## M0 Launch preparation

**Outcome:** expand the product while preserving the existing private pilot.

- [ ] **M0-01** Record the baseline for sign-in, membership removal, agreements,
  enrollment, training progress and certificate attribution. Define migration
  and rollback checks that preserve live activity.
- [ ] **M0-02** Review the [security follow-up](https://github.com/nerdymatters/postern/issues/1),
  resolve and verify auth-relevant findings before opening registration, and
  record the disposition of remaining findings in that issue.
- [ ] **M0-03** Define account-service hosting, data storage, Google OAuth
  configuration, backups and operating ownership. Keep managed account secrets
  and private resource data outside the public repository.

**Ready when:** the launch has a verified compatibility baseline, documented
security dispositions and a usable deployment/recovery plan. Preparation does
not block independent profile design work.

## M1 Accounts and profiles

**Outcome:** someone can sign in, own a profile and reach My Posterns, whether or
not they belong to the existing private space.

Implementation evidence for M1-01 through M1-07 is recorded in the
[validation report](docs/reports/accounts-profiles.md). Checked implementation
tasks do not represent a completed production rollout.

- [x] **M1-01** Design the new-user, existing-member and no-access flows. Settle
  session/storage interfaces, public/private package boundaries and profile
  visibility. Proposed default: explicitly publish selected profile fields.
- [x] **M1-02** Add durable account/profile storage and verified external identity
  links using an immutable account ID and Google provider/subject. Handle repeat
  sign-in, missing optional claims and identity conflicts without silently
  merging accounts by email. Keep profiles independent of roster refreshes.
- [x] **M1-03** Separate account sessions from private-space sessions. Bind them
  to the same verified identity; refuse or deliberately switch accounts on a
  mismatch. Preserve existing membership checks, agreements and enrollment.
  A space refusal must leave the account usable.
- [x] **M1-04** Build profile editing, a shareable `/@handle` page and My Posterns
  with a zero-space state. Seed name/avatar from Google when available, preserve
  user edits, handle reserved/duplicate names and keep account pages independent
  of private-content loading. Verify mobile and keyboard use plus error states.
- [x] **M1-05** Implement a minimal public profile response. Keep email, private
  memberships, resource/activity metadata, enrollment photos and training
  records private. Keep certificate names separate from profile display names.
- [x] **M1-06** Add signup/profile write limits, bounded storage, account
  suspension/profile unpublication and deletion/retention behavior that respects
  separately owned organization history. Preserve configurable closed
  registration for self-hosted installations.
- [x] **M1-07** Verify direct API/media denial for nonmembers; stable identity
  and handle behavior; same-identity session binding; logout/account switching;
  and roster revocation/outage affecting only the private space. Confirm existing
  progress, certificates and protected-content requirements remain intact.
- [ ] **M1-08** Pilot with an existing member, an outside Google account and a
  revoked member. Verify deployment, recovery and operational controls before
  broader registration. Record release evidence.
  Hosted sign-in and two-account mismatch/switching checks passed against the
  preview account service and synthetic space data; live member acceptance and
  production operating decisions remain open.

**Done when:** an outside Google user can create and edit a profile with zero
private access; an existing member retains training access/history; removing
space membership leaves the account intact; public profiles expose only the
selected public fields. Public signup does not depend on the private roster's
availability or acceptance of a space's agreements.

## M2 Private spaces and resource catalog

**Outcome:** owners manage separate private spaces and explicitly share resources
with chosen audiences.

- [ ] **M2-01** Define owners, spaces, memberships, audience groups, resource IDs
  and supported actions. Scope roles to a space. Treat current roster groups as
  reporting metadata until real isolation is implemented.
- [ ] **M2-02** Build invitations, acceptance, membership management and
  revocation. Decide who may create spaces and under what limits.
- [ ] **M2-03** Adapt existing documents, videos and training to the resource
  catalog. Preserve existing links and historical attribution; handle legacy
  identity conflicts explicitly during migration.
- [ ] **M2-04** Enforce space/resource permissions across discovery, content,
  media tickets, caches, search, exports, background jobs and restores. Define
  scoped activity records and measurable revocation behavior.
- [ ] **M2-05** Verify two independent spaces cannot discover, read or manage one
  another's private resources, including through direct requests and restored
  data. Pilot a second audience with a real owner.

**Done when:** owners can invite/revoke audiences, resources are consistently
scoped, and two-space isolation is demonstrated beyond the interface.

## M3 Read-only agent access

**Outcome:** an approved AI agent can discover and read selected resources with
its own limited authority.

- [ ] **M3-01** Add named agent identities with accountable owners, separate
  credentials, explicit resource/action grants, expiry and revocation. Bound
  delegated access by the owner's current permissions.
- [ ] **M3-02** Expose authorized listing, search and retrieval through the
  resource API and an MCP adapter. Apply the same checks to metadata, indexes
  and content; validate credential audience and scope.
- [ ] **M3-03** Make external AI processing/export an explicit owner choice.
  Treat resource content as untrusted data that cannot expand authority. Record
  the agent's own identity in access/activity records.
- [ ] **M3-04** Verify denied resources cannot appear through search or content
  access; credentials expire/revoke as intended; and one real assistant can
  complete a useful read-only workflow.

**Done when:** an owner can grant, inspect and revoke a named agent's bounded
access without sharing a human browser session.

## M4 Rust folder client

**Outcome:** an owner connects a device and shares a selected folder with a
specific audience.

- [ ] **M4-01** Settle the first platform, transport and availability contract.
  Proposed pilot: portable Rust core, macOS first, outbound connection and a
  live read-only folder. Stored snapshots require a separate explicit choice.
- [ ] **M4-02** Implement device enrollment, protected credential storage,
  selected-root preview, audience assignment, presence/last-seen status and
  visible pause/disconnect controls.
- [ ] **M4-03** Enforce grants on listings and reads. Use resource IDs and safe
  filesystem access; test traversal, symlink escape, replacement races, hidden
  secrets, transfer limits and interrupted reads.
- [ ] **M4-04** Verify device revocation, expired grants and offline behavior.
  Prepare signed distribution and an authenticated update path before general
  distribution; pilot a real folder with an invited recipient.

**Done when:** recipients can access only the selected resources while authorized,
and device unavailability or revocation has clear, verified behavior. Explain
that revocation cannot recall copies already downloaded.

## M5 Screen sharing

**Outcome:** a space can contain a host-approved live screen session.

- [ ] **M5-01** Evaluate a pinned [p2p.kiwi](https://github.com/dont-be-evil-company/p2p.kiwi)
  release, integration interfaces, license notices and security assumptions.
  Prototype before committing to embedding, forking or a delivery estimate.
- [ ] **M5-02** Prove Postern-authorized admission, participant identity binding,
  removal of a connected participant, host stop and relay/network failure
  handling. A private invite-link page alone does not satisfy this task.
- [ ] **M5-03** Build and pilot view-only sessions with visible host controls,
  scoped activity records and a defined revocation deadline. Treat remote
  control as a separate future permission and design decision.

**Done when:** live-session permissions and stop/revoke behavior are enforced by
the integration, including after a participant has joined.

## M6 Bounded compute

**Outcome:** a person or agent can invoke one owner-approved computation.

- [ ] **M6-01** Select a real workload, intended audience and operator; define
  typed inputs/results, output ownership and cost responsibility.
- [ ] **M6-02** Prototype an isolated worker with CPU/GPU, memory, time, storage
  and spend limits; explicit network policy; cancellation; and scoped audit.
- [ ] **M6-03** Connect a named job capability to resource permissions. Verify
  unauthorized requests, quota exhaustion, cancellation and failure recovery
  before expanding beyond the pilot workload.

**Done when:** the chosen job completes within its permissions and limits, and
owners can observe and stop it. Arbitrary uploaded programs or remote shells
require a separate design.
