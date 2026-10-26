"""Private caption assets must stay bound to a video hash and fit the player response budget."""
import pytest
from postern import captions

TRACK = 'WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nWelcome.\n'


def test_load_only_recognized_video_hash_tracks(tmp_path):
    digest = 'a' * 64
    (tmp_path / f'{digest}.en.vtt').write_text(TRACK)
    (tmp_path / 'notes.txt').write_text('Editorial notes are not a player track.')
    assert captions.load(tmp_path) == {digest: TRACK}


@pytest.mark.parametrize('name,content', [('chapter1.en.vtt', TRACK), ('A' * 64 + '.en.vtt', TRACK),
    ('a' * 64 + '.en.vtt', TRACK + 'x' * (256 * 1024)), ('a' * 64 + '.en.vtt', 'Welcome.'),
    ('a' * 64 + '.en.vtt', 'WEBVTT\n\nNo timed cues.')])
def test_invalid_caption_files_refuse_startup(tmp_path, name, content):
    (tmp_path / name).write_text(content)
    with pytest.raises(ValueError): captions.load(tmp_path)


def test_sample_training_has_no_private_transcripts():
    assert captions.load() == {}
