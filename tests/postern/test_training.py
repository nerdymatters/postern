"""Private training survives a code upgrade without leaking into the software package."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from postern import exam, quiz, serve, training
from postern.config import Config
from postern.service import Service


TRACK = 'WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nA fictional library welcome.\n'
DIGEST = 'a' * 64


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'old-checkout' / 'postern'
    root.mkdir(parents=True)
    for name in ('quizzes.json', 'final_exam.json'):
        shutil.copyfile(training.SAMPLE / name, root / name)
    (root / 'captions').mkdir()
    (root / 'captions' / f'{DIGEST}.en.vtt').write_text(TRACK, encoding='utf-8')
    return root


def contents(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def write_lock(source, path):
    # Historical releases keyed each question to the first 16 hex digits of this list hash.
    data = json.loads((source / 'quizzes.json').read_text(encoding='utf-8'))
    lock = {question['id']: hashlib.sha256(json.dumps([question['answer'], question['options']],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
            for chapter in data['chapters'] for question in chapter['questions']}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock), encoding='utf-8')


def change_question(source):
    path = source / 'quizzes.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['chapters'][0]['questions'][0]['options'][0]['en'] += ' changed'
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def test_google_startup_requires_private_training_before_opening_store_or_database(tmp_path, monkeypatch):
    import postern.service as service_module

    def unexpected(*args, **kwargs):
        pytest.fail('Invalid training reached the store or database constructor')

    monkeypatch.setattr(service_module, 'Database', unexpected)
    monkeypatch.setattr(service_module, 'GoogleStore', unexpected)
    config = Config(data_dir=tmp_path / 'data', google_client_id='example', host_credentials='unused',
                    allowlist_sheet_id='example', drive_folder_id='example', database_sheet_id='example')
    with pytest.raises(ValueError, match='Missing private training'):
        Service(config)
    assert not (config.data_dir / 'postern.sqlite3').exists()


def test_demo_uses_samples_only_when_private_directory_is_entirely_absent(tmp_path):
    data = tmp_path / 'data'
    assert training.resolve(data, 'demo') == training.SAMPLE
    bank, final, tracks = training.load(training.resolve(data, 'demo'))
    assert bank and final[exam.KEY]['questions'] and tracks == {}
    assert not data.exists()
    (data / 'training').mkdir(parents=True)
    with pytest.raises(ValueError, match='Missing private training'):
        training.resolve(data, 'demo')


def test_broken_private_directory_symlink_does_not_enable_demo_fallback(tmp_path):
    (tmp_path / 'training').symlink_to(tmp_path / 'missing', target_is_directory=True)
    with pytest.raises(ValueError, match='Missing private training'):
        training.resolve(tmp_path, 'demo')


@pytest.mark.parametrize('missing', ['quizzes.json', 'final_exam.json', 'captions'])
def test_incomplete_private_content_refuses_startup_instead_of_using_samples(source, missing):
    target = source.parent / 'data' / 'training'
    shutil.copytree(source, target)
    path = target / missing
    shutil.rmtree(path) if path.is_dir() else path.unlink()
    with pytest.raises(ValueError, match='Missing private'):
        Service(Config(data_dir=target.parent, mode='demo'))
    assert not (target.parent / 'postern.sqlite3').exists()


def test_migration_preserves_bytes_versions_captions_and_private_permissions(source, tmp_path):
    before = training.load(source)
    original = contents(source)
    data = tmp_path / 'data'
    result = training.migrate(source, data)
    target = data / 'training'
    assert result['status'] == 'installed'
    assert training.load(target) == before
    assert result['quiz_versions'] == {str(number): entry['version'] for number, entry in before[0].items()}
    assert result['exam_version'] == before[1][exam.KEY]['version']
    assert before[2] == {DIGEST: TRACK}
    for name, value in original.items():
        assert (target / name).read_bytes() == value
    for path in [data, target, *target.rglob('*')]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert json.loads((target / 'migration.json').read_text())['sha256'] == result['sha256']


def test_service_reads_migrated_content_instead_of_bundled_defaults(source, tmp_path):
    path = source / 'quizzes.json'
    bank = json.loads(path.read_text(encoding='utf-8'))
    bank['chapters'][0]['questions'][0]['prompt']['en'] = 'A private fictional practice prompt.'
    path.write_text(json.dumps(bank, ensure_ascii=False), encoding='utf-8')
    expected = training.load(source)
    assert expected[0][1]['version'] != quiz.load()[1]['version']
    training.migrate(source, tmp_path / 'data')
    service = Service(Config(data_dir=tmp_path / 'data', mode='demo'))
    try:
        assert service.permissions.quizzes == expected[0]
        assert service.final_exam == expected[1]
        assert service.subtitles == {DIGEST: TRACK}
    finally:
        asyncio.run(service.close())


def test_identical_migration_is_idempotent_and_does_not_rewrite_files(source, tmp_path):
    data = tmp_path / 'data'
    training.migrate(source, data)
    target = data / 'training'
    before = {str(path.relative_to(target)): (path.read_bytes(), path.stat().st_mtime_ns)
              for path in target.rglob('*') if path.is_file()}
    assert training.migrate(source, data)['status'] == 'already installed'
    after = {str(path.relative_to(target)): (path.read_bytes(), path.stat().st_mtime_ns)
             for path in target.rglob('*') if path.is_file()}
    assert after == before


def test_migration_refuses_to_overwrite_different_installed_content(source, tmp_path):
    data = tmp_path / 'data'
    training.migrate(source, data)
    before = contents(data)
    (source / 'captions' / f'{DIGEST}.en.vtt').write_text(TRACK.replace('welcome', 'map'), encoding='utf-8')
    with pytest.raises(ValueError, match='different training content'):
        training.migrate(source, data)
    assert contents(data) == before


def test_check_validates_without_creating_data_or_changing_source(source, tmp_path):
    data = tmp_path / 'not-created'
    before = contents(source.parent)
    result = training.migrate(source, data, check=True)
    assert result['status'] == 'ready to migrate'
    assert not data.exists()
    assert contents(source.parent) == before


def test_malformed_bank_never_installs_partial_training(source, tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'keep.txt').write_text('existing data')
    before = contents(data)
    (source / 'quizzes.json').write_text('{bad json')
    with pytest.raises(ValueError):
        training.migrate(source, data)
    assert contents(data) == before
    assert sorted(path.name for path in data.iterdir()) == ['keep.txt']
    target = data / 'training'
    shutil.copytree(source, target)
    with pytest.raises(ValueError):
        Service(Config(data_dir=data, mode='demo'))
    assert not (data / 'postern.sqlite3').exists()


def test_legacy_registry_is_copied_and_changed_question_ids_refuse_startup(source, tmp_path):
    legacy = source.parent / 'tests' / 'postern' / 'quiz_ids.lock.json'
    write_lock(source, legacy)
    data = tmp_path / 'data'
    training.migrate(source, data)
    target = data / 'training'
    assert (target / 'quiz_ids.lock.json').read_bytes() == legacy.read_bytes()
    assert training.load(target) == training.load(source)
    change_question(target)
    with pytest.raises(ValueError, match='new ID'):
        Service(Config(data_dir=data, mode='demo'))
    assert not (data / 'postern.sqlite3').exists()


def test_invalid_registry_does_not_leave_a_partial_migration(source, tmp_path):
    write_lock(source, source.parent / 'tests' / 'postern' / 'quiz_ids.lock.json')
    change_question(source)
    data = tmp_path / 'data'
    data.mkdir()
    for check in (True, False):
        with pytest.raises(ValueError, match='new ID'):
            training.migrate(source, data, check=check)
        assert list(data.iterdir()) == []


def test_preflight_checks_configured_training_and_reports_missing_private_data(source, tmp_path, monkeypatch):
    # Tool discovery and native inference are unrelated to which training directory is checked.
    from postern import derive_child
    monkeypatch.setattr(serve.shutil, 'which', lambda name: None)
    monkeypatch.setattr(serve, 'profile_compiles', lambda: (True, 'stubbed'))
    monkeypatch.setattr(derive_child, 'detect_faces', lambda image: [])
    path = source / 'quizzes.json'
    bank = json.loads(path.read_text(encoding='utf-8'))
    bank['chapters'] = bank['chapters'][:1]
    path.write_text(json.dumps(bank, ensure_ascii=False), encoding='utf-8')
    data = tmp_path / 'data'
    training.migrate(source, data)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'data_dir': str(data), 'mode': 'google'}))
    checks = {name: (ok, detail) for name, ok, detail in serve.preflight(config)}
    assert checks['quiz bank'] == (True, '1 chapter quiz(zes), 12 questions')
    assert checks['final exam and subtitles'] == (True, '20 bilingual questions, pass with 16; 1 English tracks')
    shutil.rmtree(data / 'training')
    missing = {name: (ok, detail) for name, ok, detail in serve.preflight(config)}
    assert missing['quiz bank'][0] is False
    assert missing['final exam and subtitles'][0] is False
    assert 'Missing private training' in missing['quiz bank'][1]


@pytest.mark.parametrize('problem', ['empty', 'guessable'])
def test_migration_refuses_empty_or_guessable_chapter_banks(source, tmp_path, problem):
    path = source / 'quizzes.json'
    bank = json.loads(path.read_text(encoding='utf-8'))
    if problem == 'empty':
        bank['chapters'] = []
        message = 'at least one chapter'
    else:
        # Every question is valid, but always choosing a would pass every hand.
        for chapter in bank['chapters']:
            for question in chapter['questions']:
                question['answer'] = 'a'
        message = 'cannot deal balanced, unguessable hands'
    path.write_text(json.dumps(bank, ensure_ascii=False), encoding='utf-8')
    data = tmp_path / 'data'
    with pytest.raises(ValueError, match=message):
        training.migrate(source, data)
    assert not data.exists()


@pytest.mark.parametrize('linked', ['source-file', 'destination'])
def test_migration_refuses_symlink_assets_or_destination(source, tmp_path, linked):
    data = tmp_path / 'data'
    data.mkdir()
    if linked == 'source-file':
        path = source / 'quizzes.json'
        outside = tmp_path / 'outside-quizzes.json'
        path.rename(outside)
        path.symlink_to(outside)
        message = 'regular files'
    else:
        (data / 'training').symlink_to(source, target_is_directory=True)
        message = 'destination must not be a symlink'
    before = contents(source)
    with pytest.raises(ValueError, match=message):
        training.migrate(source, data)
    assert contents(source) == before
    if linked == 'destination':
        assert (data / 'training').is_symlink()
        assert (data / 'training').resolve() == source
    else:
        assert list(data.iterdir()) == []


@pytest.mark.parametrize('concurrent_change', ['source-changed', 'destination-created'])
def test_migration_refuses_concurrent_changes_and_cleans_staging(source, tmp_path, monkeypatch, concurrent_change):
    data = tmp_path / 'data'
    target = data / 'training'
    temporary_directory = training.tempfile.TemporaryDirectory

    @contextmanager
    def staging_with_concurrent_change(*args, **kwargs):
        with temporary_directory(*args, **kwargs) as staging:
            # Another process acts after source validation and before the copy is published.
            if concurrent_change == 'source-changed':
                (source / 'captions' / f'{DIGEST}.en.vtt').write_text(TRACK.replace('welcome', 'map'), encoding='utf-8')
            else:
                target.mkdir()
                (target / 'existing.txt').write_text('Do not replace this installation.')
            yield staging

    monkeypatch.setattr(training.tempfile, 'TemporaryDirectory', staging_with_concurrent_change)
    message = 'files changed during migration' if concurrent_change == 'source-changed' else 'destination appeared during migration'
    with pytest.raises(ValueError, match=message):
        training.migrate(source, data)
    if concurrent_change == 'source-changed':
        assert list(data.iterdir()) == []
    else:
        assert contents(data) == {'training/existing.txt': b'Do not replace this installation.'}
        assert list(data.iterdir()) == [target]


def test_migration_cli_check_reports_success_without_writing(source, tmp_path):
    data = tmp_path / 'data'
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'data_dir': str(data)}))
    before = contents(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'postern.training', 'migrate', '--source', str(source),
                             '--config', str(config), '--check'], cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['status'] == 'ready to migrate'
    assert output['caption_tracks'] == 1
    assert not data.exists()
    assert contents(tmp_path) == before


@pytest.mark.parametrize('problem', ['invalid-config', 'missing-source'])
def test_migration_cli_errors_exit_nonzero_without_writing(source, tmp_path, problem):
    data = tmp_path / 'data'
    config = tmp_path / 'config.json'
    config.write_text('{not json' if problem == 'invalid-config' else json.dumps({'data_dir': str(data)}))
    selected_source = source if problem == 'invalid-config' else tmp_path / 'missing-source'
    before = contents(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'postern.training', 'migrate', '--source', str(selected_source),
                             '--config', str(config)], cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'Training migration refused:' in result.stderr
    assert 'Traceback' not in result.stderr
    assert not result.stdout
    assert not data.exists()
    assert contents(tmp_path) == before
