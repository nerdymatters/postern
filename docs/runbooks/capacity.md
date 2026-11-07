# Capacity rehearsal

Choose load tiers appropriate to your own workload. An account count is not a simultaneous-stream count; loopback and synthetic tests do not certify production capacity.

## Isolated local rehearsal

From the repository root on the Mac development checkout:

```sh
.venv/bin/python -m pip install -e '.[test]' psutil
nice -n 10 .venv/bin/python -m scripts.capacity --scenario signin --users 50
nice -n 10 .venv/bin/python -m scripts.capacity --scenario enrollment --users 50 --seconds 120 --cloud-delay 0.1
nice -n 10 .venv/bin/python -m scripts.capacity --scenario enrollment --users 50 --seconds 120 --cloud-delay 1 --with-reader
nice -n 10 .venv/bin/python -m scripts.capacity --users 25 --seconds 60 --history-hours 9
nice -n 10 .venv/bin/python -m scripts.capacity --users 50 --seconds 120 --history-hours 9
```

Run these sequentially so they do not compete with each other. The script binds
an ephemeral loopback port, creates new synthetic data under `.context/capacity/`,
and uses FakeStore. It accepts no production URL, credentials, or data directory.
It closes the test service afterward. Reports and synthetic databases remain for
inspection. Large history fixtures use substantial disk and memory; inspect the chosen user and duration settings before running.

The sign-in scenario exercises the real demo HTTP endpoint with unique accounts
sharing an address. Google verification and its concurrency are covered separately
by regression tests with a controlled verifier. No rehearsal authenticates with
Google or records activity for real staff.

The enrollment scenario uses preauthenticated, consented synthetic sessions,
real photo sanitization through the macOS sandbox, and private FakeStore uploads.
`--cloud-delay` adds a known delay to each fake file upload. It does not simulate
Google quotas, key fetching, all Google round trips, or camera/device behavior.
`--with-reader` adds one already-enrolled synthetic reader making video range
requests and saving progress while the group enrolls. It requires a spare roster
account and checks persisted progress against acknowledged updates numerically.
The existing reader stays active for the full `--seconds` window even when all
captures finish earlier, so a fast enrollment cannot skip the progress heartbeat.

The mixed scenario exercises real authorization, audit commits, range responses,
15-second progress reporting, a document page for one in five viewers, catalog
refreshes, and one admin dashboard refresh every 30 seconds. It ramps arrivals
over ten seconds by default. `--ramp-seconds 0` tests simultaneous arrivals.
It uses opaque 8 MiB byte fixtures and a synthetic 12-hour video timeline. These
are deliberately not playable MP4 files: this measures the HTTP/data path, not
decoding, video quality, player buffering, or rendering on staff devices.

Reports include per-route latency/status counts, audit queue and commit times,
event-loop lag, process memory, sampled whole-host CPU reserve, byte integrity,
and comparison of acknowledged progress with persisted records by user. Reports
also identify the backend, source revision, native binary hash and Python/host;
workload CPU excludes fixture setup, startup, post-workload verification, shutdown
and converter subprocesses. `startup` separately records normal service-start
wall/CPU and sampled peak RSS, including Rust checkpoint priming;
`comparable_process_cpu_seconds` adds startup and workload CPU for both modes.
`first_dashboard_ms` records the first actual workload report request.
`progress_cache` contains bounded counts and retained bytes, without identities.
Writer operations
record thread CPU, queue and commit time grouped by the owning background task
or `request_or_other`. Operation CPU excludes SQLite commit; task labels do not
give exclusive per-job CPU because API and background reads share worker threads.
Client and server share a process/event loop, so CPU/memory results include both.
The workload client expires idle connections after four seconds, before the
server's unchanged five-second keepalive deadline. This reduces races when a
server closes an idle socket; no request is retried. Both backends pay the same
reconnection cost. A functional run with an untraced `ReadError` remains failed.
A separate scaled probe reproduced an idle-close race and supports this
mitigation; it does not establish the original reset's cause.
Synthetic readers release each video response after the scenario's existing response checks.
`process_rss_after_setup_mib` records memory before requests; sampled memory still
includes allocations retained from fixture setup.
Resource guards run during startup, workload and final verification. Startup
must still finish within ten seconds. A failed or cancelled startup closes its
lifespan and preserves `startup-failure.json` alongside other receipts.
After mixed traffic stops, a separate HTTP dashboard response is compared with
a fresh Python replay of authoritative events. Its status appears in
`integrity.raw_python_dashboard_matches`; it does not count toward workload
coverage, measured request latencies or comparable CPU. Its elapsed time is
reported under `verification`, and sampled memory still includes it.
`process_rss_by_phase_mib` labels the same samples as startup, workload or
verification. `process_rss_startup_workload_peak_mib` is the largest startup or
workload sample. The raw Python comparison runs in the measured process after
traffic stops, so its allocations appear only under verification and in the
all-phase `process_rss_mib`. Every resource guard still covers verification.

