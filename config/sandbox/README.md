# The converter sandbox profile

`derive.sb` is the macOS Seatbelt profile that every Postern conversion job runs under (ENG-D6). It is
checked in so it can be reviewed and diffed: uploaded bytes are parsed by Poppler, libheif, and FFmpeg, and the
account running them holds the host Drive token and the session secret in `~/.config/postern/`.

`sandbox-exec -f derive.sb -D NAME=value ...` supplies each `(param "NAME")`. A missing parameter is a
compile error, so the profile cannot silently lose a rule. **Every value must be a fully resolved path.**
Seatbelt matches the real path, so passing `/var/folders/...` instead of `/private/var/folders/...`
compiles cleanly and then denies every write — the job fails inside pdftoppm with no obvious cause.

| Parameter | Value |
| --- | --- |
| `SOURCE` | the one staged upload being converted |
| `CHILD` | `postern/derive_child.py` |
| `FACE_MODEL` | the bundled `postern/models/face_detection_yunet_2023mar.onnx` |
| `PYTHON` | `sys.executable` |
| `PYVENV` | `sys.prefix/pyvenv.cfg` |
| `STDLIB`, `PURELIB`, `PLATLIB` | `sysconfig.get_path(...)` |
| `BASE_PREFIX` | `sys.base_prefix` |
| `OUTPUT` | the per-job temporary directory |

## Why each rule is there

- `(deny default)` — the job gets nothing it is not handed. Notably absent: any network rule, so a
  converter cannot call out even if a document, image, or video parser is executing attacker code.
- `(allow process*)` — the child forks `pdfinfo`, `pdftoppm`, `ffprobe`, or `ffmpeg`, and they inherit this profile.
- `(allow sysctl-read)` — CPython reads `hw.*` at startup.
- `(allow mach-lookup ...logger ...cfprefsd.daemon)` — the two services CoreFoundation contacts before
  `main()`. Without them the process dies with a Mach port failure.
- `(allow file-read-metadata)` — `stat()` is not a read of contents, and Python's import machinery stats
  hundreds of paths that do not exist. Denying it costs a startup crash and buys nothing.
- `(allow file-read* (literal "/"))` — **load-bearing.** dyld opens `/` itself before Python starts; without
  this the process aborts before any of our code runs. Verified 2026-09-21.
- The `SOURCE`/`CHILD`/`FACE_MODEL`/`PYTHON`/`PYVENV` literals — the exact five files the job may read by name. One
  upload, not a directory of uploads.
- The runtime subpaths — the standard library, the venv's site-packages, the base interpreter, and
  `/System/Library`, `/usr/lib`, `/usr/share` for the system frameworks. `/opt/homebrew/Cellar` and
  `/opt/homebrew/opt` are where Poppler, libheif, FFmpeg, and their dylibs live; `/opt/homebrew/bin` holds only
  symlinks into `Cellar`, so it does not need a rule of its own.
- `(subpath (param "OUTPUT"))` — the only writable place. The directory is created per job and deleted
  after, so a job cannot leave anything behind or see another job's pages.
- `/dev/null`, `/dev/urandom`, `/dev/random` — subprocess plumbing and Python's hash seed.

Not reachable, by construction and by test: `~/.config/postern/`, the staging directory as a whole, the
derivatives cache, the audit database, and the network.

Selfie jobs run the bundled YuNet face detector on the oriented, square-cropped
portrait before saving. Only the specific pinned model is newly readable, not its
parent directory. Native OpenCV/NumPy dependencies use the existing site-packages
allowlist; CPU inference uses one thread. No network or new write path is allowed.
Known child exit codes 60–64 communicate face rejection or an unavailable detector
without exposing parser diagnostics. Missing, corrupted or unreadable models fail
closed. Ordinary photo/PDF/video attachments do not run face detection.

MP4 preparation accepts one H.264 video stream, AAC audio if present, duration up to three hours,
and up to 3840 × 2160 pixels. FFmpeg copies the streams into a fast-start MP4 and strips source
metadata; it does not transcode or bake a watermark. The recorded length is the prepared MP4's when
it is shorter than the source's, so a longer track the remux drops cannot leave full playback out of
reach. Its protocol allowlist is `file` only. Video
output is bounded by `video_upload_limit` plus 16 MiB for remux overhead, while document output uses
`derive_output_limit`. The existing process, memory, CPU, and wall-time limits still apply.

## Checking it

Every conversion job runs this file: `postern/derive.py` builds the `sandbox-exec -f derive.sb -D ...`
command for each job (`job_command`). `postern.serve install` compiles the profile with the placeholders
substituted before it installs anything, and its preflight runs the real converter and face detector
under it on a blank photo, expecting "no face found". `tests/postern/test_ops.py` proves the job command
applies this file with every parameter supplied, pins exactly what it may read and write (only the job
directory and `/dev/null` are writable), and that a read of a `postern/config.json` under it is refused;
`tests/postern/test_selfie_faces.py` drives a real capture under a patched copy of it, proving production
jobs load the file.
