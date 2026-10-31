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

The mixed scenario exercises real authorization, audit commits, range responses,
15-second progress reporting, a document page for one in five viewers, catalog
refreshes, and one admin dashboard refresh every 30 seconds. It ramps arrivals
over ten seconds by default. `--ramp-seconds 0` tests simultaneous arrivals.
It uses opaque 8 MiB byte fixtures and a synthetic 12-hour video timeline. These
are deliberately not playable MP4 files: this measures the HTTP/data path, not
decoding, video quality, player buffering, or rendering on staff devices.

Reports include per-route latency/status counts, audit queue and commit times,
event-loop lag, process memory, sampled whole-host CPU reserve, byte integrity,
and comparison of acknowledged progress with persisted records by user. Client
and server share a process/event loop, so CPU/memory results include both.

The rehearsal stops on memory pressure or sustained whole-host CPU reserve below
30%. When the existing local Postern health endpoint is detected, it checks that
service periodically and stops if process, permission, audit, or storage health
fails. The automatic guard does not evaluate backup freshness; inspect that
separately before and during longer runs. A shared-Mac rehearsal still
consumes CPU, disk, and memory: use a separate host or an agreed quiet window for
longer tests. These stop conditions are safeguards, not proof of no user impact.

Inspect `report.json`, not merely the process exit status. Functional errors cause
a nonzero exit, but latency/CPU acceptance and workload coverage still require
review of the report. Any abort, unexpected
HTTP error, unfinished enrollment, integrity mismatch, or missing request family
means the scenario did not pass. Very short runs may never reach a 15-second
heartbeat or a 30-second dashboard refresh.

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
