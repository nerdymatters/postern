# Private standby backups and deployment verification

This runbook describes the operator workflow rehearsed with a macOS primary and
a private Debian VM on a separate Linux host. It supplements the
[Linux installation guide](linux.md) and [cloud recovery guide](recovery.md).
The dated results below are a sanitized summary of private operator evidence;
they are not continuous monitoring or a production-readiness certification.
The operator-specific transfer, validation and archive scripts are not included
in this repository. This is a checklist for implementing and reviewing those
controls, not an installer for them.

## Roles and boundaries

Keep exactly one production writer. The primary serves readers and owns the
authoritative event history. The backup host receives encrypted, one-way
snapshots, stored outside the test VM. Its working test installation is a
separate copy: receiving a newer backup does not refresh that installation.
Neither copy is live replication or automatic failover.

Restrict the test installation to a private network and an explicit operator
access policy. A copy of production data remains confidential even behind a
private tunnel. Keep its configuration, credentials, ports and data directory
separate from production. Disable production exports, notifications and other
external writers during a rehearsal. Use reserved synthetic identities for
test activity, and never promote the working test database after adding it.

## Make and verify a backup

1. Check source identity, free disk space and the configured resource reserve
   before staging. Use SQLite's online backup operation for a live database;
   copying only its main file can miss acknowledged writes in the WAL. Preserve
   any sidecars belonging to a closed archived fixture until their role is
   understood. A database checkpoint alone is not an atomic snapshot of media,
   training and configuration: inventory and verify those assets separately,
   and reject or retry a set that changed while it was being collected.
2. Record the release, configuration dependencies and a manifest of expected
   files, sizes and hashes. Include required training, branding and media;
   retain configuration and credentials in an appropriately protected recovery
   channel. Google audit exports do not reconstruct all private assets. Use
   encryption in transit and at rest, with recovery secrets available separately
   from the machine whose loss is being rehearsed.
3. Transfer to a restricted receiver. Reject unexpected paths, symlinks,
   incomplete manifests, excess size and digest mismatches. Keep partial work
   distinguishable from accepted snapshots. A successful transfer is not yet a
   successful restore.
4. Restore the complete snapshot into a new, owned scratch directory. Verify
   repository integrity, exact file membership and every expected hash. Check
   database integrity and the retained source history. Keep the original backup
   immutable; make any diagnostic changes only in the restored copy.
5. Start a separate validation copy with a fresh private config. A raw SQLite
   snapshot can contain live sessions: invalidate copied sessions and tokens
   before serving it, verify that old credentials fail, and admit only the
   intended test identities. Exercise authentication, authorized media access,
   denied access and retained history. Stop the owned process and verify cleanup
   before recording startup validation as successful.

For cloud reconstruction, use `postern.recover` and its
[empty-directory procedure](recovery.md#restore-into-an-empty-directory).
It has different semantics from raw snapshot restoration: it rebuilds from
confirmed cloud records, omits sessions and preserves a recovery gate and any
incomplete-history flag. Do not treat a raw copy as having those protections
automatically.

## Run recurring validation safely

Serialize transfer/restore jobs that share staging or storage. Bound their
runtime, memory and disk consumption; refuse before starting the worker when
capacity is insufficient. Privileged service launchers and every parent of
their executable path must be administrator-controlled. Run application and
converter work as the unprivileged service owner. Restrict cleanup to resources
created by the current run, and verify that the serving process, configuration
and original history remain unchanged afterward.

Record three separate outcomes: snapshot accepted, full restore verified, and
isolated application startup verified. Monitor the age of the latest successful
result at each stage against the operator's recovery-point objective (maximum
acceptable data loss). A timer reporting no work is useful only when the newest
snapshot is already validated. Preserve failures and refusals alongside later
successes; do not overwrite them with a green summary.

A manual run proves the commands worked at that time. To validate the schedule,
observe an elapsed sender cycle followed by restore and startup validation.
Check sleeping-source behavior, service-user lifetime, reboot and power return
separately. A guest restart does not prove that its physical host can unlock
its disk or start the user manager unattended.

## Retain the operator recovery kit

Keep a reviewed allowlist of deployment scripts, unit definitions, sanitized
configuration templates, runbooks and verification receipts in an encrypted
operator archive. Exclude staff data, raw logs, caches, dependencies and secrets
from that kit; protect the actual data and credentials through their separate
backup channels. Retain an independently accessible recovery password/key and
instructions for locating each channel after primary-host loss.

