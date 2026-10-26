#!/usr/bin/env bash
# Vercel "Build Command" for the Postern SPA (wired in /vercel.json): build with
# the Bun that vercel-install.sh chose. VITE_POSTERN_API must be set in the Vercel
# project to the API's HTTPS origin; without it the bundle talks to its own origin,
# which is what the FastAPI-served copy wants.
set -euo pipefail
WEB="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WEB"
BUN="$(cat .bun-path 2>/dev/null || command -v bun)"  # a command line: expand unquoted
echo "vercel-build: using $BUN (bun $($BUN --version))"
echo "vercel-build: VITE_POSTERN_API=${VITE_POSTERN_API:-<same origin>}"
$BUN run build
