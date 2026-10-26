import re
from pathlib import Path

from postern import honors, quiz


def test_the_web_apps_video_finished_rule_matches_the_server():
    # Value: protects=the reader and the server agree when a video counts as finished; fails_when=END_TOLERANCE or END_FRACTION changes on one side only (staff told they skipped, or offered a quiz the server refuses); why_new=guide.tsx copies the server's tuned numbers; seam=none
    source = (Path(__file__).parents[2] / 'dashboard/web/src/guide.tsx').read_text(encoding='utf-8')
    assert float(re.search(r'END_TOLERANCE = ([\d.]+)', source)[1]) == quiz.END_TOLERANCE
    assert float(re.search(r'END_FRACTION = ([\d.]+)', source)[1]) == quiz.END_FRACTION


def test_the_web_apps_certificate_rules_match_the_server():
    # Value: protects=a printed certificate's date shares the year in its number, and the name field allows what the server saves; fails_when=EASTERN or NAME_LIMIT changes on one side only (a December 31 pass dated one year and numbered the next, or names cut short in the form); why_new=format.ts and honors.tsx copy the server's values; seam=none
    web = Path(__file__).parents[2] / 'dashboard/web/src'
    assert re.search(r"EASTERN = '([^']+)'", (web / 'format.ts').read_text(encoding='utf-8'))[1] == honors.EASTERN.key
    assert int(re.search(r'NAME_LIMIT = (\d+)', (web / 'honors.tsx').read_text(encoding='utf-8'))[1]) == honors.NAME_LIMIT
