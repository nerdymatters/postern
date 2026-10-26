# Release and upgrade

Build a source-and-web pilot bundle from a clean, reviewed commit. Keep `VERSION`, Python/web package versions and `CHANGELOG.md` aligned. Run the Python and frontend tests/build and status checks for status changes. Passing CI is the normal merge gate; any unavailable-CI exception must be explicitly approved for that release and recorded without calling CI green.

```sh
.venv/bin/python scripts/package_release.py --output .context/release
```

Tag the exact merged commit and upload its archive and `SHA256SUMS` as a pilot prerelease. Inspect the archive for credentials, private training, machine paths and installation artifacts. Keep root license/notice files with the bundle. Do not distribute a Python wheel as a complete host installation: operational files live beside the Python package.

## Private training content

Existing private pilots before v0.15.0 bundled their course. Retain the old checkout's `postern/` and `tests/postern/quiz_ids.lock.json` in the same relative layout before updating. From the new code:

```sh
.venv/bin/python -m postern.training migrate --source /path/to/old-checkout/postern \
  --config /path/to/private/config.json --check
.venv/bin/python -m postern.training migrate --source /path/to/old-checkout/postern \
  --config /path/to/private/config.json
```

The dry run writes nothing. Migration validates banks and an available ID registry, copies into an owner-only stage and installs atomically. Identical reruns are harmless; differing destinations are refused. Preserve bytes, question IDs, caption hash filenames and content versions to retain active attempts and progress. Back up `<data_dir>/training/` separately; Google audit/media backup cannot reconstruct it.

Google mode requires both banks and a captions directory. Demo falls back to fictional samples only when the entire private training directory is absent; partial installations fail.

## Branding and certificate migration

Before installing v0.16.0, create [branding.json](../branding.md) with the exact existing operator identity, contact, IP notice, default roster group, certificate prefix and PDF title. Production requires that file. Preserve agreement versions for unchanged text; increment them if the agreement itself changes. Preserve the prefix to keep historical certificate numbers stable. No scores, accepted events or database rows need rewriting.

Set the web hosting project's API origin, organization and privacy-contact environment before deploying generic frontend configuration. Public build-time privacy text must agree with runtime branding and remain usable while the API is down.

Existing LaunchDaemons keep their names. Use `--label YOUR_EXISTING_LABEL` for service management; do not reinstall generic jobs alongside them. Existing status monitoring also needs its deployment-specific URLs and labels preserved privately before deploying generic monitor code.

## Roll out

1. Record the installed commit. Save an owner-only config/training/branding backup. For a live SQLite snapshot, use `sqlite3.Connection.backup()`; copying a WAL database file alone is not a consistent backup.
2. Ensure the installed checkout is clean, then fast-forward it to the intended release. Keep its data, credentials and virtual environment. Install changed pinned dependencies when required and refresh the editable package.
3. Run `.venv/bin/python -P -m postern.serve install --check-only --config /path/to/private/config.json` with the existing config. Resolve every failure before restarting.
4. Restart only the API job being upgraded. If using launchd, target its exact installed label. Without a privileged session, the owner may send SIGTERM only after verifying that exact API PID, executable, arguments, working directory and ownership; its installed KeepAlive job restarts it.
5. Verify the new process/commit, local/public health, Google mode, expected training counts, privacy copy and authorized reader/admin behavior. Test camera/consent behavior only with a willing real account. Never generate synthetic production staff activity.

Deploy matching API/web versions when their contract changes. Vercel may deploy automatically when main changes; account for that ordering before merging. Background workers, proxy and monitor only need restart when their own configuration/code changes.

## Roll back

Select the retained previous code and restart the same API job. Preserve the **current** database, credentials, portraits and staff activity. Never restore an older database over newer activity during routine rollback. v0.15.0 ignores the separate branding file; older releases load their retained bundled training. Keep both private training and branding in place.

## Content import

Content is not a software release. Publish PDFs/photos and compatible MP4s through normal authenticated upload and confirmation. Preserve original sources and avoid duplicate copies. Supported video is H.264/AAC, at most 2 GiB, three hours and 3840×2160. This is not automatic Drive synchronization or general transcoding. Validate playback/captions on target devices without marking training complete merely for testing.
