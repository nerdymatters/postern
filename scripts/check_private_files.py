"""Fail when the folder reserved for private material is in this repository.

The top-level name ``docs-private``, in any letter case, is reserved for material
that is kept outside this repository. It must not be here: not in the files, and
not in any commit, including a commit that a later one reverts.

With no arguments the check reads the history of the checked-out commit. CI adds
``--all-refs`` so every branch and tag it fetched is read as well.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess


RESERVED = 'docs-private'
ROOT = Path(__file__).resolve().parents[1]
RULE = (f'{RESERVED}/ must never be committed to this repository, and a later revert does not undo it '
        '(see CONTRIBUTING.md).')
# Variables that make git answer for another repository than the folder it runs in.
GIT_LOCATION = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_COMMON_DIR',
                'GIT_NAMESPACE')
# Variables that change how git reads a path filter. With GIT_LITERAL_PATHSPECS=1, which
# `git --literal-pathspecs` hands to the commands it starts, the filter below would match nothing.
GIT_PATHSPEC = ('GIT_LITERAL_PATHSPECS', 'GIT_GLOB_PATHSPECS', 'GIT_NOGLOB_PATHSPECS', 'GIT_ICASE_PATHSPECS')


def git(repo: Path, *args: str) -> str:
    env = {name: value for name, value in os.environ.items() if name not in GIT_LOCATION + GIT_PATHSPEC}
    # stderr is not captured, so git's own explanation reaches the log when it fails.
    return subprocess.run(['git', *args], cwd=repo, check=True, stdout=subprocess.PIPE, text=True,
                          env=env).stdout.strip()


def problem(repo: Path, all_refs: bool = False) -> str | None:
    """Why this checkout breaks the rule or cannot be checked, or None when it keeps the rule."""
    # Letter case is ignored: on a case-insensitive disk Docs-Private and docs-private are one folder.
    present = sorted(entry.name for entry in repo.iterdir() if entry.name.lower() == RESERVED)
    if present:
        return f'{present[0]} is in the files. {RULE}'
    # git answers for whichever repository encloses a folder. From a subfolder, or from a copy of this
    # script inside another checkout, a clean answer would be about the wrong paths.
    top = Path(git(repo, 'rev-parse', '--show-toplevel'))
    if not top.samefile(repo):
        return f'{repo} is not the top folder of a checkout (that is {top}), so it cannot be checked.'
    # A shallow clone hides older commits, so a clean answer from it would prove nothing.
    if git(repo, 'rev-parse', '--is-shallow-repository') == 'true':
        return 'The history is incomplete (a shallow clone), so earlier commits cannot be checked.'
    revisions = '--all' if all_refs else 'HEAD'
    touched = git(repo, 'rev-list', '--full-history', '--topo-order', revisions, '--',
                  f':(top,icase){RESERVED}').splitlines()
    if touched:
        # Newest first, so the last one is where the folder came in: the history has to change from there.
        return f'{RESERVED} was committed in {touched[-1]}. {RULE}'
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('repo', nargs='?', type=Path, default=ROOT, help='checkout to inspect (default: this one)')
    parser.add_argument('--all-refs', action='store_true',
                        help='read every branch and tag in the clone, not only the checked-out commit')
    args = parser.parse_args(argv)
    found = problem(args.repo, all_refs=args.all_refs)
    if found:
        # The "::error" prefix makes GitHub show the line as an annotation on the run.
        print(f'::error title=Private folder::{found}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
