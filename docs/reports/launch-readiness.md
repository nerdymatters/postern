# Launch-readiness implementation — October 29, 2026

Baseline: public main `83453b9`. This round prepares changes on a feature branch;
it does not deploy them or open registration.

## Changes

- Certificate holders remain visible in Staff progress with zero published
  training items. A shared response generation prevents an older full load from
  overwriting a just-saved certificate name or its print copy.
- Quiz selections use their unique labels instead of repeatedly computing
  accessible names/styles for every radio. Assertions and timeouts are unchanged.
- Pillow is updated to 13.0.0 and image formats are restricted before decoder
  dispatch. The pilot OAuth client chain and affected frontend/status dependencies
  are refreshed. The day-one harness has a fully pinned dependency set generated
  from `requirements.in`, constrained to the tested pilot versions.
- CI declares read-only contents permission, disables checkout credential
  persistence and pins the seven action references to full commit SHAs resolved
  from their existing major-version tags on October 29.
- The [approved Terms](../terms/2026-10-29.md) and versioned account acceptance flow
  cover new accounts, existing accounts and revisions. The private account
  service contains the migration and server enforcement. No pricing or billing
  implementation is included.

## Verification

The results in this section and the flake/security evidence below describe the
earlier implementation round. They are historical evidence, not final checks
of the publication candidate; final release verification is recorded separately.

The two certificate regression cases and both image-admission cases failed
before their fixes. Focused certificate checks (56 tests), image/stamping checks
(6) passed afterward. The full Python run passed 726 tests; its one failure was
the documentation-packaging check while new documents were still being added.
After adding them to the tracked file inventory, all 21 packaging checks passed.
No application failure remained. Runtime tests used the refreshed pilot lock.

The frontend suite passed 304 tests and its production build passed. An initial
suite run exposed an unflushed mocked sign-in effect; the test now awaits React's
async `act` before querying the mock button, without changing deadlines. Private
account tests passed 36 cases plus typecheck and a Worker build dry run. Status
checks passed 14 cases and the Node 24 Wrangler build dry run. A local D1 rehearsal
applied the two migrations around an existing synthetic account/session and
verified exact preservation plus an empty acceptance table. Production was not
accessed by that rehearsal.

The native adversarial pass found that the signup checkbox also prevented an
existing signed-out user from reaching deletion during open registration. Google
sign-in now remains available without acceptance, while the server still refuses
unchecked new-account creation. The targeted regression fails with the old
condition reintroduced and passes after the fix. The source re-review found no
remaining concrete issues; its test/fixture inspection was summary-only.

Outside review also caught the difference between an available deletion API and
a usable deletion screen. Invalid Terms configuration now returns an explicit
`terms_unavailable` state through config and existing-account sign-in/session,
allowing the user to reach retry, sign-out and deletion. Signup, profile writes
and acceptance stay blocked. A signup revision retains an explanation when
resetting the acceptance choice. Rejected acceptance/profile requests also count
toward their attempt limits. The final native source re-review found no remaining
concrete issue. Outside reviews covered the earlier revisions; the final recovery
change was verified by tests and the native review, without another outside pass.
A suggested
HEIF suffix regression was disproved by executing both isolated converters:
both reject HEIF bytes named `.jpg`. Parser failures remain masked by the parent
converter's generic error. Explicitly submitting a stale acceptance remains a
409 by contract; unchecked existing-account sign-in is available.

## Flake evidence

On unchanged main, the quiz file passed 10/10 isolated runs (15.68–19.51 seconds
per run). With label queries it passed 10/10 (7.38–8.73 seconds), using the same
initial dependency installation. No timeout, retry, assertion or skip was added.
A diagnostic adding four milliseconds to each computed-style lookup caused
both original journeys to time out; both passed with the query change. A
control on the refreshed dependencies also failed with the original query,
separating the query fix from the dependency upgrade.

An intentional incorrect-score mutation failed at the intended score assertion
in 10/10 runs before and 10/10 after the query change: measured
`P(fail | that regression) = 1.0` in both samples. This is evidence for the tested
regression, not a claim about every possible defect.

