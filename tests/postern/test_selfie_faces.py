"""Face presence is enforced at the upload boundary, before any private storage write."""
import io
import json
import shutil
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageOps

from conftest import events
from postern import derive, derive_child, selfie, serve
from postern.derive import SelfieFaceError
from test_selfie import capture, enrollment, enroll, failing, login, read_photo

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'


def portrait():
    with Image.open(FIXTURES/'selfie-face.jpg') as image:
        return image.convert('RGB').resize((256, 256))


def encoded(image, **kwargs):
    output = io.BytesIO()
    image.save(output, 'JPEG', quality=90, **kwargs)
    return output.getvalue()


def rejected_photo(kind):
    image = Image.new('RGB', (256, 256), '#888888')
    if kind == 'objects':
        with Image.open(FIXTURES/'photo.jpg') as source:
            image = source.convert('RGB')
    elif kind == 'group':
        image.paste(portrait().resize((128, 128)), (0, 64))
        image.paste(portrait().resize((128, 128)), (128, 64))
    elif kind == 'tiny':
        image.paste(portrait().resize((96, 96)), (80, 80))
    elif kind == 'outside_crop':
        # A face in discarded pixels cannot qualify the stored center square.
        image = Image.new('RGB', (1024, 256), '#888888')
        image.paste(portrait(), (0, 0))
    return encoded(image)


@pytest.mark.parametrize(('kind', 'code'), [('blank', 'face_required'), ('objects', 'face_required'),
    ('group', 'multiple_faces'), ('tiny', 'face_too_small'), ('outside_crop', 'face_required')])
def test_real_detector_rejects_without_saving_or_allocating_cloud_id(client, service, monkeypatch, kind, code):
    original = service.store.new_id
    def unexpected():
        pytest.fail('Rejected photos must not reach Drive')
    monkeypatch.setattr(service.store, 'new_id', unexpected)
    headers = login(client)
    result = capture(client, headers, rejected_photo(kind))
    assert (result.status_code, result.json()['error']) == (422, code), result.text
    assert not service.db.read(lambda db: list(db.execute('SELECT * FROM selfies')))
    assert not events(service, 'selfie_consent_accepted')
    assert not list((service.config.data_dir/'selfies').iterdir())
    assert not list((service.config.data_dir/'staging').iterdir())
    assert client.get('/api/posts', headers=headers).json()['error'] == 'selfie_required'
    # Retakes remain possible after a rejection, without signing in again.
    monkeypatch.setattr(service.store, 'new_id', original)
    assert capture(client, headers).status_code == 202


@pytest.mark.parametrize('mirrored', [False, True])
def test_phone_rotation_and_mirroring_accept_a_face(client, mirrored):
    image = portrait()
    if mirrored:
        image = ImageOps.mirror(image)
    exif = Image.Exif()
    exif[274] = 6  # Stored sideways; must orient before detecting.
    result = capture(client, login(client), encoded(image.transpose(Image.Transpose.ROTATE_90), exif=exif))
    assert result.status_code == 202, result.text


def test_failed_replacement_keeps_previous_enrollment(client, service):
    headers = login(client)
    identity = enroll(client, headers)
    original = (service.config.data_dir/'selfies'/(identity+'.jpg')).read_bytes()
    assert capture(client, headers, rejected_photo('blank')).status_code == 422
    assert client.get('/api/selfie', headers=headers).json()['id'] == identity
    assert read_photo(service, identity)['status'] == 'active'
    assert (service.config.data_dir/'selfies'/(identity+'.jpg')).read_bytes() == original
    assert client.get('/api/posts', headers=headers).status_code == 200


def test_existing_enrollment_is_not_rechecked_or_invalidated(client, service, monkeypatch):
    headers = login(client)
    identity = enroll(client, headers)
    # Legacy enrollments need no detector or migration to remain valid.
    import hashlib
    legacy = rejected_photo('blank')
    (service.config.data_dir/'selfies'/(identity+'.jpg')).write_bytes(legacy)
    service.db.submit(lambda db: db.execute('UPDATE selfies SET sha256=? WHERE id=?',
        (hashlib.sha256(legacy).hexdigest(), identity))).result()
    def unexpected(*args):
        pytest.fail('Existing enrollment must not run new capture validation')
    monkeypatch.setattr(service.deriver, 'run', unexpected)
    assert client.get('/api/selfie', headers=headers).json()['id'] == identity
    assert client.get('/api/posts', headers=headers).status_code == 200


def test_detector_unavailable_is_retryable_and_does_not_accept_photo(client, service, monkeypatch):
    def unavailable(*args):
        raise SelfieFaceError('face_check_unavailable')
    monkeypatch.setattr(service.deriver, 'run', unavailable)
    result = capture(client, login(client))
    assert (result.status_code, result.json()['error']) == (503, 'face_check_unavailable')
    assert not list((service.config.data_dir/'selfies').iterdir())
    assert not list((service.config.data_dir/'staging').iterdir())
    assert not events(service, 'selfie_consent_accepted')


