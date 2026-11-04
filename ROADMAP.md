# Postern roadmap

Postern is becoming a resource hub where owners share access with people and AI
agents. Profiles identify participants; private spaces organize resources and
audiences; permissions govern what each participant may do. Resources can live on
a hosted service, an organization's server or a connected device.

This is the master-plan implementation checklist, created on Oct 26, 2026.
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
- **Current milestone:** M1, accounts and profiles. The production account
  service passed a controlled pilot on Oct 28, 2026 from a separate pilot
  frontend; registration stays closed outside watched pilot windows. Opening
  registration waits for M0 launch preparation, which runs alongside it.
- **Next engineering priority:** migrate the Python resource server to Rust,
  promoted to P1 by the owner on Nov 1, 2026 because office-Mac overload remains
  after the v0.19.1 security rollout. Start with workload attribution and the
  first bounded migration slice before further M2 content integration and
  M3–M6 implementation. Urgent production/security fixes and required launch
  checks retain precedence; existing M2 work remains tracked.
- **Later milestones:** ordered proposals, with implementation details to settle
  at each milestone. Their order is not a release-date commitment.
- **Tracking:** task IDs remain stable. Check a task only after its outcome is
  verified, and add the relevant PR, issue or validation reference beside it.
  Keep unresolved choices visible in the task; use the milestone's exit criteria
  to decide whether it is ready to release.

| Milestone | Status | Dependency |
|---|---|---|
| M0 Launch preparation | In progress; parallel with M1 | Current deployment baseline |
| M1 Accounts and profiles | Production pilot passed; registration closed | M0 launch checks before release |
| Rust server migration | P1; reducer released, first live pilot rolled back; query-cost fix in validation | Complete indexed-Python rollout and Rust live acceptance in the [runbook](docs/runbooks/rust-server.md) |
| UI design and marketing | Planned with Ploy.ai; parallel with M1 | Product direction and current user flows |
| M2 Private spaces and resource catalog | Isolated metadata/onboarding implemented; content integration and hosting pending | M1 identity and access boundaries |
| M3 Read-only agent access | Later | M2 resource API and permissions |
| M4 Rust folder client | Later | M2 resource API and permissions |
| M5 Screen sharing | Research, then implementation | M2 permissions and a validated integration |
| M6 Bounded compute | Research, then implementation | M2 permissions and a selected workload |

M3 and M4 can proceed independently after M2. Screen-sharing research can run
earlier; it does not require rewriting the upstream application in Rust.

## Rust server migration

**Outcome:** reduce the office resource server's measured operating cost while
preserving its API and durable behavior. This workstream migrates the Python
server in `postern/`; M4 remains a separate Rust folder client.

