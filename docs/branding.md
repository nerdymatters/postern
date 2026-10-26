# Installation branding

Google mode requires an owner-only `<data_dir>/branding.json`. Demo mode can use neutral defaults. This file is separate from `config.json`, so older private pilot code can ignore it during rollback. It is not uploaded by the Google backup process.

```json
{
  "organization_name": "Example Learning Team",
  "privacy_contact_email": "privacy@example.invalid",
  "ip_notice": "Materials belong to their respective rights holders. Ask the administrator before copying or sharing them.",
  "default_group": "Staff",
  "certificate_prefix": "EX",
  "certificate_title": "Training Certificate"
}
```

Create the file with mode `0600`, owned by the account running Postern. Keep the data directory `0700`. Values are bounded plain text; the server validates email and certificate-prefix syntax and rejects unknown or malformed settings. Empty organization/contact values produce neutral wording and a direction to contact the installation administrator. An explicit `{}` chooses all generic defaults for a new Google installation.

The API exposes only the fields needed by the browser. The privacy page also uses `VITE_POSTERN_ORGANIZATION_NAME` and `VITE_POSTERN_PRIVACY_CONTACT_EMAIL` build-time fallbacks so operator information remains available during API outages. Keep those public values consistent with the private file. Never place credentials in a `VITE_` value.

## Existing installations

Before installing generic code, copy the exact former organization/contact, intellectual-property notice, default group, certificate prefix and PDF title into this file. Preserve the existing agreement versions when merely relocating unchanged text. If the meaning of an agreement changes, increment its corresponding version in `config.json` so readers review it again.

Certificate numbers are derived from the first passing exam event and configured prefix. **Changing the prefix changes old certificate numbers.** Preserve it for a migration; no event/database rewrite is needed. Certificate year and printed dates retain the existing America/New_York rule. Do not change timezone assumptions during this migration.

Back up branding alongside private training. Run preflight before restart. Rolling back to the older code leaves this file in place and preserves the current database, configuration and staff activity.
