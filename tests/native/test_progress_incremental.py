"""Incremental native evidence must equal both full replayers at every prefix."""
import copy
import json
import random
import pytest
from postern import progress, progress_backend

pytest.importorskip('postern_progress_native')


def test_generated_prefixes_aliasing_and_midstream_failures():
    module = progress_backend.native()
    assert module.CHECKPOINT_VERSION == 1
    items = {'pdf': {'kind': 'pdf', 'pages': 4}, 'photo': {'kind': 'photo', 'pages': 1},
             'video': {'kind': 'video', 'pages': 0, 'duration_seconds': 100}}
    metadata = progress_backend.metadata(items)
    random.seed(11)
    checked = 0
    for trial in range(100):
        rows = []
        for number in range(random.randrange(1, 100)):
            kind = random.choice(['view_requested', 'item_open', 'page_served', 'item_summary', 'video_progress'])
            detail = random.choice([
                {}, {'page': random.choice([-1, 0, 1, 4, True, 1.0])},
                {'pages': [0, 1, 3, 9, True, 0.0]},
                {'ranges': [[0, 10], [5, 20], [99, 100.5], [100.1, 100.2]]},
                {'ranges': [[-1, 30], [20, 15], [0, 100.6], None, [True, 20]]},
                {'unused': '\ud800'},
            ])
            rows.append((str(random.randrange(4)), random.choice(list(items)), kind,
                         float(random.randrange(1, 100)), json.dumps({'detail': detail})))
        checkpoint = []
        seen = []
        while rows:
            count = random.randrange(1, 18)
            current, rows = rows[:count], rows[count:]
            seen.extend(current)
            prior = copy.deepcopy(checkpoint)
            incremental, next_checkpoint = module.reduce_incremental(metadata, iter(current), progress._evidence, checkpoint)
            assert checkpoint == prior, 'input checkpoint mutated'
            reference = module.reduce(metadata, iter(seen), progress._evidence)
            python_rows = [dict(zip(('email', 'item_id', 'type', 'ts', 'payload'), row)) for row in seen]
            python = progress._reduce_python(items, python_rows)
            assert incremental == reference == python, (trial, incremental, reference, python)
            # Caller mutation of the response must not affect the next checkpoint.
            for state in incremental.values():
                state['pages'].add(999)
            checkpoint = next_checkpoint
            checked += 1
        unchanged = copy.deepcopy(checkpoint)
        result, _ = module.reduce_incremental(metadata, iter(()), progress._evidence, checkpoint)
        assert result == reference
        def failed_cursor():
            # Force at least one entire256-row chunk to be applied before failure.
            for _ in range(300):
                yield ('0', 'pdf', 'page_served', 999.0, '{"detail":{"page":2}}')
            raise OSError('injected mid-cursor failure')
        try:
            module.reduce_incremental(metadata, failed_cursor(), progress._evidence, checkpoint)
        except OSError:
            pass
        else:
            raise AssertionError('cursor failure suppressed')
        assert checkpoint == unchanged
        result, _ = module.reduce_incremental(metadata, iter(()), progress._evidence, checkpoint)
        assert result == reference



@pytest.mark.parametrize('clock', [2**53+1, 2**63, True, 0, -1, float('nan'), float('inf')])
@pytest.mark.parametrize('seed', [False, True])
def test_nonportable_clocks_rejected_before_numeric_conversion(clock, seed):
    native = progress_backend.native()
    metadata = [('pdf', 'pdf', 4, 0)]
    checkpoint = [('r', 'pdf', [], [], True, 0, clock)] if seed else []
    rows = [] if seed else [('r', 'pdf', 'item_open', clock, '{}')]
    with pytest.raises((ValueError, OverflowError, TypeError)):
        native.reduce_incremental(metadata, iter(rows), progress._evidence, checkpoint)


@pytest.mark.parametrize('clock', [2**53-1, 2**53, 1, 1.25])
def test_exact_clock_boundaries_survive_continuation(clock):
    native = progress_backend.native()
    metadata = [('pdf', 'pdf', 4, 0)]
    first, checkpoint = native.reduce_incremental(metadata, iter([('r', 'pdf', 'item_open', clock, '{}')]), progress._evidence, [])
    second, _ = native.reduce_incremental(metadata, iter(()), progress._evidence, checkpoint)
    assert first == second and second['r', 'pdf']['last_activity_at'] == clock


def test_attempt_overflow_rejects_without_mutating_checkpoint():
    checkpoint = [('r', 'pdf', [], [], False, 2**64-1, None)]
    before = copy.deepcopy(checkpoint)
    with pytest.raises(OverflowError):
        progress_backend.native().reduce_incremental([('pdf', 'pdf', 4, 0)],
            iter([('r', 'pdf', 'view_requested', 1.0, '{}')]), progress._evidence, checkpoint)
    assert checkpoint == before
