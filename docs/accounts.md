# Accounts and profiles

Accounts let a person maintain a Postern profile independently of membership in
a private space. A valid Google sign-in can create an account when registration
is open. Private resources still require admission by the resource server's
roster, followed by its existing agreements and enrollment requirements.

The interface calls each private resource space a **Postern**. **Posterns** in
the navigation opens **My Posterns**, described as “Private spaces and resources
shared with you.” **Profile** remains separate. These labels do not change the
account/resource boundary or the existing routes.

## Deployment boundary

This repository supplies the account/profile interface in the web app and the
private resource API. Managed signup, account storage and operator account
administration live in a separate private service. An operator can connect an
implementation of the API contract below or keep the standalone reading room.

Set `VITE_POSTERN_ACCOUNTS_API` at frontend build time to the account API's HTTPS
base URL. Continue to configure `VITE_POSTERN_API` for the separate private
resource API. Neither setting is a secret; both are included in the browser
bundle. Do not put database credentials, operator credentials or Google client
secrets in `VITE_*` settings. Leave `VITE_POSTERN_ACCOUNTS_API` unset to retain the
standalone sign-in flow.

The account service must accept the frontend's exact origin and Google OAuth
client ID. That frontend origin must also be authorized in Google's client
configuration. A new deployment hostname is not automatically authorized.
Development HTTP origins are for loopback only; deployed traffic uses HTTPS.

## Account API contract

All responses use JSON. Authenticated requests carry an opaque account bearer
token. The account API must not accept a space token, and the resource API must
not accept an account token. Store only token hashes on the account server.
Account sessions expire and can be revoked. Account/identity/profile responses
and errors use `Cache-Control: no-store`.

| Method and path | Request | Response |
|---|---|---|
| `GET /v1/config` | None | `google_client_id`, `registration_open`, `terms` |
| `POST /v1/auth/google` | `credential` from Google Identity Services; current `terms` acceptance for signup | `token`, `account` |
| `GET /v1/session` | Account bearer token | `account` |
| `POST /v1/terms/accept` | Account bearer token; `accepted: true`, `version`, `sha256` | Updated `account` |
| `POST /v1/auth/signout` | Account bearer token | `signed_out: true` |
| `PATCH /v1/profile` | Account bearer token; `handle`, `display_name`, `bio`, `website`, `published` | Updated `account` |
| `DELETE /v1/account` | Account bearer token; `confirm: true` | `deleted: true` |
| `GET /v1/profiles/:handle` | None | `profile`, or indistinguishable 404 for unavailable profiles |

An authenticated account contains `id`, private `email`, `identity` and
`profile`. Its verified identity is `{issuer, subject}`. For Google the canonical
issuer is `https://accounts.google.com` and the subject is the verified `sub`
claim. A profile contains `handle` (initially null), `display_name`, `avatar_url`
(nullable), `bio`, `website` and `published`.

The public response contains only `handle`, `display_name`, `avatar_url`, `bio`
and `website`. Missing, unpublished, suspended and deleted profiles return the
same 404. Neither public responses nor profile metadata may reveal email,
provider subject, private memberships, enrollment images or training activity.

Errors use `{error, message}` with an appropriate HTTP status: 401 for a missing
or expired session, 403 for disabled accounts or closed new registration, 409 for
a conflicting handle, 422 for invalid profile data and 429 for rate limits.
Opening registration must never weaken private-space admission.

## Account Terms

The current Terms descriptor is `{version, sha256, url}`: an immutable version,
SHA-256 of the published document and its HTTPS URL. Configuration returns
`terms: null` until publication and activation. An account response adds
`terms: {...descriptor, required, accepted_at}`; `accepted_at` is a server Unix
timestamp or null. Older services may omit the field during a closed pilot;
this compatibility path is not sufficient for opening registration.

Malformed active configuration returns `terms_unavailable: true` on config and
authenticated account responses, with `terms: null` and registration closed.
Existing users can still authenticate and reach a recovery screen with retry,
sign-out and confirmed deletion. It offers no acceptance or profile/space-entry
controls. Health, signup, profile writes and acceptance fail closed until the
operator repairs the configuration. This differs from a deliberately inactive
closed pilot, where Terms are null and `terms_unavailable` is false.

New accounts require `terms: {accepted: true, version, sha256}` in the verified
Google sign-in request. The service atomically creates the account and its
acceptance record. Registration fails closed without configured Terms. An
unchecked box, an old version or a different document hash cannot create an
account. Account creation alone does not select or authorize a paid tier.

Existing accounts can sign in while registration is closed. If the current
version/hash has no acceptance record, the account screen requires an unchecked
explicit acceptance before profile editing or entering a private space through
that screen. The account API separately refuses profile writes with HTTP 428
`terms_required`; hiding a button is insufficient. Sign-out, account deletion,
privacy information and public profile reads remain available. The standalone
resource host retains its own membership, agreements and enrollment policies.

