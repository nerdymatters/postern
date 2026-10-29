# Accounts and profiles implementation validation

Updated: Oct 28, 2026. M1 passed a controlled [production pilot](#production-pilot)
on a separately provisioned account service, with the account frontend on a
separate pilot origin and registration closed outside watched pilot windows. The
main site's frontend and public registration are unchanged. The account feature is opt-in through
`VITE_POSTERN_ACCOUNTS_API`, so a default deployment retains the standalone
reading room.

## Implemented

- An independent account API client, private My Posterns home, profile editor,
  explicit publication, public profile projection, account privacy and deletion.
- Separate account and space sessions, matched by verified issuer/subject.
  Account switching, abandoned sign-ins and late responses cannot reattach a
  previous identity's private state. Private sign-in failure preserves the
  Postern account; failed private bootstrap offers retry.
  Account sign-out revokes distinct cached and shared browser space sessions,
  including a session opened in another tab after the account tab loaded.
  Each receiving tab revokes its own cached bearer on account change. Sign out
  inside a Postern also ends the account session. Temporary private-host checks
  retain their credential for retry; resource denials do not force new sign-in.
- Legacy training deep links normalize into the private-space namespace while
  preserving query, fragment and browser history; Back stays in training.
- Own-session identity fields in the resource API, with a distinct local demo
  issuer. Membership, consent, enrollment and protected-media guards remain in
  place; training identity/history storage is unchanged.
- A separately maintained and privately backed-up Worker/D1 account service with stable Google
  identity, opaque hashed sessions, private-by-default profiles, atomic handle
  uniqueness, rate/account/session limits, suspension and deletion controls.
- Updated pilot lock to certifi 2026.10.23 for the auth-related security follow-up.

## Verification

| Check | Evidence and result |
|---|---|
| Python regression tests | 711 passed in a fresh full run with the editable installation in place. Two dependency deprecation warnings remain; no test failures. |
| Frontend baseline | Before the final release-review repairs, 281 tests passed, including account/session/profile isolation, legacy training-link regressions, recoverable transport/response errors and cross-tab server revocation; TypeScript and production build passed. |
| Final source review checks | 293 frontend tests and the production build passed on macOS and in an isolated Linux ARM64 container. A fresh macOS Python run passed 711 tests. New cases cover each receiving tab's private-session revocation, account-wide Sign out, retry after transient private-host failure, resource-level refusals, bounded startup requests, owner navigation and Unicode bio limits. These are source and fixture checks, not a new production pilot. |
| Private account service | 28 tests passed using real SQLite transactions and locally signed JWTs; typecheck and Worker dry run passed. |
| Local Worker/D1 | Actual HTTP checks covered schema health, invalid Google credentials, synthetic stored sessions, unpublished/published profile behavior, update, deletion/cascade and origin refusal. Synthetic fixtures were removed. |
| Hosted preview | An isolated frontend and account Worker/D1 deployment passed HTML/bundle configuration, health, initially closed-registration, unauthorized-session and missing-profile checks. With preview registration enabled for the controlled pilot, real Google sign-in, account home, unpublished profile loading, reload persistence, sign-out and a subsequent profile route requiring login passed on the hosted frontend. No profile was edited or published. |
| Real Google pilot | The existing web OAuth client was configured and re-opened to verify both test origins. The first identity was denied by an empty synthetic roster, admitted to the agreement gate, then revoked while retaining its unpublished account. A second authorized identity subsequently completed account sign-in. A mismatched account/space identity was refused; each matching identity reached the unchecked agreement gate. Signing out in another tab cleared the first tab's private state, and the replacement account required fresh space authentication. Both profiles remained unpublished. The pilot ended signed out with the synthetic roster empty and local servers stopped. This does not verify live production-member access or training activity after agreement acceptance. |
| Cross-tab sign-out repair | A read-only session audit exposed a space bearer left active after browser sign-out because the account tab cached an older token. Two regression cases failed before the fix; those and a deduplication control passed afterward. A real Google replay then revoked the new space session through explicit sign-out while its invitation remained active. The final read-only snapshot found two distinct unpublished accounts, matching account/space identities, no live account or space sessions and no accepted agreements. Network-failure recovery for revocation is not established by this successful-request check. |
| Production history compatibility | Candidate and deployed code returned exactly matching historical progress, complete staff dashboard and existing certificate results from one read-only production database snapshot. Schema and history-processing modules were unchanged. This did not exercise live HTTP authentication, media, enrollment changes or the running process. |
| Hosted recovery | A separate, unexposed D1 database containing only synthetic records passed export, bookmark restore, session purge, profile unpublication/suspension and reconciliation preserving a post-snapshot account deletion. The disposable database was deleted. Preview and production data were untouched. |
| Hosting limits | The authenticated operator dashboard confirmed the actual Workers plan and the matching preview database's Time Travel window. Account identifiers, applicable quotas and retention evidence are kept in the private deployment record. No plan, billing, database or deployment settings were changed. |
| Browser | Chromium exercised the account home, editing, visitor preview, publishing and anonymous profiles at desktop and mobile sizes. A separate keyboard-only flow verified Tab/Enter/Space navigation, editing, publishing, saving and visible focus indicators. No private API requests occurred during the account/profile flow. These checks used synthetic API fixtures. |
| Final UI recheck | Native-browser Google sign-in showed My Posterns and No Posterns yet. An intentionally unavailable private host offered retry and return while the account remained usable. The unpublished profile editor and preview headings were checked, including keyboard focus and a 375-pixel phone emulation. No profile was edited or published. Google COOP warnings and expected connection-refused messages were observed; this was not a clean-console or real-device certification. |
| Targeted dependency checks | Current OSV results contained no findings for the private account lockfile or certifi 2026.10.23. This does not close the other dependency/workflow findings in issue #1. |
| Source secret checks | Local Gitleaks scans of candidate source snapshots found no secrets. Runtime/operator configuration was excluded from publication candidates. |
| Earlier independent review | A scoped auth/privacy review found and resolved a private-bootstrap retry defect and clarified operator database targeting and restore quarantine. No supported P1/P2 finding remained in that scope; this is not a complete security assessment. |
| Release review | Native review of the fixes found no remaining blocker; a synthetic three-tab helper probe confirmed revocation of all three cached credentials. The external adversarial pass completed with a nonblocking stale-save-notice observation. Its separate design pass timed out, and structured-review formatting did not satisfy the automatic validator; those coverage limits remain explicit. |

The Python tests run on macOS with the locked dependencies. Reproduce the public
checks with an editable install, `.venv/bin/python -m pytest tests -q`, and the frontend's
`bun run test` / `bun run build`. The private account service owns its own tests
and operational runbook.

## Production pilot

On Oct 28, 2026 the project owner ran a controlled pilot against production with
two of their own Google accounts. The resource API ran v0.17.1, deployed through
the ordinary release process. Checks read the account and resource databases
read-only and recorded only counts, booleans, field names and status codes.

| Check | Evidence and result |
|---|---|
| Deployment | A separate production Worker and D1 database with a 10-account cap, the existing web OAuth client and CORS limited to one pilot frontend origin. That origin was also added to the resource API's allowed origins and to Google's authorized JavaScript origins. The main site kept the standalone frontend. Identifiers and configuration are kept in the private deployment record. |
| Registration window | Registration was closed by default. It was opened for the pilot under a watcher that closed it automatically within six minutes, once both pilot accounts existed; no other account was created. An earlier window closed at its deadline with no signups. Its first watcher stopped on a stalled database read and was restarted, so that window went unwatched for about six minutes. |
| Existing member | An administrator with training history signed in, entered the private space, accepted the agreements and completed selfie enrollment. New sessions and events attached to the same canonical identity as the earlier history, and the role was unchanged. |
| Revoked member | A member added for the test entered the private space. After the owner deleted its roster row, the next roster refresh removed the membership and revoked its private session. The account stayed active and signed in. |
| Outside account | With no membership left, the same account was refused by the private space's Google sign-in. It then set a handle in the profile editor and published its profile. The public profile response contained exactly the handle, display name, avatar URL, bio and website, without the account email, and the `/@handle` page loaded. After unpublishing, the profile response returned 404. |
| Operator controls | The operator tool suspended the account, which deleted its sessions and kept its profile unpublished; its Google sign-in was refused. Restoring it returned it to active with no sessions and no publication. Registration stayed closed. |
| Recovery | D1 Time Travel history is available for the production database. No live restore was performed; the synthetic hosted recovery drill above remains the restore evidence. |
| Health | Afterwards the resource API reported ready with its steady database-handle count and no new tracebacks. The account service's health and configuration endpoints reported ready with registration closed. |

Operating decisions are recorded in the private deployment record: the project
owner is the responsible operator; recovery relies on D1's seven-day Time Travel
with no additional exports; usage is reviewed after each change and on request,
with no automatic alerting.

Not covered: a never-invited Google account, registration while the roster
source is unavailable, the account frontend on the main site and load beyond the
pilot.

## Remaining before broader registration

- Resolve and verify the auth-relevant findings in the
  [security follow-up](https://github.com/nerdymatters/postern/issues/1) (M0-02).
- Write and publish Postern's Terms of Service, then build and verify how accounts
  accept them, including existing accounts and later revisions (M0-04).
- Choose when to enable the account frontend on the main site. Rebuilding without
  `VITE_POSTERN_ACCOUNTS_API` restores the standalone UI; it does not disable the
  account API or unpublish profiles.
- Review usage against the hosting plan's limits before raising the account cap.

M1-08 is complete. Public launch remains part of the M0 launch lane.
