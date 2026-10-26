"""Attribution stays readable without consuming any source-page area."""
import io

import pytest
from PIL import Image, ImageDraw

from postern.stamp import chip, label, stamp


@pytest.mark.parametrize('size', [(400, 600), (1600, 2200)])
def test_footer_preserves_source_and_fits_a_maximum_length_email(size, monkeypatch):
    # A 254-character address with valid local-part and domain-label lengths.
    email = 'a' * 64 + '@' + '.'.join(['b' * 63, 'c' * 63, 'd' * 57, 'com'])
    assert len(email) == 254
    source = Image.new('RGBA', size, (32, 74, 119, 255))
    ImageDraw.Draw(source).rectangle((0, size[1] - 12, size[0] - 1, size[1] - 1), fill='red')
    rendered = []
    draw_text = ImageDraw.ImageDraw.text

    def record_text(draw, xy, text, *args, **kwargs):
        rendered.append((text, draw.textbbox(xy, text, font=kwargs['font'])))
        return draw_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record_text)
    framed = chip(source, email, 29_000_000)
    assert framed.width == source.width
    assert framed.height > source.height
    assert framed.crop((0, 0, source.width, source.height)).tobytes() == source.tobytes()
    assert ''.join(text for text, _ in rendered) == label(email, 29_000_000)
    assert len(rendered) > 1
    for _, (left, top, right, bottom) in rendered:
        assert 0 <= left < right <= framed.width
        assert source.height < top < bottom < framed.height


@pytest.mark.parametrize('thumbnail, content_size', [(False, (800, 1200)), (True, (400, 600))])
def test_encoded_stamp_keeps_bottom_of_document_above_footer(tmp_path, thumbnail, content_size):
    source = Image.new('RGB', (800, 1200), 'white')
    ImageDraw.Draw(source).rectangle((0, 1160, 799, 1199), fill=(220, 20, 20))
    path = tmp_path / 'page.png'
    source.save(path)
    with Image.open(io.BytesIO(stamp(path, 'reader@example.invalid', thumbnail=thumbnail, minute=29_000_000))) as rendered:
        width, height = content_size
        assert rendered.format == 'JPEG'
        assert rendered.width == width
        assert rendered.height > height
        # The original bottom border is intact; attribution occupies new space.
        red, green, blue = rendered.getpixel((width // 2, height - 5))
        assert red > 180 and green < 60 and blue < 60
        assert min(rendered.getpixel((width - 5, rendered.height - 3))) > 220
