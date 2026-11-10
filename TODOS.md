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
v0.19.1 security rollout. Reducing server CPU, memory and request cost became a
priority on Nov 1. On Nov 10 the owner put Linux support and M2 content ahead of
the slices that remain after RS-07.

**Context:** Requested Oct 28, 2026; promoted by the owner on Nov 1, 2026 from P3
to P1 and the next engineering priority. On Nov 10, 2026 the owner re-ordered the
work: finish RS-07, then [Linux support](ROADMAP.md#linux-support), then M2
content. RS-08 and RS-09 resume after the first outside Postern, or earlier where
measurement shows a cost. This is the server in `postern/`, separate from the
roadmap's M4 Rust folder client. Existing audience/grant work remains tracked.

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

**Next measured task:** RS-08 waits behind Linux support and M2 content. When it
resumes, rank the next slice by measured cost. The
health-query cost noted earlier was fixed in v0.22.1 (RS-05). The office Mac also
runs other services, scheduled jobs, browsers and coding agents, so start
by attributing whole-host CPU and memory to process groups over about two days
(from November 9), then measure per-route server cost only if the API's share
warrants another port. Sustained acceptance stays open before changing the
serving default.

**Current round (Nov 9):** the RS-07 live pilot started on November 9; v0.25.0
(2:22 PM EST) and v0.25.1 (4:40 PM EST) were installed while Rust served. The owner removed
`POSTERN_PROGRESS_BACKEND` from the API's launchd job, `progress-backend.json`
selects Rust, and Rust has served production since 11:10 AM EST with the watchdog
guarding. A read-only parity check matched right after the switch, and the owner's
phone document check passed. The compact progress table built under v0.23.0 serves
both backends. RS-07 steps:
1. ~~Install v0.23.0 with Python active.~~ Done Nov 7.
2. ~~Measure the Rust checkpoint on a read-only copy of production data.~~ Done
   Nov 7 (see Completed).
3. ~~Run the guarded live pilot, which needs local administrator authentication
   and an actual staff document check.~~ Started Nov 9; the document check passed.
4. Observe sustained Rust operation before changing the default. Owner decision
   D1 (Nov 7): switch through the owner's `progress-backend.json` with an ordinary
   restart, so no administrator password is needed after one launchd change, and
   let the watchdog return to Python automatically on a health, memory, CPU or
   parity breach. Shipped in v0.24.0 and hardened in v0.24.1 and v0.24.2 (see the
   [cutover runbook](docs/runbooks/rust-server.md#cutover-and-rollback)); the one
   launchd change (step 3) needs the owner.

The [remaining migration checklist](ROADMAP.md#remaining-migration-checklist)
lists RS-07–RS-09.

**Review follow-ups (not release blockers):**
- Startup priming has no deadline. A hung prime keeps the API from listening; the
  watchdog then selects Python and alerts, but restarts only a process it can verify
  as listening, so the owner must restart it by hand. A bounded prime would need its
  own signal to the watchdog, since health does not touch the progress worker.
- One progress worker serves both reader snapshots and admin full replays. A
  rebuild after invalidation delays reader requests behind it.
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
- **v0.24.2's review left two alert-wording notes (PR #21):**
  - the fallback alert picks its launchd wording from the record the check read
    first, but the not-restarted note from a second read, so a launchd restart
    between the two reads can put both in one alert;
  - a dropped launchd-override record always gets the launchd wording, even when
    the owner already removed the setting and a failed start left the old record.

**Compatibility:** Preserve API responses, authorization/revocation, durable
audit admission, single-writer/job ownership, training progress and certificate
attribution. Use existing tests as the behavior baseline and HTTP parity tests
for each migrated path. Require measured improvement and a rehearsed rollback
before production cutover.

**Effort:** XL, delivered in bounded slices
**Priority:** P1 for RS-07, the live pilot. RS-08 and RS-09 follow Linux support
and M2 content.
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

- **2026-11-09 — Show why progress reads use the full event history.** A row the
  compact progress table cannot hold (such as a payload over 64 KiB) or an
  unfinished rebuild kept every history read on the original query with no signal,
  and a failing rebuild batch showed only in the API log. `postern.serve backend`
  now prints a `progress reads` line from a read-only look at the database, and an
  admin's private health adds the counts with the writer's rebuild error and
  consecutive failures. Included in v0.25.2.

- **2026-11-09 — Keep the API up through progress faults it can recover from.**
  A native fault or Rust panic while priming the staff report's checkpoint no
  longer stops startup (launchd would restart the API into the same fault); the
  report's next request builds it, and native errors still count for the watchdog.
  A failed idle rebuild batch of the compact progress table now retries after 30
  seconds, doubling to at most 30 minutes, instead of waiting for a restart. Two of
  the RS-07 round's review follow-ups. Included in v0.25.1.

- **2026-11-09 — Let the serving process check its own Rust results.** Required
  before Rust becomes the default: the watchdog's parity check runs in its own
  process, so it could not see drift in the serving process's checkpoint, or native
  errors that leave health, CPU and memory normal. When the staff report extends its
  checkpoint, the API now compares the result with a full Python replay of the same
  snapshot (at once, then at most every 15 minutes), serves Python's result if they
  differ, counts native errors on progress paths, and records both for the watchdog,
  which treats a difference or three native errors as a breach. `postern.serve
  backend` shows the serving process's own checks. Included in v0.25.0.

- **2026-11-08 — Close the guard notes from v0.24.1's review (PR #20).** After
  `postern.serve backend python`, the status says `FAIL` until the API stops serving
  Rust, and `backend rust` reports a launchd override at once. The guard's counts
  start again when the source changes, alerts under a launchd override no longer
  promise a return to Python, and a serving record from a process that has exited
  is no longer treated as an override. A fallback that cannot confirm the serving
  process says it did not restart it and alerts once if that persists. An alert
  says when the media conversions could not be listed, and a test lists a
  converter beside a command line that is not UTF-8. Included in v0.24.2.

- **2026-11-08 — Harden the watchdog's Rust guard before the RS-07 pilot.** The
  notes from v0.24.0's review rounds are fixed. `postern.serve backend` judges the
  guard by the current selection; the heartbeat records whether the guard could
  verify the serving process, and three unverifiable checks alert. A memory, CPU
  or parity breach is retried on every check until Python is written, and a
  skipped restart is retried while the same process still serves Rust. A refused
  selection value is replaced with Python, a failed start replaces the serving
  record, guard errors keep the verified process, a failing notifier never skips
  the fallback, the SIGKILL wait covers a media conversion (120 s) and SIGKILL
  ends the API's own converters, huge limits keep their defaults, and the parity
  check streams history and is tested with the real native module on the compact
  copy. A heartbeat from a watchdog older than v0.24.1 is refused, and Rust forced
  by the launchd job is reported and alerted even without a Rust selection file.

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
