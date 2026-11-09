# Changelog

## 0.25.0 — Let the serving API check its own Rust progress (2026-11-09)

### Added

- Catch Rust progress drift where it happens. The watchdog's parity check runs in its own process, so it could not see the serving API's staff-report checkpoint. When the staff report extends that checkpoint, the API now compares the result with a full Python replay of the same snapshot, first at once and then at most every 15 minutes. If they differ, it serves Python's result, drops the checkpoint and records the difference.
- Count native errors on reader progress and the Staff progress report. Database errors, which Python would meet too, do not count.
- Return Rust to Python when the serving API reports trouble about itself. The API records its comparisons and native errors in `progress-backend-check.json`; the watchdog treats any difference, or three native errors, from the verified serving process as a breach.
- See the serving process's own checks in `postern.serve backend`: its comparisons, native errors, and comparisons that could not run.

### Upgrade

- Restart the watchdog job after installing this release so it reads the new record. A release before 0.25.0 writes no record, so `postern.serve backend` prints `FAIL` for the in-process check until the API restarts on 0.25.0.

## 0.24.2 — Trust the Rust guard's status and alerts during the pilot (2026-11-08)

### Fixed

- Know when Python has really taken over. After `postern.serve backend python`, the status now says `FAIL` until the API stops serving Rust. `backend rust` also reports a launchd override straight away, because the file will not apply until that setting is removed.
- Start the guard's health, memory and CPU counts again when the serving source changes, so checks from a launchd override period never count against the process that serves from the selection file.
- Stop old records from misleading the guard. A serving record left by a process that has exited is no longer treated as a launchd override or as Rust still serving, so failed health checks return Rust to Python again.
- Get alerts that say what actually happened. Under a launchd override, alerts no longer promise a return to Python. When the watchdog cannot confirm the serving process, it says it did not restart it, and it alerts once more if three later checks still cannot confirm it. When the media conversions of a stuck API cannot be listed, the alert says so.

### Upgrade

- Restart the watchdog job after installing this release so the new checks run. The heartbeat format is unchanged.

## 0.24.1 — Make the automatic return to Python reliable before the Rust pilot (2026-11-08)

### Fixed

- See whether the Rust guard is really protecting you. `postern.serve backend` now judges the watchdog by the current selection. It says no guard is needed while Python is selected, reports a launchd job that forces a backend, and prints `FAIL` when Rust is selected but the watchdog has not checked it, is older than this release, or cannot confirm which process serves Rust.
- Return to Python even when the first attempt goes wrong. The watchdog keeps a memory, CPU or parity breach until it has written Python, retries a restart it could not finish while the same process still serves Rust (up to three times), and alerts after three checks in which it cannot confirm the serving process.
- Recover from a selection the API refuses, such as `"backend": "Rust"`, the same way as from an unreadable one. An API start that fails while reading the selection now records the failure, so an old launchd-override record is never trusted.
- Stop a stuck API without cutting a media conversion short. The watchdog waits 120 seconds before SIGKILL and then ends the conversions that API was running. A guard error keeps the confirmed process for the restart, and an alert that cannot be sent never stops the fallback.
- Keep the default for an absurdly large `limits` value, and run the parity check without loading the whole history into memory.

### Upgrade

- Restart the watchdog job after installing this release: `postern.serve backend` reports a heartbeat from an older watchdog as `FAIL`.

## 0.24.0 — Switch the progress backend without administrator rights (2026-11-08)

### Added

