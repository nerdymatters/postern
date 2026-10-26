"""Per-reader page stamping. The overlay is reused per reader and minute; pages are composited per request.

The stamp names the reader and the minute so a screenshot or a photograph of the screen still
identifies who was looking. It is deliberately not proof of reading, only of what was served.
"""
from __future__ import annotations
from functools import lru_cache
import io
import time
from PIL import Image, ImageDraw, ImageFont

THUMBNAIL = (400, 600)


def label(reader, minute):
    return f'{reader} · {time.strftime("%b %-d %Y %-I:%M %p", time.localtime(minute * 60))}'


@lru_cache(maxsize=256)
def overlay(size, reader, minute):
    # Cached per reader/minute/size: the same reader's pages share one rendered overlay.
    text = label(reader, minute)
    canvas = Image.new('RGBA', size)
    # Two small, outlined marks retain attribution on crops without crossing every
    # line of a handout. The complete, high-contrast identity lives below the page.
    font = ImageFont.load_default(size=max(8, min(18, size[0] // 75)))
    draw = ImageDraw.Draw(canvas)
    for fraction in (0.28, 0.72):
        draw.text((size[0] // 2, int(size[1] * fraction)), text, font=font,
                  anchor='mm', fill=(65, 65, 65, 65), stroke_width=1,
                  stroke_fill=(255, 255, 255, 65))
    return canvas


def chip(page, reader, minute, selfie_path=None):
    # Add a footer outside the source image, so attribution never covers its text.
    font_size = max(8, min(16, page.size[0] // 85))
    font = ImageFont.load_default(size=font_size)
    text = label(reader, minute)
    pad = max(5, page.size[0] // 160)
    # Long identities wrap instead of shrinking into illegibility or being clipped.
    avatar_size = max(24, min(96, page.width // 18)) if selfie_path else 0
    inset = avatar_size + pad if selfie_path else 0
    width = max(1, page.width - pad * 2 - inset)
    lines, line = [], ''
    for character in text:
        if line and font.getlength(line + character) > width:
            lines.append(line)
            line = ''
        line += character
    lines.append(line)
    line_height = font_size + 4
    framed = Image.new('RGBA', (page.width, page.height + max(avatar_size, len(lines) * line_height) + pad * 2), (248, 249, 250, 255))
    framed.paste(page, (0, 0))
    draw = ImageDraw.Draw(framed)
    draw.line((0, page.height, page.width, page.height), fill=(220, 222, 224, 255))
    if selfie_path:
        with Image.open(selfie_path) as photo:
            framed.paste(photo.convert('RGB').resize((avatar_size, avatar_size)), (pad, page.height + pad))
    for index, line in enumerate(lines):
        draw.text((pad + inset, page.height + pad + index * line_height), line, font=font, fill=(65, 65, 65, 255))
    return framed


def stamp(path, reader, thumbnail=False, minute=None, selfie_path=None):
    """Return stamped JPEG bytes for one derived page image."""
    minute = int(time.time() // 60) if minute is None else minute
    with Image.open(path) as source:
        page = source.convert('RGBA')
    if thumbnail:
        page.thumbnail(THUMBNAIL)
    page = Image.alpha_composite(page, overlay(page.size, reader, minute))
    page = chip(page, reader, minute, selfie_path)
    output = io.BytesIO()
    page.convert('RGB').save(output, format='JPEG', quality=85 if not thumbnail else 78)
    return output.getvalue()
