"""Load private training content, or migrate it out of an older private checkout.

Production requires data_dir/training. Only demo mode may use the fictional
sample shipped with Postern, and only when no private training directory exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import captions, exam, quiz

SAMPLE = Path(__file__).with_name('sample_training')


def resolve(data_dir, mode):
    root = Path(data_dir).expanduser().resolve() / 'training'
    if mode == 'demo' and not root.exists() and not root.is_symlink():
        return SAMPLE
    require_files(root)
    return root


def require_files(root):
    root = Path(root)
    for name in ('quizzes.json', 'final_exam.json'):
        if not (root / name).is_file():
            raise ValueError(f'Missing private training file: {root / name}. Run postern.training migrate before starting Postern.')
    if not (root / 'captions').is_dir():
        raise ValueError(f'Missing private captions directory: {root / "captions"}')


def load(root, *, lock_path=None):
    root = Path(root)
    require_files(root)
    bank = quiz.load(root / 'quizzes.json')
    final = exam.load(root / 'final_exam.json')
    tracks = captions.load(root / 'captions')
    if not bank:
        raise ValueError('The training quiz bank must contain at least one chapter')
    lock = Path(lock_path) if lock_path is not None else root / 'quiz_ids.lock.json'
    if lock.is_file():
        identities = json.loads(lock.read_text(encoding='utf-8'))
        for entry in bank.values():
            for question in entry['questions']:
                identity = [question['answer'], question['options']]
                digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
                if identities.get(question['id']) != digest:
                    raise ValueError(f'Question {question["id"]} differs from quiz_ids.lock.json; changed answers/options need a new ID')
    return bank, final, tracks


def source_files(root):
    root = Path(root)
    require_files(root)
    files = {name: root / name for name in ('quizzes.json', 'final_exam.json')}
    files.update({f'captions/{p.name}': p for p in (root / 'captions').glob('*.en.vtt')})
    lock = root / 'quiz_ids.lock.json'
    if not lock.exists():
        lock = root.parent / 'tests' / 'postern' / 'quiz_ids.lock.json'
    if lock.is_file():
        files['quiz_ids.lock.json'] = lock
    for path in files.values():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Training inputs must be regular files: {path}')
    return files


def describe(root):
    files = source_files(root)
    bank, final, tracks = load(root, lock_path=files.get('quiz_ids.lock.json'))
    weak = [str(number) for number, entry in bank.items() if entry['tier'] != 'full']
    if weak:
        raise ValueError('Quiz chapters cannot deal balanced, unguessable hands: ' + ', '.join(weak))
    return {
        'chapters': len(bank), 'questions': sum(len(entry['questions']) for entry in bank.values()),
        'quiz_versions': {str(number): entry['version'] for number, entry in bank.items()},
        'exam_version': final[exam.KEY]['version'], 'caption_tracks': len(tracks),
        'sha256': {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()},
    }


def migrate(source, data_dir, *, check=False):
    """Copy only training assets; validate before atomically installing, never overwrite."""
    source = Path(source).expanduser().resolve()
    data_dir = Path(data_dir).expanduser().resolve()
    target = data_dir / 'training'
    files = source_files(source)
    expected = describe(source)
    if target.is_symlink():
        raise ValueError('The training destination must not be a symlink')
    if target.exists():
        if describe(target) != expected:
            raise ValueError(f'{target} already contains different training content; nothing was overwritten')
        return {'status': 'already installed', **expected}
    if check:
        return {'status': 'ready to migrate', **expected}
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.training-', dir=data_dir) as temporary:
        stage = Path(temporary) / 'training'
        stage.mkdir(mode=0o700)
        (stage / 'captions').mkdir(mode=0o700)
        for name, path in files.items():
            destination = stage / name
            # Create owner-only before writing any private bytes.
            with destination.open('xb') as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(path.read_bytes())
        if describe(stage) != expected:
            raise ValueError('Training files changed during migration; nothing was installed')
        manifest = stage / 'migration.json'
        with manifest.open('x') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(expected, output, indent=2)
            output.write('\n')
        if target.exists() or target.is_symlink():
            raise ValueError('The training destination appeared during migration; retry to verify it')
        stage.rename(target)
    return {'status': 'installed', **expected}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('migrate', help='Copy an old private checkout into the configured data directory')
    command.add_argument('--source', type=Path, required=True, help='Old checkout/postern containing the original training files')
    command.add_argument('--config', type=Path, required=True)
    command.add_argument('--check', action='store_true', help='Validate and report without writing')
    args = parser.parse_args(argv)
    try:
        settings = json.loads(args.config.expanduser().read_text())
        result = migrate(args.source, settings['data_dir'], check=args.check)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Training migration refused: {error}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
