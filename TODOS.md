# TODOS

This file tracks maintenance and deferred fixes. The [master-plan checklist](ROADMAP.md)
tracks accounts and profiles, private spaces, agent access, the Rust folder client,
screen sharing and computing resources.

## Staff progress

### Show certificates in Staff progress when no training content is published

**What:** Keep certificate holders (and their View and Print certificate controls) visible in Staff progress when no content is published.

**Why:** Certificates outlive the catalog. While every post is archived, for example when the SOP set is replaced, admins cannot view or print anyone's certificate.

**Context:** `dashboard/web/src/training-progress.tsx` replaces the whole people table with "No published training content yet." when `report.total_items` is 0, although `GET /api/admin/progress` still returns each person's `certificate`. Found by the outside review of 0.12.0. Add a zero-item test.

**Effort:** S
**Priority:** P2
**Depends on:** None

## Certificate page

### Stop an older reading-room load from undoing a just-saved name

**What:** Give the app's full load path (`reload()`) the same invalidation the progress refresh got in 0.12.0.

**Why:** If a full reload, for example after a selfie renewal, is still on its way when someone saves their certificate name, its older answer can put the old name back on screen and in the print copy until the next refresh.

**Context:** `dashboard/web/src/app.tsx`: `onSaved` now calls `refreshProgress()`, which cancels only the progress-refresh effect, not the load effect. One response-generation counter across both paths fixes it. Found by the outside review of 0.12.0.

**Effort:** S
**Priority:** P2
**Depends on:** None

### Show feedback when "Check again" runs

**What:** Show "Checking…" and "Checked just now", or a retryable error, after tapping Check again on `/badges` and `/certificate`.

**Why:** A tap that finds nothing new, or fails, looks like a dead button.

**Context:** Deferred by the owner in the 0.12.0 review. The pages call the app's `refreshProgress`, which waits 250 ms and swallows errors (`dashboard/web/src/app.tsx`). The quiz page's own Check again shows the pattern to copy.

**Effort:** S
**Priority:** P3
**Depends on:** None

## Certificate names

### Make a new name win over a future-dated saved name

**What:** Stamp a name save no earlier than just after the person's latest `certificate_name` event.

**Why:** `chosen()` prints the latest-stamped name. If the office Mac's clock jumped back, or a restore came from a Mac whose clock ran ahead, a new save is accepted and reported as saved, but the older future-dated name keeps printing.

**Context:** `postern/honors.py` `chosen()` and `rename()`. Found by the adversarial review of 0.12.0.

**Effort:** S
**Priority:** P3
**Depends on:** None

### Use a restore-stable tie-break for certificate events

**What:** Break timestamp ties in `FIRST_PASS` and `chosen()` with something a restore preserves, such as the event id, instead of `seq`.

**Why:** Recovery assigns new `seq` numbers in import order, so two events with the same timestamp can resolve differently after a restore: a different certificate number or printed name.

**Context:** `postern/honors.py` orders both queries by `seq`; `postern/recover.py` imports batches in batch-id order. A tie needs two events in the same second (Final Exam attempts are limited per minute), so it mostly affects restores from the summary log.

**Effort:** S
**Priority:** P3
**Depends on:** None

### Keep zero-width joiners inside names

**What:** Keep U+200C and U+200D when they sit between letters or marks in `tidy()`.

**Why:** Persian, Sinhala and some Indic spellings need them. `tidy()` drops every format character, so those names print misspelled.

**Context:** `postern/honors.py` `tidy()`. Thai and English names are unaffected. Keep refusing the invisible names tested in `tests/postern/test_honors.py`.

**Effort:** S
**Priority:** P3
**Depends on:** None

### Cut long suggested names at a word, and count length like the server

**What:** Cut staff-list or Google names over 80 characters at a word or grapheme boundary, and count the name field's length after the same Unicode normalization the server applies.

**Why:** A long Thai name loses the marks of its last syllable. A few rare characters expand under normalization, so the field can accept a name the server then refuses with "1 to 80 characters".

**Context:** `postern/honors.py` `fitted()` and `NAME_LIMIT`; `dashboard/web/src/honors.tsx` `maxLength={NAME_LIMIT}`.

**Effort:** S
**Priority:** P3
**Depends on:** None

### Correct the restore claim in honors.py's docstring

**What:** Say that a restore keeps a certificate only while its first passing Final Exam attempt reached an export tier.

