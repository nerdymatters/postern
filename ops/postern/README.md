# Operating Postern on a Mac

Start with the [host runbook](../../docs/runbooks/pilot.md). Keep all filled
configuration files outside Git, owned by the service operator, with mode 0600.

| File | Purpose |
| --- | --- |
| `config.example.json` | API configuration template. |
| `watchdog.example.json` | Optional SMTP settings beside the API config; the server does not read the mail password. |
| `reboot-check.sh` | Verify local services after FileVault unlock and operator login. |
| `status-heartbeat.example.json` | Private heartbeat endpoint, token and installed launchd targets. |
| `status-heartbeat.py` | Send only process states; `--check` observes without sending. |
| `install-status-heartbeat.py` | Install the optional sender outside the checkout. |
| `traffic-report.py` | Summarize private Caddy logs into UTC daily HTTP-body totals. |

## Install

```sh
.venv/bin/python -P -m postern.serve install --check-only
sudo .venv/bin/python -P -m postern.serve install --config ~/.config/postern/config.json --user "$USER"
.venv/bin/python -P -m postern.serve status
```

Installation uses `org.nerdymatters.postern`, plus `.watchdog` and `.proxy` jobs.
The watchdog remains separate so a stalled server cannot stop its alerts.
`--user-agent` selects login-scoped jobs instead of boot LaunchDaemons; these
start only after the operator logs in. `install --check-only` writes nothing.

All management commands accept `--label YOUR_INSTALLED_LABEL`. Existing hosts
must keep their actual label during upgrades; updating code never renames jobs.
The same label selects server, watchdog and proxy for install, status and uninstall.
Installation refuses another Postern job using the same configuration, or an
occupied API port without the selected job, even with `--force`.

```sh
.venv/bin/python -P -m postern.serve status --label YOUR_INSTALLED_LABEL
sh ops/postern/reboot-check.sh ~/.config/postern/config.json YOUR_INSTALLED_LABEL
```

Use `--force` only to replace a reviewed, differing plist for the same installation.
Do not install a second label family during an upgrade. Changing labels is an
explicit operator migration: stop/remove the old jobs, preserve data/configuration,
and then install the chosen family. The program does not perform that migration.

## Proxy and HTTPS ingress

The checked-in [Caddyfile](Caddyfile) listens only on `127.0.0.1:18789` and
forwards to the API on `127.0.0.1:8790`. If you change the API's configured port,
update the reverse-proxy destination to match before running preflight or
installing the proxy job.

Supply an independent HTTPS ingress that forwards to port 18789 using PROXY
protocol v2. Caddy requires that metadata and replaces client-supplied forwarding
headers with the connection's client address. Ordinary HTTP forwarding without
PROXY protocol will fail. Keep both listeners bound to loopback, restrict who can
reach the ingress, and only enable `trust_loopback_proxy` after verifying this
boundary. The service installer does not configure a public hostname or tunnel.

Preserve an existing installation's ingress and tunnel job names. Verify public
`/health` and the client-address policy after setup and after reboot; local API
health alone does not prove that the HTTPS route is available.

## Monitoring and traffic

The watchdog detects an unresponsive local service, but cannot send mail when
the host or its network is down. The optional [cloud status service](../../docs/runbooks/status.md)
adds external reachability and process heartbeats. It does not send alerts.

Caddy's private aggregate logs exclude request URLs, headers and staff identities.
`traffic-report.py --log-directory PATH --output PATH` reports UTC daily HTTP-body
bytes, excluding CDN traffic, TLS overhead and Google transfers. It is not an ISP
billing meter. Keep reports outside public repositories.

Retain private training, branding, configuration, data and portraits across
upgrades. Follow the [release and rollback instructions](../../docs/runbooks/release.md);
never restore an old database over newly recorded staff activity for a code rollback.
