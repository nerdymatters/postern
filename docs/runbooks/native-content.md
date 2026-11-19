# Native content rehearsal

This opt-in adapter connects the resource catalog to protected document pages and
video, and connects explicitly bound training resources to the existing training
service. It is an isolated M2 integration step. The ordinary entrypoint remains
the existing application; adding these modules does not migrate production data
or enable a hosted native service.

## Run the acceptance checks

Use an unprivileged account with the repository's `test` and `postgres` extras,
PostgreSQL 17+ tools, Poppler, ffmpeg and the configured macOS Seatbelt or Linux
bubblewrap sandbox:

```sh
.venv/bin/python -m scripts.content_rehearsal --output .context/content-check
```

The evidence directory must be new. This command accepts no production DSN or
existing data root. Tests create owned PostgreSQL clusters with private Unix
sockets, private object directories, synthetic signing keys and two separate
legacy training services. One transport test listens on an ephemeral loopback
port. Keep the output private: it contains synthetic credentials and fixtures.

The report records source hashes, versions, JUnit results and the complete
collected/executed test inventory. It clears ambient pytest selection/plugin
options and disables plugin autoloading. Every declared module must contribute
tests, with no deselection and exactly one successful setup, call and teardown
per collected case. Changed source, missing evidence, skipped tests, timeout or
failure prevents a passing report. Interrupting or terminating the command stops
its owned process group and reaps its child before returning. The checks include
real PDF/video conversion, stamped HTTP delivery, permission races, actual TCP
backpressure, two-space isolation and fresh quarantined restores. Platform
sandbox results apply only to the platform on which the command ran.

## Explicit runtime capabilities

Apply core migrations through `010` with the owner migration tool. Keep the
runtime login restricted to `postern_resource_service`; it cannot read object
keys or raw presentation tables, publish content, or mutate identity bindings.

Pass the same `SpaceService` to `create_app(..., spaces=spaces, content=content)`
and `ContentService(spaces, objects, policy=policy)`. The content service closes
its reader when the application stops. `LocalContentObjects` receives an existing
absolute private directory and only exposes verified reads. Offline preparation
uses the separate `LocalContentPublisher` capability.

Without a policy adapter, content is denied. `NativeReadingPolicy` requires an
explicit set of native spaces that have no additional consent, enrollment or
training prerequisites. It does not replace those gates in an existing
installation. PostgreSQL still checks current sessions, accounts, membership,
publication, revision and `view` grants for every reader, including owners.

`prepare_content` uses the existing sandboxed converter and takes an existing
space UUID, resource UUID and revision. It preserves the original bytes and
returns their descriptor plus a complete derivative inventory. The offline
operator inserts that original into `postern.objects` and calls
`postern.publish_content` in one owner transaction. Preparation never creates a
resource identity, binds a person, changes old links, or modifies a serving
database. Failed preparation cannot publish a partial SQL presentation.

This private local backend accepts originals up to 64 MiB and individual derived
objects up to 8 MiB. Video is split into independently hashed chunks. Object
creation is immutable; reads reject changed bytes, symlinks, unsafe ownership or
permissions, special files, unexpected size and hash mismatches. Missing
derivatives never fall back to original files.

## HTTP contract

Paths below are relative to `/v1/spaces/{space}/resources/{resource}`. Each request
specifies `revision` as a positive query parameter and normally uses the native
`ps_` bearer. JSON responses contain only public presentation metadata.

| Method and suffix | Result |
| --- | --- |
| `GET /document` | Resource ID, revision and page count |
| `GET /document/pages/{page}.jpg` | Zero-based, reader-stamped JPEG |
| `GET /thumbnails/{index}.jpg` | Protected, reader-stamped thumbnail when prepared |
| `GET /video` | Resource ID, revision, duration and the current reader's stable label |
| `POST /video/ticket` | Opaque, resource/revision-scoped 30-second media ticket |
| `GET /video/content.mp4` | Prepared MP4, with one optional byte range |

A media ticket is a reference to its issuing session, not cached permission.
Every request and subsequent video chunk rechecks live authority. A ticket works
only on its exact video path and revision; restart, expiry, logout, membership
revocation or restore-generation changes invalidate its use. Never log ticket
query strings or place them in persistent browser storage.