During consecutive busy host samples, `host_cpu_diagnostics` keeps the last ten
intervals with active request counts, snapshot cost and the five busiest visible
processes. Process CPU percentages use one core as 100%, unlike whole-host CPU;
short-lived or inaccessible processes may be absent. PID and creation time identify
each process; arguments and environment values are not collected. Use these
records to investigate CPU stops, without treating incomplete runs as accepted.

The rehearsal stops on memory pressure or sustained whole-host CPU reserve below
30%. When the existing local Postern health endpoint is detected, it checks that
service periodically and stops if process, permission, audit, or storage health
fails. The automatic guard does not evaluate backup freshness; inspect that
separately before and during longer runs. A shared-Mac rehearsal still
consumes CPU, disk, and memory: use a separate host or an agreed quiet window for
longer tests. These stop conditions are safeguards, not proof of no user impact.

Inspect `report.json`, not merely the process exit status. Functional errors cause
a nonzero exit. Required request-family minimums and progress from every active
reader are recorded in `workload_coverage` and enforced before success; latency/CPU
acceptance still requires review of the report. Any abort, unexpected
HTTP error, unfinished enrollment, integrity mismatch, or missing request family
means the scenario did not pass. Very short runs may never reach a 15-second
heartbeat or a 30-second dashboard refresh.

## Paired reducer measurements

The deterministic concurrent contracts in
`tests/postern/test_progress_concurrency.py` complement these load measurements.
They hold eight completed HTTP snapshots before disclosure, continue serving and
recording document pages, then change session, role, consent, recovery or audit
availability. They also check current quiz locks after a pass, certificate
identity, missing-media handling, and a failed export followed by retry and
restore into a separate synthetic database. The normal CI lanes run them with
both the Python and real Rust reducers. These controlled interleavings do not
claim sustained fault-load capacity, real cloud behavior or a staff-device check.

