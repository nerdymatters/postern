"""Reject unapproved formats before running their parser, even under a JPEG name."""
import sys

import pytest
from PIL import Image, UnidentifiedImageError

from postern import derive_child


@pytest.mark.parametrize('kind', ['image', 'selfie'])
def test_unapproved_parser_is_never_entered(tmp_path, monkeypatch, kind):
    source = tmp_path / 'upload.jpg'
    Image.new('1', (100, 100)).save(source, format='XBM')
    output = tmp_path / 'derived'
    output.mkdir()
    Image.init()
    factory, accepts = Image.OPEN['XBM']
    entered = []

    def probe(*args, **kwargs):
        entered.append(True)
        return factory(*args, **kwargs)

    monkeypatch.setitem(Image.OPEN, 'XBM', (probe, accepts))
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', Image.MAX_IMAGE_PIXELS)
    # The parent sandbox tests cover these limits. Never apply child limits to pytest.
    monkeypatch.setattr(derive_child.resource, 'setrlimit', lambda *args: None)
    monkeypatch.setattr(derive_child.os, 'umask', lambda *args: None)
    monkeypatch.setattr(sys, 'argv', ['derive', str(source), str(output), kind, '10', '1000000', '1000000', '/unused'])
    with pytest.raises((ValueError, UnidentifiedImageError)):
        derive_child.main()
    assert entered == [], 'the disallowed parser ran before format admission'
    assert list(output.iterdir()) == []
