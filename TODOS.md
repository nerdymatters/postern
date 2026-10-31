# TODOS

This file tracks maintenance and deferred fixes. The [master-plan checklist](ROADMAP.md)
tracks launch preparation, accounts and profiles, private spaces, agent access, the
Rust folder client, screen sharing and computing resources.

## Infrastructure

### Migrate the server from Python to Rust

**What:** Evaluate rewriting the Python resource API in Rust for better performance and storage use.

**Why:** The owner wants a faster, leaner server as usage grows.

**Context:** Owner request, Oct 28, 2026; low priority. The server is the Python package in `postern/`; the roadmap's M4 Rust folder client is separate work. Start by profiling where time and storage go before choosing a full rewrite or a targeted Rust component, and keep the existing API contract and tests as the compatibility bar.

**Effort:** XL
**Priority:** P3
**Depends on:** None

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
