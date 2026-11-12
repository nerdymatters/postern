# Postern

Postern is a self-hosted staff reading room for private documents, images, videos and training. Readers sign in with Google; a Google Sheet determines membership and roles. Administrators publish material and inspect delivery, playback and assessment history.

The current server targets **macOS**, with Google Drive and Sheets for durable originals, catalog and audit exports. The web app can be hosted separately. This is a pilot, not a managed cloud service. Conversions also run on Linux under bubblewrap, and v0.27.0 adds systemd supervision and packaging, but they have been rehearsed only in a container, so Linux is not yet a supported production host.

## What it does

- Serve stamped document pages and a video player with the reader's identity and time. These discourage casual sharing; they cannot prevent screenshots or recording.
- Record sign-ins, content delivery and reported video playback. A served document is not proof of attention or comprehension.
- Provide bilingual English/Thai chapter quizzes, a final assessment, badges and printable certificates.
- Support staff, manager, executive and administrator roles, with optional multi-group rosters.
- Optionally require a consented selfie before reading. Local face-presence detection is not face matching, identity verification or liveness detection.
- Keep a local SQLite audit writer, verified cloud publication, recovery gates and an independent optional status monitor.

Operator training content and branding live outside the software package. The included Cloud Library course is fictional demonstration content.

## Try it locally

Use macOS with Python 3.13, Bun 1.4 or later, and the native converter tools. Homebrew users can install the tools with `brew install poppler ffmpeg caddy`. The host must provide `sandbox-exec`. On Debian or Ubuntu, install `poppler-utils ffmpeg bubblewrap` instead: conversions run under bubblewrap 0.8 or later, and Ubuntu 23.10 and later also need the AppArmor rule in [`config/sandbox/`](config/sandbox/README.md#linux-derivebwrap).

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-pilot.lock
.venv/bin/python -m pip install --no-deps -e .
mkdir -p .context
cat > .context/demo-config.json <<'JSON'
{"mode":"demo","data_dir":"~/.local/share/postern-demo"}
JSON
.venv/bin/python -m postern.serve run --config .context/demo-config.json
```

In another terminal:

```sh
cd dashboard/web
bun install --frozen-lockfile
bun run dev
```

Open `http://localhost:5173`. Use `admin@example.invalid` or `reader@example.invalid`; demo mode accepts synthetic identities and must stay on loopback. Its initial catalog is empty. An administrator can upload sample material and publish it. Never expose demo mode to the Internet or use it with confidential staff material.

## Install for an organization

Start with [Google setup](docs/runbooks/google-setup.md), then the [Mac installation guide](docs/runbooks/pilot.md) (the [Linux runbook](docs/runbooks/linux.md) has been rehearsed only in a container so far). Production requires private configuration, credentials, training and an explicit `branding.json` file. Use [branding settings](docs/branding.md) to specify the operator, privacy contact, agreement and certificate identity.

The [release guide](docs/runbooks/release.md) covers upgrades from private pilot installations, including byte-preserving training migration and preserving existing certificate numbers. Back up training and branding independently of Google media/audit backups. Rehearse [recovery](docs/runbooks/recovery.md) before relying on it.

For separate web hosting, set `VITE_POSTERN_API` to your HTTPS API origin in your hosting project's environment, then build with `bun run build` in `dashboard/web`. Set `VITE_POSTERN_ORGANIZATION_NAME` and `VITE_POSTERN_PRIVACY_CONTACT_EMAIL` there as public fallback text for the privacy page when the API is unavailable. Variables beginning with `VITE_` are public browser content; never put secrets in them. `vercel.json` supplies build and routing settings but contains no installation endpoint.

## Documentation

- [Architecture and trust boundaries](docs/architecture.md)
- [Web behavior and development](dashboard/web/README.md)
- [Enrollment and privacy](docs/runbooks/selfie-preview.md)
- [Capacity rehearsal and known limits](docs/runbooks/capacity.md)
- [First Rust server slice, measurements and rollback](docs/runbooks/rust-server.md)
- [Linux host install, update and rollback](docs/runbooks/linux.md) (rehearsed in a container; a real host has not served yet)
- [Isolated resource pilot and health diagnostics](docs/runbooks/pilot-rehearsal.md)
- [Optional independent status service](docs/runbooks/status.md)
- [Account isolation](docs/runbooks/account-isolation.md)
- [Optional accounts and profiles](docs/accounts.md)
- [Private Posterns: M2 contract](docs/private-spaces.md) (isolated metadata/onboarding implemented; content integration and hosting pending)
- [Multi-Postern storage evaluation](docs/designs/storage.md) (local experiment; migration pending)
- [Ownership transfer](docs/runbooks/ownership-transfer.md) (explicit recipient acceptance; verified in isolation)
- [Membership management](docs/runbooks/membership-management.md) (isolated APIs and browser controls; not deployed)
- [Space creation and invitation acceptance](docs/runbooks/space-onboarding.md) (isolated APIs and browser flow; not deployed)
- [Isolated account/resource service integration](docs/runbooks/service-integration.md) (local; not deployed)
- [PostgreSQL schema and migration rehearsal](docs/runbooks/postgres-rehearsal.md) (local; not deployed)
- [Product roadmap](ROADMAP.md)
- [Known follow-up work](TODOS.md)
- [Release notes](CHANGELOG.md)

## Development

Frontend tests require Node 24 on `PATH` alongside Bun. See the
[web commands](dashboard/web/README.md#commands) for the Apple Silicon fallback.

```sh
.venv/bin/python -m pip install -e '.[test,postgres]' psutil
# PostgreSQL 17+ CLI tools are required by tests/storage; use Homebrew on macOS.
brew install postgresql@17
export PATH="$(brew --prefix postgresql@17)/bin:$PATH"
.venv/bin/python -m pytest tests -q
bun run --cwd dashboard/web test
bun run --cwd dashboard/web build
```

The optional status Worker has its own checks: `cd status && bun install --frozen-lockfile && bun test`. Its Wrangler dry run needs Node 24. Python integration tests exercise the real converter sandbox: Seatbelt on macOS, and bubblewrap on Linux, where CI's `pytest-linux` job runs the same suite. The Seatbelt tests skip on Linux, so do not substitute a Linux test run for the macOS checks.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the DCO sign-off workflow and [SECURITY.md](SECURITY.md) for private vulnerability reports.

## License and brand

First-party code is available under **MIT OR Apache-2.0**, at your option: [MIT](LICENSE-MIT), [Apache-2.0](LICENSE-APACHE). Read [NOTICE](NOTICE) and [third-party licenses](docs/licenses/README.md) for included dependencies and assets.

The Postern name and brand artwork have [separate usage rules](docs/brand/LICENSE-brand.txt). Those rules do not restrict the software license or prevent independently branded hosting. Functional UI icons and third-party assets retain their stated licenses.
