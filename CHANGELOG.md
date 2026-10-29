# Changelog

## 0.18.0 — Hosted Terms and launch readiness (2026-10-29)

- Read the approved hosted account [Terms of Service](https://postern.fyi/legal/terms/2026-10-29.html) without signing in or running JavaScript. The account interface supports explicit acceptance of each revision, recovery when Terms are unavailable, and account deletion without accepting. Registration and private-service activation remain separate launch gates.
- Read Thai explanations and recovery controls when access, staff-list verification, audit recording or enrollment photos are unavailable.
- View and print staff certificates even with no published training content. A delayed page refresh can no longer replace a newly saved certificate name.
- Restrict image decoding to supported formats and update security-sensitive dependencies. Quiz test selection is faster while preserving the same scoring assertions and timeouts.

## 0.17.1 — Long-running API reliability (2026-10-27)

- Keep the reading room accepting staff after days of uptime. The API now closes each read-only database connection when a read finishes, including refused reads, so open files no longer pile up until macOS's default limit of 256 refuses new connections. Upgrading needs only the code update and an API restart; data, configuration and dependencies are unchanged.

## 0.17.0 — Accounts and profiles (2026-10-27)

- With an independently configured account service, sign in with Google, reach private Posterns from My Posterns, and edit a profile with an optional public address. Profiles start unpublished; accounts do not grant access to private resources. See [account setup](docs/accounts.md).
- Keep account access available when a private Postern refuses access or is temporarily unavailable. Private entry verifies the same Google identity and retains existing invitation, agreement and enrollment requirements.
- Preserve old training links and browser Back navigation inside private Posterns. Sign out clears account and private state across open tabs and attempts to revoke their sessions.
- Update the pilot certificate bundle used by authenticated connections. Account registration and production rollout still require the documented deployment and compatibility checks; standalone installations remain the default.

## 0.16.0 — Public-release preparation

- Keep operator identity, privacy contact, intellectual-property notice, roster fallback and certificate identity in a private branding file.
- Supply generic installation examples, configurable service labels and independently configured monitoring URLs.
- Publish first-party code under MIT OR Apache-2.0, with separate brand rules, third-party notices, contribution sign-off guidance and private security reporting.
- Remove customer deployment records, historical design artifacts and developer-machine integrations from the public source tree.

This public source starts from the private pilot's v0.15.0 behavior: private training migration, bilingual assessments and certificates, optional selfie enrollment, recovery and bounded selfie retries. Private pilot history, releases and customer content are not included in the public repository.