Restore the kit completely, then compare exact membership and hashes before
calling it recoverable. Historical commands are evidence, not instructions to
replay against a new machine: recheck paths, identity, ownership and capacity.
Before reclaiming source disk space, independently restore and verify the exact
allowlisted files being removed, confirm they are closed historical artifacts,
and retain the rollback copy. Do not include active databases or unreviewed
directories in that cleanup.

Set retention and an independent offsite copy deliberately. A backup outside
the VM survives VM loss; a second VM on the same host does not provide an
independent physical failure domain. Measure a complete restore to establish a
recovery-time objective, including credential retrieval and any manual unlock.

## Promote only through a controlled cutover

Use the [Linux recovery steps](linux.md#linux-recovery) and the
[catalog verification and promotion gate](recovery.md#check-the-restored-directory)
where applicable. Before routing readers to a replacement:

1. Quiesce production and take a fresh, verified checkpoint. Record the accepted
   history boundary and any acknowledged loss window.
2. Fence the former writer and its watchdog/restart paths so it cannot resume
   after a reboot. Prove that fencing persists and that stale return is refused.
   Keep the former host's data for investigation.
3. Restore into a clean target, reconcile current membership, consent, privacy
   retention and identity rules, and invalidate old sessions. Verify training,
   media, certificates and history without erasing truthful gap markers.
4. Review production credentials and routes, then admit the replacement as the
   sole writer. Verify authenticated reads and writes, backend selection,
   fallback behavior, status freshness and the new backup owner.
5. Rehearse rollback with the writes accepted since cutover. Switching code back
   does not authorize replacing current data with a stale snapshot. Data/export
   compatibility alone does not establish that an older launcher is safe.

These are promotion prerequisites, not a record that production cutover has
occurred. Provider selection and a production migration remain separate work.

## Verified status: 18 November 2026

Evidence was recorded through 17:31 UTC. Private raw receipts remain with the
operator; infrastructure identifiers, staff records, credentials and backup
identifiers are intentionally absent here. Results span several releases;
they do not imply every lifecycle check was repeated on the current release.

| Area | Verified result | Remaining boundary |
| --- | --- | --- |
| Deployment | Debian VM on a physical Linux host: installation, upgrade, uninstall/reinstall, service restart, guest reboot, host autostart after unlock, and historical watchdog fallback rehearsed. The isolated test installation serves release 0.30.2 with Python. | Linux production support, a published Linux native wheel and a production cutover are not established. Cold power-on requires manual disk unlock; automatic power-on after AC return remains untested. |
| Backup restoration | Three encrypted primary snapshots fully restored, hash-checked and validated through isolated application startup and history checks. The normal host timer validated the latest manually sent snapshot. | The elapsed sender attempt failed its disk reserve guard; later manual success did not prove a successful elapsed sender cycle. No live synchronization or automatic test-copy refresh. |
| Linux media | 46 selected converter, sandbox, refusal and positive-path cases passed on the Debian guest. | Targeted checks, not a full-suite run, all Linux distributions or sustained load. Ubuntu-specific AppArmor policy still needs an affected host. |
| Native backend | 17 parity cases and a synthetic paired performance rehearsal passed with matching Python/Rust results. | The representative-data diagnostic refused its post-staging capacity guard before any worker started. It produced no representative performance result; the serving backend remains Python. |
| Isolated sharing and recovery | 11 selected sharing, session, audit, revocation and PostgreSQL recovery cases passed in disposable prerequisites. | Does not complete M2 content adapters, hosted migration, two-space content isolation or a real second-owner pilot. |
| Writer fencing | A disposable two-guest service-manager rehearsal preserved acknowledged synthetic writes, maintained source fencing across reboot, refused stale return and cleaned up its owned resources. | Both guests used one physical host. Independent-host failure handling and real Postern production cutover were not exercised. |
| Operator archive | The encrypted recovery kit passed full restore, exact-membership and hash verification; installed automation and source copies were rechecked. | The private automation is not distributed by this documentation change. Independent offsite recovery, retention and operational recovery objectives still need verification. |
| Serving installations | Final readbacks retained the primary's active backend/processes and the test installation's original history prefix and configuration. | These are point-in-time checks. Private ingress and scoped synthetic browser flows do not establish public ingress, real identity-provider login, physical-camera or complete browser acceptance. |

The next evidence to collect is the complete elapsed backup cycle, followed by
offsite/whole-host recovery and the remaining deployment and product acceptance
gates. Resolve the capacity constraint before retrying representative diagnostics;
do not lower the guard to manufacture a result. Choose the cloud provider after
these private deployment rehearsals, then verify that provider's actual storage,
networking and recovery behavior before production migration.