- [ ] **RS-01** Establish the v0.19.1 baseline: per-process CPU and memory,
  disk I/O, request p50/p95 and error rates, database query cost, audit queue
  latency and background-job load. Distinguish the API from development/build
  processes and whole-host contention. Use short read-only production sampling
  and isolated synthetic workloads; run heavy rehearsals away from the serving
  Mac or in an agreed quiet window. Start with the existing
  [capacity harness](docs/runbooks/capacity.md) and record its client/server
  measurement limits. Include the health/audit-query path observed during the
  rollout; its dominant cost still needs profiling.
  **Partial evidence, Nov 1:** API/PostgreSQL/development CPU and RSS, disk I/O,
  bounded SQLite aggregates, health timings and synthetic reducer profiling are
  recorded in the [first-slice runbook](docs/runbooks/rust-server.md). Representative
  request p95/error rates, audit queue latency and job attribution remain open.
  **November 2 follow-up:** a passive sample averaged 20.3% whole-host CPU.
  The unchanged health query scans all 230,727 events; three read-only samples
  measured 164 ms median query time versus 237 ms for three health requests.
  These small sequential samples identify a query cost, not representative p95
  or a complete explanation of host overload. Query/index experiments should
  measure audit-write cost as well as reads before selecting a change.
  **November 4 candidate:** three additive indexes reduce the same synthetic
  health query from 130 to 62 ms and idle exporter lookups to about 0.15–0.20 ms.
  Exact query results, existing-database startup and rollback compatibility pass;
  write costs and limits are recorded in the
  [query investigation](docs/runbooks/rust-server.md#health-and-export-index-candidate-november-4).
  This addresses measured scan cost, not a proven cause of the live timeout.
- [x] **RS-02** Select one bounded server path for the first Rust implementation
  from that evidence. Record the API/storage boundary, numeric CPU/memory/latency
  acceptance targets and rollback route before coding. Address query or
  scheduling costs revealed by the baseline. Keep a single owner for database
  writes, audit ordering and background jobs during coexistence.
  **Selected:** read-only training history reduction. Targets: at least 30%
  lower roster wall/CPU, reader regression no more than max(10%, 2 ms), and
  peak RSS within Python +32 MiB. Health-query and development-load work remain
  explicit follow-ups; neither is solved by changing the reducer language.
- [x] **RS-03** Implement that slice and compare Python and Rust on the same
  fixtures and workload. Add HTTP parity coverage for authorization, revocation,
  audit-before-disclosure, recovery and the migrated path's durable effects.
  Preserve training progress, certificate attribution and existing refusal
  behavior; retain the existing regression assertions and test deadlines.
  **Released November 1:** [v0.20.0 / PR #13](https://github.com/nerdymatters/postern/pull/13)
  includes the optional PyO3 wheel, Python default/rollback, differential fixtures
  and HTTP disclosure/restart checks. Shared-host synthetic roster replay wall
  and CPU both fell 49.9%; this is not a production capacity result.
  [Merged-commit CI](https://github.com/nerdymatters/postern/actions/runs/41362837319)
  passed: macOS Python 892 tests with six expected optional-native skips, Rust
  967 tests, Linux native 70 tests per Python version and frontend 372 tests,
  plus Rust and status-worker checks. Production Rust acceptance remains open.
- [ ] **RS-04** Rehearse cutover and rollback with disposable data, then stage
  the verified slice on the office Mac with backups and health checks. Preserve
  current activity, private training and configuration. Plan the next migration
  slice from measured results before expanding feature work.
  **Disposable rehearsal passed:** Python → Rust → Python restarts preserve
  newly recorded progress, first-pass certificate identity/name, event bytes
  and schema. **Office staging completed November 1:** the exact merged release
  and CI-tested wheel were installed with private backups, 15 passing preflight
  checks and ready local/public API health. Python remains the serving backend;
  the native binary checksum and separate-process Rust import were verified.
  **November 2 benchmark passed:** after bounded attempts found no quiet window,
  a user-requested retry passed the unchanged rule and all four resource gates.
  Roster wall/CPU fell 48.5%, reader wall fell 48.1%, peak worker RSS fell 10.5 MiB
  and all paired digests matched. The operator confirmed the Python reading
  room opens with correct progress and quiz locks. See the
  [quiet-host result](docs/runbooks/rust-server.md#quiet-host-acceptance-november-2).
  **First live pilot stopped:** local administrator authentication and a checked
  backup enabled Rust on the existing job. A health probe then hit its unchanged
  two-second timeout; the helper restored Python against the current database.
  Python identity and local/public health are verified, with private settings
  and training retained. This rehearsed activation and rollback mechanics, but
  the Rust staff-result and new-activity comparison were not recorded before
  the stop. Investigate the health delay before repeating the live acceptance
  checks; the [pilot record](docs/runbooks/rust-server.md#first-live-pilot-november-2)
  distinguishes verified rollback from incomplete compatibility acceptance.
  Broader load attribution remains in RS-01.

**First deliverable:** an attributed baseline, one selected Rust migration slice
and explicit performance/compatibility acceptance criteria. The first slice is
complete only after parity, measured improvement and rollback are verified.
The remaining server migration stays open until its responsibilities have moved.

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

- [x] **M0-01** Record the baseline for sign-in, membership removal, agreements,
  enrollment, training progress and certificate attribution. Define migration
  and rollback checks that preserve live activity.
  Recorded in the [launch baseline and rollback checklist](docs/runbooks/launch-readiness.md)
  with the Oct 28 pilot evidence and Oct 29 local verification. The additive
  Terms migration preserved a legacy account/session in disposable local D1
  without inferring acceptance; no live restore was performed.
- [ ] **M0-02** Review the [security follow-up](https://github.com/nerdymatters/postern/issues/1),
  resolve and verify auth-relevant findings before opening registration, and
  record the disposition of remaining findings in that issue.
  Dependency and CI source remediation shipped in v0.18.0
  ([PR #8](https://github.com/nerdymatters/postern/pull/8)); the
  [launch-readiness report](docs/reports/launch-readiness.md) records checks.
  Issue disposition and resource/status runtime version/behavior verification
  remain before opening registration.
- [x] **M0-03** Define account-service hosting, data storage, Google OAuth
  configuration, backups and operating ownership. Keep managed account secrets
  and private resource data outside the public repository.
  Recorded with the production pilot on Oct 28, 2026
  ([validation report](docs/reports/accounts-profiles.md#production-pilot)):
  a Worker and D1 database, the existing web OAuth client, recovery through
  D1 Time Travel without extra exports, and the project owner as operator.
  Identifiers and configuration stay in the private deployment record.
- [ ] **M0-04** Before opening registration, write and publish Postern's Terms
  of Service, then build and verify how accounts accept them, including accounts
  created before the terms existed and later revisions. Keep them separate from
  each private space's own agreements.
  The owner approved [Terms version 2026-10-29](docs/terms/2026-10-29.md) for
  publication. The static page was published and its exact bytes verified with
  v0.18.0 ([publication evidence](docs/reports/launch-readiness.md#publication-round)).
  Private-service activation and a hosted acceptance/revision check remain.
  Registration is still closed.

**Ready when:** the launch has a verified compatibility baseline, documented
security dispositions, a usable deployment/recovery plan, and published Terms
of Service with a built and verified acceptance flow. Preparation does not
block independent profile design work.

## M1 Accounts and profiles

**Outcome:** someone can sign in, own a profile and reach My Posterns, whether or
not they belong to the existing private space.

Implementation evidence for M1-01 through M1-07 and the M1-08 production pilot
is recorded in the [validation report](docs/reports/accounts-profiles.md).
The account frontend runs only on a separate pilot origin, and public
registration is not open; checked tasks do not represent a completed rollout.

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
- [x] **M1-08** Pilot with an existing member, an outside Google account and a
  revoked member. Verify deployment, recovery and operational controls before
  broader registration. Record release evidence.
  Production pilot passed on Oct 28, 2026
  ([validation report](docs/reports/accounts-profiles.md#production-pilot)).
  The outside-account checks reused the revoked member's account after its
  removal, and recovery was checked for availability without a live restore.

**Done when:** an outside Google user can create and edit a profile with zero
private access; an existing member retains training access/history; removing
space membership leaves the account intact; public profiles expose only the
selected public fields. Public signup does not depend on the private roster's
availability or acceptance of a space's agreements.

## M2 Private spaces and resource catalog

**Outcome:** owners manage separate private spaces and explicitly share resources
with chosen audiences.

- [x] **M2-01** Define owners, spaces, memberships, audience groups, resource IDs
  and supported actions. Scope roles to a space. Treat current roster groups as
  reporting metadata until real isolation is implemented.
  Defined in the [private-space contract](docs/private-spaces.md) and
  [source-grounded implementation plan](docs/designs/private-spaces.md) on Oct 29,
  2026. This completes the design-definition task. An opt-in metadata/session
  runtime is now verified in isolation; complete content isolation is still pending.
  M2-06's provider selection and migration remain open.
- [ ] **M2-02** Build invitations, acceptance, membership management and
  revocation. Decide who may create spaces and under what limits.
  The owner selected operator-approved creators with configurable per-account
  limits. [Isolated onboarding APIs](docs/runbooks/space-onboarding.md) now create
  personal spaces/owner memberships, issue/revoke account-bound invitations,
  accept them atomically and list My Posterns. Retry, race and readmission checks
  preserve the permission boundary. On Oct 30, the opt-in browser flow added My
  Posterns, creation, invitation issue/revoke and recipient-only preview/acceptance,
  verified against both isolated services. [Membership management](docs/runbooks/membership-management.md)
  now adds scoped directory, role/removal confirmations, optimistic revision checks
  and retryable self-departure with last-owner protection. [Ownership transfer](docs/runbooks/ownership-transfer.md)
  now requires exact recipient acceptance and atomically changes responsibility
  and roles, with cancellation, decline, expiry and retry/race checks. Hosted
  rollout remains; this task is not complete.
- [ ] **M2-03** Adapt existing documents, videos and training to the resource
  catalog. Preserve existing links and historical attribution; handle legacy
  identity conflicts explicitly during migration.
- [ ] **M2-04** Enforce space/resource permissions across discovery, content,
  media tickets, caches, search, exports, background jobs and restores. Define
  scoped activity records and measurable revocation behavior.
- [ ] **M2-05** Verify two independent spaces cannot discover, read or manage one
  another's private resources, including through direct requests and restored
  data. Pilot a second audience with a real owner.
- [ ] **M2-06** Choose storage for more users and spaces, including
  permission-checked search, retrieval and analytics, before M2-03 moves
  existing resources into the catalog and before M2-04 and M3-02. Migrate from
  today's SQLite, Drive and Sheets storage once, without losing training
  history, certificates or audit events.
  The owner approved PostgreSQL plus private object storage following the Oct 29
  [storage evaluation](docs/designs/storage.md). A local synthetic probe passed at the owner's
  chosen 100-space / 10,000-account / 100,000-resource workload, with one million
  activity events. These are evaluation inputs, not quotas or hosted capacity
  claims. The [versioned schema and migration rehearsal](docs/runbooks/postgres-rehearsal.md)
  now verify scoped privileges, transactional lifecycle changes, quarantined
  imports and synthetic history/file preservation. The [isolated service integration](docs/runbooks/service-integration.md)
  now connects native account/profile/session operations and scoped resource
  metadata/search/revocation, including signed handoff and audit-before-disclosure.
  Legacy content/object adapters, provider/region/budget, real account-store cutover
  and complete hosted/object recovery verification remain; this task is not complete.

**Done when:** owners can invite/revoke audiences, resources are consistently
scoped, two-space isolation is demonstrated beyond the interface, and existing
training history, certificates and audit events survive the storage move.

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
