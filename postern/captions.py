"""English subtitles bound to the original video's SHA-256, never to a title or chapter.

Files stay in the host's private training directory. The authorized player response supplies
the matching WebVTT; the public web bundle contains no training transcript.
"""
from pathlib import Path
import re

ROOT = Path(__file__).with_name('sample_training') / 'captions'


def load(root=ROOT):
    tracks = {}
    for path in Path(root).glob('*.en.vtt'):
        digest = path.name.removesuffix('.en.vtt')
        if not re.fullmatch('[a-f0-9]{64}', digest) or path.stat().st_size > 256 * 1024:
            raise ValueError('Subtitles need a video SHA-256 filename and a size below 256 KiB')
        text = path.read_text(encoding='utf-8')
        if not text.startswith('WEBVTT\n') or '-->' not in text:
            raise ValueError(f'Invalid WebVTT subtitles: {path.name}')
        tracks[digest] = text
    return tracks
