# Accounts and profiles implementation validation

Date: 2026-10-26. M1 implementation is prepared; the live Google/member pilot and
production rollout remain pending. The account feature is opt-in through
`VITE_POSTERN_ACCOUNTS_API`, so a default deployment retains the standalone
reading room.

## Implemented

- An independent account API client, private My spaces home, profile editor,
  explicit publication, public profile projection, account privacy and deletion.
- Separate account and space sessions, matched by verified issuer/subject.
  Account switching, abandoned sign-ins and late responses cannot reattach a
  previous identity's private state. Private sign-in failure preserves the
  Postern account; failed private bootstrap offers retry.
- Own-session identity fields in the resource API, with a distinct local demo
  issuer. Membership, consent, enrollment and protected-media guards remain in
  place; training identity/history storage is unchanged.
- A separately maintained private Worker/D1 account service with stable Google
  identity, opaque hashed sessions, private-by-default profiles, atomic handle
  uniqueness, rate/account/session limits, suspension and deletion controls.
- Updated pilot lock to certifi 2026.10.23 for the auth-related security follow-up.

## Verification

| Check | Evidence and result |
|---|---|
| Python regression tests | 710 passed in the full 711-case run. The installer smoke initially failed because the fresh test environment lacked an editable install; after `pip install --no-deps -e .`, that exact test passed. No source fix was required for that setup failure. |
| Frontend | 270 tests passed, including 21 new account/session/profile cases; TypeScript and production build passed. |
| Private account service | 28 tests passed using real SQLite transactions and locally signed JWTs; typecheck and Worker dry run passed. |
| Local Worker/D1 | Actual HTTP checks covered schema health, invalid Google credentials, synthetic stored sessions, unpublished/published profile behavior, update, deletion/cascade and origin refusal. Synthetic fixtures were removed. |
| Browser | Chromium exercised My spaces, editing, visitor preview, publishing and anonymous profiles at desktop and mobile sizes. No private API requests occurred during the account/profile flow. These checks used synthetic API fixtures. |
| Targeted dependency checks | Current OSV results contained no findings for the private account lockfile or certifi 2026.10.23. This does not close the other dependency/workflow findings in issue #1. |
| Source secret checks | Local Gitleaks scans of candidate source snapshots found no secrets. Runtime/operator configuration was excluded from publication candidates. |
| Independent review | A scoped auth/privacy review found and resolved a private-bootstrap retry defect and clarified operator database targeting and restore quarantine. No supported P1/P2 finding remained in that scope; this is not a complete security assessment. |

The Python tests run on macOS with the locked dependencies. Reproduce the public
checks with an editable install, `python -m pytest tests -q`, and the frontend's
`bun run test` / `bun run build`. The private account service owns its own tests
and operational runbook.

## Remaining release evidence

- Configure the exact pilot frontend origin on the real Google web OAuth client.
- Run the admitted-user, outside-account and controlled-revoked-member pilot
  with real Google identities and the updated private-space API.
- Verify the hosted account database recovery procedure on isolated data and
  record the actual operating/backup limits before production registration.
- Deploy the updated resource API and certifi dependency through the ordinary
  release process; the implementation test environment is not production.
- Confirm production settings, recovery and training compatibility, then enable
  the account frontend and registration deliberately.

These remaining checks are M1-08 and the M0 launch lane. Do not mark the whole
milestone complete from fixture-based tests or a preview alone.
