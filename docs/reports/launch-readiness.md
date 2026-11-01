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

## Security follow-up — November 1, 2026

The office installation was verified at v0.19.0 (`e4d3713`), with all 50 pilot
lock pins matching its installed Python environment and `pip check` passing.
This included certifi 2026.10.23, Pillow 13.0.0, pillow-heif 1.7.0,
google-auth 2.58.0, google-auth-oauthlib 1.5.0, OAuthlib 4.0.0 and
opentelemetry-api 1.45.1. The image restrictions and workflow hardening described
above were already released in v0.18.0.

The installed Requests stack fetched Google's OAuth certificate endpoint with
normal TLS verification and the installed certifi bundle. No process or launch
configuration override for Requests/cURL/OpenSSL certificate paths was present.
An existing staff account completed a fresh Google sign-in at the production
site, and the operator confirmed that **Continue to admin without a selfie**
opened admin successfully. This verifies sign-in/admin access; it does not claim
selfie enrollment or a reading flow that requires enrollment was exercised.
Consent settings were unchanged.

Synthetic images passed through the installed converter and its real macOS
sandbox: JPEG, PNG and HEIF converted successfully; GIF bytes named `.jpg` and
HEIF bytes named `.jpg` were rejected. No staff content was uploaded or altered.
This is compatibility/admission evidence, not exploitation of a parser advisory.

A fresh source/history scan at v0.19.0 found zero Gitleaks findings and zero
offline zizmor findings. The current OSV database no longer matched the original
76 advisory identities, but reported four newer pyasn1 0.6.4 decoder advisories:
[GHSA-2qm8-x737-gmw5](https://github.com/advisories/GHSA-2qm8-x737-gmw5),
[GHSA-9m75-vc3g-4x39](https://github.com/advisories/GHSA-9m75-vc3g-4x39),
[GHSA-fm9c-5rcf-2q5p](https://github.com/advisories/GHSA-fm9c-5rcf-2q5p) and
[GHSA-mm57-4v3p-rchx](https://github.com/advisories/GHSA-mm57-4v3p-rchx).
The v0.19.1 candidate pins [pyasn1 0.6.5](https://github.com/pyasn1/pyasn1/releases/tag/v0.6.5).
Its online OSV scan assessed 325 exact package occurrences with zero affected
matches: pilot 50, day-one 20, frontend 164 and status 91. All four lockfiles
were explicit inputs; the pilot copy named `requirements.txt` was byte-identical.
Scanning used no advisory suppressions or resolution of unpinned ranges.

Tools were Gitleaks 8.30.1, OSV-Scanner 2.7.0 (SCALIBR 0.5.3) and zizmor 1.31.1.
Exact commands, input digests, binary digests and raw results are retained in
private verification artifacts. Gitleaks covered the current public files and
public Git history; zizmor remained offline. Dependency compatibility checks
included valid DER decoding and rejection of truncated DER. Static inspection
found the application's Google public-key verification uses cryptography; the
optional `rsa` package required by its pyasn1-based backend was not installed.
This observation is not a claim that every transitive path is unreachable.

The status Worker still runs an earlier deployment, version
`f8918d43-744c-41fe-aacb-71a1b46bef6d`. Its retrieved JavaScript bundle contains
only first-party modules, with no external imports or CommonJS requires; its
SHA-256 is `7fe43cddf6899b99573bb7ab5917c483fe900364171573a4a69303527330bb58`.
The affected status packages are build tooling, not dependencies in that deployed
application bundle. The updated status lock passed all 14 tests and a Node 24
Wrangler dry build. This check does not imply a new status deployment.

The candidate suite exposed a separate test whose overlong assertion expiry was
computed during collection. In a long run it became valid before the request.
The test now computes that expiry at request time; the production 60-second
lifetime cap and refusal assertions are unchanged. Simulated collection aging
fails the old case and passes the corrected case; all 17 service API tests pass.

The new pyasn1 pin still requires release and office rollout at the time this
candidate record is written. Final commit-bound scanner results, full checks,
deployed versions and closure are recorded in
[issue #1](https://github.com/nerdymatters/postern/issues/1).
The assessment remains partial: no runtime exploit reproduction, complete native
dependency inventory, exhaustive transitive reachability or GitHub API-dependent
zizmor checks are claimed.
