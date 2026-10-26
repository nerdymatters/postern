# Accounts and profiles

Accounts let a person maintain a Postern profile independently of membership in
a private space. A valid Google sign-in can create an account when registration
is open. Private resources still require admission by the resource server's
roster, followed by its existing agreements and enrollment requirements.

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
| `GET /v1/config` | None | `google_client_id`, `registration_open` |
| `POST /v1/auth/google` | `credential` from Google Identity Services | `token`, `account` |
| `GET /v1/session` | Account bearer token | `account` |
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

A private-space 401/403 or roster outage affects that space's experience.
Account logout and account switching clear both local sessions and protected
media/progress state. Late responses from a previous identity must not restore
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
for separately tracked dependency/workflow work.