@pytest.mark.parametrize('missing', [False, True])
def test_missing_or_changed_model_fails_closed_and_preflight_reports_it(tmp_path, monkeypatch, missing):
    model = tmp_path/'model.onnx'
    if not missing:
        model.write_bytes(b'not the pinned model')
    monkeypatch.setattr(derive_child, 'FACE_MODEL', model)
    with pytest.raises(derive_child.FaceCheckError, match='face_check_unavailable'):
        derive_child.check_face(portrait())
    checks = {name: ok for name, ok, detail in serve.preflight(tmp_path/'config.json')}
    assert checks['selfie face detector'] is False


def detector_check(tmp_path):
    """The preflight's 'selfie face detector' row, as (ok, detail)."""
    return {name: (ok, detail) for name, ok, detail in serve.preflight(tmp_path/'config.json')}['selfie face detector']


def without_model(tmp_path):
    """The checked-in profile, denying only the model: native imports and image preparation still work."""
    profile = tmp_path/'derive.sb'
    profile.write_text(derive.PROFILE.read_text() + '\n(deny file-read* (literal "'+str(derive_child.FACE_MODEL.resolve())+'"))\n')
    return profile


def test_sandbox_model_read_failure_is_reported_without_parser_details(client, service, monkeypatch, tmp_path):
    monkeypatch.setattr(derive, 'PROFILE', without_model(tmp_path))
    result = capture(client, login(client))
    assert (result.status_code, result.json()['error']) == (503, 'face_check_unavailable')
    assert 'Traceback' not in result.text and 'onnx' not in result.text


@pytest.mark.parametrize('box', [(-30, 30, 130, 140), (170, 30, 130, 140)])
def test_face_extending_outside_frame_is_rejected(monkeypatch, box):
    monkeypatch.setattr(derive_child, 'detect_faces', lambda image: [[*box, 70, 80, 110, 80, 90, 100, 80, 120, 105, 120, .95]])
    with pytest.raises(derive_child.FaceCheckError, match='face_out_of_frame'):
        derive_child.check_face(portrait())


# Value: protects=every face-check outcome the converter reports reaches the person as its own refusal, and a new one
#   still asks for a retake; fails_when=FACE_REFUSALS misses a FACE_FAILURES code (a KeyError and a 500 before);
#   why_new=the messages were keyed apart from the exit codes; seam=none
def test_every_face_check_outcome_has_a_refusal_and_a_new_one_asks_for_a_retake(client, service, monkeypatch):
    assert set(selfie.FACE_REFUSALS) == set(derive_child.FACE_FAILURES.values())
    monkeypatch.setattr(service.deriver, 'run', failing('face_blurry'))
    result = capture(client, login(client))
    assert (result.status_code, result.json()) == (422, {'error': 'face_blurry', 'message': selfie.RETAKE[1]})


# Value: protects=preflight fails when the detector cannot run where enrollment runs it; fails_when=the face-detector
#   check runs only in the server process, outside the profile and limits; why_new=a sandbox-only failure showed
#   green before; seam=none
def test_preflight_runs_the_face_detector_inside_the_converter_sandbox(tmp_path, monkeypatch):
    assert detector_check(tmp_path)[0] is True
    monkeypatch.setattr(derive, 'PROFILE', without_model(tmp_path))
    ok, detail = detector_check(tmp_path)
    assert ok is False and 'inside the converter sandbox' in detail


# Value: protects=an operator sees the real cause when the sandboxed face check cannot finish; fails_when=a timeout or
#   launch failure of the sandboxed check lands in the generic install-dependencies message; why_new=the catch-all
#   covered the new check too; seam=none
def test_preflight_names_a_sandboxed_face_check_that_cannot_finish(tmp_path, monkeypatch):
    import subprocess
    def stuck():
        raise subprocess.TimeoutExpired(['/usr/bin/sandbox-exec'], 60)
    monkeypatch.setattr(serve, 'face_check_in_sandbox', stuck)
    ok, detail = detector_check(tmp_path)
    assert ok is False
    assert 'inside the converter sandbox' in detail and 'Install the locked dependencies' not in detail


# Value: protects=a selfie is prepared whenever there is room for a selfie; fails_when=the converter again checks every
#   job against the document output budget; why_new=the first disk check ignored the kind; seam=none
def test_a_selfie_needs_room_for_a_selfie_not_for_a_document(config, monkeypatch):
    config.validate()
    usage = shutil.disk_usage(config.data_dir)
    room = usage._replace(free=config.free_floor + 1024 * 1024)
    monkeypatch.setattr(derive.shutil, 'disk_usage', lambda path: room)
    assert derive.Deriver(config).run(FIXTURES/'selfie-face.jpg', config.data_dir/'staging'/'selfie-room', 'selfie')['pages'] == 1
    with pytest.raises(derive.DeriveCapacity):
        derive.Deriver(config).run(FIXTURES/'photo.jpg', config.data_dir/'derived'/'no-room', 'photo')


