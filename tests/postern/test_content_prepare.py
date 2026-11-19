"""Real native PDF/MP4 preparation through the existing OS sandbox."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from PIL import Image
import psutil
import pytest

from postern.content_objects import LocalContentPublisher
from postern import content_prepare, derive
from postern.content_prepare import ContentPrepareError, prepare_content


@pytest.fixture
def preparation(tmp_path):
    root = (tmp_path/'objects').resolve()
    root.mkdir(mode=0o700)
    with LocalContentPublisher(root, max_object_bytes=64 * 1024 * 1024) as publisher:
        yield publisher, dict(space_id=str(uuid.uuid4()), resource_id=str(uuid.uuid4()), revision=1)


def pdf():
    buffer = io.BytesIO()
    with Image.new('RGB', (72, 96), 'white') as first, Image.new('RGB', (72, 96), 'blue') as second:
        first.save(buffer, format='PDF', save_all=True, append_images=[second])
    return buffer.getvalue()


def test_actual_pdf_original_pages_and_thumbnail_inventory(preparation):
    publisher, identity = preparation
    source = pdf()
    result = prepare_content(publisher, **identity, kind='document', source=source)
    assert result.manifest['metadata'] == {'page_count': 2}
    assert result.original.key == '/'.join((identity['space_id'], identity['resource_id'], '1', hashlib.sha256(source).hexdigest()))
    assert publisher.read(result.original) == source
    assert [(d.representation, d.index) for d in result.descriptors] == [('page', 0), ('thumbnail', 0), ('page', 1), ('thumbnail', 1)]
    for descriptor in result.descriptors:
        data = publisher.read(descriptor)
        assert hashlib.sha256(data).hexdigest() == descriptor.sha256
        with Image.open(io.BytesIO(data)) as image:
            assert image.format == 'JPEG' and 0 < image.width*image.height <= 8_000_000
    repeated = prepare_content(publisher, **identity, kind='document', source=source)
    assert repeated == result
    assert all(set(item) == {'representation', 'index', 'sha256', 'size'} for item in result.inventory)


def test_actual_video_remux_and_exact_chunks(preparation, tmp_path):
    publisher, identity = preparation
    clip = tmp_path/'native.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=12',
                    '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)],
                   check=True, capture_output=True, timeout=30)
    source = clip.read_bytes()
    result = prepare_content(publisher, **identity, kind='video', source=source, chunk_bytes=1024)
    metadata = result.manifest['metadata']
    assert publisher.read(result.original) == source
    assert 0 < metadata['duration_ms'] <= 1000
    assert metadata['chunk_bytes'] == 1024 and metadata['chunk_count'] == len(result.descriptors) > 1
    blocks = [publisher.read(d) for d in result.descriptors]
    assert all(len(block) == 1024 for block in blocks[:-1])
    assert sum(map(len, blocks)) == metadata['total_bytes']
    assert [d.index for d in result.descriptors] == list(range(metadata['chunk_count']))
    assert all(d.representation == 'video' for d in result.descriptors)
    served = tmp_path/'prepared.mp4'
    served.write_bytes(b''.join(blocks))
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', str(served)],
                           check=True, capture_output=True, timeout=15)
    assert probe.stdout.strip() == b'h264'


@pytest.mark.parametrize('change', [dict(kind='legacy'), dict(space_id='../elsewhere'), dict(revision=True),
                                  dict(resource_id=str(uuid.uuid4()).upper()), dict(source=b''), dict(chunk_bytes=0)])
def test_invalid_identity_and_bounds_publish_nothing(preparation, tmp_path, change):
    publisher, identity = preparation
    values = dict(identity, kind='document', source=pdf())
    values.update(change)
    with pytest.raises(ContentPrepareError): prepare_content(publisher, **values)
    assert list((tmp_path/'objects').iterdir()) == []


def test_bad_document_uses_real_sandbox_and_leaves_no_published_inventory(preparation, tmp_path, monkeypatch):
    publisher, identity = preparation
    owned = []
    original_temporary = content_prepare.tempfile.TemporaryDirectory
    def temporary(*args, **kwargs):
        value = original_temporary(*args, **kwargs)
        owned.append(value.name)
        return value
    monkeypatch.setattr(content_prepare.tempfile, 'TemporaryDirectory', temporary)
    with pytest.raises(ContentPrepareError):
        prepare_content(publisher, **identity, kind='document', source=b'%PDF-1.7\nnot a valid document')
    assert list((tmp_path/'objects').iterdir()) == []
    assert owned and all(not content_prepare.Path(path).exists() for path in owned)


@pytest.mark.parametrize('failure', ['interrupt', 'unexpected', 'already_exited'])
def test_interrupted_preparation_reaps_owned_converter_group_before_cleanup(preparation, tmp_path, monkeypatch, failure):
    """Actual owned subprocesses prove cleanup; real sandboxing is tested above."""
    publisher, identity = preparation
    helper = tmp_path/'owned_converter.py'
    helper.write_text('''import json, os, pathlib, subprocess, sys, time
if sys.argv[1] == 'already_exited':
    sys.exit(0)
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
pathlib.Path(sys.argv[2]).write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(60)
''')
    processes, pids, jobs = [], [], []

    def command(source, output, *args):
        jobs.append(output)
        return [sys.executable, '-I', str(helper), failure, str(output/'owned-pids.json')]

    def alive(pid):
        try:
            process = psutil.Process(pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def interrupted_wait(self, process):
        processes.append(process)
        if failure == 'already_exited':
            process.communicate(timeout=3)
            assert process.returncode == 0
        else:
            # Do not interrupt until both the owned parent and its child exist.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    pids[:] = json.loads(Path(process.args[-1]).read_text())
                    break
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.01)
            assert pids and all(alive(pid) for pid in pids)
        if failure == 'unexpected':
            raise RuntimeError('owned accounting failure')
        raise KeyboardInterrupt()

    try:
        with monkeypatch.context() as changes:
            changes.setattr(derive, 'job_command', command)
            changes.setattr(derive.Deriver, 'wait', interrupted_wait)
            with pytest.raises(RuntimeError if failure == 'unexpected' else KeyboardInterrupt):
                prepare_content(publisher, **identity, kind='document', source=pdf())
        assert processes and all(process.returncode is not None for process in processes)
        assert all(not alive(pid) for pid in pids)
        assert jobs and all(not directory.exists() and not directory.parent.parent.exists() for directory in jobs)
        assert list((tmp_path/'objects').iterdir()) == []
        # Interrupted work releases the offline slot only after cleanup. A real
        # sandboxed conversion can subsequently publish a complete presentation.
        assert prepare_content(publisher, **identity, kind='document', source=pdf()).manifest['metadata']['page_count'] == 2
    finally:
        # Keep a failing lifecycle regression from leaving its test helpers alive.
        for process in processes:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)
