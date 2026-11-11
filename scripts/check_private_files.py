"""Fail when the folder reserved for private material is in this repository.

The top-level name ``docs-private/`` is reserved for material that is kept
outside this repository. It must not be here: not in the files, and not in any
commit, including a commit that a later one reverts. CI runs this check on the
full history.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess


RESERVED = 'docs-private'
ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def problem(repo: Path) -> str | None:
    """Why this checkout breaks the rule, or None when it keeps it."""
    reserved = repo / RESERVED
    if reserved.exists() or reserved.is_symlink():
        return f'{RESERVED} is in the files'
    # A shallow clone hides older commits, so a clean answer from it would prove nothing.
    if git(repo, 'rev-parse', '--is-shallow-repository') == 'true':
        return 'the history is incomplete (a shallow clone), so earlier commits cannot be checked'
    commit = git(repo, 'rev-list', '-1', '--full-history', 'HEAD', '--', RESERVED)
    if commit:
        return f'{RESERVED} is in commit {commit}'
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('repo', nargs='?', type=Path, default=ROOT, help='checkout to inspect (default: this one)')
    args = parser.parse_args(argv)
    found = problem(args.repo)
    if found:
        # The "::error" prefix makes GitHub show the line as an annotation on the run.
        print(f'::error title=Private folder::{found}. {RESERVED}/ must never be committed to this repository.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
