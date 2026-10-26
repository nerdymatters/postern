# Install the macOS host

Use a stable checkout or extracted release outside a temporary coding workspace. This server depends on macOS `sandbox-exec`; Linux is not a supported production host.

## 1. Private configuration

Copy [config.example.json](../../ops/postern/config.example.json) to `~/.config/postern/config.json`, fill the Google settings described in [Google setup](google-setup.md), and set mode `0600`. Use an owner-only data directory outside cloud-synced folders. Exclude unstamped derivatives and credentials from uncontrolled backups.

Important settings:

| Setting | Meaning |
| --- | --- |
| `mode` | `google` for production; `demo` only for isolated loopback testing |
| `data_dir` | Private database, training, branding, portraits and conversion/cache files |
| `origins` | Exact HTTPS web origins; never `*` |
| `google_client_id` | Web sign-in client ID |
| `host_credentials` | Private host OAuth token path |
| `allowlist_credentials` | Private read-only roster credential path |
| `allowlist_sheet_id`, `allowlist_tabs` | Roster source and optional ordered groups |
| `drive_folder_id`, `database_sheet_id` | App-owned original/audit storage and catalog |
| `port` | Loopback API port, normally 8790 |
| `selfie_enabled`, `selfie_folder_id` | Optional enrollment and its dedicated private folder |
| `selfie_retention_days` | Retired portrait retention, 0–365 days; default 90 |

Never repoint an existing installation at different storage without a planned migration.

Install reviewed `training/quizzes.json`, `training/final_exam.json` and `training/captions/` under `data_dir`. The captions directory is required even when empty. Existing hosts use the [training migration](release.md#private-training-content); copying the demo course is not a migration of real staff assessments.

Create [branding.json](../branding.md) under `data_dir`. Production refuses absent or invalid branding. Keep these files `0600`, directories `0700`, and an independent private backup.

## 2. Install

Install Python 3.13, Bun 1.4 or later, Poppler, FFmpeg and Caddy. Homebrew users can use `brew install poppler ffmpeg caddy`. Create `.venv`, install `requirements-pilot.lock`, then install the package with `--no-deps -e .` as in the [README](../../README.md).

```sh
.venv/bin/python -P -m postern.serve install --check-only --config ~/.config/postern/config.json
```

Every prerequisite must pass: native tools, proxy/profile, training, branding, model inference outside and inside the sandbox, and private file permissions. A successful preflight is not a Google sign-in or load test.

```sh
.venv/bin/python -P -m postern.serve install --config ~/.config/postern/config.json
```

The installer renders API, watchdog and proxy LaunchDaemons for the service owner and prints the specific privileged command if root access is needed. New installations use `org.nerdymatters.postern` and its suffixes. An existing installation must supply its current prefix with `--label YOUR_EXISTING_LABEL` to install, inspect or uninstall; do not rename active services during an ordinary upgrade. Never use `--force` without reading the reported differences.

`--user-agent` is a login-scoped fallback, not boot-time availability. FileVault still requires disk unlock after reboot. Rehearse an actual reboot and check all services and your ingress before claiming unattended recovery.

## 3. HTTPS and web

The API binds to loopback. Follow [proxy notes](../../ops/postern/README.md) for Caddy and an independent HTTPS ingress. Only enable `trust_loopback_proxy` when the protected proxy boundary supplies trustworthy client addresses. Do not expose the raw API or reuse another application's tunnel configuration.

Set your frontend host's `VITE_POSTERN_API`, organization and privacy-contact values before its first build. Register that frontend origin in both Google and Postern. The privacy route must stay available without a working API.

## 4. Verify and operate

```sh
.venv/bin/python -P -m postern.serve status --config ~/.config/postern/config.json
```

For existing service names, append the same `--label` used for installation. Check local and public `/health`, Google sign-in, expected roster roles, reader/admin behavior and privacy copy. Use a consenting real account for real-camera acceptance; do not manufacture staff activity or bypass consent.

Configure [watchdog email](../../ops/postern/watchdog.example.json) privately if required. The optional [independent status service](status.md) can report outages when the office host is unreachable. A monitor cannot guarantee availability; test heartbeat expiry and alarms.

Use [release](release.md), [recovery](recovery.md) and [capacity](capacity.md) instructions for upgrades, restoration and load rehearsals. Preserve the single-process ownership model.
