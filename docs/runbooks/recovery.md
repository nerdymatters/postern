# Recovery rehearsal and restoration

`postern.recover` rebuilds a Postern host from the cloud copies alone: the confirmed catalog in Postern DB,
the sealed JSONL batches in the Drive folder, the yearly summary log and the original files. It never reads
the old data directory and never copies a live SQLite file. Running it once against a scratch directory, and
reading every line of its summary, is a pilot prerequisite: until it has been rehearsed, "we have a backup" is
an assumption.

Private training and branding are separate restore steps. Postern does not upload
`<data_dir>/training/` to Google, and neither question text nor captions can be reconstructed
from quiz and playback events. Keep a private backup of both banks, the caption directory
and the migrated question-ID lock whenever training content changes. Back up `branding.json` separately too; restoring the same certificate prefix and agreement text is required to preserve identity and consent semantics.

The rehearsal proves the restore path, the batch verification and the catalog-verification gate. It does not
prove that Google will be reachable on the bad day, and it says nothing about events that were never exported
— those are an acknowledged loss window, not something recovery can repair.

## Before you start

- Know the live data directory (`data_dir` in the config, normally `~/Library/Application Support/Postern`).
  Everything below happens somewhere else.
- Have the host credentials the config names, and Drive/Sheets reachable as that account. In `google` mode the
  command reads Drive and Sheets; it writes nothing to either.
- Have the matching private `training/` backup and its checksums. Keep it outside the
  empty restore target until the cloud restore finishes.
