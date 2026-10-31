# PostgreSQL schema and migration rehearsal

**October 29, 2026: implemented locally, not deployed.** The approved storage
architecture now has a versioned PostgreSQL schema, offline import tools and a
repeatable synthetic rehearsal. Default resource/account entrypoints still use SQLite and D1. An
[opt-in service integration](service-integration.md) now exercises native PostgreSQL
adapters in an isolated environment. No live account, roster, file or session
was moved. M2-06 remains open through hosted integration and verified cutover.

## Run the isolated rehearsal

Install the project's `test,postgres` extras and put PostgreSQL 17+ CLI tools on
`PATH`. The same native prerequisite is included in the macOS CI job. From the
repository root:

```sh
.venv/bin/python -m scripts.migration_rehearsal --output .context/my-schema-rehearsal
```

The output path must be new. The command runs the real PostgreSQL tests with
synthetic fixtures, retains JSON/JUnit results, logs and fixture bundles, and
removes each owned server directory on exit. It clears inherited `PG*` settings,
uses a temporary Unix socket accessible only to the current OS user, and opens no
TCP listener. It does not accept an existing server address or production input.
PostgreSQL is required: the tests do not substitute SQLite or silently skip.

The [runner](../../scripts/migration_rehearsal.py) records hashes of its consumed
schema, migration code and tests. The [tests](../../tests/storage) are the
executable specification for this round. They are separate from the earlier
[100-space storage experiment](../designs/storage.md#local-experiment-and-reproduction),
which measured query behavior on a smaller, nonproduction schema. This rehearsal
uses small comprehensive fixtures; it is not a throughput benchmark at that scale.

## Schema and privileges

[`001_spaces.sql`](../../postern/sql/001_spaces.sql) creates 20 tables in the
`postern` schema. [`002_service_sessions.sql`](../../postern/sql/002_service_sessions.sql)
adds account-login references, membership-bound resource sessions and the scoped
runtime functions. [`apply_schema`](../../postern/pg.py) applies pending versions
in one transaction, serializes installers and verifies each recorded SHA-256. Reapplying identical bytes is safe;
a changed applied migration is refused. Future deployed changes need a new
migration version, not edits to version 1. This first version has not been deployed.

| Records | Purpose and guard |
| --- | --- |
| Authority and principals | Quarantine/generation and namespaced immutable account references; deleted identities cannot reactivate |
| Owners, spaces, memberships | Responsible ownership, scoped roles, one active membership per account/space; deferred active-owner constraint; revoked membership IDs never reactivate |
| Invitations and audiences | Bounded fields, hashed invitation secret, same-space inviter/accepted-member/group references; acceptance workflow remains M2-02 |
| Resources, child relationships, grants | Immutable resource scope, explicit actions/publication, same-space foreign keys; no implicit child inheritance |
| Objects | Immutable resource/revision/hash/length manifest and generated private object key |
| Activity, operations and outbox | Append-only audit, actor-bound idempotency and transactional delivery intent; dispatch/lease worker remains to be implemented |
| Imports, legacy rows, maps and bindings | One checkpoint per source installation and target space; exact archived records, stable resource mapping and explicit identity-evidence references |

The roles are `NOLOGIN` capabilities. Deployment must provision separate login
roles with only the needed capabilities; the SQL checks reused roles for unsafe
login/superuser/RLS-bypass attributes and escalation paths. The migration operator
needs role/schema creation rights. Existing account/profile/session schemas are
not part of the public resource migration.

- `postern_owner` owns schema internals and fixed-search-path helper functions.
  It is not a runtime login. Every table enables and forces RLS; its explicit
  internal policy permits the restricted definer functions to operate.
- `postern_reader` can select only scoped space/membership/activity records and
  catalog columns. It cannot read resource bindings, object keys, invitation
  secrets, raw legacy history or managed profiles/sessions.
- `postern_writer` inherits reader access and can execute membership revocation.
  It cannot directly write tables or change an owner role as an administrator.
- `postern_identity_writer` can suspend/delete/resume an identity through the
  lifecycle interface. Account resumption never restores revoked memberships.
  Account-service changes record a service action and affected principal, without
  falsely attributing an operator's suspension to the affected person.

The trusted server supplies transaction-local `postern.actor_id`,
`postern.space_id` and `postern.generation` after authenticating its caller. These
SQL settings are **not credentials**; do not give a browser or agent a database
login. Unset/stale context denies reads. The opt-in `postern_resource_service`
role cannot set a context and read tables: only its audited, session-authenticating
functions are exposed. Native pool and handoff checks now cover metadata access;
legacy content gates remain application work.
`can_resource` is only the catalog-policy decision; it does not satisfy Terms,
enrollment, training, media or disclosure-audit requirements by itself. It permits
only `discover`/`view` in M2, even though the schema reserves later action names.

Membership revocation locks account rows before space rows, rechecks authority
after waiting, and commits revoked grants/groups, revision, operation result,
audit and outbox together. An outbox failure rolls everything back. Competing
owner removals cannot remove the last owner. Account suspension/deletion revokes
memberships across spaces and suspends an orphaned active space; other spaces and
historical attribution survive. Ownership creation/transfer and invitation APIs
are not exposed by this migration.

## Legacy checkpoint contract

[`migration_bundle.py`](../../postern/migration_bundle.py) supplies offline Python
functions for a later controlled migration adapter. It has no production CLI.

1. `export_legacy(database, installation, objects, output)` reads a SQLite snapshot
   through a read-only connection. `installation` is a deliberately assigned UUID;
   `objects` maps each catalog SHA-256 to a previously obtained original file.
   It refuses an unexpected table set and verifies every copied catalog original's
   length and digest. It writes private files in a new bundle directory.
2. The bundle contains a versioned manifest, streamed `rows.jsonl` and hash-named
   originals. Each record has a canonical envelope with its original column
   values; event payload strings are preserved without normalization. The returned
   manifest digest must be retained through a trusted channel, not rediscovered
   from an untrusted replacement bundle.
3. `import_legacy(db, bundle, expected_digest, space, bindings, object_root)`
   requires a quarantined authority and an empty suspended destination space.
   It verifies the bundle, streams history into bounded COPY batches, creates
   deterministic resource/membership IDs, and commits the database changes as one
   transaction. Replays require the same checkpoint and binding evidence. A
   different checkpoint needs a fresh rehearsal/target; this is not delta replication.
4. `bindings` maps a legacy email to `{principal_id, evidence_ref}`. Supply only
   independently verified identity links and a nonsecret reference to their
   evidence. Email equality alone is insufficient. Unbound entries remain archived
   without membership; duplicate account bindings are rejected. Old Admin labels
   map to scoped administrators, never responsible owners. Bootstrap ownership
   explicitly before any later activation.
5. Published, prepared legacy resources receive explicit all-member grants in the
   imported space, preserving the old roster access model. Drafts and archives stay
   unavailable to ordinary members. Child records have their own grants. Ambiguous
   identities and incomplete preparation never become implicit access.

All persistent legacy tables are archived except reusable video tickets; session
rows omit `token_hash`. Those historic rows are not recreated as active sessions.
Agreement, enrollment, quiz, view and certificate records stay in the restricted
archive. The rehearsal verifies raw rows, quiz standing, certificate identity and
saved-name history using the real legacy recovery functions. The old URL/adapter
serving bridge still needs implementation; having a mapping is not live URL compatibility.

This local object adapter stages catalog originals using generated UUID/revision/
hash keys and create-only publication. It never overwrites a differing file.
Objects can remain private and unreferenced after SQL failure; no distributed
transaction or garbage-collection worker is claimed. The rehearsal does not fetch
Drive, replay live Sheets changes, restore enrollment image bytes, rebuild
protected derivatives or test an S3 provider. Preserve those source stores and
retention policies until their adapters and full inventories are verified.

## Managed account conversion

The private account checkout has a separate PostgreSQL migration preserving the
existing account IDs, issuer/subject, profile fields, case-insensitive handles,
session hashes, rate limits and exact Terms acceptance version/hash/timestamp.
Its lifecycle trigger joins suspension/deletion to the core policy transaction;
deleting an account removes its private sessions/acceptances while preserving
space history and suspending an orphaned space.

A separate local rehearsal populated SQLite using the actual private D1 migrations,
then compared every account-table field after PostgreSQL import. It checked that
legacy acceptance was not invented, handle collisions were rejected, a failed
delete rolled back both schemas, and resource credentials could not read account
records. The private schema/code and synthetic evidence remain outside this public
package. This is not a remote D1 export test or a deployed PostgreSQL account adapter.
Existing private-service activation and M0 launch gates remain unchanged.

## Restore and release gates

`restore_quarantined` creates a new database with connections disabled, revokes
public CONNECT, and only then permits the privileged restore connection. It
restores a logical dump, rotates generation and suspends spaces. Application
logins remain unable to connect. A failed restore stays closed for investigation.
It never grants CONNECT, reconciles history or reopens a space automatically.
Provision trusted cluster roles first; do not run arbitrary untrusted SQL dumps.

Before migration is ready for real data:

- Implement account and resource repository adapters against this schema, with
  pooled-connection context reset, required audit, service authentication, and
  existing agreement/enrollment/training behavior. Complete invitation, owner
  bootstrap/transfer and outbox processing with their own tests.
- Select provider/region/budget, recovery objectives and retention; require
  pgvector support for a later semantic-search evaluation, without enabling
  embeddings now. Measure the production schema at the selected workload.
- Rehearse real D1 export/validation and controlled source write handoff. For the
  legacy host, use a bounded final write freeze and full final checkpoint, or build
  and verify delta/reverse replay first. This importer alone does not promise
  zero-downtime cutover or rollback after destination writes begin.
- Verify complete Drive/Sheets/catalog/enrollment inventories, legacy URLs and
  content adapters. A retired enrollment record does not justify deleting its
  separately governed source bytes.
- Exercise database PITR plus independently restored private objects. Reconcile
  later revocations/deletions against surviving external evidence before allowing
  application connections and activating anything. If evidence is absent, remain
  closed. The local logical restore is not PITR or a hosted object-recovery drill.
- Run all M2-04/05 isolation and timed-media-revocation scenarios on the integrated
  services before a second owner's pilot. Keep M2-06 unchecked until its actual
  migration outcome is verified.
