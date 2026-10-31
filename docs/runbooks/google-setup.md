# Google setup

Use a dedicated Google Cloud project and operator account for this installation. Do not reuse another application's credential, project or private storage merely because it is on the same Mac. Follow your organization's account and key policies; do not weaken an organization policy to make setup easier.

## Sign-in and app-owned storage

Enable the Drive and Sheets APIs. Configure an OAuth consent screen with your operator identity, support contact, public website and `/privacy` URL. Review Google's current [OAuth production requirements](https://developers.google.com/identity/protocols/oauth2/production-readiness/policy-compliance) before publishing; consent/testing and verification requirements depend on the account and scopes.

Create a Web OAuth client for browser sign-in. Register the exact HTTPS web origin and any explicitly used localhost development origins. Put its client ID in `google_client_id`. Keep the public page's `strict-origin-when-cross-origin` referrer policy; the app receives an ID token directly.

Create a Desktop OAuth client for the host's installed-app flow. Save its downloaded JSON outside the checkout with mode `0600`. The host uses `drive.file`, limited to files accessible through the app's grant. Browser sign-in does not authorize host storage access.

## Roster reader

The manually maintained roster Sheet is separate from app-created storage. Create a dedicated service account with no project roles. Where your organization's policy permits JSON keys, save its key outside the checkout with mode `0600`; share only the roster Sheet with that account as Viewer. Set `allowlist_credentials` and `allowlist_sheet_id` in private `config.json`. The server requests read-only Sheets access.

The header row needs `Email`; optional `Name` feeds the suggested certificate name,
and optional `Role` controls access.

Use a person's full name in `Name`, without job titles or notes such as `Nok (Server)`.
Until the person saves a certificate name, Postern compares the current Sheet name
with the Google name recorded on their first passing Final Exam attempt. It prefers
a plain name, then more words, then more words written as names; the Sheet wins a tie.
A Sheet name containing a note or other non-name text is used only when no Google
name is known.
Saving **Keep this name** or **Change name** takes precedence over later Sheet edits.
Profile display names are separate. See [certificate names and printing](../../dashboard/web/README.md#badges-and-certificate)
for the selection, length and recovery rules.

The `Role` values are:

| Role | Chapter quizzes | Administration |
| --- | --- | --- |
| Blank, Staff, Reader | Required in sequence | No |
| Manager | Optional | No |
| C-Suite, CSuite, C-Level | Optional | No |
| Admin | Optional | Yes |

Unknown roles, conflicting roles for the same canonical address or malformed accounts reject the whole refresh. The previous roster remains usable only within its freshness window. Administrators are subject to the same membership/freshness checks. The final exam has its own prerequisites: only Admin may bypass Chapter 9.

For several groups, set exact ordered worksheet names:

```json
"allowlist_tabs": ["Head Office", "Branch A", "Branch B"]
```

Each sheet uses `A:Z` with the same headers. An empty list retains the single `allowlist_range`; its display group comes from `branding.json`. Explicit worksheet names remain authoritative. Shared accounts keep one identity and history across groups. Removing the last membership revokes access. Blank-email contact rows in multi-sheet rosters are reported but do not grant sign-in. Before changing sources, compare memberships and roles with the old roster; never infer a role from a job title.

## Bootstrap app storage

Prepare private configuration from [the example](../../ops/postern/config.example.json), then run:

```sh
.venv/bin/python -m postern.bootstrap --config ~/.config/postern/config.json \
  --client-secrets /path/to/private/desktop-client.json
```

Complete the browser authorization as the intended host account. Bootstrap creates app-owned storage/catalog resources, records their IDs privately and checks the roster. It is safe to rerun for those resources. It does not create or populate the roster, training banks or branding file.

Keep browser client IDs in the public app; keep host tokens, service-account keys and downloaded client-secret files on the server. Store no credentials in Git, release bundles or frontend environment variables. Continue with [Mac installation](pilot.md).
