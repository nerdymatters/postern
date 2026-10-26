# Account and ingress isolation

Give Postern its own Google project, OAuth clients, roster reader, app-owned storage and public ingress. Sharing a Mac with another application does not justify sharing credentials or changing that application's routes. Keep exact project IDs, service-account identities, URLs, socket locations and migration inventories in private operator records.

Use separate state directories and service labels for independent tunnel daemons. A local Caddy listener and the API must agree on the client-address trust boundary; PROXY protocol is trustworthy only behind the protected ingress that emits it. Test public TLS, client addresses and health before moving the frontend's API origin. Existing browser tabs can retain old endpoints, so plan any transition deliberately.

## Moving cloud accounts

A storage migration is separate from a release. Provider account types and ownership-transfer rules differ; inspect them before acting. If copying is required, retain originals and verify destination ownership, checksums, catalog values and rollback access. Preserve application IDs and current SQLite history. A host's `drive.file` grant is distinct from browser access; a file shared in the browser is not necessarily accessible to that app grant.

Postern recovery recognizes migrated portrait mappings only with the source identifier and matching SHA-256 metadata. Do not rewrite sealed audit events to change cloud IDs. Account and storage references must switch together while writers are stopped; never replay an old database over later staff activity.

Google IAM, OAuth support contacts, host tokens, roster access, watchdog mail and public ingress are separate controls. Verify each after a planned migration. Do not revoke the source account until the new installation and retained rollback path are verified.
