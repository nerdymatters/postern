# The converter sandbox profile

`derive.sb` is the macOS Seatbelt profile that every Postern conversion job on macOS runs under (ENG-D6); Linux uses `derive.bwrap` (see [below](#linux-derivebwrap)). It is
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

Every conversion job on macOS runs this file: `postern/derive.py` builds the `sandbox-exec -f derive.sb -D ...`
command for each job (`job_command`). `postern.serve install` compiles the profile with the placeholders
substituted before it installs anything, and its preflight runs the real converter and face detector
under it on a blank photo, expecting "no face found". `tests/postern/test_ops.py` proves the job command
applies this file with every parameter supplied, pins exactly what it may read and write (only the job
directory and `/dev/null` are writable), and that a read of a `postern/config.json` under it is refused;
`tests/postern/test_selfie_faces.py` drives a real capture under a patched copy of it, proving production
jobs load the file. On Linux, `tests/postern/test_sandbox.py` pins `derive.bwrap` the same way (on both
platforms), and runs the real bwrap to prove the secrets, writes outside the job, the network and new user
namespaces are refused, that a sandbox bwrap cannot build is a host fault, and that the memory bound stops a
job's whole process tree; the face-detector twins in `test_selfie_faces.py` drive a capture under a copy of
`derive.bwrap` without the model. CI's `pytest-linux` job runs the full suite on Ubuntu.

## Linux: `derive.bwrap`

On Linux every conversion job runs under [bubblewrap](https://github.com/containers/bubblewrap) 0.8.0 or
later at `/usr/bin/bwrap` instead (LX-01). `derive.bwrap` holds its rules in the same order as
`derive.sb`, one option per line, and `sandbox_command` in `postern/derive.py` builds every job's command
and preflight's checks from it, so there is one place to review. Its `{NAME}` operands are the same
parameters as above. They are not resolved: bwrap mounts each path where the child will look for it, so a
venv's `bin/python` symlink becomes a plain file inside the sandbox.

- `--unshare-all`, `--unshare-user` — new user, PID, network, IPC, UTS and cgroup namespaces. The network
  namespace has only its own loopback interface, which bwrap brings up at 127.0.0.1 and which reaches nothing
  outside the job. With no other interface or route, that is how "no network rule" is expressed here. The
  job cannot see or signal the server's processes. `--unshare-all` alone only tries the user namespace;
  `--unshare-user` makes it required, so a host that cannot create one refuses the job.
- `--disable-userns` — code running in the job cannot create another user namespace, which would otherwise
  hand it a fresh set of capabilities to probe the kernel with.
- `--die-with-parent`, `--new-session` — the job dies with the server's kill of its process group, and it
  cannot push input into a terminal it does not own.
- `--cap-drop ALL` — no capabilities inside, even if the server were ever started as root.
- `/usr` read-only, with the merged-`/usr` links, the dynamic loader's cache, `/etc/alternatives` and
  fontconfig's settings — the system libraries, Poppler and FFmpeg. Debian reaches some libraries only through
  `/etc/alternatives` (FFmpeg's `libblas.so.3`), and that directory holds only symlinks. Nothing else from
  `/etc` is mounted: no `passwd`, no TLS keys, no host configuration.
- `--proc /proc`, `--dev /dev` — a process table of the job's own processes, and the null, zero, full, random,
  urandom and terminal devices only.
- The input, converter, model, interpreter, venv marker and runtime — read-only, exactly as above. The venv
  marker is optional: an interpreter outside a venv has none, and bwrap refuses a missing path.
  Poppler and FFmpeg must live under `/usr` (the distribution's packages, or `/usr/local`); copies from Linuxbrew,
  snap or `/opt` pass the preflight's `which` but are not mounted inside the job, so the preflight also runs
  `pdftoppm`, `pdfinfo`, `ffmpeg` and `ffprobe` inside the sandbox, with the job's environment and directory, and
  fails when one cannot start there.
- `--bind {OUTPUT}` — the only writable mount. There is no `/tmp` at all; `TMPDIR` is the job directory.

The home directory, `~/.config/postern/`, the data directory, the staging directory as a whole and the
derivative cache are simply not mounted. Resident memory is bounded from the server as on macOS: it adds up
the job's whole process tree, because bwrap moves the job into a new session. The child's address-space
limit applies on Linux, which accepts it.

bwrap stays the job's parent. It reports a job killed by signal N as exit 128 + N, which the server reads
back as that signal, so a CPU or file-size limit still counts against the upload. Its own setup failures
exit 1 with a `bwrap: ` message; those, a missing `/usr/bin/bwrap` and an unreadable `derive.bwrap` are
host faults, never billed to the upload. A converter that has been taken over could print that message itself,
but it would only turn its own failure into a retryable one, still within the daily server-fault budget.

**Ubuntu 23.10 and later** let only programs that AppArmor approves create an unprivileged user namespace.
Install `postern-bwrap.apparmor` as its first lines describe; the preflight says so when bwrap's namespace setup is
what fails. Debian does not restrict user namespaces and needs no rule. The rule applies to every user of the host:
anyone may then run `/usr/bin/bwrap` with a user namespace, as on Debian, so it restores the pre-23.10 behavior for
bwrap rather than for Postern alone. Ubuntu's stricter `bwrap-userns-restrict` profile, which also removes
capabilities from what bwrap runs, has not been tested with `derive.bwrap`.
