# Postern web

One phone-first SPA: the training contents, reader/player, and admin screens. Vite + React 19 +
TypeScript + Tailwind v4, built with Bun. The same bundle is served two ways, so nothing about
the API's address is compiled in twice:

- by FastAPI itself (`static_dir` in the server config) — build with no `VITE_POSTERN_API`,
  and every request goes to the same origin;
- by Vercel (`/vercel.json` at the repo root) — set `VITE_POSTERN_API` in the Vercel project to
  the API's HTTPS origin.

## Accounts and profiles

Set `VITE_POSTERN_ACCOUNTS_API` to a separately hosted account API to enable the
**My Posterns** account home (`/home`), profile editor (`/settings/profile`) and public profiles
(`/@handle`). With this variable absent, the existing standalone reading room
continues to be the application entry point. Navigation calls the account home
**Posterns**; **Profile** remains separate. For local development, set it to
`/account-api`; Vite forwards that prefix to `http://127.0.0.1:8787`.

The account API uses `/v1/config`, `/v1/auth/google`, `/v1/session`, `/v1/profile`,
`/v1/profiles/:handle`, `/v1/terms/accept`, `/v1/auth/signout` and `/v1/account`.
See the [account Terms contract](../../docs/accounts.md#account-terms) for revision
acceptance and recovery behavior. Account sessions are
stored separately from reading-room sessions. Public profile reads send no
account credential and render only handle, chosen name, Google avatar, bio and
HTTPS website. Profiles are private until their owner explicitly publishes them.

The existing reading room is available through `/s/pilot`; old training deep
links also pass through the account/space gate. Space entry independently
verifies Google sign-in and membership. Before loading private content, the
client binds the space session's verified issuer and subject to the account's
identity. An older API without identity fields fails closed in account mode.
Deploy the compatible space API before enabling the account frontend. A space
refusal never signs out the Postern account. Account sign-out and account
switching clear both browser sessions and protected media.

Account pages do not require a space's agreements or selfie enrollment. Email,
private membership, training results and enrollment photographs are not public
profile fields. Profile names do not change certificate names. The account
privacy page explains publication, deletion and separately retained space data.

## Commands

```bash
bun install
bun run dev        # http://localhost:5173, proxying /api to 127.0.0.1:8790
bun run build      # tsc -b && vite build -> dist/
bun run test       # vitest
bun run typecheck  # tsc -b --force
```

Run the server in demo mode next to it (`mode: "demo"` in the server config). The landing page
then shows an email box instead of the Google button, which is what the Playwright harness uses.

## Shape

| file | what it holds |
| --- | --- |
| `src/api.ts` | private resource routes, the space session bearer token, and the mapping of the server's refusal states |
| `src/accounts-api.ts` | account/profile API requests, account token storage and change notifications, and verified identity matching |
| `src/accounts.tsx` | My Posterns, profile editing/publication, public profiles, account privacy/deletion, and the account-to-space entry gate |
| `src/app.tsx` | config/session gate, the refusal states, the shell and the routes |
| `src/theme.tsx`, `index.html` | day/night preference, system-theme updates, header toggle, and early theme application before React renders |
| `src/PosternWordmark.tsx`, `src/PosternIcons.tsx` | outlined logo and 22 decorative action/format icons; see the [logo kit](../../docs/brand/logo/README.md) |
| `src/ui.tsx`, `src/index.css` | shared controls, surfaces, dark styles, and the responsive `header-action` style; `Label` (English with its Thai line under it on buttons, both read by screen readers), `Thai`, and `Message`, which shows an "English / ไทย" server message as two lines |
| `public/` | browser/home-screen icons, manifest, and third-party icon licenses |
| `src/landing.tsx` | Google Identity Services, the demo email box, the in-app browser page |
| `src/consent.tsx` | independent activity/IP agreements, both initially unchecked; declining reaches no content |
| `src/selfie.tsx` | explicit selfie consent, camera capture/retake/save, storage acknowledgement and renewal |
| `src/guide.tsx` | the next step ("Continue training" on the home card, or the ready quiz): first the SOP chapters not yet passed, from Chapter 1 (what is left, then the quiz; exempt roles every chapter, without quizzes), marked required; then an unlocked, unpassed required Final Exam; then material added to a chapter already passed; then the optional Modules. An item still being prepared is never a step to open: the card says "… is still being prepared. Check back soon." instead, and in a chapter it keeps the quiz closed. Also "How your training works" in English and Thai, expanded by default for every role and still collapsible, with larger text and a thicker border, and the client copy of the finished-item rule |
| `src/grid.tsx` | the guide, chapter quiz badges, the "SOP chapters progress" bar (the chapters' items only, as the Modules are optional; "Overall progress" over every item when nothing is a chapter) and the contents; no duplicate preview cards or thumbnail fetches |
| `src/contents.tsx` | module grouping (the SOP chapters by number, then the optional Modules and other categories by name; the order of the contents on the home card and in the reader, of the next step, and of the reader's Next button; Admin → Categories and Admin → Staff progress still list groups by name, as the admin data carries no chapter numbers) under "SOP chapters · each ends with a quiz" and "Modules · optional, no quiz" headings when both are present, attachment rows with individual progress, chapter quiz rows and locks (the next locked chapter names the quiz in the way, later ones the chapter before them), and equal-item page/playback coverage with explicit unknown history |
| `src/quiz.tsx` | chapter quiz page (`/quiz/{chapter}`) and Final Exam (`/final-exam`), the unfinished-items checklist shown before a chapter is finished ("Check again" checks in place and says what it found), results and review (review first after a fail; every pass shows its answers, and as retakes after a pass ask the questions from that pass again, a new set of questions is promised only before a pass), quiz status badges, and the client copy of the chapter lock rule |
| `src/reader.tsx` | attachment contents and deep links, a note on every chapter page, for staff, saying what is left before its quiz, the Next button after the final page loads or video ends (within a chapter or module the next document, or the chapter's next unfinished item, then its quiz; chapter to chapter and Module to Module, reading order: "Start" a chapter (saying, to staff, that it ends with a quiz) until it is begun or passed, then "Continue" it, or "Next: Module 2 (optional)" ("Next module: <title>" on a site without quizzes); leaving the last chapter or the last section, staff go to required work left in another chapter (its items or quiz) from `guide.tsx`, else on in reading order, and after the last section to anything optional still open, else the end of the content; exempt roles follow reading order, with a required Final Exam offered before optional material; Admins access their optional Final Exam from Contents; a site without quizzes follows reading order until the end of the last section, then offers anything still open; where all that is left is still being prepared, it says so in place of a button), a link to an item still being prepared says so instead of showing another item, and one that failed to prepare says an admin needs to upload it again, the locked-chapter screen (both halves of the rule, and a way back to the unfinished chapter), selected-item focus and contents scrolling when selection changes or Contents opens (only the sidebar scrolls on desktop; progress refreshes do not move focus), and stamped pages drawn into a canvas |
| `src/video.tsx` | native MP4 player, personal overlay, fullscreen wrapper, continuous-playback interval reporting that re-sends unsaved intervals, watched time ("Watched m:ss of m:ss"; skipped parts do not count), a warning after a skipped part in place of the quiz, and English subtitle controls kept in sync with native CC |
| `src/admin.tsx`, `src/post-status.tsx`, `src/report.tsx` | post search/status filters, separate archive, upload/publish, who opened it, denied sign-ins |
| `src/honors.tsx` | badges (one per chapter quiz passed, All chapters, the Final Exam) and their medal art, the home-page shelf, `/badges` with the next badge to earn, the first-pass celebration on a quiz result (medal and confetti, skipped for reduced motion), and the certificate: `/certificate` with its printed name and printing, and the print-only copy `index.css` prints alone |
| `src/training-progress.tsx` | searchable staff dashboard, overall/module/file progress, chapter quiz standing, a separate Final Exam result with its certificate (number, printed name, view and print), incomplete-history indicators |
| `src/categories.tsx`, `src/post-title.tsx` | category and post-title renaming, validation, and pending backup feedback |
| `src/blobs.ts` | every object URL, so sign-out can revoke all of them |
| `src/views.ts` | the per-item reading attempt id, stable across remount and refresh |

Images are fetched with the `Authorization` header and drawn from the response blob. There is no
original-download route. Video uses a native player with bounded byte-range responses from the Mac.
`POST /api/views/{view_id}/video` requires the reader’s session and returns a media ticket scoped to
that view. The ticket is bearer access: anyone holding its URL can request that view while the ticket
and owning session remain valid. Treat it as private, and never include it in reports or logs.

`GET /api/video/{ticket}` requires a single byte range and returns at most 4 MiB per response, with
`Cache-Control: no-store`. Each request rechecks the owning session, current consent, fresh allowlist,
and post availability, then audits the request and rechecks authorization before returning bytes.
Tickets expire after 20 minutes without renewal. The player sends authenticated
`POST /api/views/{view_id}/video-progress` heartbeats every 15 seconds, renewing the ticket even when
no new intervals are reported. Sign-out, removal, archive, or stale permissions stop further requests;
already buffered bytes cannot be recalled.

Progress requests contain `ranges`, up to 32 `[start_seconds, end_seconds]` pairs. The server bounds
these by duration, and bounds the seconds this player has not reported before by elapsed viewing time
(up to 2× speed plus scheduling allowance), so a re-sent report costs nothing. It stores
`video_progress` events in both the detailed and summary export tiers. When a report does not reach
the server (offline, or a 5xx response), the player keeps its ranges and sends them again with the
next heartbeat or when it closes; ranges the server refused are dropped. Coverage unions overlapping
intervals so repeats and seeks add no credit. The first accepted nonempty playback report records a
video open; preparing the player or requesting bytes alone does not. Browser reports are activity
evidence, not proof of attention. Page opens continue to count after successful delivery.

The small overlay displays the reader’s email, the UTC time the player was opened, and
the private enrollment portrait when enabled. It stays inside
the fullscreen wrapper, but is not baked into the MP4. Hiding download controls does not prevent
saving video bytes or removing the overlay. Per-user encoded copies are deferred for latency tests.

Admin MP4 uploads use `POST /api/admin/posts/{post_id}/videos` with a 2 GiB bound; the existing
`/items` endpoint retains its 100 MiB request bound for documents/photos. Both use the same guarded
prepare, Drive verification, and catalog-confirmation publication flow.

## English subtitles

When a matching track is installed, `POST /api/views/{view_id}/video` includes its WebVTT text
in `subtitles` after the existing view authorization. The API loads
`<data_dir>/training/captions/<original-video-sha256>.en.vtt` at startup, matching the original video's
SHA-256, not its title or chapter. Each file must start with `WEBVTT`, contain cue timing,
and be at most 256 KiB. No match means no track or subtitle button. This loader check does
not establish translation accuracy or timing quality.

The frontend creates a temporary VTT blob for a native English subtitle track, on by default.
**English subtitles: On/Off** and the player's native CC menu update the same track state.
Track URLs are released on unmount and through the app's sign-out blob cleanup. Caption files
belong in the host's private data directory, never the API package, `dashboard/web/public` or the
public web bundle. Replacing a video requires a reviewed track for its new hash. The
`captions/` directory is required even when it contains no tracks.

## Day and night mode

Use the sun/moon control in the signed-in header to switch themes. Before a choice
is saved, Postern follows the system colour scheme and responds to its changes.
The choice is stored in this browser's `localStorage` as `postern-theme`, with
value `light` or `dark`. If storage is blocked, the toggle still works for the
current app session. The early script in `index.html` applies the theme before
React renders to prevent a light flash, and both it and `useTheme` update the
browser's theme colour. Keep their storage key and theme rules in sync.

The theme applies to sign-in, agreements, enrollment, reading and admin screens.
Header actions share rounded outlines: 44 px square on phones and 36 px tall from
640 px, with action labels visible on wider screens. Theme stays icon-only. See
the [logo kit](../../docs/brand/logo/README.md) for icons, colours and usage examples.

## Agreements and enrollment

Release frontend and API together. `POST /api/consent` requires `version`,
`ip_version`, `monitoring_accepted: true` and `ip_accepted: true`; IP acceptance is
required even when selfies are disabled. `GET /api/selfie` reports enrollment state,
`POST /api/selfie` accepts the camera file and `consent_version`, and
`GET /api/selfies/{id}.jpg` serves guarded private portraits. Admin history is at
`GET /api/admin/selfies`. Enrollment becomes active only after private storage
confirmation. Older sessions without browser binding must sign in again.

The camera needs HTTPS and explicit consent. After a dropped or failed save the page
reconciles server state before offering another upload; a refused photo (413, 422 or
429) created no pending capture, so it skips that check; any 422 asks for a new photo, a 429
disables "Use this photo" for the rest of the enrollment screen's session. Inline
`503 selfie_unavailable` and `503 face_check_unavailable` responses pause it for 15 seconds;
audit and authorization outages still open whole-screen refusals. The reconciliation check
defensively handles `selfie_unavailable`, though the current state route only checks
authorization and the database and does not emit it. Retake preserves both restrictions.
The pause uses elapsed time, refreshes when a backgrounded browser returns,
and has a separate English/Thai live status announcing when retrying is available. Kept
captures and photos that could not be prepared (including converter resource limits and
decoder crashes) count toward ten a day per account; a
face-check rejection with a saved release counts only toward 30 attempts a day; a server fault
(detector, converter host, private storage or disk) counts toward neither when its release
record is saved. Failed releases keep the attempt counted; all reservations count toward 90 a day. Missing portraits block
document/photo rendering with a retry state; optional report/video portraits may show
a grey placeholder while the video email/time overlay remains. Cached portraits never wait;
restoring one no longer waits for another person's face check, but shares a lock with other
uncached portrait restores and the selfie worker's private-storage upload or cleanup.
See [enrollment and retention](../../docs/runbooks/selfie-preview.md).
The `codex/selfie-preview` branch disables automatic Vercel deployment; its isolated
password-gated preview uses a separate API and synthetic catalog.

## Renaming categories

In Admin, expand **Categories**, choose **Rename**, enter a name (1–200 characters), then
**Save category name**. The rename applies to all posts in the displayed category,
including drafts and archived posts. Cancel leaves everything unchanged. Names
already used by another category are rejected rather than merging groups. The
contents grouping rules also apply here: case variants and `module1`/`Module 1`
are one category; uncategorized posts appear under General. The 200-character limit
also applies when creating a post and is checked after these naming rules are applied.

Categories starts collapsed and shows the category count. The selected post's upload
and preparation panel appears above it. Collapsing and reopening Categories keeps
an unfinished rename in place while you remain on the Admin page.

`POST /api/admin/categories/rename` accepts `{category, name}` and requires current
admin authorization and both current entry agreements; no selfie is required. Local
labels and the audit event update atomically. Published/archived posts enqueue metadata-only
catalog writes, retaining IDs, visibility, publication times, originals and
activity. The UI shows pending saves and backup delays. Pending publication,
archive or rename operations block another rename for that category. Catalog writes
are confirmed one post at a time; the whole category is backed up only when all
pending saves finish. Recovery uses each post's last confirmed labels. A later
archive supersedes a queued rename while preserving the new label. Draft labels
reach the catalog when published. Already committed renames continue backing up
if the initiating administrator later leaves.

## Renaming content titles

In **Admin → Posts → Manage**, choose **Rename title**, enter a title (1–120
characters), then **Save title**. Drafts, published posts and archived posts can
be renamed. This changes the post title shown in the reading room and reader;
attachment filenames, shared links, categories and reading progress stay the same.

`POST /api/admin/posts/{post_id}/rename` accepts `{title}` under the normal admin
authorization and current entry agreements; no selfie is required. Title and audit
event commit together.
Published/archived titles use durable metadata-only catalog operations; drafts
reach the catalog on publication. The UI refreshes the reading-room list and shows
pending backups or retries. Pending publication, archive or metadata operations
block conflicting renames. Archiving supersedes a pending title backup while
preserving the new title. No database migration or content re-upload is required.


## Chapter quizzes

Every published category whose name starts with its chapter number, such as `SOP Chapter 3: …`,
`Chapter 3 …` or `บทที่ ๓ …`, is a chapter. Module categories (`Module 1` to `Module 3`) and names
that only mention a chapter later (`Appendix to Chapter 3`) never have a quiz and are never locked.
Quizzes and passes follow that number: renaming `SOP Chapter 3` to `SOP Chapter 4` moves the
chapter-4 quiz and the chapter-4 passes onto it, so renumber chapters only together with
`<data_dir>/training/quizzes.json`. The questions live in that private file:
a pool of twelve per chapter, half "conduct" (what to do and not do) and half
"mindset", four options each, in English with Thai underneath. The server checks the file at startup
and refuses to start if a question is incomplete. Each chapter's version is a hash of its pool and of
how it is dealt (hand size, pass mark, dealing rules), so after an edited bank or a dealing change is
loaded by an API restart, a quiz opened before it is refused with `409 quiz_changed` and the reader
starts again, instead of being graded against the new answers or left with a hand of the wrong size.
Option ids reach the page, so in each chapter every letter is the correct answer three times, in an
order with no pattern across chapters: no letter rule learned from other chapters (copying a chapter,
or picking the letter most or least used at the same question number) should pass more than 8% of a
chapter's hands. Review private banks against these bounds when editing content; public tests
exercise the fictional sample bank.

Each attempt deals five questions from the pool. A hand has at least two questions of each focus, no
answer letter holds four (the pass mark) of its correct answers, and in each language no single
option-length rank does either, so no one-letter or one-rank strategy such as "pick b" or "pick the
longest" can pass; two ranks together may. At startup the
server lists every hand that meets these rules. Among those it deals one with the fewest questions from the person's
previous attempt (none, for the twelve-question bank), then the fewest times seen, choosing at random
between equals. Once a chapter is passed, retakes are dealt only from the questions of the person's
passing attempts and of any attempt since their first pass, topped up from the pool only when a bank
edit removed some; apart from those top-ups, a pass has already shown their answers. The hand is kept in `quiz_draws` until an attempt answers it, so reloading the page
or opening it on another device shows the same questions; the attempt spends the hand. Reopening it
reads no attempt history (only dealing a new hand does): recording a pass drops hands dealt before
it, so a kept hand after a pass was dealt under the retake rule, and only a hand older than the pass,
which a rollback to 0.7.x can leave, is checked against what the person was shown. Before a pass,
a second submission of it returns `409 quiz_changed`. After a pass, once the quiz is reopened elsewhere
the new hand has the same questions, so a page still showing the spent hand can be answered again;
that reveals nothing new. `quiz_seen` counts the questions each person has
answered per chapter and is updated with `quiz_standing`, including during restores; the first start
of 0.8.0 fills it once from earlier attempts. Hands are not restored: after a restore everyone is
dealt afresh. If a restored pass lacks its answer history, retakes fill the missing question ids
from the start of the pool; these questions stay fixed after an attempt, just like top-ups after a
bank edit. Options are shuffled each time the page loads.

Staff open a chapter's quiz only after finishing the chapter: every page of each document
opened and every video played for all but 2 seconds or 2% of its length, whichever is longer
(players stop a fraction of a second early, and a stall or failed report can drop a few seconds of a
long video; the missing part can be anywhere in the video). The player re-sends playback the server
could not record, and the server counts each second once per player, so a re-sent report never
crowds out new playback. Attachments that failed to prepare do not hold the quiz back; one being
prepared again after its cached pages were cleared still has to be finished. Until the chapter is
finished the quiz page lists
what is left, with links, instead of the questions; the reader's end-of-chapter button leads to the
next unfinished item before the quiz. Missing restored history counts as unopened. Admins, managers
and C-suite can open any chapter quiz without reading. Video completion rests on playback the browser
reports, which is activity evidence rather than proof of attention; the speed bound applies per
open player. Because of the end allowance, a video can open its quiz while its progress reads 98%. The quiz routes
replay a reader's history on a read snapshot outside the audit writer, then confirm against the
current catalog in the writer, so newly published material can only keep a quiz closed.

Staff must answer at least 80% correctly (four of five) to pass. A chapter opens only after every
earlier published chapter's quiz has been passed; the server enforces this on every content request
with `423 chapter_locked`, including thumbnails and video. Archived chapters do not block later ones,
so to replace a chapter's handout, publish the new post before archiving the old one; otherwise
later chapters are open to everyone until the replacement is published.
Passing is permanent: retaking a passed quiz never locks a chapter again. Admins, managers and
C-suite (the Sheet's Role column) may skip chapter quizzes and can still take them; their results are
recorded the same way.

`GET /api/quizzes/{chapter}` returns `ready` and the `unfinished` item ids; when ready it also returns
the dealt questions with options shuffled and without the answer key (until then `questions` is
empty), `total` (questions per attempt) and `pool` (questions in the chapter's pool).
Submitting before the chapter is finished returns `409 chapter_unfinished`.
`POST /api/quizzes/{chapter}/attempts` takes `version` and one answer per dealt question, grades on the
server, and records a `quiz_attempt` event (chapter, score, pass, answers); the answered question ids
are the hand. `quiz_opened` events record the dealt ids. Both routes return
`404 quiz_not_found` for a chapter that is not published or has no quiz; a submission with a missing
or unknown answer returns `422 answers_required`. Answers to pool questions outside the current hand
mean a page showing a hand that was already answered (or lost in a restore) and return `409
quiz_changed`, as does a second submission of a spent hand before a pass; the web app then asks the reader to start
again and refreshes their standing, so a pass recorded from another screen shows. Quiz refusals carry
English and Thai in one message (`English / ไทย`); the web app shows the Thai on its own line. After a failed attempt
the response reports only the score, without saying which answers were wrong. Because each attempt before a pass
deals different questions, changing one answer at a time no longer isolates a question's key; retakes after a pass re-ask only questions a pass already showed, plus any top-ups. After every passing
attempt the response returns each answer, whether it was right, and its explanation (`results` is empty
only after a failed attempt); as retakes after a pass ask only questions from the person's passing attempts
and any attempt since their first pass (topped up only after a bank edit), retakes cannot collect the rest of the pool's answers. Retakes are unlimited in practice: up to ten submissions a minute and
100 a day per account (`429 rate_limited`, counted after sign-in from the audit record), and every
attempt shows in Staff progress. Attempts are audit events in both export tiers, so a pass survives a
lost batch through the summary log. Each attempt also updates one `quiz_standing` row per person and
chapter in the same transaction (restores update it as they import), so locks and standing never
replay a person's attempt history. A chapter is locked again only if its passing attempt reached neither tier, for example
when the Mac failed before the next export; that person retakes the quiz, and Staff progress says the
attempt is missing from the available history.

`GET /api/posts` adds `chapter` and `locked_by` to each post, plus `quizzes` (the reader's standing
per chapter, including `unfinished` item ids) and `quiz_exempt`. Admin progress adds `quizzes` (with
each person's `unfinished` item ids), `quizzes_passed` and `quiz_exempt` per person and `quiz_total`
overall, read from `quiz_standing` in the same snapshot as the history scan.

## Final Exam

Open **Final Exam** after the Chapter 9 entry in Contents, or use **Take the Final Exam**
after passing that chapter. The route is `/final-exam`. Admins may take it optionally without
passing Chapter 9; Staff, Managers and C-Suite must have passed Chapter 9. The chapter must
still be published for every role. The optional
Modules remain accessible independently of the exam.

The bank is `<data_dir>/training/final_exam.json`: all 20 bilingual questions per attempt, four options
each, with 16 correct required to pass. It is not a chapter question pool. The signed-in
Postern account supplies the identity; no separate form name or email is needed. A failed
attempt shows its score; a pass shows answers and explanations. Retakes preserve an existing
pass, and chapter-quiz option-length and letter-balance guarantees do not apply to this bank.

`GET /api/quizzes/0` and `POST /api/quizzes/0/attempts` reuse the quiz request/response format,
version checks, durable hands, account attempt limits and audit/recovery machinery. Key `0`
is reserved for the exam; chapter numbers must be positive. Access returns `423 chapter_locked`
until Chapter 9 is passed (except Admins), or `404 quiz_not_found` if Chapter 9 is unavailable.
`GET /api/posts`, quiz submission responses and each person in `GET /api/admin/progress`
include a separate `final_exam` standing (or `null` when unavailable). It does not increase
`quiz_total`, `quizzes_passed` or the chapter progress bar. Staff progress has a separate
Final Exam column and detailed result. Its standing includes `optional: true` only for Admins,
and the exam response has `exempt: true` for Admins. Optional does not grant a pass: attempts
still need 16/20 and remain in the audit history. Current server-side roles determine access,
so demotion restores the Chapter 9 prerequisite without deleting earlier results.

## Badges and certificate

Badges keep no state of their own; each is a pass the reading room already returns. A chapter badge is
that chapter's `quizzes[]` standing with `passed` and `passed_at`, **All chapters** is every chapter
passed (dated by the latest pass), and **Final Exam** is `final_exam.passed` or a certificate. Passing
is permanent, but the wall lists only published chapters with a quiz: archiving a whole chapter hides
its badge until it is published again, and publishing a new chapter quiz adds a badge still to earn,
so **All chapters** counts the chapters published now. The certificate does not depend on the catalog.
The result screen celebrates only when the standing before the attempt was not passed, so a retake
after a pass earns nothing new. Medal faces follow the topics
of the nine SOP chapters by number; any other number gets a plain award.

`GET /api/posts` adds `certificate`: `{number, name, awarded_at, name_confirmed}` once the person has
passed the Final Exam, `null` before that and while a restore is unverified. `number` is
`PG-<year>-<8 hex digits>`, derived from the event id of the first passing Final Exam attempt, and
`awarded_at` is that attempt's time. The year, like every badge and certificate date, is in Eastern time
(`America/New_York`, the restaurants' zone), so all copies agree wherever they are opened. Retakes change
neither, and a restore brings back the same certificate as long as that attempt reached either export
tier; if it reached neither, the earliest restored pass sets the number and date. `name` comes from the
person's latest `certificate_name` event. Otherwise it is the fuller of their staff Sheet name and the
Google account name recorded on that first passing attempt: a plain name first (no note in brackets,
separator, digit or second person, such as `Nok (Server)` or `Nok & Ploy`), then more words, then more
words written as names, with the Sheet winning ties. A Sheet name that is not plain is used only when no
Google name is known. If neither has a letter or digit, it is the part of the email before `@`. The person, Staff progress and a restored server therefore all suggest the same
name. `name_confirmed` says whether the person saved it: **Keep this name** saves the suggestion as it
is, and **Change name** saves another.

`POST /api/certificate/name` with `{name}` saves a name. The server drops control and invisible
characters, keeps at most three combining marks on one letter and collapses spaces. The result must be
1–80 characters with at least one letter or digit (`422 invalid_name`). It refuses before a pass
(`404 certificate_not_found`), after 20 changes a day (`429 rate_limited`) and during a restore
(`409 history_restoring`). A `200` returns the updated `certificate`, in the same shape as on
`GET /api/posts`, with `name_confirmed: true`. The `certificate_name` event is in both export tiers.
`GET /api/admin/progress` adds each person's `certificate`; Staff progress shows its number and printed
name, and can view and print it.

The certificate is laid out in container units: the screen copy scales with its width, and the printed
copy fills the page. Printing, from the button or the browser's own Print, shows only the certificate.
It is rendered a second time directly into `document.body`, and `index.css` hides everything else for
print, with its colours kept. The landscape page rule is part of that copy, so it applies only while a
certificate is on screen; other screens print with the browser's own settings. The page has no margin,
so Chrome prints no headers or footers, and `index.css` insets the copy by 0.3 in instead. While the
copy is mounted the page title is "the configured certificate title", the name browsers give a saved PDF; it holds
no name or number, because a shared tablet keeps its history. Dates use the
American format on every device (`src/format.ts`), Thai lines included. An older API sends no
`certificate`. Without a certificate or a `final_exam` standing (the reading room did not load, a restore is being
checked, or the exam is not published) both pages say certificate details are not available right now
and offer Check again; they never tell a holder to pass the exam. Badges still show, but after a Final
Exam pass the badges and certificate pages say the certificate is being prepared until the API is updated, so restart the API straight after merging (see
the [release checklist](../../docs/runbooks/release.md#branding-and-certificate-migration)).

## Finding posts and staff progress

Admin posts show labelled status badges: amber Draft, blue Publishing, green Published,
and grey Archived in both day and night mode.
Use **Find a post** to search titles or categories and **Post status** to filter active
posts. Expand the separate **Archived** section for retired content; a search with
archive matches opens it automatically and shows the matching count. Archiving keeps
its existing immediate effect on reader access.

Open **Admin → Staff progress** for the current staff list, including admins, against
currently published attachments. Search by person, filter by progress state, or sort
by progress, recent activity or fewest quizzes passed. **Details** expands chapter quiz
attempts (a **Not ready** badge marks a quiz the person cannot take until they finish that
chapter), module and individual file coverage.
Choose a restaurant tab to scope the staff count, average, completion cards, search,
filters and details to that location. The first returned roster group is selected initially. Tabs support
Left/Right, Home and End keys and horizontal scrolling on phones. A person in several
tabs has one canonical email and shared activity; repeated rows do not duplicate their
progress. Changing tabs clears person/status filters and expanded details.
Rows awaiting an email are excluded from login access and shown as a worksheet-row
notice in their tab. Empty configured tabs remain visible. Spreadsheet refreshes update
membership automatically; **Refresh** reloads the latest report in an open browser.
Use **Refresh** for newly recorded activity. Draft and archived items do not contribute
to these totals; their historical per-post reports remain available in Admin.

Each attachment has equal weight. Documents count unique valid pages opened, photos
count once, and videos count the union of recorded playback intervals, so rewatching
a segment does not inflate progress. The overall card averages these per-person
fractions. Completed means every current item's recorded coverage is 100%; it does
not prove understanding. Missing history is shown as Unknown or a lower bound;
missing video duration is Unavailable. Last viewed uses actual recorded viewing
events, not page-summary export times or attempts. Adding or archiving material
changes the current denominator.

The progress data comes from admin-only `GET /api/admin/progress`, which returns
`people`, `total_items`, `quiz_total`, `history_incomplete`, ordered `groups` and
`roster_warnings` (`group`, worksheet `row`, `message`). Each person includes `groups`, completion
counts, last activity, per-item coverage and chapter quiz standing; no sessions or media tickets are returned.
Older responses without grouping fields are displayed under the generic Staff group. Group metadata is
derived from the current roster and replaced in the same transaction as membership;
it does not partition training content or confer administrative rights.
The endpoint returns `409 history_restoring` until recovery catalog verification is
finished, and uses the same authorization and agreement requirements as other admin
routes. History is read from a consistent snapshot outside the audit writer, then
admin access is rechecked and `training_progress_viewed` is committed before returning
the report. Opening the report does not change reader progress.

Admin management does not require a selfie; the reading room, document/video delivery,
and enrollment photo images still require enrollment even for admins when selfies
are enabled. The selfie screen offers admins **Continue to admin without a selfie**,
which does not create an enrollment or mark any training as opened.

In per-post reports, “Tried to open · no playback recorded” means a video opening
was requested but no playback was confirmed. For documents, “Tried to open · no
pages loaded” means no page-delivery open was confirmed. These are recorded-activity
states, not diagnoses that loading failed.

## Installation branding

The API supplies operator/certificate display fields from the private branding file. Build-time `VITE_POSTERN_ORGANIZATION_NAME` and `VITE_POSTERN_PRIVACY_CONTACT_EMAIL` keep privacy contact available when the API is down. See [branding](../../docs/branding.md). `VITE_POSTERN_API` is supplied by each deployment, never a checked-in customer endpoint.