Accepting an old descriptor returns HTTP 409 `terms_changed`; the interface
offers to reload and requires a fresh unchecked choice. A repeated acceptance
is idempotent and keeps the original server timestamp. Revisions preserve prior
acceptance rows. Deleting an account removes its acceptance records alongside
its sessions and profile. An acceptance is associated with the authenticated
account, never an email or account ID supplied by the request body.

The owner approved [Terms version 2026-10-29](terms/2026-10-29.md), effective
October 29, 2026, using Nerdy Matters LLC, nerd@nerdymatters.com, and
Massachusetts, USA. Freemium is one planned tier; prices, limits and paid
purchase terms remain separate. The public page was published and verified with
v0.18.0 on October 29 ([publication evidence](reports/launch-readiness.md#publication-round)).
Private-service activation and hosted acceptance/revision verification remain
pending, with registration closed. See the [launch checklist](runbooks/launch-readiness.md) for
publication, activation and rollback checks.

## Profile behavior

New profiles start unpublished. Google name/photo, when present, seed the
profile; later sign-ins do not overwrite the person's edits. Handles are
case-insensitive, unique, 3–30 ASCII letters/digits with internal hyphens, and
system names are reserved. Names and bios are bounded plain text. Website links
must use HTTPS without embedded credentials. Avatar import is constrained to
supported Google picture hosts; there is no arbitrary upload or server fetch.

Publishing exposes the selected profile fields to anyone with the link. Taking
the profile private removes that public response. Enrollment selfies and
certificate names belong to the private space and do not become profile data.
Account deletion removes account-owned profile/identity/session data; it does
not delete another organization's training, certificate or audit records.

## Session isolation

The account-enabled browser stores its account token separately from the legacy
space token. No Google ID token is kept as a long-lived session credential.
Tokens in browser storage are exposed to successful same-origin script
injection; render profile text safely, constrain URLs, keep script dependencies
controlled and operate all endpoints over HTTPS.

Account pages and public profiles must render without requesting private posts
or requiring the private host's availability. Entering a private space verifies
that account and space sessions identify the same Google subject. Email or name
equality is insufficient. Session responses lacking a verified identity fail
closed; local demo identities cannot impersonate Google identities.

A private-space refusal or roster outage affects that space's experience.
Temporary private-host failures retain the credential and offer a retry without
opening protected content. Resource-level denials preserve the session; a failed
session authorization check requires fresh private sign-in.
Account logout (including Sign out inside a Postern) and account switching clear
both local sessions and protected media/progress state. Each open tab also
attempts to revoke its own cached private credential when the account changes.
Server revocation is best effort when the network is unavailable. Late responses from a previous identity must not restore
the prior user's session or content.

## Launch and recovery

1. Deploy and verify the resource API's own-session identity fields and the
   auth-related certificate-bundle dependency update first. Preserve its roster,
   training, data directory and existing access requirements.
2. Provision an isolated account service/database. Configure allowed origins,
   Google client ID, registration/account limits and operator suspension,
   unpublication and deletion controls. Keep preview and production data apart.
3. Back up or record a recovery point before schema changes and rehearse recovery
   on disposable preview data. An application rollback must not rewind live
   account or training activity accidentally.
4. Build the account-enabled frontend for an explicitly authorized pilot origin.
   Verify an admitted Google user, an outside Google user and controlled
   membership revocation. Synthetic fixtures alone do not prove live Google
   configuration or production membership behavior.
5. Test profile publication/unpublication, two-account switching, direct private
   API/media denial, account-service and roster outages, and existing training
   progress/certificates. Confirm account operator controls before broad signup.
6. Enable the new frontend only after those checks. To restore the standalone
   UI, rebuild without `VITE_POSTERN_ACCOUNTS_API`; retain account data and
   independently stop registration if needed. Do not restore an old database
   merely to roll back the interface.

See [M1 in the roadmap](../ROADMAP.md#m1-accounts-and-profiles) for the release
criteria and [the security follow-up](https://github.com/nerdymatters/postern/issues/1)
for separately tracked dependency/workflow work. Opening registration also waits
for [M0 launch preparation](../ROADMAP.md#m0-launch-preparation), including
published Terms of Service with a built and verified acceptance flow.

The next space model is defined in the [M2 private-space contract](private-spaces.md).
The default account bridge still checks the single legacy host. An explicitly
enabled [native onboarding flow](runbooks/space-onboarding.md) now lists real
memberships, creates approved spaces and previews/accepts account-bound invitations
against the isolated PostgreSQL services. This has not been deployed; it does not
move legacy content or change its access requirements.
