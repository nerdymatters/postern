"""Executed with -I inside the converter sandbox. No application imports or secrets."""
import json
import os
import math
from pathlib import Path
import resource
import subprocess
import sys
import warnings


FACE_MODEL = Path(__file__).with_name('models') / 'face_detection_yunet_2023mar.onnx'
FACE_MODEL_SHA256 = '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'
# Only these exit codes cross the sandbox boundary. Never expose parser diagnostics.
FACE_FAILURES = {60: 'face_required', 61: 'multiple_faces', 62: 'face_too_small',
                 63: 'face_out_of_frame', 64: 'face_check_unavailable'}


class FaceCheckError(Exception):
    def __init__(self, exit_code):
        self.exit_code = exit_code
        super().__init__(FACE_FAILURES[exit_code])


def detect_faces(image):
    """Offline CPU detection on the sanitized square; no embeddings or persisted landmarks."""
    try:
        import hashlib
        import cv2
        import numpy as np
        if hashlib.sha256(FACE_MODEL.read_bytes()).hexdigest() != FACE_MODEL_SHA256:
            raise ValueError('Unexpected face model')
        # Bound CPU use during enrollment bursts; no GPU or network backend.
        cv2.setNumThreads(1)
        detector = cv2.FaceDetectorYN.create(str(FACE_MODEL), '', image.size, .85, .3, 500,
                                            cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
        _, faces = detector.detect(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
        return [] if faces is None else faces.tolist()
    except Exception as error:
        raise FaceCheckError(64) from error


def check_face(image):
    faces = detect_faces(image)
    if not faces:
        raise FaceCheckError(60)
    if len(faces) != 1:
        raise FaceCheckError(61)
    face = faces[0]
    if len(face) != 15 or not all(math.isfinite(value) for value in face):
        raise FaceCheckError(64)
    x, y, width, height = face[:4]
    edge = min(image.size)
    if min(width, height) < edge * .25:
        raise FaceCheckError(62)
    # Allow small detector-box overshoot, but require the eyes, nose and mouth in frame.
    margin = edge * .05
    if (x < -margin or y < -margin or x+width > image.width+margin or y+height > image.height+margin
            or any(not 0 <= face[i] < image.width or not 0 <= face[i+1] < image.height
                   for i in range(4, 14, 2))):
        raise FaceCheckError(63)


def main():
    source, output, kind, max_pages, max_pixels, max_bytes, poppler = sys.argv[1:]
    source, output = Path(source), Path(output)
    max_pages, max_pixels, max_bytes = int(max_pages), int(max_pixels), int(max_bytes)
    resource.setrlimit(resource.RLIMIT_CPU, (45,45))
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes,max_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64,64))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (2*1024**3, 2*1024**3))
    except (ValueError, OSError):
        pass  # macOS refuses address-space limits; the parent bounds resident memory instead.
    os.umask(0o077)
    if kind == 'video':
        # MP4 only, local protocols only, no transcoding or arbitrary network inputs.
        tools = Path('/opt/homebrew/bin')
        probe = json.loads(subprocess.run([str(tools/'ffprobe'), '-v', 'error', '-protocol_whitelist', 'file',
            '-show_entries', 'format=duration:stream=codec_type,codec_name,width,height', '-of', 'json', str(source)],
            capture_output=True, check=True, timeout=15).stdout)
        duration = float(probe['format']['duration'])
        videos = [s for s in probe['streams'] if s['codec_type'] == 'video']
        audio = [s for s in probe['streams'] if s['codec_type'] == 'audio']
        if not math.isfinite(duration) or not 0 < duration <= 10800 or len(videos) != 1 or videos[0]['codec_name'] != 'h264':
            raise ValueError('Use an H.264 MP4 of at most three hours')
        if videos[0]['width'] * videos[0]['height'] > 3840 * 2160 or any(s['codec_name'] != 'aac' for s in audio):
            raise ValueError('Use video up to 4K with AAC audio')
        target = output/'video.mp4'
        subprocess.run([str(tools/'ffmpeg'), '-v', 'error', '-nostdin', '-protocol_whitelist', 'file',
            '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy', '-movflags', '+faststart',
            '-map_metadata', '-1', str(target)], capture_output=True, check=True, timeout=60)
        size = target.stat().st_size
        if size > max_bytes:
            raise ValueError('Video exceeds the item budget')
        # Staff watch the remuxed file, which keeps only the first video and audio tracks; a longer
        # dropped track must not make the recorded length unreachable.
        served = json.loads(subprocess.run([str(tools/'ffprobe'), '-v', 'error', '-protocol_whitelist', 'file',
            '-show_entries', 'format=duration', '-of', 'json', str(target)], capture_output=True, check=True, timeout=15).stdout)
        length = float(served.get('format', {}).get('duration', 'nan'))
        if math.isfinite(length) and 0 < length < duration:
            duration = length
        (output/'manifest.json').write_text(json.dumps({'pages': 0, 'bytes': size, 'duration_seconds': duration}))
        return
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = max_pixels
    warnings.simplefilter('error', Image.DecompressionBombWarning)
    if kind == 'pdf':
        info = subprocess.run([str(Path(poppler)/'pdfinfo'),str(source)],capture_output=True,check=True,timeout=15).stdout.decode(errors='replace')
        pages = next(int(line.split(':',1)[1]) for line in info.splitlines() if line.startswith('Pages:'))
        if not 1 <= pages <= max_pages:
            raise ValueError(f'PDF must have 1–{max_pages} pages')
        used = 0
        for n in range(1,pages+1):
            prefix = output/f'page-{n-1}'
            subprocess.run([str(Path(poppler)/'pdftoppm'),'-f',str(n),'-l',str(n),'-singlefile','-scale-to','2100','-jpeg',str(source),str(prefix)],capture_output=True,check=True,timeout=30)
            used += prefix.with_suffix('.jpg').stat().st_size
            if used > max_bytes:
                raise ValueError('Derived output exceeds the item budget')
    else:
        if source.suffix.lower() in ('.heic','.heif'):
            import pillow_heif
            pillow_heif.register_heif_opener()
        with Image.open(source) as image:
            if image.format not in (('JPEG', 'PNG') if kind == 'selfie' else ('JPEG','PNG','HEIF','HEIC')):
                raise ValueError('Unsupported photo format')
            image = ImageOps.exif_transpose(image)
            if kind == 'selfie':
                if min(image.size) < 96:
                    raise ValueError('Selfie is too small')
                image = ImageOps.fit(image.convert('RGB'), (256, 256))
                check_face(image)
                # Copy pixels into a new image to discard all source metadata.
                clean = Image.new('RGB', image.size)
                clean.paste(image)
                clean.save(output/'page-0.jpg', format='JPEG', quality=85)
            else:
                image.thumbnail((2000,2000))
                image.convert('RGB').save(output/'page-0.jpg',format='JPEG',quality=88)
        pages = 1
    with Image.open(output/'page-0.jpg') as thumb:
        thumb.thumbnail((400,600))
        thumb.convert('RGB').save(output/'thumb.jpg',format='JPEG',quality=82)
    size = sum(p.stat().st_size for p in output.glob('*.jpg'))
    if size > max_bytes:
        raise ValueError('Derived output exceeds the item budget')
    (output/'manifest.json').write_text(json.dumps({'pages':pages,'bytes':size}))


if __name__ == '__main__':
    try:
        main()
    except FaceCheckError as error:
        sys.exit(error.exit_code)
