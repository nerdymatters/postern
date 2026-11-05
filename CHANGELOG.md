# Changelog

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