**Why:** The docs were corrected in 0.12.0; the module docstring still says every restore shows the same certificate.

**Context:** `postern/honors.py` module docstring; `docs/runbooks/recovery.md` has the accurate wording.

**Effort:** S
**Priority:** P4
**Depends on:** None

## Product decisions

### Decide how admins can check a printed certificate

**What:** Choose between locking the printed name after it is first confirmed, showing admins the name history, and making certificate numbers searchable in Staff progress.

**Why:** A holder can change the printed name up to 20 times a day under the same number. Staff progress shows only the latest name and cannot look up a number, so an admin cannot check a certificate someone presents.

**Context:** Raised by the adversarial review of 0.12.0. Every name change is already kept as a `certificate_name` audit event.

**Effort:** M
**Priority:** P3
**Depends on:** None

## Documentation

### Close the 0.12.0 documentation gaps

**What:** Note in `docs/runbooks/google-setup.md` that the Sheet's Name column now feeds the suggested certificate name, describe when the home page's "Your certificate is ready" card shows, and cover the medal and seal art in the logo kit.

**Why:** The 0.12.0 documentation audit found these gaps.

**Context:** See the Documentation section of the 0.12.0 pull request.

**Effort:** S
**Priority:** P4
**Depends on:** None

## Selfie enrollment

### Complete Thai copy on whole-screen refusal pages

**What:** Add Thai server messages, titles and hints for access, authorization, audit and portrait-outage refusal pages.

**Why:** Thai-first staff still see English-only whole-screen outage guidance, although inline selfie guidance is bilingual.

**Context:** Owner-deferred in the selfie release review (D2 item 1B). Start with `dashboard/web/src/refusal.tsx` and the corresponding server refusals; keep the shared `Bilingual` presentation and verify each refusal route.

**Effort:** S
**Priority:** P2
**Depends on:** None

### Make selfie reservations idempotent across writer deadlines

**What:** Give reservations and refunds a durable identity so a commit after the writer deadline can be reconciled without charging a failed capture or refunding twice.

**Why:** A reservation can commit after the 1.5-second deadline while the user sees the audit-paused screen; a failed best-effort release also leaves the attempt counted.

**Context:** Owner-deferred in D2 item 5A; documented in `docs/runbooks/capacity.md`. The admission bound reduces writer crowding but cannot establish rollback. Start with `postern/selfie.py` and `postern/db.py`; preserve the ten/30/90 rolling-window ceilings and ambiguous-insert retention.

**Effort:** M
**Priority:** P2
**Depends on:** None

## Accounts

### Create Postern's Terms of Service

**What:** Write Postern's Terms of Service.

**Why:** Postern has an account privacy page but no terms of service, and public registration will need them.

**Context:** Owner request, Oct 28, 2026. Also a launch task in the roadmap (M0-04), which gates opening registration. The account privacy page is `AccountPrivacy` in `dashboard/web/src/accounts.tsx`. Keep the terms separate from each private space's own agreements, which spaces still require when someone enters.

**Effort:** M
**Priority:** P2
**Depends on:** None

## Infrastructure

### Upgrade the database for more users and new features

**What:** Choose and plan a database upgrade that supports more users and future features such as search, retrieval and analytics.

**Why:** The resource API keeps its local state and audit queue in one SQLite database with a single writer connection; Google Drive holds originals, portraits and audit batches, and a Google Sheet holds the catalog. That fits one organization's staff, but more users and search or analytics workloads will need more than this storage offers.

**Context:** Owner request, Oct 28, 2026; low priority. Also a roadmap task (M2-06), to choose before M2-03, M2-04 and M3-02. The SQLite schema and writer live in `postern/db.py`, and Drive and Sheets storage is in `postern/storage.py`; the separate account service already uses Cloudflare D1. Start by measuring growth and the queries search and analytics would need. Any migration must keep training history, certificates and audit events intact.

**Effort:** L
**Priority:** P3
**Depends on:** None

### Migrate the server from Python to Rust

**What:** Evaluate rewriting the Python resource API in Rust for better performance and storage use.

**Why:** The owner wants a faster, leaner server as usage grows.

**Context:** Owner request, Oct 28, 2026; low priority. The server is the Python package in `postern/`; the roadmap's M4 Rust folder client is separate work. Start by profiling where time and storage go before choosing a full rewrite or a targeted Rust component, and keep the existing API contract and tests as the compatibility bar.

**Effort:** XL
**Priority:** P3
**Depends on:** None

## Completed