- Choose a scratch directory outside the live one, on a disk with room for every original to be converted
  again, including video staging and prepared MP4s. Install `ffmpeg` and `ffprobe` as
  described in the [Mac runbook](pilot.md#2-install). It will hold staff material when the command finishes.
- The rehearsal may run while Postern is serving. It only reads the cloud, and the restored directory has its
  own database, so the live single writer is untouched.

## Restore into an empty directory

```sh
.venv/bin/python -P -m postern.recover \
    --config ~/.config/postern/config.json \
    --into ~/postern-rehearsal-$(date +%Y-%m-%d)
```

The command refuses, before creating anything, a target that already holds files, the live data directory
itself, any directory inside it, and any directory that contains it. Exit status: `0` everything the cloud
holds was restored, `2` restored with gaps you must read, `1` refused and did nothing.

After that command finishes, copy the private `training/` backup and `branding.json` into the restored data
directory. Preserve bank contents, question IDs and caption hash filenames; compare checksums
against the backup. Set directories to 0700 and files to 0600 for the service owner. Before
starting or promoting the restored host, run preflight with a spare config pointing at the
restored data directory. A successful cloud restore alone does not establish that private
training content is present. Google mode refuses to start without valid training assets.

Before reading the cloud catalog, restoration persists recovery mode and marks history incomplete.
Those flags survive an interruption and an ordinary service restart. A completed restore updates the
history flag from its results; the recovery gate stays on until operator verification.

It prints one summary, also written to `recovery-report.json` (0600) in the restored directory:

```
batches            41 of 42 imported
events             20,118 imported, 1,204 already present, 63 of them from the summary log
publications       37 post(s), 44 attachment(s), 44 derivative(s) rebuilt from the originals
sessions           0 restored — every reader signs in again
unknown history    3f1c…: its SHA-256 does not match the recorded digest — 500 event(s) are unknown, not 'not opened'
history            INCOMPLETE — verify the ranges above
recovery mode      on — reader content stays hidden until an operator verifies the catalog
```

- **batches** — each one is checked against the metadata recorded beside it when it was sealed: schema
  version, byte length and SHA-256. A batch that fails any check is imported not at all; its bytes are left in
  `exports/` for inspection and its range is reported as unknown history.
- **events** — imported at their original timestamps, deduplicated by `event_id`, so importing the same batch
  or the same summary row twice changes nothing. "Already present" is the overlap between the detail batches
  and the summary log, which is expected and is how a corrupt batch still leaves its summary tier behind.
- **unknown history** — sets the `history_incomplete` flag. The opened/not-opened report then shows those
  readers as unknown for that window. Absent evidence during a known gap is not proof that nobody opened
  anything, and it must never be presented as one.
- **sessions** — always zero. Sessions, bearer tokens and the members table are not restored; readers sign in
  again and membership comes from the fresh allowlist read the service performs at startup (ENG-D2).
- **derivatives** — rebuilt by converting the originals from Drive again, after checking each original's
  SHA-256 against the catalog. Nothing is trusted from a page cache. An original the store no longer holds
  leaves its attachment `failed` with the reason, and the post visible with that attachment unavailable.
  MP4 duration metadata is reprobed and the fast-start video rebuilt from the verified original.
- **video progress** — exported `video_progress` interval events restore watched coverage, including
  summary-tier copies if a detailed batch is missing. Deduplication preserves event identity and coverage
  unions overlaps. Missing history still shows unknown or a lower bound. Playback tickets are not restored;
  each reader signs in again and opens a fresh player.
- **chapter quizzes** — imported `quiz_attempt` events rebuild passes, scores and, from 0.8.0,
  per-question counts in `quiz_seen`; duplicate events do not count twice. Unanswered hands in
  `quiz_draws` are not restored, so readers reopen their quiz for a fresh hand. A restored pass keeps
  retakes within its recorded question history. If that pass has no answer ids, the hand is filled
  from the start of the current pool and stays fixed after an attempt. A pass missing from both
  export tiers cannot be recovered; the reader must pass again. See the
  [quiz notes](../../dashboard/web/README.md#chapter-quizzes). Final Exam attempts use the same
  restore path under reserved key `0`, with a separate result from chapter standings. Install the
  matching banks and reviewed caption files from the private backup into
  `<restored-data-dir>/training/`; these assets are not reconstructed from quiz or playback
  events. Preserve any host-specific media/page limits needed to rebuild its originals.
- **badges and certificates** — nothing separate to restore. Badges are the restored chapter and Final
  Exam passes. A certificate's number and date come from the first passing Final Exam attempt's event id
  and time, and its printed name from the latest imported `certificate_name` event. A restore brings back
  the same certificate while that first passing attempt is in either tier. If it is in neither, the
  earliest restored pass gives the certificate a new number and date; with no restored pass, the person
  must pass the Final Exam again. A name change in neither tier is lost, so the previous saved name, or
  the suggested one, prints instead. Certificates stay hidden until the restored catalog is verified.

## Enrollment photo recovery (0.4.0)

Recovery restores confirmed selfie metadata from private audit exports, then fetches
portraits from the dedicated private Drive folder only while they remain retained.
Preserve `selfie_folder_id`; changing it can strand retained photos and prevent safe
cleanup. For an account migration, follow the [migration safeguards](account-isolation.md#moving-cloud-accounts)
and configure the copied private folder. From 0.11.1, recovery maps a copied
portrait's `postern_migration_source` to its new Drive ID when `kind=selfie` and
its `sha256` matches the enrollment record. Duplicate source mappings, missing
checksums or mismatched checksums stop recovery; sealed events are never rewritten.
Sessions and tokens are never restored, so readers sign in again and accept
the current activity/IP agreements. Browser binding and enrollment checks still apply.

A consent record without a confirmed enrollment event is ambiguous: the final event
may not have reached the cloud. Such records remain quarantined and unreadable until
the original enrollment expiry plus the configured retention period (90 days for
this rollout). They are not immediately deleted. Confirmed retirement/purge events
restore their recorded state. The restore command does not delete cloud photos;
the enabled service's retention worker performs due cleanup after startup.

Access ends at the retention deadline even when cloud deletion fails. Failed purges
retry with backoff and appear as an administrator warning. If a private portrait needed to render a document or photo is missing, that content
fails closed with a retry screen. Optional report/video portrait requests can show
a grey placeholder on a storage error; the video email/time overlay remains.
Disabling enrollment pauses cleanup, so re-enable it promptly after an incident.

## Check the restored directory

```sh
SCRATCH=~/postern-rehearsal-$(date +%Y-%m-%d)
sqlite3 "$SCRATCH/postern.sqlite3" \
  'select key,value from meta;' \
  'select status,count(*) from posts group by status;' \
  'select derive_status,count(*) from items group by derive_status;' \
  'select count(*) from sessions;' \
  "select type,count(*) from events group by type order by 2 desc;"
```

Expect: `recovery_mode` true; `history_incomplete` matching the summary; post counts matching what the owner
believes was published and archived; every attachment `ready` except the ones the summary named; zero
sessions; and event counts in the same shape as the live host's (`select type,count(*) …` on the live
database, read-only, is the honest comparison).

A draft that was never published, and an archive that was never committed, are not in the cloud and will not
come back. That is the expected result, not a defect — confirm the owner understands which of their material
is in that state before the pilot rather than after a real failure.

Category and title renames restore from each post's last confirmed catalog entry.
A local rename whose backup was still pending can therefore restore with its old
label. Category-wide backups commit one post at a time, so a failure partway through
can leave mixed old/new category labels in the recovered catalog. Check labels as
well as publication status before promotion; renaming stays blocked while recovery
mode is on. IDs, attachments and recovered activity keep their existing associations.

To see it as a reader would, point a spare config at `$SCRATCH` (`data_dir`, `recovery_mode` absent or false,
a free port) and start the app. Signed-in readers get an empty wall and `403 access_denied` on every page,
thumbnail, view, and video request while recovery mode is on, including administrators. That is the gate: an unsynced local
archive may have been lost with the old disk, so nothing is shown until a person has checked the catalog.

## Promote or discard

Promote only after the catalog check above. With the service stopped, so the single writer is not running:

```sh
sqlite3 "$SCRATCH/postern.sqlite3" "update meta set value='false' where key='recovery_mode';"
```

Then move the directory into place, make sure the config's `data_dir` names it and that `recovery_mode` is not
set to true in the config (startup re-applies it if it is), restart the service, and confirm `/health` reports
permissions `ready` before telling anyone to sign in. Leave `history_incomplete` alone: it is a fact about the
restored history, and the report is supposed to keep saying so.

Discard a rehearsal completely. The scratch directory holds real derived pages and the real event history:

```sh
rm -rf "$SCRATCH"
```

Record the date, the summary lines and anything that surprised you in a private, ignored report directory. A rehearsal whose output
nobody read is not a rehearsal.

## Demo mode

With `mode: demo` the store is the `fake-cloud` directory under the live data directory; the command reads it
and refuses to run if it is not there. Use this to practise the sequence, not to judge Google behaviour. The
automated equivalent is `tests/postern/test_recovery.py`, which drives the same code path end to end:

```sh
.venv/bin/python -m pytest tests/postern/test_recovery.py tests/postern/test_video_recovery.py -q
```
