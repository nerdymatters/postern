"""The check that keeps the folder reserved for private material out of this
repository, run against scratch repositories. Nothing here inspects the real
checkout: the reserved folder exists on purpose in places that are not this
repository."""
import os
import shutil
import subprocess

import pytest

from scripts.check_private_files import RESERVED, main, problem

# Scratch repositories must not pick up the developer's or the runner's git settings.
GIT_SETTINGS = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
GIT_ENV = {**os.environ, **GIT_SETTINGS}


def git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, env=GIT_ENV)


def commit_file(repo, path, message):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('x\n')
    git(repo, 'add', '--', path)
    git(repo, 'commit', '-m', message)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # The check runs git itself, so it gets the same isolation as the helpers above.
    for name, value in GIT_SETTINGS.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / 'repo'
    path.mkdir()
    git(path, 'init', '--initial-branch=main')
    commit_file(path, 'README.md', 'base')
    return path


def add_then_remove(repo):
    commit_file(repo, f'{RESERVED}/plan.md', 'add')
    git(repo, 'rm', '-r', RESERVED)
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


# Value: protects=problem() reports the reserved folder when any commit reachable from HEAD touched it, even
#   after a revert or through a merged side branch, and names that commit; fails_when=--full-history is dropped
#   (the merge case goes quiet), the path filter is removed or changed, or only the current files are read;
#   why_new=no test ran this rule (test_package_release only keeps the folder out of release archives);
#   seam=none; no_claim=that CI runs the script or fetches the full history (scratch repositories; unit only)
@pytest.mark.parametrize('arrange', [
    lambda repo: commit_file(repo, f'{RESERVED}/plan.md', 'add'),
    add_then_remove,
    reverted_on_a_merged_branch,
    reserved_name_as_a_file,
], ids=['committed', 'committed then removed', 'reverted on a merged branch', 'committed as a file then removed'])
def test_history_that_ever_held_the_reserved_folder_is_refused(repo, arrange):
    arrange(repo)
    # Only the history may give it away: take the folder out of the files where a case left it there.
    shutil.rmtree(repo / RESERVED, ignore_errors=True)
    assert not (repo / RESERVED).exists()
    found = problem(repo)
    assert found is not None and found.startswith(f'{RESERVED} is in commit ')
    named = found.rsplit(' ', 1)[1]
    touched = subprocess.run(['git', 'show', '--name-only', '--format=', named], cwd=repo, check=True,
                             capture_output=True, text=True, env=GIT_ENV).stdout
    assert RESERVED in touched


# Value: protects=problem() refuses the reserved name when it is only in the files, before any commit;
#   fails_when=the files check is removed or follows only directories (a file or a dangling link slips by);
#   why_new=the history cases above delete the path first, so they never reach this branch; seam=none;
#   no_claim=none beyond protects
@pytest.mark.parametrize('make', [
    lambda path: path.mkdir(),
    lambda path: path.write_text('x\n'),
    lambda path: path.symlink_to('missing-target'),
], ids=['folder', 'file', 'dangling link'])
def test_reserved_name_in_the_files_is_refused(repo, make):
    make(repo / RESERVED)
    assert problem(repo) == f'{RESERVED} is in the files'


# Value: protects=problem() stays quiet for a clean history and for names that only resemble the reserved one;
#   fails_when=the path filter widens to a prefix, a suffix or any depth, which would fail every honest pull
#   request that has docs/private or a nested folder of that name; why_new=the refusal tests cannot see a
#   check that refuses everything; seam=none; no_claim=none beyond protects
def test_clean_history_and_lookalike_names_pass(repo):
    for path in ('docs/private/notes.md', f'{RESERVED}.md', f'nested/{RESERVED}/notes.md', f'{RESERVED}-old/notes.md'):
        commit_file(repo, path, f'add {path}')
    assert problem(repo) is None


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
    assert found is not None and 'history is incomplete' in found


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
