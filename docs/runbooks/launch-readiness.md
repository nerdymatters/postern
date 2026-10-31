# Launch baseline, migration and rollback

This is the M0-01 compatibility record and the release checklist for the next
account-service changes. It does not authorize opening registration. The
[Oct 28 production pilot](../reports/accounts-profiles.md#production-pilot)
remains the live baseline; the next round adds local verification, not another
production pilot. Keep registration closed until M0-02 and M0-04 are complete.

## Compatibility baseline

| Behavior to preserve | Existing evidence | Repeatable check before release |
|---|---|---|
| Verified sign-in and identity binding | Oct 28 existing-member and outside-account pilot; identity uses Google issuer/subject | `tests/postern/test_signin.py`; `dashboard/web/src/accounts.test.tsx`; private account verifier/session tests |
| Membership removal | Oct 28 roster removal revoked private access and retained the hosted account | `tests/postern/test_restaurant_roster.py`; direct API/media denial and account isolation tests |
| Space agreements | Existing member reached and explicitly accepted the private agreement gate | `tests/postern/test_ip_agreement.py`; frontend consent tests; account Terms remain separate |
| Enrollment | Oct 28 member completed selfie enrollment | `tests/postern/test_selfie.py`, face/preview/release cases; new format-admission test; no sandbox or rolling-window limits removed |
| Training history | Candidate/deployed comparison on a read-only production snapshot matched per-person progress and staff reports | `test_progress.py`, `test_training_progress.py`, quiz/video/recovery suites; no training schema change in this round |
| Certificate attribution | Prior snapshot comparison matched certificate number, award time, chosen name and confirmation | `tests/postern/test_honors.py`; frontend late-response and empty-catalog regressions; stamping tests |

Run the public Python suite, frontend tests/build, status Worker checks, and
private account service tests/typecheck/build. Run Python with the pilot lock;
run JavaScript with frozen Bun locks. Preserve the results in the release record.
The [launch-round report](../reports/launch-readiness.md) records this round's
results and limitations. Synthetic tests do not establish live OAuth settings,
production dependency versions or real-device behavior.

## Terms publication and activation

Publication steps 1–2 were completed with v0.18.0 on October 29, 2026; see the
[release evidence](../reports/launch-readiness.md#publication-round) for the
canonical URL, exact SHA-256 and hosted checks. Private-service activation and
hosted acceptance/revision checks in steps 3–7 remain pending. Keep the published
revision immutable; activation does not require publishing it again.

1. The owner approved [version 2026-10-29](../terms/2026-10-29.md), effective
   October 29, 2026, including Nerdy Matters LLC, nerd@nerdymatters.com,
   Massachusetts, USA, and the freemium/multiple-tier model. The approved
   document is bundled as an independent static HTML page under
   `/legal/terms/2026-10-29.html`. Prices, tier limits and paid purchase terms
   remain separate decisions. Owner approval is recorded; no claim of outside
   legal review is made.
2. Publish an immutable HTTPS document. Download the served bytes and record
   their SHA-256 alongside the version and URL. Keep every accepted revision
   available; never overwrite an old revision in place.
3. With registration closed, apply the account service's additive Terms migration.
   It creates `terms_acceptances` and leaves accounts, profile data and sessions
   unchanged. There is no backfill: existing activity cannot establish consent.
4. Deploy the compatible account API and frontend. Configure `TERMS_VERSION`,
   `TERMS_SHA256` and `TERMS_URL` together on the private account service only
   after the final document is available, in one deployment rather than three
   sequential configuration writes. Incomplete configuration blocks signup and
   profile/acceptance writes and fails health checks. Existing accounts can still
   sign in to the unavailable-Terms screen and sign out or delete their account.
5. Verify a legacy account is gated, an unchecked or stale submission is refused,
   a repeated acceptance keeps its original server time, and accepting the
   current revision permits profile edits. Verify sign-out and confirmed deletion
   without acceptance, and that switching accounts discards a late response.
6. On a disposable preview deployment, activate a second revision. A previously
   accepted account must be gated again, and the first acceptance must remain in
   history. Confirm the displayed URL/version/hash match what the API accepts.
7. Verify the deployed gate and dependency inventory before a separately approved
   registration window. Confirm the configured cap and a working closure watcher.
   Registration requires both `REGISTRATION_OPEN=true` and published Terms.

The frontend tolerates an older API during the closed pilot. It cannot provide
server enforcement by itself. Do not open registration with an older backend.

## Rollback without losing activity

Record the deployed frontend/API revisions, dependency lock digests, active
Terms descriptor and a D1 recovery bookmark before changes. Keep private
identifiers and credentials in the private deployment record. For the resource
host, retain the current data directory, enrollment settings and export state.

For an application rollback, close registration first and verify the public
configuration reports it closed. Roll back code or rebuild the frontend without
`VITE_POSTERN_ACCOUNTS_API` as appropriate. Preserve both databases and the new
acceptance table. Once Terms are activated, keep a gate-aware account API and
the active descriptor; reverting to pre-gate code would bypass the requirement
for legacy accounts even with new registration closed. A forward repair is
safer than such a rollback.

After rollback, verify existing account/profile IDs, publication choices,
acceptance timestamps, suspensions/deletions, training event IDs, quiz passes,
certificate numbers/names and enrollment metadata. Counts alone do not prove
identity attribution. Check sign-out/revocation as well as successful sign-in.

A database restore is a separate recovery operation, not a code rollback.
Rehearse on disposable data first. Restoring an older D1 point can resurrect
deleted accounts, publication or sessions and lose later acceptances. Keep
registration closed, invalidate restored sessions, quarantine restored profiles
and reconcile post-bookmark deletions/suspensions before serving traffic.
Retain the current Terms descriptor; a lost acceptance requires renewed explicit
acceptance, never inference from sign-in or profile activity.

For resource history, follow [recovery](recovery.md): import stable event IDs,
preserve timestamps and report missing history. A certificate is preserved only
if its first passing exam event reached a retained export tier. Do not rewind
the live training database to undo an account frontend change.

The earlier hosted recovery rehearsal used synthetic data and preserved a
post-snapshot deletion. No production restore was performed in this round.
