# TODOS

This file tracks maintenance and deferred fixes. The [master-plan checklist](ROADMAP.md)
tracks launch preparation, accounts and profiles, private spaces, agent access, the
Rust folder client, screen sharing and computing resources.

## Infrastructure

### Migrate the server from Python to Rust

**What:** Start an incremental migration of the Python resource server to Rust,
with a measured baseline and one bounded server path as the first deliverable.
Track implementation in the [Rust server migration](ROADMAP.md#rust-server-migration)
workstream.

**Why:** The office Mac's overload remains a performance problem after the
v0.19.1 security rollout. Reducing server CPU, memory and request cost is now a
priority before further feature expansion.

**Context:** Requested Oct 28, 2026; promoted by the owner on Nov 1, 2026 from P3
to P1 and the next engineering priority. This is the server in `postern/`, separate
from the roadmap's M4 Rust folder client. Existing audience/grant work remains
tracked; new feature expansion follows the first migration slice.

**First steps:** Attribute CPU, memory, disk I/O and request latency to the API,
background workers, database queries and development/build processes. Separate
shared-host contention from server cost; a language change alone does not remove
expensive queries or competing development jobs. Use the existing isolated
[capacity rehearsal](docs/runbooks/capacity.md), with comparable workloads and
numeric acceptance targets recorded before implementing the first Rust slice.

**First-slice status (Nov 2):** the optional Rust training-history reducer shipped
in [v0.20.0](https://github.com/nerdymatters/postern/releases/tag/v0.20.0).
The merged commit passed all six CI lanes and was installed on the office Mac
with verified backups, dependencies and API health. The tested native wheel is
installed and imports successfully; Python remains active. See the
[measurements and rollback runbook](docs/runbooks/rust-server.md).
The November 2 and November 5 quiet-host benchmarks passed all four gates with
matching results. Earlier live attempts stopped on the original CPU/health
guards. The November 5 document-reading pilot subsequently passed the baseline,
Rust and restored-Python staff checks, preserving all 34 events including five
document-page reads. RS-04's smoke/rollback check passed; the intermittent health
delay and broader live acceptance remain open. Python continues serving.
Broader RS-01 capacity attribution also remains open.
Address the measured health/audit history scan and competing development jobs
before widening the migration. The server migration as a whole is not complete.
The November 2 read-only follow-up measured 164 ms median for the health query's
full event scan. The November 4 index candidate halves the synthetic query's wall/CPU cost and
removes full-history scans from idle exporter selection. Exact result parity,
existing-database startup and old-code rollback pass. Single-event write wall
cost rises about 2%; sealing 500 events rises about 10%. See the runbook for
measurements and limits. Exact candidate/merged CI and the indexed Python rollout
completed in v0.21.1. Prepare the [isolated pilot and health diagnostics](docs/runbooks/pilot-rehearsal.md)
before broader Rust rollout. A passing short pilot does not explain the earlier
timeout or establish representative latency under load.

**Compatibility:** Preserve API responses, authorization/revocation, durable
audit admission, single-writer/job ownership, training progress and certificate
attribution. Use existing tests as the behavior baseline and HTTP parity tests
for each migrated path. Require measured improvement and a rehearsed rollback
before production cutover.

**Effort:** XL, delivered in bounded slices
**Priority:** P1 — next engineering workstream
**Depends on:** v0.19.1 compatibility baseline (available); workload attribution
and acceptance targets precede the first implementation. Urgent production and
security fixes still take precedence.

### Verify concentrated native catalog workloads before production enablement

**What:** Improve and benchmark resource listing when a member can see few or no
resources in a large space, preserving authorization, audit and revocation checks.

**Why:** The agreed synthetic pilot (100 spaces, 10,000 accounts, 100,000 resources)
passed a no-visible-resource probe at about 0.8–0.9 seconds. Concentrating 10,000
resources in one space exceeded the service's five-second deadline. An extra grant
index alone did not resolve it. These are local probes, not customer quotas or a
hosted capacity guarantee.

**Effort:** M
**Priority:** P1
**Depends on:** Complete before enabling native Posterns in production; v0.19.0
keeps that service disabled.

## Completed

- **2026-10-29 — Check printed certificates by number and saved-name history.**
  Owner chose admin-only number lookup and name history, keeping name corrections
  available. Staff progress searches current certificate numbers and printed names;
  **All locations** searches the whole current roster. **Details → Show name history**
  loads audited, paginated saved names with timestamps and incomplete-history notices.
  Unsaved suggestions are not historical evidence. See the
  [web guide](dashboard/web/README.md#badges-and-certificate). Included in v0.19.0.

- **2026-10-29 — Close the certificate documentation gaps.**
  [Google setup](docs/runbooks/google-setup.md#roster-reader) explains how the
  Sheet name competes with the Google name and when a saved name takes precedence.
  The [web guide](dashboard/web/README.md#badges-and-certificate) covers the
  home-page certificate card, current name normalization and configured prefix.
  The [logo kit](docs/brand/logo/README.md#medals-and-certificate-seal) documents
  medal states, chapter faces, the certificate seal and accessibility. Included in v0.19.0.

- **2026-10-29 — Preserve joined names and shorten suggestions safely.**
  Certificate names keep U+200C/U+200D between letters or marks, including through
  saves and restores. Long suggestions stop at a whole word or complete grapheme.
  The name form counts cleaned Unicode code points, preserves text while editing,
  and sends the normalized name; the server still enforces the 80-character limit.
  Python and browser checks agree across a 7,622-case Unicode corpus, including
  the Ahom mark whose category changed in Unicode 16. Grapheme fitting adds the
  `regex` runtime dependency, pinned in the pilot lockfile. Included in v0.19.0.

- **2026-10-29 — Certificate check feedback and event ordering.**
  `/badges` and `/certificate` now show bilingual checking, checked and retryable
  error states without duplicate requests or losing keyboard focus. Background
  refreshes keep their debounce and stale-response guards. New names beat
  future-dated choices; event-id tie-breaks preserve names and numbers when
  restores import tied events in a different order. The honors module's restore
  claim now states the export requirement. Included in v0.19.0. See the [recovery notes](docs/runbooks/recovery.md).

- **2026-10-29 — Make selfie reservations idempotent across writer deadlines.**
  Durable reservation identities pair each attempt with at most one refund.
  The worker and the next capture reconcile interrupted work, including after
  restart, while preserving committed photos, live captures and the ten/30/90
  rolling limits. Included in v0.19.0. See the
  [reservation lifecycle](docs/runbooks/capacity.md#durable-selfie-reservations).

- **Completed: v0.18.0 (2026-10-29) — Complete Thai copy on whole-screen refusals.**
  Access, roster verification, audit and portrait refusal messages, titles,
  hints and recovery buttons now use the shared bilingual presentation.
  All four UI states and the server refusal routes are checked.

- **Completed: v0.18.0 (2026-10-29) — Show certificates in Staff progress with no published content.**
  The people table and View/Print controls remain available with an empty
  catalog; a zero-item regression verifies both controls.
- **Completed: v0.18.0 (2026-10-29) — Stop an older reading-room load from undoing a saved name.**
  Full loads and progress refreshes share a response generation; a forced late
  response cannot overwrite the displayed or printed certificate name.

See the [verification report](docs/reports/launch-readiness.md).