# Value: protects=a selfie is prepared while readers hold the document cache over its budget, and a document in that
#   state reports host capacity, not a bad file; fails_when=selfie jobs trim the derivative cache again, or trim()
#   raises a plain DeriveError; why_new=no test reached trim() at capacity; seam=none
def test_a_full_derivative_cache_refuses_documents_not_selfies(config, monkeypatch):
    config.validate()
    config.derivative_budget = 1024
    # Ample disk, so only the cache can refuse.
    room = shutil.disk_usage(config.data_dir)._replace(free=config.free_floor + 1024**3)
    monkeypatch.setattr(derive.shutil, 'disk_usage', lambda path: room)
    deriver = derive.Deriver(config)
    held = config.data_dir/'derived'/'held'
    held.mkdir()
    (held/'page-0.jpg').write_bytes(bytes(4096))
    def convert(path):
        # The page is leased while it renders, so trimming cannot evict it.
        assert deriver.run(FIXTURES/'selfie-face.jpg', config.data_dir/'staging'/'selfie-held', 'selfie')['pages'] == 1
        with pytest.raises(derive.DeriveCapacity, match='derivative cache'):
            deriver.run(FIXTURES/'photo.jpg', config.data_dir/'derived'/'photo', 'photo')
    deriver.read_path('held', 'page-0.jpg', convert)


# Value: protects=a converter that exits cleanly without its manifest is a conversion fault, never a storage fault to
#   retry (a selfie would be given back as a server fault); fails_when=the missing-manifest OSError escapes Deriver.run
#   again; why_new=no test ran a child that writes no manifest; seam=none
def test_a_converter_without_a_manifest_is_a_conversion_fault(config, tmp_path, monkeypatch):
    config.validate()
    silent = tmp_path/'silent_child.py'
    silent.write_text('import sys\nsys.exit(0)\n')
    monkeypatch.setattr(derive, 'CHILD', silent.resolve())
    with pytest.raises(derive.DeriveFault, match='Unexpected converter output') as caught:
        derive.Deriver(config).run(FIXTURES/'photo.jpg', config.data_dir/'derived'/'silent', 'photo')
    assert isinstance(caught.value.__cause__, OSError)


# Value: protects=a converter the host cannot run (a profile that is missing after a deploy, a sandbox that refuses
#   to start) is given back as a server fault, never billed as a bad photo that burns the day's captures;
#   fails_when=sandbox-exec's own failure (exit 65) is reported as a plain DeriveError again;
#   why_new=moving the profile to disk added a runtime dependency no test broke; seam=none
def test_a_converter_the_host_cannot_run_is_a_server_fault_not_a_retake(client, service, monkeypatch, tmp_path):
    monkeypatch.setattr(derive, 'PROFILE', tmp_path/'missing.sb')
    headers = login(client)
    with pytest.raises(derive.DeriveFault):
        service.deriver.run(FIXTURES/'selfie-face.jpg', service.config.data_dir/'staging'/'no-profile', 'selfie')
    result = capture(client, headers)
    assert (result.status_code, result.json()['error']) == (503, 'selfie_unavailable')
    released, = events(service, 'selfie_capture_released')
    assert json.loads(released['payload'])['detail'] == {'reason': 'converter', 'fault': 'server'}
    monkeypatch.undo()
    assert capture(client, headers).status_code == 202


# Value: protects=upload-triggered resource limits and decoder crashes spend the ten-capture budget, not the
#   ninety-reservation server-fault budget; fails_when=negative return codes are refunded as DeriveFault;
#   why_new=the host-fault regression test covers only sandbox startup; seam=child process outcome
@pytest.mark.parametrize('returncode', [1, -signal.SIGXCPU, -signal.SIGXFSZ, -signal.SIGSEGV, -signal.SIGABRT, -signal.SIGKILL])
def test_converter_crashes_and_resource_limits_count_as_bad_photos(client, service, monkeypatch, returncode):
    headers = login(client)
    monkeypatch.setattr(derive.subprocess, 'Popen', lambda *args, **kwargs: SimpleNamespace(
        returncode=returncode, communicate=lambda **kwargs: (b'', b'private decoder diagnostics')))
    for _ in range(selfie.CAPTURES_PER_DAY):
        result = capture(client, headers)
        assert (result.status_code, result.json()['error']) == (422, 'invalid_selfie')
        assert 'private decoder diagnostics' not in result.text
    assert capture(client, headers).status_code == 429
    assert len(events(service, 'selfie_capture_attempt')) == selfie.CAPTURES_PER_DAY
    assert events(service, 'selfie_capture_released') == []
    assert not list((service.config.data_dir/'selfies').iterdir())
    assert not list((service.config.data_dir/'staging').iterdir())


def test_ordinary_photo_attachments_do_not_require_a_face(config):
    from postern.derive import Deriver
    config.validate()
    result = Deriver(config).run(FIXTURES/'photo.jpg', config.data_dir/'derived'/'object', 'photo')
    assert result['pages'] == 1