- Switch staff progress between Python and the optional Rust backend without administrator rights. `python -m postern.serve backend rust` (or `python`) saves an owner-only selection beside the configuration file, and the next ordinary API restart uses it. `postern.serve backend` shows the selection, the backend the serving process started with and why, and whether the watchdog is guarding it. Python stays the default.
- Return to Python automatically when Rust misbehaves. While Rust is selected, the watchdog checks health, API memory and CPU. Every 15 minutes it also replays the live history with both backends in a read-only snapshot. Three failed health checks, memory or CPU above its limits for several checks, or any parity difference make it select Python, restart only the verified API process and send one alert that says what happened. It never selects Rust. See [cutover and rollback](docs/runbooks/rust-server.md#cutover-and-rollback).

### Upgrade

- A backend set in the launchd job's environment still overrides the selection file. Remove `POSTERN_PROGRESS_BACKEND` from the API job once, with administrator rights, and restart the watchdog so the new guard runs; see [step 3](docs/runbooks/rust-server.md#cutover-and-rollback).

### Rollback

- Releases before 0.24.0 ignore the selection file and serve Python unless the launchd job sets a backend. Their watchdog does not guard Rust.

## 0.23.0 — Faster staff progress with optional incremental Rust evidence (2026-11-07)

### Changed

- Load reading progress and the Staff progress report from a compact copy of the progress events. Both backends use it once it is built. The full audit log stays authoritative and is still exported unchanged. On an existing database, the writer builds the copy in idle time after the first start, and reads use the full log until it is ready. If the copy's tables or triggers go missing, startup discards and rebuilds it. It never trusts an incomplete copy.
- Keep a bounded, per-person evidence checkpoint for the Staff progress report when the optional Rust backend is selected, so each report reads only new activity. Catalog, roster or history changes make it rebuild from the full record. In the representative 50-reader test, the Rust report's p95 was 42 ms, against 3.14 s for Python, with 33% less CPU. Python remains the default and the rollback path; see the [capacity protocol](docs/runbooks/capacity.md#incremental-dashboard-candidate-protocol) for the bars and the retained failures.
- Require version 0.2.0 of the optional native wheel. It adds the checkpoint interface, and selecting Rust with an older wheel now fails at startup instead of serving.

### Added

- Validate capacity and sustained faults on disposable hosted Macs. Capacity reports now label memory by startup, workload and verification. The fault rehearsal holds report disclosures while session, role and consent change, and covers missing media and a failed and retried export, all under reader traffic. After traffic stops, it checks raw-replay parity and a same-database Python rollback.

### Rollback

- Rolling serving code back to v0.22.1 keeps the current database, including the compact tables and triggers, which go on mirroring writes. That release's offline checkpoint exporter rejects upgraded databases, so keep the v0.23.0 exporter; see [checkpoint compatibility](docs/runbooks/postgres-rehearsal.md).

## 0.22.1 — Pending-backup health performance (2026-11-05)

### Fixed

- Check backup health without scanning already confirmed activity. Health retains the exact pending count and oldest-event time, while audit export still seals at most 500 selected events and retries the same bytes. Startup builds derived metadata once; the [rollout runbook](docs/runbooks/rust-server.md#pending-health-query-candidate-november-5) records measurements, startup checks and rollback limits.
- Keep existing offline migration checkpoints readable after the health optimization. New checkpoints retain the original format and omit rebuildable health metadata. After rolling serving code back to v0.22.0, use the newer offline exporter for upgraded snapshots and retain the live metadata; see [checkpoint compatibility](docs/runbooks/postgres-rehearsal.md).

## 0.22.0 — Health diagnostics and isolated Rust rehearsals (2026-11-05)

### Added

- Diagnose slow health checks with optional private timing records for request handling, worker queues, database reads and event-loop delays. Tracing is off by default, bounds active traces and file storage, and preserves the existing health response and deadline.
- Rehearse document reading, quiz unlocking and Python-to-Rust rollback on disposable local data through real HTTP requests. Controlled delay probes check the timing diagnostics without using production data or changing production routing. See the [rehearsal runbook](docs/runbooks/pilot-rehearsal.md) for commands and acceptance limits.

## 0.21.1 — Health and backup query performance (2026-11-04)

- Check service health and find pending audit backups with less work as retained history grows. Additive SQLite indexes reduce measured query cost while preserving pending-backup status, export order and existing activity. Startup creates the indexes automatically; code rollback can keep them and the current database. See the [measurements and rollout limits](docs/runbooks/rust-server.md#health-and-export-index-candidate-november-4).

## 0.21.0 — Staff progress filter, sorting and reports (2026-11-04)

- See who still owes required training with **Incomplete required progress** under Progress status. It lists everyone short of a confirmed 100%, including missing history and unavailable coverage, so a gap in the records never reads as finished.
- Sort Staff progress by any column. Select a heading to sort by it, and again to reverse; **Sort by** offers the same orders on a phone, where the headings are hidden.
- Save what is on screen with **Export report**: the selected tab with its search, filter and sort, as a CSV file for a spreadsheet or as a printable report through the print window. Exports keep the table's wording, including Unknown and “At least”, and show times in Eastern. See [what each file contains](dashboard/web/README.md#finding-posts-and-staff-progress).
- Read on the privacy page that administrators can save these reports to their own devices. The monitoring notice is unchanged, so nobody has to accept it again.
- This release changes only the web app; the API, data and configuration stay as they are.

## 0.20.0 — Optional Rust progress replay (2026-11-01)

- Enable faster training-history replay with an optional Rust component. The paired synthetic workload used about half the CPU while preserving page coverage, quiz completion and certificate behavior. Python remains the default and rollback option; production activation requires the [controlled pilot checks](docs/runbooks/rust-server.md#cutover-and-rollback).
- Build the optional component from locked sources and check compatibility on macOS and Linux. The release includes its source and build metadata; Python continues to own database access, authorization, audit admission and durable writes.

## 0.19.2 — Required training progress (2026-11-01)

- See Required progress before Overall progress in Staff progress. It measures published Chapter 1–9 content, chapter quiz passes and the Final Exam, excluding optional modules. Each location's summary and each person's row show the new measure, with missing history and unavailable coverage clearly marked.

## 0.19.1 — Security and test reliability (2026-11-01)

- Run Vitest with an explicit Node 24 runtime and one worker, reduce quiz-test DOM lookup work, await mocked React updates, and give independent reading scenarios fresh fixtures. Preserve scoring and unlock assertions, and give the complete chapter journey a measured ten-second completion budget; see the [reliability record](docs/reports/launch-readiness.md#quiz-test-follow-up--november-1-2026).
- Update the pilot's pyasn1 dependency to 0.6.5, which fixes four ASN.1 decoder denial-of-service advisories. See the [verification record](docs/reports/launch-readiness.md#security-follow-up--november-1-2026) for the scan scope and deployment checks.

## 0.19.0 — Certificate checks and private-space foundations (2026-10-30)

- Check printed certificates by number or saved-name history from Staff progress. Admins can search the whole current roster; readers retain name corrections, and incomplete restored history is clearly marked.
- Preserve joined names and shorten suggested names at complete words or graphemes. Certificate checks show retryable feedback, and newly saved names remain current after clock corrections without extending the daily correction limit.
- Recover interrupted selfie reservations across writer deadlines and restarts without duplicate refunds or losing retained photos.
- Rehearse private Posterns with PostgreSQL storage, quarantined imports, account-bound resource access, operator-approved space creation, invitations, membership management and ownership transfer requiring recipient acceptance. These features require separate service configuration and remain disabled in the existing production installation. See the [private-space guide](docs/private-spaces.md).

## 0.18.0 — Hosted Terms and launch readiness (2026-10-29)

- Read the approved hosted account [Terms of Service](https://postern.fyi/legal/terms/2026-10-29.html) without signing in or running JavaScript. The account interface supports explicit acceptance of each revision, recovery when Terms are unavailable, and account deletion without accepting. Registration and private-service activation remain separate launch gates.
- Read Thai explanations and recovery controls when access, staff-list verification, audit recording or enrollment photos are unavailable.
- View and print staff certificates even with no published training content. A delayed page refresh can no longer replace a newly saved certificate name.
- Restrict image decoding to supported formats and update security-sensitive dependencies. Quiz test selection is faster while preserving the same scoring assertions and timeouts.

## 0.17.1 — Long-running API reliability (2026-10-27)

- Keep the reading room accepting staff after days of uptime. The API now closes each read-only database connection when a read finishes, including refused reads, so open files no longer pile up until macOS's default limit of 256 refuses new connections. Upgrading needs only the code update and an API restart; data, configuration and dependencies are unchanged.

## 0.17.0 — Accounts and profiles (2026-10-27)

- With an independently configured account service, sign in with Google, reach private Posterns from My Posterns, and edit a profile with an optional public address. Profiles start unpublished; accounts do not grant access to private resources. See [account setup](docs/accounts.md).
- Keep account access available when a private Postern refuses access or is temporarily unavailable. Private entry verifies the same Google identity and retains existing invitation, agreement and enrollment requirements.
- Preserve old training links and browser Back navigation inside private Posterns. Sign out clears account and private state across open tabs and attempts to revoke their sessions.
- Update the pilot certificate bundle used by authenticated connections. Account registration and production rollout still require the documented deployment and compatibility checks; standalone installations remain the default.

## 0.16.0 — Public-release preparation

- Keep operator identity, privacy contact, intellectual-property notice, roster fallback and certificate identity in a private branding file.
- Supply generic installation examples, configurable service labels and independently configured monitoring URLs.
- Publish first-party code under MIT OR Apache-2.0, with separate brand rules, third-party notices, contribution sign-off guidance and private security reporting.
- Remove customer deployment records, historical design artifacts and developer-machine integrations from the public source tree.

This public source starts from the private pilot's v0.15.0 behavior: private training migration, bilingual assessments and certificates, optional selfie enrollment, recovery and bounded selfie retries. Private pilot history, releases and customer content are not included in the public repository.
