"""The check that keeps the folder reserved for private material out of this
repository, run against scratch repositories. Nothing here inspects the real
checkout: the reserved folder exists on purpose in places that are not this
repository."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts import check_private_files
from scripts.check_private_files import GIT_LOCATION, RESERVED, main, problem

# Scratch repositories must not pick up the developer's or the runner's git settings, and an inherited
# GIT_DIR (set when tests run from a hook or `git rebase --exec`) must not send their commits elsewhere.
GIT_SETTINGS = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
GIT_ENV = {**{name: value for name, value in os.environ.items() if name not in GIT_LOCATION}, **GIT_SETTINGS}


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True, env=GIT_ENV).stdout


def commit_file(repo, path, message):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('x\n')
    git(repo, 'add', '--', path)
    git(repo, 'commit', '-m', message)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # The check runs git itself, so it gets the same settings as the helpers above.
    for name, value in GIT_SETTINGS.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / 'repo'
    path.mkdir()
    git(path, 'init', '--initial-branch=main')
    commit_file(path, 'README.md', 'base')
    return path


def add_then_remove(repo, name=RESERVED):
    commit_file(repo, f'{name}/plan.md', 'add')
    git(repo, 'rm', '-r', name)
    git(repo, 'commit', '-m', 'remove')


def reverted_on_a_merged_branch(repo):
    git(repo, 'checkout', '-b', 'side')
    add_then_remove(repo)
    git(repo, 'checkout', 'main')
    commit_file(repo, 'other.md', 'main moves on')
    git(repo, 'merge', '--no-ff', '-m', 'merge', 'side')


def reserved_name_as_a_file(repo):
    commit_file(repo, RESERVED, 'add as a file')
    git(repo, 'rm', RESERVED)
    git(repo, 'commit', '-m', 'remove')


def added_paths(repo, commit):
    """Paths a commit added, lower-cased."""
    listing = git(repo, 'show', '--name-status', '--format=', commit)
    return [line.split('\t', 1)[1].lower() for line in listing.splitlines() if line.startswith('A\t')]


# Value: protects=problem() reports the reserved folder when any commit reachable from HEAD touched it, even
#   after a revert or through a merged side branch, and names the commit that added it; fails_when=--full-history
#   is dropped (the merge case goes quiet), the path filter is removed or changed, only the current files are
#   read, or the newest touching commit (the removal) is named instead of the first; why_new=no test ran this
#   rule (test_package_release only keeps the folder out of release archives); seam=none; no_claim=that CI runs
#   the script or fetches the full history (scratch repositories; unit only)
@pytest.mark.parametrize('arrange', [
    lambda repo: commit_file(repo, f'{RESERVED}/plan.md', 'add'),
    add_then_remove,
    reverted_on_a_merged_branch,
    reserved_name_as_a_file,
    lambda repo: add_then_remove(repo, 'Docs-Private'),
], ids=['committed', 'committed then removed', 'reverted on a merged branch', 'committed as a file then removed',
        'another letter case'])
def test_history_that_ever_held_the_reserved_folder_is_refused(repo, arrange):
    arrange(repo)
    # Only the history may give it away: take the folder out of the files where a case left it there.
    shutil.rmtree(repo / RESERVED, ignore_errors=True)
    assert not (repo / RESERVED).exists()
    found = problem(repo)
    assert found is not None and ' was committed in ' in found
    named = found.split(' was committed in ', 1)[1].split('.', 1)[0]
    assert any(path == RESERVED or path.startswith(f'{RESERVED}/') for path in added_paths(repo, named))


# Value: protects=problem() refuses the reserved name when it is only in the files, before any commit, in any
#   letter case; fails_when=the files check is removed, follows only directories (a file or a dangling link
#   slips by) or compares names exactly; why_new=the history cases above delete the path first, so they never
#   reach this branch; seam=none; no_claim=none beyond protects
@pytest.mark.parametrize('name,make', [
    (RESERVED, lambda path: path.mkdir()),
    (RESERVED, lambda path: path.write_text('x\n')),
    (RESERVED, lambda path: path.symlink_to('missing-target')),
    ('Docs-Private', lambda path: path.mkdir()),
], ids=['folder', 'file', 'dangling link', 'another letter case'])
def test_reserved_name_in_the_files_is_refused(repo, name, make):
    make(repo / name)
    found = problem(repo)
    assert found is not None and found.startswith(f'{name} is in the files')


# Value: protects=problem() stays quiet for a clean history and for names that only resemble the reserved one;
#   fails_when=the path filter widens to a prefix, a suffix or any depth, which would fail every honest pull
#   request that has docs/private or a nested folder of that name; why_new=the refusal tests cannot see a
#   check that refuses everything; seam=none; no_claim=none beyond protects
def test_clean_history_and_lookalike_names_pass(repo):
    for path in ('docs/private/notes.md', f'{RESERVED}.md', f'nested/{RESERVED}/notes.md', f'{RESERVED}-old/notes.md'):
        commit_file(repo, path, f'add {path}')
    assert problem(repo) is None
    assert problem(repo, all_refs=True) is None


# Value: protects=with all_refs the check also reads branches and tags that the checked-out commit does not
#   descend from, and without it only the checked-out history; fails_when=--all-refs stops reaching rev-list,
#   so a branch pushed without a pull request is never read, or the default starts reading every ref and fails
#   a developer's clone that tracks other remotes; why_new=every other case keeps the folder on HEAD's own
#   history; seam=none; no_claim=which refs the CI checkout fetches
@pytest.mark.parametrize('park', [
    lambda repo: None,
    lambda repo: (git(repo, 'tag', 'kept', 'wip'), git(repo, 'branch', '-D', 'wip')),
], ids=['unmerged branch', 'tag whose branch is gone'])
def test_other_branches_and_tags_are_read_only_with_all_refs(repo, park):
    git(repo, 'checkout', '-b', 'wip')
    commit_file(repo, f'{RESERVED}/plan.md', 'add')
    git(repo, 'checkout', 'main')
    park(repo)
    assert problem(repo) is None
    found = problem(repo, all_refs=True)
    assert found is not None and ' was committed in ' in found
    assert main([str(repo)]) == 0 and main(['--all-refs', str(repo)]) == 1


# Value: protects=a shallow clone is refused instead of passing because the offending commit is out of sight;
#   fails_when=the shallow guard is removed, so a CI checkout that stops fetching the full history turns the
#   history rule into a silent pass; why_new=every other case runs on a complete history; seam=none;
#   no_claim=that the workflow asks for the full history (a local file clone stands in for the CI checkout)
def test_shallow_clone_is_refused_rather_than_trusted(repo, tmp_path):
    add_then_remove(repo)
    commit_file(repo, 'later.md', 'later')
    shallow = tmp_path / 'shallow'
    git(tmp_path, 'clone', '--depth', '1', repo.as_uri(), str(shallow))
    assert not (shallow / RESERVED).exists()
    found = problem(shallow)
    assert found is not None and 'shallow clone' in found


# Value: protects=a folder that is not the top of a checkout is refused, because git would answer for the
#   repository around it and the paths would not line up; fails_when=the top-folder comparison is removed, so
#   a subfolder (or a copy of the script inside another checkout) reports a clean history it never read;
#   why_new=every other case passes the top of a scratch repository; seam=none; no_claim=none beyond protects
def test_subfolder_of_a_checkout_is_refused_instead_of_passing(repo):
    add_then_remove(repo)
    (repo / 'sub').mkdir()
    assert problem(repo) is not None
    found = problem(repo / 'sub')
    assert found is not None and 'not the top folder' in found


# Value: protects=the check reads the repository in the folder it was given even when the environment points
#   git somewhere else; fails_when=git() stops dropping GIT_DIR and its siblings, so a run from a hook or
#   `git rebase --exec` inspects (and these tests would write to) another repository; why_new=no other case
#   sets a git location variable; seam=none; no_claim=none beyond protects
def test_inherited_git_dir_does_not_redirect_the_check(repo, tmp_path, monkeypatch):
    decoy = tmp_path / 'decoy'
    decoy.mkdir()
    git(decoy, 'init', '--initial-branch=main')
    add_then_remove(decoy)
    monkeypatch.setenv('GIT_DIR', str(decoy / '.git'))
    assert problem(repo) is None
    add_then_remove(repo)
    assert problem(repo) is not None


# Value: protects=the command exits 1 and prints one GitHub error annotation when the rule is broken, and
#   exits 0 in silence when it is kept; fails_when=main() ignores problem(), returns 0 on a finding, or drops
#   the ::error line CI turns into the run's annotation; why_new=the cases above call problem() directly and
#   never the command CI runs; seam=none; no_claim=how GitHub renders the annotation
def test_command_exit_code_and_annotation(repo, capsys):
    assert main([str(repo)]) == 0
    assert capsys.readouterr().out == ''
    commit_file(repo, f'{RESERVED}/plan.md', 'add')
    assert main([str(repo)]) == 1
    out = capsys.readouterr().out
    assert out.startswith('::error title=Private folder::') and out.count('\n') == 1
    assert f'{RESERVED}/ must never be committed' in out


# Value: protects=run with no arguments, as CI runs it, the script inspects the checkout it lives in and its
#   exit status reaches the shell; fails_when=the __main__ line stops passing main()'s result to SystemExit
#   (the job would always be green) or ROOT stops pointing at the script's own checkout; why_new=every other
#   case calls main() or problem() in-process with an explicit path; seam=none; no_claim=that the workflow
#   calls the script (a copy in a scratch repository stands in for the CI checkout)
def test_script_run_without_arguments_checks_its_own_checkout(repo):
    script = repo / 'scripts' / 'check_private_files.py'
    script.parent.mkdir()
    shutil.copy(check_private_files.__file__, script)

    def run():
        # Started from another folder: the script must find its checkout from its own location.
        return subprocess.run([sys.executable, str(script)], cwd=Path(repo).parent, capture_output=True,
                              text=True, env=GIT_ENV)

    clean = run()
    assert (clean.returncode, clean.stdout) == (0, '')
    commit_file(repo, f'{RESERVED}/plan.md', 'add')
    refused = run()
    assert refused.returncode == 1 and refused.stdout.startswith('::error title=Private folder::')


# Value: protects=the check fails loudly when git cannot answer, instead of reading "no output" as "no
#   commit"; fails_when=check=True is dropped from the git helper, so a folder that is not a repository (or a
#   broken git) returns None and the job goes green; why_new=every other case runs in a working repository;
#   seam=none; no_claim=the wording of that failure (a traceback after git's own message)
def test_folder_that_is_not_a_repository_fails_instead_of_passing(tmp_path, monkeypatch):
    for name, value in GIT_SETTINGS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path))
    plain = tmp_path / 'plain'
    plain.mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        problem(plain)