The browser reader additionally requires `VITE_POSTERN_NATIVE_CONTENT=true` and
the existing native account/resource API configuration. It links document/video
catalog entries to revision-scoped readers, clears media on account/space changes
or hidden tabs, and rechecks authority while visible. See the
[dashboard configuration](../../dashboard/web/README.md). The flag remains off
by default; it does not expose native training screens.

Pages, thumbnails, metadata and errors use `no-store`. Images and video reject
conditional cache requests; there is no `304` shortcut. No native original-file,
download, export or arbitrary object-key endpoint is provided. Rendering and
object I/O occur outside database locks. A second authority check and required
audit commit complete before pixels or each video chunk can leave the service.

The renderer has bounded input, output, pixels, memory, CPU, process count and
concurrency. It receives only the selected image and the server-resolved stable
principal label. Its staging directory is read-only; bounded stdout carries the
JPEG. Timeout and cancellation kill the process group and reap its direct child.
A shared two-second cleanup deadline covers pipe draining and confirmation that
no live group members remain.
If that cannot be verified, the request fails and its private job directory and
renderer slot stay reserved. This bounds uncertain workers to the existing two
slots; inspect and stop them before restarting the service to restore capacity.

The opt-in browser reader overlays the video viewer label and current time.
That overlay is not embedded into the MP4 bytes. Prepared document images are
stamped on the server before delivery.

An outer ASGI deadline bounds the actual transport send, including middleware:
two seconds per blocked send, ten seconds after response headers, and thirty
seconds before headers. Images and video emit at most 64 KiB per send. Slow or
revoked streams terminate with an incomplete response. These limits cannot
withdraw bytes already delivered to a client.

## Training keeps both authorities

`SpaceTraining` is a separate optional adapter. Register an exact space with one
`LegacyTrainingSource(installation_uuid, service)` and provision its immutable
presentation and principal binding with explicit import evidence. A legacy
installation or database cannot be registered to two spaces. Registered legacy
services are started and closed by their caller.

Requests use both the native bearer and an independent
`X-Postern-Legacy-Authorization: Bearer …` credential. The native principal must
match the explicit legacy binding, and the legacy service must independently
authorize the current session, roster, consent and enrollment. Matching email
text alone never establishes a native identity.

Training routes are under
`/v1/spaces/{space}/resources/{resource}/revisions/{revision}/training`:

| Method and suffix | Existing behavior reused |
| --- | --- |
| `GET /progress` | The bound reader's stored chapter result |
| `GET /quiz` | Existing chapter/final-exam availability and dealt hand |
| `POST /quiz` | Existing server-side scoring, limits and durable attempt |
| `GET /certificate` | The reader's original earned certificate |
| `POST /certificate/name` | Existing saved-name rules |

Mutations require a UUID `operation_id`. The adapter commits a native intent
audit, then holds native authority locks across the actual legacy transaction.
The legacy effect and its retry journal commit together; final native audit must
commit before a response is returned. An uncertain response is retried with the
same ID and payload. This is coordinated recovery, not a distributed atomic
transaction. Pending writes are cancelled when possible; an already-running
write must settle before its authority lease is released.

Native POST bodies are limited to 32 KiB before JSON parsing, including chunked
requests. Quiz answer counts and individual identifiers are bounded separately.
If event-only recovery retains a committed operation's correlation event but
loses its response journal, retry returns `operation_recovery_required` instead
of repeating the effect. Both existing export tiers retain that correlation
without exporting cached quiz responses.

The existing lesson reader still records training completion. Native document
reads do not fabricate legacy page views, video progress or quiz credit. Hidden
prerequisite/item references are redacted. Previously earned own history can
remain available after a resource is archived, but active membership and both
identities are still required. Quiz questions, submissions and certificate-name
changes require current content permission and publication.

## Recovery and remaining rollout work

Restore PostgreSQL into a new closed database with `restore_quarantined`. Its new
generation invalidates copied credentials, and spaces remain suspended. Copy
objects into a separate private root and verify the complete original and
derivative inventories. Restore training SQLite databases together with their
durable history and operation journals. Reconcile revocations and privacy changes
that occurred after the snapshot before explicitly opening an individual space.
An intact object directory alone is never authority to serve content.

The rehearsal proves behavior with controlled synthetic identities. Production
principal-binding review, a real second-owner pilot, hosted account integration,
operational revocation reconciliation and a supported production rollout remain
separate work. Private local directories do not implement the approved
per-Postern encryption/key-custody design. Cloud provider selection remains last;
Office Mac remains the production primary until an explicit cutover.
