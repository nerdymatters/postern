"""Generate non-confidential fixtures and opaque harness credentials."""
from __future__ import annotations

import argparse
import secrets
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from .common import private_json


def prepare(directory, pages=8, readers=25, video=False):
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o700)
    (directory / 'pages').mkdir()
    images = []
    for page in range(pages):
        image = Image.new('RGB', (1600, 2100), 'white')
        draw = ImageDraw.Draw(image)
        draw.text((90, 70), f'POSTERN SYNTHETIC BULLETIN - PAGE {page + 1}', fill='black')
        for row in range(35):
            y = 180 + row * 48
            draw.rectangle((90, y, 1450 - (row % 5) * 55, y + 18), fill=(80 + row * 3, 105, 135))
        image.save(directory / 'pages' / f'{page}.jpg', quality=88)
        images.append(image)
    images[0].save(directory / 'synthetic.pdf', save_all=True, append_images=images[1:])
    for image in images:
        image.close()
    private_json(directory / 'credentials.json', {
        'operator': secrets.token_urlsafe(32),
        'readers': {secrets.token_urlsafe(32): f'reader-{n}@example.invalid' for n in range(readers)},
    })
    if video:
        (directory / 'hls').mkdir()
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                        '-i', 'testsrc2=size=1280x720:rate=30', '-t', '60', '-an',
                        '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '2M',
                        '-minrate', '2M', '-maxrate', '2M', '-bufsize', '4M',
                        '-x264-params', 'nal-hrd=cbr:force-cfr=1', '-g', '180',
                        '-sc_threshold', '0', '-f', 'hls', '-hls_time', '6',
                        '-hls_playlist_type', 'vod', '-hls_segment_filename',
                        str(directory / 'hls' / 'seg_%03d.ts'),
                        str(directory / 'hls' / 'index.m3u8')], check=True, timeout=180)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--video', action='store_true', help='Requires ffmpeg; generates a 2 Mbps synthetic HLS clip')
    args = parser.parse_args()
    prepare(args.directory, video=args.video)
    print(f'Fixtures ready in {args.directory}; credentials are in credentials.json (not printed).')


if __name__ == '__main__':
    main()
