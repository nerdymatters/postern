# Optional service status monitoring

The status service is a separate Cloudflare Worker and D1 database. It checks the
web app and API from the cloud every minute and accepts process heartbeats from
the Mac. It cannot exercise Google sign-in or prove that every application task is
making progress. Only named states and timestamps are published; staff data,
credentials, local paths, PIDs and raw logs stay private.

Missing, invalidly configured or older-than-three-minute readings are **Unknown**.
An optional secondary connection is visible but does not determine overall health.
The heartbeat's API, proxy, watchdog and tunnel processes are all required. The
backup check reflects activity export, not a complete recovery rehearsal. Seven
days of observed changes are retained; gaps are not an uptime SLA.

## Deployment settings

The checked-in `status/wrangler.jsonc` has no production route or target URLs.
Copy it to ignored `status/wrangler.production.json` and configure these values:

| Setting | Meaning |
| --- | --- |
| `account_id` | Your Cloudflare account ID. |
| `routes` | Your verified status hostname, using a custom-domain route. |
| `d1_databases[0].database_id` | Your status database ID, replacing the zero placeholder. |
| `vars.WEBSITE_URL` | Your public web app URL. It also supplies the status page's Open Postern link. |
| `vars.API_HEALTH_URL` | Your public API's `/health` URL; use the same API origin as the web app's `VITE_POSTERN_API`. |
| `vars.SECONDARY_HEALTH_URL` | Optional alternative connection's `/health` URL; leave blank if unused. |
| Worker secret `HEARTBEAT_TOKEN` | A new dedicated random secret, at least 32 characters. Never put it in the Wrangler file. |

Targets must be absolute HTTPS URLs without embedded credentials or fragments.
Unset or invalid targets generate Unknown readings without a network request.
The worker does not follow redirects. A successful website reading requires both
the app HTML and its same-origin JavaScript entry bundle.

## Local checks

Use Bun for tests and Node 24 for Wrangler. Running Wrangler itself under Bun can
hang its local workerd proxy.

```sh
cd status
bun install --frozen-lockfile
bun test
bun x wrangler deploy --dry-run
bun x wrangler d1 migrations apply postern-status --local
bun run dev --port 8898
# In another shell, invoke the scheduled handler:
curl --max-time 20 'http://localhost:8898/cdn-cgi/handler/scheduled'
curl --max-time 5 http://localhost:8898/api/status
```

On an Apple Silicon Mac without Node on PATH, an existing Node 24 installation or
`bunx --package=node-bin-darwin-arm64@24 node node_modules/wrangler/bin/wrangler.js`
can run Wrangler. Set disposable local variables in ignored `status/.dev.vars`;
never use the production heartbeat token for tests. With no target settings,
local scheduled checks intentionally remain Unknown.

## First cloud deployment

1. Run `bun x wrangler login` and `bun x wrangler whoami` from `status/` to select
   the account that owns your status hostname. Do not overwrite existing DNS records.
2. Create a database with `bun x wrangler d1 create postern-status`, then fill the
   private production configuration described above.
3. Apply the schema with
   `bun x wrangler d1 migrations apply postern-status --remote --config wrangler.production.json`.
4. Generate a new random heartbeat token and store it using
   `bun x wrangler secret put HEARTBEAT_TOKEN --config wrangler.production.json`.
   Use the secret prompt or stdin, never a command argument or chat.
5. Run `bun run deploy`. Verify the deployment revision and custom hostname.
   Cron propagation can take several minutes; leave readings Unknown until measured.
6. Prepare and install the Mac heartbeat as below. Check genuine cloud measurements,
   process freshness, missing-heartbeat expiry, and HTTP401 for unauthenticated POSTs.

No paid plan is required by this guide. Check account-wide platform usage before
choosing a plan; this application must not enable paid billing automatically.

## Mac heartbeat

Copy `ops/postern/status-heartbeat.example.json` to
`~/.config/postern/status-heartbeat.json`, set its mode to 0600, and supply the same
token as the Worker. Set `url` to your status service's HTTPS `/api/heartbeat`.
Set each `agents` value to the actual launchd service target on your Mac. The GUI
user number and tunnel label are installation-specific. Do not rename existing
jobs merely to match the generic example.

```sh
.venv/bin/python ops/postern/status-heartbeat.py --config ~/.config/postern/status-heartbeat.json --check
.venv/bin/python ops/postern/status-heartbeat.py --config ~/.config/postern/status-heartbeat.json
.venv/bin/python ops/postern/install-status-heartbeat.py --config ~/.config/postern/status-heartbeat.json
```

`--check` prints only local states and sends nothing. With no config file it checks
the generic default labels; an existing installation should always supply its
configured targets. Sending requires an explicit HTTPS URL and owner-only config.
Only the four allowlisted state IDs leave the machine. Redirects never receive
the token, and error messages exclude token and endpoint details.

The installer copies the sender outside the checkout and creates a login-scoped
`org.nerdymatters.postern.status-heartbeat` job. `--label` selects a different
heartbeat job name. It refuses an existing heartbeat installation rather than
creating duplicate senders. Logs go to `~/Library/Logs/postern/status-heartbeat.log`.

For an existing installation, retain its job name and supply its current target
labels, endpoint and token in the private config **before** replacing the installed
sender. Review its plist and installed script path explicitly; the installer does
not overwrite or rename them. Merely updating this source checkout does not update
a previously copied heartbeat script.

## Operation and rollback

Changing a connection means updating the web app's private `VITE_POSTERN_API`,
Worker target variables and heartbeat targets together, then verifying each.
Monitoring never changes application routes automatically.

For a bad Worker release, use
`bun x wrangler rollback --config wrangler.production.json` to a known good
revision; keep D1 data. To stop heartbeats, unload only the selected heartbeat
job, leaving the application, proxy, watchdog and tunnel untouched. Readings
expire to Unknown within three minutes.

Keep previous private configuration and the installed sender for rollback.
Rotate the local token and Worker secret in one maintenance window; failed
heartbeats remain visible as Unknown until both agree. Retain the sender's
`PosternStatusHeartbeat/1.0` User-Agent when changing it.
