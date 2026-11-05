# Isolated resource pilot and health diagnostics

The accounts pilot frontend is not automatically a separate resource service.
Before repointing a pilot hostname, inspect its resource API setting. Both hosted
portals used the office resource API when checked on November 5, 2026. Changing
the office backend therefore affects both portals.

## Run a disposable backend rehearsal

From a source checkout with the normal test dependencies and the
compatible optional Rust wheel installed:

```sh
.venv/bin/python -m scripts.pilot_rehearsal --output /path/to/new-pilot-evidence
```

Run with assertions enabled. The harness refuses `-O` and `PYTHONOPTIMIZE`
before creating output because its compatibility checks use assertions.

The output directory must not exist. No production URL, config or credentials
can be supplied. The harness creates a private synthetic roster, sample quizzes,
SQLite database, fake cloud store and pre-derived document images. It starts
three separate processes, Python → Rust → Python, sequentially on ephemeral
`127.0.0.1` ports. An exclusive fixture lock and confirmed process exit prevent
two writers. The services expire after two minutes; the parent also stops each
child on success or failure.

The real HTTP flow verifies:

- Anonymous and non-admin refusals, and the staff progress report.
- Decodable, stamped document pages and recorded page progress.
- An unfinished chapter cannot expose quiz questions; the next chapter is
  locked until a real quiz submission passes.
- Rust reads a new page, records a quiz pass and unlocks the next chapter.
- Python rollback preserves progress, quiz state, event bytes and schema.

The report contains counts, hashes, process/backend identity and check results;
session tokens remain in memory. Read `report.json` only as a completed result
when its status is `passed`. An interrupted run is not a passing rehearsal.
Keep failed evidence directories and use a new directory for the next attempt.

These are functional checks on synthetic content. They do not validate Google
sign-in, selfie enrollment, media conversion, real-browser rendering or capacity.
The existing native rollback tests separately cover certificate identity/name
and permission/recovery refusal behavior.

## Opt-in health timing

Set `POSTERN_HEALTH_TRACE=1` in the resource-service environment before startup
to record public `GET /health` timing. It is off by default. This does not change
the response body, headers, readiness checks, database SQL or client deadline.
Other endpoints, including private health and configuration, are not traced.

Records are written only beneath the configured data directory in
`health-diagnostics/health.jsonl` and `health.jsonl.1`. Each file is bounded to
1 MiB; the directory is mode 0700 and files mode 0600. Symlink/non-regular log
targets are refused. Rotation replaces older records, so retention is finite
and depends on probe frequency. Copy the files promptly after a stopped pilot;
this rolling trace is not an incident archive.
A dedicated writer consumes a 64-record queue. At most 16 requests are traced
at once; nonblocking admission lets excess health requests execute unchanged
without tracing and increments `skipped_traces`. A full queue or failed sink
drops diagnostics; it cannot return cached health or suppress an application
error. Shutdown joins the writer for up to one second after dispatch through
the shared executor; time waiting for that executor is outside the join bound.
Missing records, `dropped_records` or `skipped_traces` mean incomplete diagnostic
coverage, not successful requests.

Each snapshot contains PID, backend, a process-local request counter, start time,
fixed stage offsets in monotonic milliseconds, and maximum observed event-loop
scheduling lag. It contains no request URL/query, headers, bearer, identity,
SQL, query result, error message, training content or biometric data. Intermediate
snapshots make an unfinished request visible before a client's timeout.

| Difference between stage offsets | What it measures |
| --- | --- |
| `handler - received` | Dispatch from this ASGI boundary to the health handler |
| `worker_started - submitted` | Waiting for the shared default worker pool |
| `query_started - worker_started` | SQLite connection and read setup |
| `query_finished - query_started` | Existing health queries and permission freshness check |
| `worker_finished - query_finished` | Reader cleanup, including connection close |
| `loop_resumed - worker_finished` | Delivering the result back to the event loop |
| `disk_finished - disk_started` | Existing disk-space check |
| `response_finished - received` | Through the final ASGI response send |

`database_error` records only that the existing database exception path ran.
An end-stage mark means the operation returned or raised, not that readiness
passed. Cancellation can return to the loop before the worker finishes; do not
interpret that order as a successful-request duration from the table above.
Missing end stages can indicate a request still in progress, cancellation or lost
diagnostics. The 50 ms event-loop timer runs only while a traced request is in
flight. It cannot measure network time, an event-loop stall before ASGI dispatch,
or client receipt. A client timeout may precede a later server completion.

To stop recording, remove the environment setting and restart through the normal
deployment procedure. Log files remain local for inspection; remove only those
diagnostic files when their evidence is no longer required.

## Validate diagnosis with controlled delays

```sh
.venv/bin/python -m scripts.pilot_rehearsal --fault-probes /path/to/new-delay-evidence
```

This starts separate disposable Python services with one controlled 2.2-second
delay in the worker queue, database setup or event loop. Fault injection exists
only in the rehearsal process; there is no diagnostic HTTP endpoint or production
fault setting. The client keeps its two-second `/health` deadline. A concurrent
`HEAD /health` request, expected to return 405 without executing the handler,
helps distinguish a broad loop stall from a slow health worker.

Each probe must time out at the client and identify the injected stage in the
server trace. This establishes tracing sensitivity; it does not explain an
earlier production timeout or make that timeout fixed.

## Hosted pilot boundary

This harness does not deploy or repoint `pilot.postern.fyi`. It uses local demo
authentication and must stay on loopback. A hosted pilot needs an independently
owned backend/database, restricted authenticated ingress or a normal Google
pilot flow, exact origins and separate storage/jobs. Keep account registration
closed and retain the previous frontend deployment for routing rollback.

On the serving Mac, a second process still shares CPU, memory and disk. Run
bounded low-concurrency checks with production health/CPU guards, or move them to
an isolated runner. No quiet minute is needed for disposable functional checks;
performance comparisons still need measured, comparable load conditions. Do not
raise the two-second production health limit or 70% CPU stop to finish a run.
