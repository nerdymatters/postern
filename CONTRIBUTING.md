# Contributing to Postern

Discuss substantial behavior or deployment changes in an issue before implementing
them. Describe the user problem, expected behavior and how the change will be
verified. For vulnerabilities, use [private reporting](SECURITY.md) instead.

Keep training content, credentials, portraits, staff identities and operational
logs out of issues, commits and fixtures. Use the fictional sample course and
isolated data directories for development. Never run tests against a live
installation's configuration or database.

## Development and checks

Follow the [README](README.md) for setup. The converter uses macOS sandboxing;
its complete test suite must run on a supported Mac with the documented native
tools. Use the project's virtual environment and Bun:

```sh
.venv/bin/python -m pytest tests -q
cd dashboard/web
bun install --frozen-lockfile
bun run test
bun run build
```

For status-worker changes, also run its tests and deployment dry run from
`status/`, following its documented Node version. Add focused regression coverage
for behavior changes, update operator documentation when setup or recovery changes,
and explain validation and limitations in the pull request. Do not weaken a test or
security check just to make a change pass.

When you add a root-level Markdown document, link it from a bundled doc such as the
README and add it to `RELEASE_FILES` in `scripts/package_release.py`; the
release-archive tests fail until both are done. Other root-level files ship only
when listed there. Agent instruction files such as `CLAUDE.md` stay unlinked and
out of the archive; list them in `UNBUNDLED_DOCS` in
`tests/postern/test_package_release.py`.

## License and sign-off

By submitting source code, documentation or sample content for inclusion, you agree
to make your contribution available under **MIT OR Apache-2.0**, unless separately
identified material has
an applicable third-party license. This does not transfer your copyright. Retain
third-party notices and explain the origin and license of any material you add.
Do not submit material you do not have the right to contribute.

Every commit must carry a `Signed-off-by` line certifying the
[Developer Certificate of Origin 1.1](docs/DCO.txt). Use your contributor identity
and an email address you control:

```text
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` adds the line from your Git identity. It is a certification, not a
cryptographic signature. Read the DCO before signing; sign only your own work or
work you are entitled to pass on under its terms. Sign-offs become part of the
public commit history. Maintainers check sign-offs before merging contributions.

The [brand policy](docs/brand/LICENSE-brand.txt) covers Postern's name and artwork
separately. Contributions to reserved first-party brand artwork require a separate
written permission agreement before acceptance; the software contribution terms
above do not establish that agreement.
