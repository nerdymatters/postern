# Ownership transfer (isolated)

Implemented October 30, 2026 for native PostgreSQL Posterns. Verified with
synthetic accounts in an owned local database; no hosted rollout or account-store
cutover. The default D1 Worker and legacy SQLite resource app are unchanged.

## Use the browser flow

1. Open a Postern, then **Ownership transfer** (`/s/{uuid}/ownership`). A current
   owner chooses an existing active member, selects their own remaining role
   (**Administrator** or **Member**), and confirms the proposal. Use **Manage
   members** to match the immutable account ID. The list is paginated.
2. Tell the recipient to open **Ownership transfer** in that Postern. There is
   no email delivery, transfer secret or bearer link. The proposal belongs to
   the exact recipient membership, not an email, profile name or supplied actor ID.
3. The recipient reviews both accounts, the role changes and expiry, selects
   **Review acceptance**, checks the responsibility acknowledgement and confirms.
   Opening the page or reviewing it never accepts. The browser offers seven days;
   the API allows one minute through seven days using server time.
4. Acceptance changes this space's responsible owner to the recipient's person
   owner record, promotes the recipient to owner, and changes the proposer to the
   selected role. Other owner memberships remain owners. The former owner may
   then leave through **Leave Postern**; the last-owner protection still applies.

Any current owner can cancel a pending proposal; only its exact recipient can
accept or decline. One live proposal is allowed per space. Ordinary members see
only their own incoming proposal; administrators have no transfer authority or
access to other people's proposals. An owner role, including a co-owner, may
initiate a transfer. The responsible owner record does not independently grant
permissions. Organization ownership selection and operator recovery of suspended
spaces remain separate work.

Incoming ownership does not grant creation approval or consume a new creation.
The original creator's lifetime creation count remains unchanged. These limits
are creation controls, not a cap on all spaces owned or a freemium entitlement.

## Account API

All routes use the live native account credential (`pa_…`), current Terms,
rate limiting and `Cache-Control: no-store`. The resource credential (`ps_…`)
cannot call them. Runtime account roles cannot read or write transfer tables.

| Method and path | Body / result |
| --- | --- |
| `GET /v1/spaces/{space}/ownership` | Own title, membership, role and policy revision; one visible pending transfer or `null` |
| `POST /v1/spaces/{space}/ownership/propose` | `operation_id`, `recipient_membership_id`, `former_role` (`admin` or `member`), `expires_in_seconds` (60–604800), `expected_revision` |
| `POST /v1/spaces/{space}/ownership/{transfer}/accept` | `operation_id`; only the addressed active membership |
| `POST /v1/spaces/{space}/ownership/{transfer}/decline` | `operation_id`; only the addressed active membership |
| `POST /v1/spaces/{space}/ownership/{transfer}/cancel` | `operation_id`; current owner |

Mutation responses contain only `transfer_id` and `state`. Status includes
the two immutable account references, remaining proposer role, expiry and
caller-specific controls. It exposes no private email or profile directory.
Missing/inaccessible transfers return the same 404. Stale proposal revisions
and conflicting pending proposals return 409; extra fields and invalid values
return 422. A failed disclosure audit returns no transfer metadata.

## Atomicity, revocation and retry

Core `006_ownership_transfer.sql` follows unchanged migrations `001`–`005`.
Private `006_ownership_transfer.sql` follows private `005` and core `006` and
grants only narrow account wrappers. Apply through the existing migration
rehearsal. New functions use fixed search paths, capability grants and forced
row-level security; internal helpers have no runtime execute grant.

Proposal and decision transactions lock affected principals before authority,
login and space, and recheck live authorization after waiting. The exclusive
space lock serializes proposal, acceptance, cancellation, membership mutation
and lifecycle changes. The deferred active-owner constraint remains in force.
Acceptance commits all of these together:

- Terminal transfer state, selected space's owner reference and both roles.
- Revocation of both participants' resource sessions in that space and pending
  invitations issued by either membership. New sessions resolve the new roles.
- One policy revision, immutable activity attribution, outbox event and receipt.

Shared owner records are never rewritten. Sibling spaces, global profiles,
existing membership IDs, resource grants, audience links and history are
preserved. An audit/outbox failure rolls back the entire operation, including a
new owner record and credential revocations.

Changing either participant's role/state, or the space's owner/state, permanently
invalidates its pending proposal. Expiry, inactive principals and an authority
generation change also prevent acceptance. Removing and readmitting the same
account does not revive a proposal. Policy changes unrelated to the agreed
roles do not themselves invalidate it; acceptance checks the immutable terms
and current authority rather than trusting the earlier preview.

Every mutation binds its operation ID to the full request, current account,
authority generation and exact acting membership. A lost response can be retried
using that same account credential even after space sessions were revoked.
Replaying a completed decision returns its receipt without changing later roles
or a later transfer. A different request under the same ID is refused. Logout,
account suspension, removal/readmission or authority rotation denies an old
receipt. A proposal retry returns its current terminal state rather than issuing
another proposal. The UI keeps uncertain requests fixed until retry or reload;
account and route changes discard late results.

## Rehearsal and remaining work

```sh
.venv/bin/python -m scripts.service_rehearsal \
  --accounts-source .context/m1/postern-accounts \
  --output .context/ownership-check
cd dashboard/web
bun run test -- --maxWorkers=1
```

The private account checkout is explicit and stays outside the public release.
Tests cover authority, exact membership binding, sibling-space preservation,
expiry/invalidation, duplicate/replayed decisions, concurrent acceptance versus
cancellation, queued acceptance versus removal, audit/outbox rollback, credential
separation, Terms, and departure after transfer. Browser checks cover proposal,
recipient consent, declining, cancellation, role results and narrow screens.

Hosted provisioning, notifications, resource/content migration and complete
private-object enforcement remain open. This verifies local metadata authority,
not a production deployment or a streaming revocation deadline.