For stage diagnosis, dispatch **tests** with `progress_profile=true` and
`capacity=false`. One disposable Mac profiles three alternating Python/Rust pairs
over 492,480 synthetic all-video events. The `progress-profile` artifact contains
streaming replay, SQLite materialization and materialized-reducer CPU/wall times,
cProfile call tables, matching-progress digests and worker guard observations.
Each worker stops after 120 seconds, five sustained seconds above 70% host CPU,
1 GiB RSS or less than 1 GiB available memory. Host preparation disables indexing
and requires 60 quiet seconds within five minutes, only on the disposable runner.
Materialization and profiling change costs, so these stage times are not additive
and never replace HTTP acceptance. No synthetic database is uploaded; the CLI
refuses an ordinary local or self-hosted runner before creating output.
For request diagnosis, dispatch with `progress_request_profile=true`, leaving
`progress_profile` and `capacity` false. This runs one Python and one Rust mixed-50
attempt with the original capacity guards. Nested spans distinguish replay and dashboard-worker wall/thread
CPU time from the enclosing service request's wall time. The `request-*` reports
retain the underlying capacity report and all spans, including failed attempts.
These two instrumented attempts diagnose waiting; they are not a replacement for
the three uninstrumented acceptance pairs. No email, payload or credential is
included in the spans, and wrappers are removed when the diagnostic ends.
`roster_checkpoint` spans include both startup priming and incremental report
replay; `roster_history` remains the full-replay path. These are nested timings,
not additive request costs.
The diagnostic also tags the existing RSS samples as startup, workload,
verification or cleanup and records boundary snapshots. It retains at most
1,000 tagged samples and reports any dropped observations. This identifies
whether a peak occurs during serving or the extra raw Python comparison;
it changes neither the original RSS distribution nor its acceptance limit.
After a successful attempt has stopped its server and closed the writer, a
guarded fresh process profiles replay, row fetching and materialized reduction
on that same synthetic database. `quiet-replay.json` and its guard receipt
separate fixture costs from traffic costs; shutdown can also checkpoint the WAL,
so this is not an identical live storage state. The profiler reads the fixture's
published catalog. These additional timings do not enter the HTTP report or
replace any acceptance target. A quiet-profile failure fails the diagnostic
while preserving the preceding capacity report and request spans.
Each replay report names its `source`: original `events` or eligible
`progress_events`. Row materialization uses that same source. A direct-schema
seed with legacy zero timestamps can stay on the original query, while the
completed HTTP fixture can use compact inputs; do not compare those fixtures as
an isolated backend improvement. See [compact progress inputs](rust-server.md#compact-progress-inputs)
for rebuilding, legacy fallback and rollback behavior.

Manually dispatch the existing **tests** workflow with `capacity=true`. It runs
on disposable macOS 15 runners using the pinned pilot dependencies and a Rust
wheel built from that checkout. It never accepts a serving URL or credentials.
Five jobs cover sign-in (50 users), enrollment (10/25 users, a one-second fake
upload delay and one existing reader), and mixed reading (25/50 users, a roster
of 228 and nine hours of history: 492,480 seeded events). Mixed and enrollment
observations last 120 seconds. Other pacing stays at the defaults documented above.

Each job runs three pairs sequentially on its own runner, in Python/Rust,
Rust/Python, Python/Rust order. Different scenario jobs use different runners.
The capacity job disables Spotlight indexing only on its disposable GitHub-hosted
Mac, then requires 60 consecutive seconds at or below 70% whole-host CPU before
starting the batch. A five-minute readiness deadline fails the job without
starting measurements. Indexing output and the readiness samples are retained.
This prepared-host environment differs from earlier unconditioned measurements;
compare backends within the same batch. Ordinary CI and office settings are unchanged.
History setup writes at most 128 events per transaction, retaining the ordinary
1.5-second audit deadline. It creates the same event count, ranges and timeline
before measurement starts. `setup.json` records operation count, maximum queue
and commit time, and the phase/reader/chunk of a failed write. Setup failure still
fails the attempt and never starts the measured workload; no automatic retry is used.
Transport failures retain their exception class in `transport_errors`, alongside
the existing failed request counts, without exception messages or request URLs.

Every report, setup/startup-failure receipt, stdout/stderr and exit code is retained, including failures. The
`capacity-*` artifacts also contain dependency versions and the checked-out
revision. A green job means functional coverage passed; it does **not** mean
performance acceptance. The original pre-incremental campaign used the following
bars. Its failed attempts remain failed; the prospective candidate protocol below
does not reclassify them:

- No abort, unexpected status, unfinished enrollment, missing family/reader,
  byte mismatch or difference between acknowledged and persisted progress.
- During-load health is sampled every five seconds with a two-second timeout,
  checking all five fields, including backup freshness. Every health latency
  must be <=2s. Existing CPU and memory safeguards still apply.
- Every request-family p95 <=2s, except selfie capture <=30s and enrollment-ready
  <=120s. Across three pairs, median Rust p95 must not exceed Python by more than
  max(20%, 25ms) for any family. Median workload process CPU must not exceed
  max(110% of Python, Python+1 CPU-second); sampled peak RSS <=Python+32MiB.
- Mixed workloads target at least 20% lower median process CPU, catalog p95 and
  dashboard p95. Sign-in/enrollment are no-regression controls. Preserve a missed
  target as a failure for investigation, rather than changing it after the run.

### Incremental-dashboard candidate protocol

Registered November 6 before the integrated candidate's HTTP measurements under
the user's standing advance approval; the owner explicitly confirmed this
Rust-target, Python-control choice later on November 6. Keep the same five scenarios, three
alternating pairs, roster, history, pacing, correctness requirements and all
non-dashboard latency targets. The two-second dashboard p95 target applies to
Rust. Record Python's full dashboard distribution and known miss as the unchanged
correctness and rollback control; exclude only that control's dashboard from the
absolute two-second gate. Relative latency limits still apply to every family.

Use startup plus workload CPU for both the 20% mixed-workload improvement target
and the no-regression control, reporting both components. Include startup in the
resource guards and sampled peak RSS comparison, keep the ten-second readiness
deadline, and issue no benchmark-only warm request. Mixed correctness additionally
requires the post-workload raw Python dashboard comparison. All other original
gates remain. Retain every attempt and report first-request latency and cache
counts. Previous campaigns and their original analyzer are unchanged.

The first full campaign on this protocol failed only its mixed-50 memory bar:
two Rust peaks exceeded Python by 36.9 and 43.2 MiB, over the 32 MiB allowance.
That analyzer took each report's all-phase maximum, which included the
post-workload raw Python comparison. A later phase-labelled mixed-50 diagnostic
put Rust's startup/workload peak at 280.3 MiB and its verification peak at
347.7 MiB; Python's were 376.4 and 389.0 MiB. In Rust mode, that comparison is
the process's only full Python replay. Subsequent campaigns compare peak RSS over
startup and workload, the window the incremental protocol names, using
`process_rss_startup_workload_peak_mib` with the same 32 MiB allowance. Until then,
this runbook had described the sampled memory as including verification. A report
without that field fails. The all-phase and verification peaks are reported
alongside it, and every resource guard still covers verification. The failed
campaign remains failed, and no attempt is reclassified.

The campaign on the post-review candidate then missed one target. At 50 readers,
the median paired catalog p95 ratio was 0.813 against the required 0.80. Every
other bar passed. Across three campaigns on unchanged Rust catalog code, this
ratio was 0.63, 0.71 and 0.81. Rust's catalog p95 stayed at 42–50 ms. Python's
varied with how much its own multi-second dashboard replays contended for the
interpreter. On November 7 the owner chose to treat catalog as a no-regression
family; PR #18 records that decision. It must stay within
Python+max(20%, 25 ms) and under two seconds.
Mixed workloads still need at least 20% lower paired-median dashboard p95 and
comparable CPU. That campaign remains failed; the next one used the same
application code under the amended bar and passed. It also met the original
all-phase 32 MiB memory bar: the largest Rust excess was 30.6 MiB, at 50 readers.

This is an isolated synthetic load comparison, not full RS-06 acceptance. Role
changes/revocation, consent, quiz/certificate, media failures, export and recovery
still need controlled checks under concurrent load; ordinary compatibility CI
does not substitute for those probes. Real devices/WAN and sustained serving
operation remain separate gates before making Rust the default.

## v0.6.3 concurrency behavior

Version 0.6.3 includes these changes on the v0.6.2 base. The earlier release moved
Google verification off the event loop and limited certificate requests to five
seconds; v0.6.3 adds bounded verification admission and shared-IP/account budgets.
The public-page referrer policy and interrupted-recovery protection are preserved.

- Sign-in budgets are 300 attempts/minute per public IP and 10/minute per verified
  canonical account. Account limits survive network changes. Both use the existing
  bounded in-memory bucket collection; multiple service processes do not share it.
- At most four Google identity verifications run together, off the event loop.
  A request waiting more than two seconds for a verification slot returns
  `503 signin_busy`; this is an admission deadline, separate from the five-second
  certificate-request timeout. Verification precedes account limiting and session creation.
- Reader progress replay uses a read-only database snapshot. Authorization is
  checked before and after; posts archived during the read are omitted, and
  entering recovery mode during the read suppresses the result. Snapshot database
  or filesystem errors return `503 catalog_unavailable`. Durable audit failure
  still prevents disclosure. Admin progress already used a read snapshot.
- The selfie worker wakes after capture and drains ready work without sleeping
  three seconds per person. Processing stays serial, cloud confirmation remains
  mandatory, and failed jobs retain their retry deadlines and retention rules.
  Retention and orphan cleanup keep a three-second maintenance cadence instead
  of running once per person while ready jobs drain.
- At most eight selfie upload preflights enter authorization/auditing together.
  Waiting requests are authorized after admission. Slots are released before
  receiving upload bytes, so a slow body does not occupy an admission slot.

Deployment is separate from packaging this release; no v0.6.3 production
deployment is recorded by these rehearsals. No schema migration, additional server
process, Rust component, or transport migration
is required. Do not increase Uvicorn worker count without redesigning ownership of
background jobs, in-memory limits, and the single SQLite writer.

## v0.14.1 selfie preparation

- Selfie preparation is one at a time per account, not across accounts: different
  people's photos (with their face check) prepare in parallel up to the converter's
  configured slots (two by default). Conversions and private-storage id allocations wait for their slots in
  the event loop, never in the worker threads API reads share, so a crowd enrolling
  at once does not stall readers.
- A capture's own writes (authorization, the attempt reservation and a release) share
  the eight-slot selfie admission bound with the upload preflight, so a burst enters
  the durable writer a few at a time.
- Restoring a portrait no longer waits for anyone's face check. It still shares one
  lock with other uncached portrait restores and the selfie worker's private-storage
  upload and cleanup. Cached portraits bypass that lock.

## Durable selfie reservations

The October 29 implementation adds a local `selfie_reservations` ledger. The
writer creates its table and index when opening an existing database; no manual
data rewrite is required. Server rollout is separate from this code change.

- Each capture gets one server-generated identity. Its reservation and attempt
  event commit together. A refund and the reservation's terminal state also
  commit together, so retrying settlement cannot refund the attempt twice.
- The selfie worker reconciles unfinished reservations after acquiring the
  account's capture lock. It skips live captures. The next capture also
  reconciles that account's unfinished reservations before applying rate limits.
  A request that times out before its reservation commits therefore receives
  a refund once the writer recovers, including after a server restart.
- A retained photo and its settled reservation commit together. An ambiguous
  photo insert still retains the prepared image for the existing upload and
  cleanup worker; reconciliation does not refund a committed capture.
- Refunds use the saved authorized context and the attempt's original timestamp.
  Expired or revoked login sessions cannot prevent compensation. If a capture
  or its settlement was interrupted before a terminal outcome committed, the
  worker treats that interruption as a server fault. Until reconciliation can
  commit, the reservation conservatively remains charged.
- The existing event-based rolling limits remain: ten retained/invalid captures,
  30 attempts excluding server faults, and 90 total reservations per 24 hours.
  Face-check rejections refund only the ten-capture budget; server faults refund
  the ten- and 30-attempt budgets. The 90-reservation ceiling is never refunded.
  Legacy attempts without ledger rows retain their existing accounting; there
  is no retroactive inference or repair of old outcomes.
- Settled ledger rows are removed after 24 hours during maintenance; audit events
  remain intact. Cloud recovery still restores its exported events and portrait
  metadata, not unfinished local capture work. Returning to an older server
  version leaves the new table unused and restores its old refund limitations.

This relies on the existing single-process server and single durable writer.
Neither the 1.5-second writer deadline nor the eight-slot selfie admission bound
has changed.

## External acceptance still required

Longer paced/soak tests and [real-device checks](dmv-video-test.md) remain necessary before claiming a capacity. Keep measurements and deployment details private. Do not increase Uvicorn process count without redesigning shared writer and job ownership.

## Sustained fault and rollback rehearsal

Dispatch **tests** with `sustained_fault_load=true` and the other diagnostic
inputs false. Separate disposable hosted Mac jobs run Python and Rust with
25 synthetic readers, a 228-person roster, nine hours of seeded history and at
least 120 seconds of document/catalog activity. No production account, URL,
configuration or database is accepted. The native wheel is built from that
checkout. Inspect `fault-load-python` and `fault-load-rust` artifacts, including
failures; successful CI exit alone is not production acceptance.

Each reader must attempt document pages and successfully fetch the catalog in
all three equal observation windows, and successfully read at least two pages.
Expected preparing responses during the explicitly injected media outage count
only as activity, never as successful reads. Every acknowledged page must match
one durable event. The positive report control admits eight reports and exactly
eight audit events. Subsequent phases hold eight completed snapshots before
revoking the synthetic admin session, role or consent; all must refuse without
report data or report-view audit events while other readers continue.
Snapshot preparation is staggered: the next report starts after the preceding
one has reached the held disclosure boundary. All eight HTTP requests are then
pending together. This controls the authorization interleaving; it does not test
eight simultaneous full-history calculations or their throughput.

The rehearsal imports quiz/certificate evidence during traffic, checks immediate
lock/certificate updates and duplicate-import idempotence, injects missing media,
and verifies failed export preserves events and pending batches before retry.
Historical imported events correctly report backup health as `delayed` until
confirmed. Only that field may differ during the controlled import/export
window; the failed-export check requires `delayed`, and successful retry must
restore `current`. Unexpected states and the two-second wall deadline still fail.
Before that import, the rehearsal pauses the background exporter and finishes any
batch it had already sealed. The deliberate failure and retry then act on the
imported backlog, not on earlier unconfirmed work; retry must confirm every event
up to the captured sequence before health may return to `current`.
After traffic stops, fresh HTTP reports must equal raw Python replay. It then
closes the application, retains the complete event identity/payload digest,
and reopens the same current database with Python. Reader progress, quiz locks,
certificates, staff report, durable pages and health must remain correct.

The host CPU, available memory and RSS guards continue through shutdown and
rollback startup. Health is checked during serving, with the same two-second
wall deadline, and explicitly after rollback; it is suspended only for planned
stop/start. Missing phases, incomplete traffic, resource failures and cleanup
failures prevent a successful receipt. A full probe has a ten-minute guard;
each application startup remains limited to ten seconds. For a small diagnostic,
run `python -m scripts.progress_fault_load --output .context/fault-smoke-new`;
it uses two readers, no historical seed and fifteen seconds. Use a fresh output
path and a development environment; `--representative` requires hosted macOS.

This proves synthetic sustained correctness, not real Google sign-in, WAN or
video capacity, provider quotas, a complete cloud restore or live staff-device
behavior. Global recovery/audit failures remain covered by the separate
controlled concurrency tests. Python rollback reuses acknowledged data; it never
restores an older backup over new work.
