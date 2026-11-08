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
completed in v0.21.1. The [isolated pilot and health diagnostics](docs/runbooks/pilot-rehearsal.md)
shipped in v0.22.0. Its traced live document/rollback pilot passed all staff checks
and preserved 77 events including 23 document-page events. The fresh benchmark
cut roster wall/CPU about 49% with matching results. Python is restored and
timing capture remains enabled.

**Next measured task:** reduce concurrent health-query cost. The wider capture
showed seven simultaneous health requests each minute spending 2.25–2.71 seconds
in the database-query stage on both backends, despite all 18 sampled Rust guard
checks passing. A read-only backup replay measured the audit-history aggregate
at 122 ms median and the latest-export lookup at about 0.05 ms. The aggregate
still scans the full covering event index. A seven-reader replay stopped at the
70% CPU guard; retain it as stopped evidence, not a benchmark pass. Attribute the
request source and test a query change on isolated data with exact health-result
parity, write-cost and rollback checks. Do not mask the cost with stale health or
a wider deadline. Sustained/representative acceptance and remaining compatibility
checks stay open before changing the serving default.

**Current round (Nov 7):** v0.23.0 is installed on the office Mac with Python
serving (7:31 PM EST). The live compact progress table finished building in
9.2 minutes; all 71,405 progress rows are eligible, so both backends read it, and a
read-only check matched the audit-log replay. The matching 0.2.0 native wheel is
installed but not loaded. Next is RS-07:
1. ~~Install v0.23.0 with Python active.~~ Done Nov 7.
2. ~~Measure the Rust checkpoint on a read-only copy of production data.~~ Done
   Nov 7 (see Completed).
3. Run the guarded live pilot, which needs local administrator authentication
   and an actual staff document check.
4. Observe sustained Rust operation before changing the default. Owner decision
   D1 (Nov 7): switch through the owner's `progress-backend.json` with an ordinary
   restart, so no administrator password is needed after one launchd change, and
   let the watchdog return to Python automatically on a health, memory, CPU or
   parity breach (v0.24.0; see the
   [cutover runbook](docs/runbooks/rust-server.md#cutover-and-rollback)).

The [remaining migration checklist](ROADMAP.md#remaining-migration-checklist)
lists RS-07–RS-09.

**Review follow-ups (not release blockers):**
- Startup priming has no deadline. A native fault while priming fails startup,
  instead of failing one request. Consider a bounded prime that falls back to
  request-time replay.
- One progress worker serves both reader snapshots and admin full replays. A
  rebuild after invalidation delays reader requests behind it.
- One nonportable row, such as a payload over 64 KiB, keeps every history read on
  the original query, with no signal. Surface it in diagnostics.
- A transient error in an idle rebuild batch pauses rebuilding until restart.
  Retry with backoff.
- The cache stamp relies on file identity and generation. The runbook forbids
  restoring an older database into the same file; a per-database token would
  make reuse fail closed anyway.
- The first live build of the compact table bumped `progress_generation` once
  per backfilled row (70,121 on Nov 7), because live writes had already mirrored
  newer rows. It is harmless, since reads switch only after completion, but it
  added write work: the live build took 9.2 minutes against 3.9 on an idle copy.
- The startup reset after removed projection objects is not atomic with
  recreating them (see PR #18's notes).
- The sustained fault-load verdicts are bare `assert`s, and two success flags
  are hard-coded, so the probe would report success under `python -O`. Use an
  always-raising check and computed flags.
- **Before the RS-07 pilot (P1): harden the watchdog guard further.** v0.24.0's
  second review round and its fix-check left these below-floor notes (PR #19):
  - `postern.serve backend` reports a missing guard heartbeat as `ok` while Rust
    is selected, and after a switch to Python it reports the last Rust heartbeat
    as `FAIL`; judge the guard line by the current selection;
  - the heartbeat says `guarding` even when `lsof` or `ps` cannot verify the
    serving process, so the memory, CPU and parity checks are not running; record
    what actually ran and report anything less as `FAIL`;
  - a parity breach whose Python write fails is retried at the next parity run
    (15 minutes), not on every check; keep the breach until the write succeeds;
  - a limit of 310 or more digits makes `limits()` raise instead of keeping the
    default;
  - no test runs the real native module through parity on the compact copy;
  - after a restart is skipped (an `lsof` or `ps` error while re-verifying), the
    selection says Python while the same process keeps serving Rust, and the guard
    stops; retry the verified restart while the serving record still says Rust;
  - a selection the API refuses that is valid JSON (for example `"backend": "Rust"`)
    is never replaced with Python, so the API keeps restarting;
  - an `environment` serving record left by a process that failed to start is
    trusted as a launchd override; have `postern.serve run` record a failed start;
  - when a guard bug raises, the verified pid is dropped, so a failing API is not
    restarted, and a failing notifier can skip the fallback;
  - SIGKILL after 30 s can cut a 90-second media conversion short and orphan its
    converter, which runs in its own session; wait longer or end its process group;
  - the parity check loads the whole joined history into memory each run.
- **Before Rust becomes the default:** the watchdog's parity check replays history
  with both native interfaces in its own process, so it cannot see drift in the
  serving process's in-memory checkpoint, or native errors there that leave health,
  CPU and memory normal. Have the API compare its checkpoint result with a full
  replay in the same snapshot from time to time, count native errors on progress
  paths, and expose a flag the watchdog treats as a breach.

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

### Version the progress projection schema before changing it

**What:** Record a projection schema version in the database. On a mismatch,
recreate `progress_events`, `progress_generation` and their triggers, then rebuild.

**Why:** Startup checks only that these objects exist, and the schema uses `CREATE
... IF NOT EXISTS`. A later release that changes a trigger body or a column would
silently keep the v0.23.0 definition on upgraded databases.

**Context:** Owner decision D2 (Nov 7): v0.23.0 is the first release with these
objects, so it is unaffected. Treat a missing version marker as the v0.23.0
definitions. Add an upgrade test that starts from the stored older definitions.

**Effort:** S
**Priority:** P2
**Depends on:** The next change to the projection schema

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

- **2026-11-07 — Measure the Rust progress checkpoint on production-shaped data.**
  Owner decision D3 made this an RS-07 precondition. On a fresh read-only copy of the
  office database (248,004 events, 24 published items), the v0.23.0 Rust checkpoint
  retained 1.77 MB, 10.5% of its 16 MiB budget. The first report took 0.17 s, against
  0.79 s for the Python replay of the audit log; later reports took 12–13 ms, with and
  without new activity. Every result matched the Python replay. The size was measured
  on the native result, since the cache's own `retained_bytes` reads 0 over budget.

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
