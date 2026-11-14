# Linux host installation, update and rollback

Target a Debian or Ubuntu systemd host with CPython 3.13 and local persistent
storage. The API, watchdog and Caddy proxy run as one dedicated, unprivileged
account in separate **system** units, enabled at boot. No interactive login is
required. The installer renders files as the owner and prints a root command;
it never invokes sudo. Keep the service account's home on storage available at
boot, without a login-unlocked encrypted home.

Conversions run under bubblewrap, and the install preflight runs the real
sandbox and each conversion tool inside it; do not start serving until it
passes. Install, a clean-exit restart, a boot, an update to a new release
directory, uninstall and the watchdog's Rust-to-Python fallback were rehearsed
with synthetic data in a disposable Debian 13 container running systemd 257 as
PID 1 (Nov 12). A real host has not served yet: repeat these steps there,
including a real reboot, journald retention, network wait-online and public
ingress, before it serves staff.

## Provision and install

1. Provision an account such as `postern`, a persistent home and private config
   and data directories (0700). Put configuration and credentials outside the
   release tree, owned by that account, mode 0600. Start with
   [config.example.json](../../ops/postern/config.example.json), the
   [private content instructions](release.md#private-training-content), and
   [branding](../branding.md). Preserve private training, question IDs,
   agreement versions and certificate prefixes on an existing installation.
2. Install CPython 3.13, venv support, poppler-utils, ffmpeg, bubblewrap, lsof,
   procps and curl, and Caddy 2.8 or later from the
   [Caddy project's Debian repository](https://caddyserver.com/docs/install#debian-ubuntu-raspbian).
   Debian's own `caddy` is too old: 2.6 (Debian 13) has no PROXY protocol
   listener, and 2.7 rejects the Caddyfile's `fallback_policy`; the preflight
   reports Caddy's own reason. Disable the package's own site
   (`sudo systemctl disable --now caddy`); Postern runs its own proxy unit. Record the exact distribution and package versions
   (`dpkg-query -W`), install from your approved pinned repository snapshot,
   and hold the reviewed versions until the next tested host update. There
   is no universal Debian/Ubuntu package-version string. Do not silently
   upgrade these packages with an application update. On Ubuntu 23.10 and
   later, install the AppArmor rule in
   [`config/sandbox/`](../../config/sandbox/README.md#linux-derivebwrap) and
   prove that bubblewrap works under the service account; do not globally
   disable AppArmor.
3. Verify the release's SHA256SUMS and extract to an owner-controlled versioned
   directory, for example `/srv/postern/releases/VERSION`. Retain old releases.
   Run the following **as the service account**, from the new release:

   ```sh
   uv venv --python 3.13 .venv
   uv pip install --python .venv/bin/python --only-binary=:all: -r requirements-linux.lock
   uv pip install --python .venv/bin/python --no-deps -e .
   .venv/bin/python -P -m postern.serve install --check-only --config /home/postern/.config/postern/config.json
   .venv/bin/python -P -m postern.serve install --config /home/postern/.config/postern/config.json --user postern --label org.nerdymatters.postern
   ```

   The Linux lock keeps every macOS pilot version unchanged. Cross-platform
   resolution was checked for CPython 3.13 / Linux x86-64; installation and
   native imports must still be verified on the selected distribution and
   architecture. The optional Rust wheel must match the target architecture,
   Python ABI and glibc baseline. Install an approved bundled wheel explicitly
   with `uv pip install --python .venv/bin/python --no-deps wheels/EXACT_NAME.whl`.
   Without it, Python remains the default.
4. Review the rendered units and `install.sh` under the owner's
   `~/.local/state/postern/install-*` directory. On a first install systemd has
   not loaded these units yet, so install can refuse only each unit's own
   `<unit>.d/` in the five unit directories. A type-wide `service.d/`, a
   `system.control`, `system.attached`, transient or generator drop-in, or a
   `<prefix>-.service.d/` for a custom label containing a hyphen would apply
   unseen. List them first; on a clean host this prints nothing:

   ```sh
   for d in /etc/systemd/system.control /run/systemd/system.control /run/systemd/transient \
            /run/systemd/generator.early /etc/systemd/system /etc/systemd/system.attached \
            /run/systemd/system /run/systemd/system.attached /run/systemd/generator \
            /usr/local/lib/systemd/system /usr/lib/systemd/system /lib/systemd/system \
            /run/systemd/generator.late; do
     ls -d "$d/service.d" "$d"/org.nerdymatters.postern*.d 2>/dev/null
   done
   ```

   Use your label in place of `org.nerdymatters.postern`, and for a label
   containing a hyphen also look for each `<prefix>-.service.d` it implies.
   Reconcile anything listed, then run the **single root command
   printed by install**. It installs the units into `/etc/systemd/system`,
   reloads systemd, enables and restarts the three services. The service
   account owns the stage, so root never runs or installs a file from it
   unchecked: the command carries the SHA-256 digest of the `install.sh` you
   reviewed, copies it into a new directory root owns and checks it, and that
   script checks each staged unit and each installed unit the same way before
   it changes anything. A unit, staged file or script changed since rendering
   stops the install. Keep the stage until installation is complete; it
   contains paths and unit definitions, not config contents. The root step
   needs GNU `sha256sum`, which Debian and Ubuntu have.
   Custom labels must be supplied consistently. `--user-agent` is macOS-only.
5. Check services, private health, alerts and ingress:

   ```sh
   .venv/bin/python -P -m postern.serve status --config /home/postern/.config/postern/config.json --label org.nerdymatters.postern
   .venv/bin/python -P -m postern.serve backend show --config /home/postern/.config/postern/config.json
   journalctl -u org.nerdymatters.postern.service -u org.nerdymatters.postern.watchdog.service
   ```

   Journal access may require an operator account or root. Configure private
   [watchdog SMTP settings](../../ops/postern/watchdog.example.json) beside
   the config and run `postern.watchdog test --config ...` deliberately to
   prove receipt. Without SMTP, alerts are local files, not external notices.
   Follow the [proxy/HTTPS ingress contract](../../ops/postern/README.md#proxy-and-https-ingress):
   Caddy listens on loopback port 18789 and requires PROXY protocol v2.
   It keeps the macOS log layout: the aggregate traffic log is
   `~/Library/Logs/postern/traffic/access-daily.log` in the service account's
   home.
   The ingress/tunnel is provisioned independently as a boot service.
6. Schedule a real reboot. Before manually starting anything, run as the
   service account (the script refuses root, because it runs the release
   tree's Python):

   ```sh
   sudo -u postern sh ops/postern/reboot-check-linux.sh /home/postern/.config/postern/config.json org.nerdymatters.postern .venv/bin/python
   ```

   It requires **enabled and active** for all three units and probes local
   health. Also check the public HTTPS URL, client-address policy and a
   synthetic document conversion. An active API does not prove ingress works.

## Update and restart

Keep the installed label, account, configuration and data directory. Take the
normal backups and record the old release path and unit files. Install the new
release and its own venv exactly as above; never move a venv after creating it.
Run preflight as the owner. Render install again with the same label. The old
units differ in executable/working-directory paths: inspect the displayed diff,
then re-run with `--force` only for those reviewed changes. Vendor units,
symlinks/masks and drop-ins are refused even with `--force`; reconcile them
explicitly instead of silently shadowing their settings. A pending
`NeedDaemonReload=yes` also stops installation: review the disk changes, reload
the manager deliberately, then render again. Install checks `/etc/systemd/system`,
`/run/systemd/system`, `/usr/local/lib/systemd/system`, `/usr/lib/systemd/system`
and `/lib/systemd/system` for `<unit>.d/`, including drop-ins the manager has not
loaded yet; the printed root script checks those directories again before any
install. Other drop-in locations (`system.control`, `system.attached`, transient
and generator directories, a type-wide `service.d/`, and `<prefix>-.service.d/`
for a custom label containing a hyphen) are refused only once systemd has loaded
them, so check `systemctl cat` for each unit after installing.
Run the newly printed root command, then status, private health and a synthetic
conversion.

For a restart without changing units, the operator runs:

```sh
sudo systemctl --system restart org.nerdymatters.postern.service
```

Restart the watchdog too when its code changes. Logs go to journald. Configure
journal retention/persistence with the host policy. `Restart=always` and disabled
start-rate limiting allow recovery from clean exits and crash loops; the
watchdog is an independent unit and alerts when health keeps failing. Both
units wait for `network-online.target`; configure the host's wait-online
provider. This target is not a guarantee of Internet reachability.

## Python fallback

As the owner, select Python using the installed release:

```sh
.venv/bin/python -P -m postern.serve backend python --config /home/postern/.config/postern/config.json
```

Then run the API restart above and `backend show`. Confirm a new API PID,
`python` from `progress-backend.json`, and healthy reads/writes. Do not remove
staff data or overwrite the database. Automatic fallback writes and syncs the
Python selection before signalling only the verified serving API with SIGTERM.
Systemd restarts it even after a clean exit. The watchdog escalates a stuck
process after 120 seconds; systemd's cgroup cleanup also covers converter
sessions. An operator's `systemctl stop` intentionally stays stopped.

The installer sets `POSTERN_API_UNIT` in the **watchdog** unit to the API's
full unit name, for example `org.nerdymatters.postern.service`. The watchdog
reads this variable when it runs `systemctl show`; a custom label must name
that same API unit, not the watchdog or proxy. Without the variable it uses
the default API unit. An invalid name or any `LoadState` other than `loaded`
is unknown, never evidence that the unit has no override. Alerts name that
load state. Inspect the watchdog unit if the API label and the reported unit
state disagree.

When nothing confirms a backend setting, an unprovable environment is recorded
as `inconclusive` in the watchdog's private guard heartbeat, with a `reason`
such as `unknown (LoadState=not-found)`, `unknown (invalid POSTERN_API_UNIT)`,
`unknown (EnvironmentFile)` or `unknown (unit environment unreadable)`.
`postern.serve backend` prints that reason and the unit inspection command
below. This does not establish that the unit sets `POSTERN_PROGRESS_BACKEND`.
Check the API unit name, its load state and the
watchdog account's ability to read it before changing any settings. `override`
is reserved for a confirmed backend setting: a value the unit lookup reads, or a
live API whose startup record says its backend came from the environment. That
record confirms the setting even while the unit lookup is unknown, so the guard
records `override` with no `reason` and `postern.serve backend` prints the
systemd override line. Restart the updated watchdog to get the new distinction;
older `override` records cannot be retrospectively classified.

Do not put `POSTERN_PROGRESS_BACKEND` in the unit, manager environment or a
service environment file for a guarded rollout. Inspect `systemctl cat UNIT`
and `systemctl show UNIT -p LoadState -p Environment -p EnvironmentFiles`; remove the
backend override, run `sudo systemctl daemon-reload`, then restart. The API's
startup record and the unit's merged Environment detect overrides. An
environment file on the API unit makes new breaches alert-only while Rust is
selected or serving, because `systemctl show` cannot tell what it sets and the
next start could take Rust whatever the selection says. This is reported in the
alert; restore inspectable configuration before relying on automatic fallback.
A brief failed lookup keeps the watchdog's last answer. After ten consecutive
failures (about five minutes at the default 30-second interval), it expires
that answer and alerts once that the API unit environment cannot be read.
New breaches remain alert-only until a lookup answers again, which also resets
the failure count and allows a future outage to alert. Before the first
successful lookup, new breaches are alert-only too. On a Python-only host this produces
no Rust breach alert, though ten lookup failures still report the observation
failure. The watchdog never edits root-owned unit configuration.

A fallback already waiting to write Python keeps writing `progress-backend.json`
while the unit environment cannot be verified, including after a lookup expires
or an environment file appears. It retains the pending memory, CPU or parity
breach, or a health fallback whose write failed while health keeps failing, and
retries the write on later checks. After writing Python it still restarts only the verified API, with the
existing limit of three later restart attempts if Rust continues serving from
the file. When the API unit is not loaded (masked or not found, for example) or
misnamed, or was when its lookup expired, systemd may not start the API again,
so the watchdog writes Python but does not restart it; the alert says to restart the API by hand. The heartbeat remains
`inconclusive`; writing Python cannot override an actual environment setting.
A confirmed backend override cancels pending writes and remains alert-only.

Prove automatic fallback using synthetic data on a disposable Linux host:
select an installed Rust wheel, induce the documented guard threshold, then
observe a durable Python selection, the old API's exit, a new Python PID and
the alerts. In the container rehearsal, freezing the Rust API with SIGSTOP
gave a durable Python selection after three failed checks (about 90 s), SIGKILL
after the 120 s SIGTERM wait, a systemd restart on Python 5 s later, and two
alerts: the return to Python and the ordinary "not answering". Mocked systemctl
tests alone do not establish that behavior.

## Roll back or uninstall

For a code rollback, first select Python and restart, then stop the API and
watchdog during the change. From the retained previous **Linux-capable**
release with its original venv, run preflight and render/install using the same
config, account and label. Review and force only the expected unit path
changes. The printed root command restarts the services. Confirm status,
private health, backend and a document. Preserve the current database, WAL,
private files and recorded staff activity; restoring an old database is disaster
recovery, not code rollback. A macOS-only release is not a Linux rollback target.

`postern.serve uninstall --label INSTALLED_LABEL` prints a root command to
disable/stop and remove that unit family, then reload systemd. Data, config,
other labels and any manually managed drop-ins remain untouched. Review any
retained drop-ins before reinstalling.

## Optional status heartbeat

Follow the [Linux heartbeat instructions](status.md#linux-heartbeat) to publish
only the API, proxy, watchdog and tunnel states to the existing status Worker.
Use explicit `systemd/UNIT.service` targets and a private mode-0600 heartbeat
config; launchd targets and defaults are not Linux unit names. The sender is
copied outside the release directory and scheduled with a separate systemd
timer. It needs no Worker change and does not replace the watchdog's alerts.
Verify the timer and public status freshness after a real reboot as well.

## Linux recovery

Record the configured `data_dir` (for example `/srv/postern/data`); there is no
separate implicit Linux data location. It holds `postern.sqlite3`, its WAL and
SHM companions while running, private training/branding and media state.
Use a SQLite backup operation for a live snapshot, never copy only the database
file while its writer is running. Keep independent backups of `training/` and
`branding.json`; cloud activity exports cannot reconstruct these assets.

Back up the service account's private config directory, normally
`/home/postern/.config/postern/`, and every external credentials path it names.
Retain the backend selection, watchdog/heartbeat settings, exact unit names
and the reviewed `/etc/systemd/system/<label>{,.watchdog,.proxy}.service`
files. Record any manual drop-ins, ingress units and heartbeat service/timer
separately. The installer stage under `~/.local/state/postern/` is not a backup
of data or credentials, and journald is not an audit-history backup.

For a disaster restore, use a spare empty directory outside the active data
path, owned by the service account with mode 0700. Follow the
[recovery rehearsal](recovery.md#restore-into-an-empty-directory) as that
account using a retained Linux-capable release, the private config and the
pinned converter packages. Restore private training and branding separately,
read the recovery report and verify the catalog/history before clearing the
recovery gate. Keep any incomplete-history flag truthful.

Before promotion, stop the selected API, watchdog and proxy system units as
administrator so no writer or fallback races the change. Retain the old data
for investigation. Point a private config at the verified restored directory,
run preflight as the owner, and follow the recovery runbook's promotion steps
with the writer stopped. If the config path or release path changes, render
and review new units using the same label family. Restart the services and
verify private/local/public health, backend selection, a synthetic conversion
and status freshness. Recovered sessions are not reused; readers sign in again.
This is disaster recovery; ordinary code rollback keeps the current data.

## Durability and releases

SQLite uses WAL and `synchronous=FULL` on both platforms. On Linux the private
health `sqlite` report has `fullfsync: 0` and `fullfsync_supported: false`;
SQLite's Unix VFS supplies the normal sync operations. macOS retains
`fullfsync: 1`. These guarantees require local storage that honors syncs;
network filesystems and storage that lies about flush completion are not a
supported durability substitute.

Linux config and backend-selection replacements fsync the temporary file,
rename it, then fsync its directory before reporting success. This prevents
power loss from resurrecting the prior Rust selection after fallback. A sync
failure prevents the watchdog from signalling the API. Diagnostic heartbeats
and recoverable selfie caches do not need this durability; authoritative audit
history remains in SQLite. Provision persistent parent directories before use.

Build a Linux source/web bundle from a clean checkout:

```sh
.venv/bin/python scripts/package_release.py --platform linux --output .context/release-linux
# If an approved CI wheel is available, add --native-wheel /path/to/EXACT_NAME.whl
```

The bundle contains `requirements-linux.lock`, the systemd template, reboot
check and these instructions. Explicit native wheels are placed in `wheels/`
and listed in RELEASE.json. Packaging rejects wrong-platform and non-Postern
wheel names and symlink inputs. Python-only releases need no native wheel.
CI's `rust-progress-linux` job builds and tests the native package but does
not upload a wheel yet. Publishing one and checking it against the host remain
release-owner work.
