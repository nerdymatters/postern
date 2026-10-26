#!/usr/bin/env bash
# Vercel "Install Command" for the Postern SPA (wired in /vercel.json).
#
# dashboard/web/bun.lock is lockfileVersion 2, written by Bun >= 1.4, and Vercel's
# build image has shipped an older Bun ("Unknown lockfile version" -> failed build).
# So choose a Bun that can read it: the container's /bun1.4 when present, the PATH
# bun when it is new enough, otherwise a pinned bun from npm. The chosen binary is
# recorded in .bun-path so vercel-build.sh builds with the same one.
set -euo pipefail
WANT="${BUN_WANT:-1.4.0}"
WEB="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WEB"

new_enough() {  # bun >= 1.4
  local v="${1%%-*}" major minor
  IFS=. read -r major minor _ <<<"$v"
  [[ "${major:-x}" =~ ^[0-9]+$ && "${minor:-x}" =~ ^[0-9]+$ ]] || return 1
  (( major > 1 || (major == 1 && minor >= 4) ))
}

BUN=""
for cand in "${BUN_BIN:-}" /bun1.4 "$(command -v bun 2>/dev/null || true)" "$HOME/.bun/bin/bun"; do
  [[ -n "$cand" && -x "$cand" ]] || continue
  v="$("$cand" --version 2>/dev/null || true)"
  if new_enough "$v"; then BUN="$cand"; break; fi
  echo "vercel-install: $cand is bun ${v:-?} (too old for bun.lock v2)"
done
if [[ -z "$BUN" ]]; then
  # the form Vercel's own issue tracker documents as working in the container
  # (vercel/vercel#17577); $BUN is a command line, so it is expanded unquoted below
  BUN="npx --yes bun@$WANT"
fi
echo "vercel-install: using $BUN (bun $($BUN --version))"
printf '%s\n' "$BUN" > .bun-path

if ! $BUN install --frozen-lockfile; then
  echo "vercel-install: frozen install failed; retrying without --frozen-lockfile"
  $BUN install
fi