All six available green main runs were inspected. Quiz-file durations ranged
from 9.031 to 18.482 seconds; the oldest available run (`4a0591d`, Oct 26) already
took 17.398 seconds. The latest (`83453b9`) took 13.424 seconds. These logs do not
identify an earlier onset commit. There are no CI runs for this unpublished
branch. The routing is
**fix in this branch**; the underlying slow queries already existed on main.

## Security scope and remaining release checks

The managed audit helper failed with `SNAPSHOT_RACE` before producing an audit
run or assessment. Separate local remediation checks use OSV-Scanner 2.7.0,
Gitleaks 8.30.1 and zizmor 1.31.1. These do not establish a completed managed
audit or runtime exploit assessment. Exact input digests and raw results are
retained in the workspace's private verification artifacts.

The final online OSV scan assessed 324 exact package occurrences across the
pilot lock (49), fully resolved day-one requirements (20), frontend Bun lock
(164) and status Bun lock (91), with zero affected matches. The pilot input was
a byte-identical copy named `requirements.txt`. `--no-resolve` checked the exact
pinned inputs; it did not invent additional versions from dependency ranges.
An earlier scan of the old day-one ranges found vulnerable inferred transitive
versions, which prompted the complete harness lock. The final inputs have no
unresolved ranges. Gitleaks found no secrets in the current public-source
snapshot; zizmor found no findings in both workflows in offline mode. GitHub
API-dependent workflow checks and a fresh history scan were not part of this
round. OAuthlib's original matches concerned authorization-server features,
while this project is an OAuth client; the updated Google OAuth stack passed
the sign-in/bootstrap coverage in the Python suite. No production TLS or OAuth
sign-in was exercised in this round.

The private account-service lock was checked separately: 99 exact package
occurrences, with no affected OSV matches. Its deployment configuration and
runtime credentials were not part of the public-source snapshot.

An additional offline compatibility check constructed a real Google OAuth
authorization URL and credentials using the updated client libraries. A
generated HEIF image also converted successfully to JPEG using Pillow 13 and
the restricted decoder list. These checks do not substitute for live Google
sign-in or the deployed native-tool inventory.

The owner approved Terms version `2026-10-29`, effective October 29, 2026.
The public page was subsequently published and verified in the publication round
below. Separate private-service activation and a hosted acceptance/revision check
remain release gates.
Registration remains closed. Installed production dependencies and live Google/image behavior must be
checked after release. Issue #1 stays open pending those dispositions and
deployment verification. See the [launch and rollback checklist](../runbooks/launch-readiness.md).

## Publication round

On October 29, 2026 the owner approved the Terms for publication. Version
`2026-10-29` is effective October 29, 2026. The standalone legal page has no
JavaScript or account API dependency. Its exact bytes and approved text are
checked against the acceptance descriptor. Publication is separate from
private-service activation; registration remains closed.

The P2 Thai refusal-copy TODO is implemented across server messages and all
four whole-screen states, including retry and sign-out controls. No access
policy or writer deadline changed.

Publication completed in v0.18.0 through
[PR #8](https://github.com/nerdymatters/postern/pull/8), merged October 29 at
15:43:25 UTC as `a1c3b52c2dfce623c56f742fdea12fed3473348e`. All six GitHub checks
passed on its head; CI recorded 727 Python, 314 frontend and 14 status tests.
The main and pilot frontend deployments reported READY for that merge.

Post-deploy checks at 15:45 UTC received HTTP 200 for the standalone Terms on
both public origins. The [canonical document](https://postern.fyi/legal/terms/2026-10-29.html)
matched SHA-256
`91e252f92e44eec5aeca199eba860935d328631737cc17a6c0e61d6a11c66a8f`.
Both responses had immutable caching, restrictive CSP, nosniff and frame denial.
Desktop and 375px mobile browser checks found no horizontal overflow or console
errors on the legal page. These are recorded release-time observations, not a
continuous health guarantee or validation of later uncommitted work.

The same release check confirmed registration closed and account acceptance
inactive. Private account-service activation, hosted acceptance/revision testing,
office Python runtime and status-worker rollout remain separate. No real Google
sign-in or account acceptance/deletion was performed by this publication check.
Security issue #1 remains open for its remaining dispositions and deployment
verification. Public Terms publication is complete; M0-04 as a whole is not.
