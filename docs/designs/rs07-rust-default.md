# RS-07: make the Rust progress reducer the serving default

Owner decision D1 A (November 13, 2026): on Monday, November 16, make Rust the serving default if the RS-07 pilot is still clean. This plan prepares that change so it can merge that day.

## Where things stand

- Since November 9, the office Mac has served progress from Rust because the owner's `progress-backend.json` selects it. The watchdog guards that selection, and the in-process check has matched every staff-report comparison it made, with no native errors (read-only `postern.serve backend`, November 13).
- `progress_backend.apply_selection` takes the backend from `POSTERN_PROGRESS_BACKEND`, then from `progress-backend.json`, then falls back to `('python', 'default')`. An explicitly requested Rust backend without the verified native module fails startup, by design (`progress_backend.native`).
- The watchdog guards the selection file: `rust` is watched and returned to Python on a breach; no file means Python, and nothing is watched.
- The Linux bundle ships no native wheel; the macOS release attaches the 0.2.0 wheel and its provenance.

## Decision: make the piloted configuration the default

Engineering review (November 13, decision 5b04ce49, under the owner's standing approval): the default is the configuration the pilot proved, a `rust` selection file guarded by the watchdog, written at install. The code default stays Python. The rejected alternative, a Rust code default with no file, needs a new guarded state, a guard-version bump and new status text that the pilot never exercised, and deleting the file would silently move a host to Rust.

## Changes

1. **macOS install writes the selection** (`postern/serve.py`, `install`): when `progress-backend.json` does not exist and `progress_backend.native()` succeeds, the install bootstraps the API and the watchdog as today (the API starts on Python, since no file exists), and only after both jobs bootstrap writes `rust` with `set_by: install` and restarts the API with `launchctl kickstart -k`, so Rust starts with its watchdog already running. Any failure before that point, including a watchdog bootstrap failure, leaves no file and the API on Python. Print `ok   progress-backend.json: rust (verified native module; postern.serve backend python keeps Python)`, or with the module missing, incompatible or raising during import, write nothing and print `note progress-backend.json: not set; Python serves (native module not installed)`. An existing selection, `python`, `rust` or unreadable, is never changed, `--force` included. `--check-only`, which the office rollout helper runs, writes nothing.
2. **Owner-owned write** (`postern/progress_backend.py`, `write_selection`): an optional owner argument changes the temporary file's owner before the rename, because the macOS rerun under sudo runs as root and the API refuses a root-owned selection. No root-owned file ever exists at the final path. `postern.serve backend` keeps writing without it.
3. **Preflight line**: the install preflight reports whether the native module is importable and compatible, so the owner sees which backend the install will select before anything is written.
4. **Linux is unchanged** (engineering review F1): `systemd.install` only renders units for a later root command, so a selection written there would apply even if that command never ran, and the Linux bundle ships no native wheel. A Linux owner who installs the wheel selects Rust with `postern.serve backend rust`, as today.
5. **Docs**: `docs/runbooks/rust-server.md` (cutover and rollback: a fresh macOS install with the wheel selects Rust under its watchdog; `postern.serve backend python` keeps Python; the code default stays Python), `docs/runbooks/linux.md` (Linux stays on Python until the owner installs the wheel and selects Rust), and ROADMAP's RS-07 item.

## Upgrade and rollback

- The office Mac keeps its `progress-backend.json` (`rust`), and its rollout does not rerun `install`, so nothing changes there. Owner decision D5 A puts this release on the office Mac with v0.29.1 and v0.29.2 after November 16.
- A fresh macOS install with the wheel ends on Rust, guarded by the watchdog it installed first. A fresh Linux install stays on Python.
- Rollback: `postern.serve backend python` and a restart, as today. A code rollback to v0.29.2 keeps whatever file the install wrote.

## Tests

- macOS `install` (fakes for `launchctl` and the native import): with the module importable and no file, both jobs bootstrap first, then `rust` is written owner-owned 0600 with `set_by: install`, then the API is kickstarted; with the module missing, at the wrong API version or raising during import, nothing is written; a watchdog bootstrap failure after the API bootstrap writes nothing and leaves Python; an existing `python`, `rust` or unreadable file is never replaced, `--force` included; `--check-only` writes nothing.
- `write_selection` with an owner: the temporary file's owner changes before the rename (call order), so the final path is never root-owned; without an owner it behaves as today.
- Preflight: the native-module line for importable, missing and incompatible modules.
- Linux: `systemd.install` writes no selection (a regression row in `test_linux_host.py`).

## Monday check (before merge)

Current status alone cannot prove the pilot stayed on Rust, because the in-process counters reset at each start and re-selecting Rust overwrites a fallback note (engineering review F3). On November 16, read only:
- `postern.serve backend` (as im, never sudo): Rust serving from `progress-backend.json`, the watchdog guarding this selection, the in-process check matching with no native errors;
- `progress-backend.json`: `backend: rust` with no `fallback` field;
- `~/Library/Logs/postern/postern.log`: every `postern: progress backend ...` start line since November 9, 11:10 AM EST reads `rust (progress-backend.json)`, and none reads `python`.
If any of these fails, hold the merge and report; otherwise merge, release v0.30.0 and roll it out with v0.29.1 and v0.29.2 under the usual quiet-minute procedure.

## Out of scope

Changing the code default, Linux automatic selection, RS-08 and RS-09, D7's watchdog lookup-expiry change (a separate Codex brief), the startup priming deadline and the other RS-07 review follow-ups in TODOS.

## Engineering review (November 13, 2026)

Reviewed by `/plan-eng-review` with a Codex outside voice. Decisions below were taken under the owner's standing advance approval (a-1759af8047f8), each at the review's recommendation; the owner can reverse any of them.

### Decision ledger

- **S1 scope (approved, decision 5b04ce49):** make the piloted configuration (a `rust` selection file guarded by the watchdog) the default at install, instead of a Rust code default. The code default stays Python.
- **S2 structure (approved):** the original arrangement, with the owner-owned write as an option on the existing `write_selection` rather than a new helper, once Linux dropped out (one caller).
- **R1 (approved):** [CRITICAL] (confidence: 8/10) `postern/serve.py:442` — the macOS install's second phase runs as root under sudo, so a plain `write_selection` would leave a root-owned selection the API refuses at startup. Remedy: chown the temporary file before the rename; test the call order.
- **R2 (approved):** [INFORMATIONAL] (confidence: 7/10) `postern/progress_backend.py:190` — `native()` converts only `ImportError`; any other import-time exception would crash the install. Remedy: any exception means "not verified", Python stays.
- **OV1 (approved, Codex High):** `postern/systemd.py:162` renders units for a later root command, so a selection written while rendering would apply even if that command never ran. Remedy: no automatic selection on Linux; the Linux bundle ships no wheel.
- **OV2 (approved, Codex High):** `postern/serve.py:447` starts the API before the watchdog; a watchdog bootstrap failure after a Rust start would leave Rust unguarded. Remedy: write the selection only after both jobs bootstrap, then kickstart the API.
- **OV3 (approved, Codex Medium):** `postern/progress_backend.py:134` counters reset at each start and re-selecting Rust overwrites fallback notes, so current status alone cannot prove the pilot. Remedy: the Monday check also reads the selection file's fallback fields and the `postern.log` start lines since November 9.
- **T1 TODO (approved, A):** extend the existing Linux host TODO: select Rust at Linux install once the Linux bundle ships the native wheel.

Approval readiness: PASS (S1, S2, R1, R2, OV1, OV2, OV3, T1; each an authorized auto-decision under a-1759af8047f8).

### What already exists

`progress_backend.native()` (wheel verification), `progress_backend.write_selection` (atomic, owner-only), the watchdog's guarded fallback for a `rust` file (in production since November 9), and `postern.serve install` on macOS (owner phase, then a sudo rerun).

### NOT in scope

Changing the code default; Linux automatic selection (until the Linux bundle ships a wheel); RS-08 and RS-09; D7 (separate Codex brief); the startup priming deadline and the other RS-07 follow-ups in TODOS.

### Failure modes

- Root-owned selection after the sudo rerun: prevented by R1 (critical gap closed in the plan).
- Rust running without its watchdog after a partial install: prevented by OV2.
- Wheel present but broken at import: Python stays (R2).
- Monday check passing on a recently restarted process that hid an earlier fallback: prevented by OV3.
Failure modes: 0 critical gaps flagged (the one critical finding is remedied in the plan).

### Implementation tasks

1. `write_selection(..., owner=None)`: chown the temporary file before the rename when an owner is given; tests for call order and the unchanged default.
2. macOS `install`: after both bootstraps, when no selection exists and `native()` succeeds, write `rust` (set_by install, owned by the service account) and kickstart the API; print the ok or note line; never touch an existing file; `--check-only` writes nothing.
3. Preflight: a native-module line.
4. A Linux regression row: `systemd.install` writes no selection.
5. Docs: `rust-server.md`, `linux.md`, ROADMAP RS-07; TODOS line (T1).

Test plan: `~/.gstack/projects/chirayukong-postern/im-rs07-rust-default-eng-review-test-plan-20261113-135625.md`.

### Completion summary

Step 0: scope reduced per recommendation (S1). Architecture 1 issue (R1), code quality 1 issue (R2), tests 0 gaps beyond the plan rows, performance no issues. Outside voice: Codex completed, 3 findings, all accepted. Unresolved decisions: 0. Critical gaps: 0.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Outside Review | Codex (gpt-6-astra), plan-eng-review outside voice | Independent 2nd opinion | 1 | completed | 3 findings (2 High, 1 Medium), 3 accepted |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES OPEN (2 issues, resolved in the plan) | 2 issues, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** Codex, plan-review phase, completed; 3 findings (Linux render-time write, watchdog bootstrap order, Monday evidence), all accepted into the plan.
- **CROSS-MODEL:** no overlap; the native review found the root-owned write and the import-exception gap, and Codex found the install sequencing and evidence gaps.
- **VERDICT:** ENG reviewed with every finding resolved in the plan — ready to implement.

NO UNRESOLVED DECISIONS
