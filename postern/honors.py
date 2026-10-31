"""The certificate for passing the Final Exam, derived from the audit history like every quiz standing.

Badges need nothing from here: each is a chapter quiz pass (or all of them, or the Final Exam), which
the reading room already returns with its date. A certificate adds a number and a printed name, and
neither is stored outside the events. The number comes from the id of the first passing Final Exam
attempt, and the name from the person's latest certificate_name event, else from the fuller of their
staff-list name and the Google account name recorded on that attempt. Both export tiers keep event ids,
names and details. A restore keeps the certificate when that first pass reached an export tier;
unexported attempts cannot be recovered. Timestamp ties use the exported id, not local import order.
"""
from __future__ import annotations
import hashlib
import json
import math
import time
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import regex

from .auth import Refusal
from .db import event, meta
from .exam import KEY

NAMED = 'certificate_name'
NAME_LIMIT = 80
PER_DAY = 20
# The certificate format's fixed time zone. The year in a certificate's number is Eastern, like its printed date (honors.tsx),
# so a pass late on December 31 never carries next year's number.
EASTERN = ZoneInfo('America/New_York')
# Letters and marks that print as nothing, so dropping category C alone keeps them: Hangul fillers, the Braille
# blank, the combining grapheme joiner, Khmer inherent vowels, variation selectors and the Egyptian hieroglyph blanks.
INVISIBLE = frozenset('\u115f\u1160\u3164\uffa0\u2800\u034f\u17b4\u17b5\u180b\u180c\u180d\u180f\U00013441\U00013442') | frozenset(
    map(chr, [*range(0xfe00, 0xfe10), *range(0xe0100, 0xe01f0)]))
# Combining marks kept on one letter: Thai stacks a vowel and a tone mark; more only piles accents over other lines.
MARKS = 3
# Inside a word, these join its parts: "Saelim-Thong", "O'Brien", "J.".
JOINS = "-'\u2019."
JOINERS = frozenset('\u200c\u200d')
# The attempt that set quiz_standing.passed_at: the first Final Exam pass, as quiz.record() counts one, with
# the Google account name the person passed under (the event's `name`, kept by both export tiers).
FIRST_PASS = ("SELECT id,json_extract(payload,'$.name') FROM events WHERE email=? AND type='quiz_attempt' AND ts=? AND CASE WHEN json_valid(payload) "
              "THEN json_type(payload,'$.detail.chapter')='integer' AND json_extract(payload,'$.detail.chapter')=? "
              "AND json_type(payload,'$.detail.score')='integer' AND json_type(payload,'$.detail.passed')='true' END "
              "ORDER BY id LIMIT 1")


def tidy(name):
    """One printable line: line breaks and runs of spaces become one space; control, format and invisible
    characters (such as zero-width spaces pasted from chat apps, or Hangul fillers) are dropped, and so are
    combining marks past MARKS on one letter. Join controls survive only between letters/marks."""
    kept, marks = [], 0
    for c in unicodedata.normalize('NFC', name or ''):
        kind = unicodedata.category(c)
        if c in INVISIBLE or (kind[0] == 'C' and not c.isspace() and c not in JOINERS):
            continue
        if c not in JOINERS:  # A join control cannot restart the combining-mark allowance.
            # Ahom medial ra became a spacing mark in Unicode 16; keep that policy on older Python too.
            marks = marks + 1 if kind in ('Mn', 'Me') and c != '\U0001171e' else 0
        if c in JOINERS or marks <= MARKS:
            kept.append(c)
    text = ''.join(c for i, c in enumerate(kept) if c not in JOINERS or (
        0 < i < len(kept)-1 and all(unicodedata.category(kept[j])[0] in 'LM' for j in (i-1, i+1))))
    # Removing a pasted format character can bring a letter and accent together.
    return unicodedata.normalize('NFC', ' '.join(text.split()))


def legible(name):
    """A name prints at least one letter or digit, so a certificate never shows a blank or a row of symbols."""
    return any(c.isalnum() for c in name)


def fitted(name):
    """Fit a suggested/restored name at a word or complete grapheme; typed names are never cut."""
    name = tidy(name)
    if len(name) > NAME_LIMIT:
        end = word_end = 0
        for match in regex.finditer(r'\X', name):
            if match.end() > NAME_LIMIT:
                break
            end = match.end()
            if name[end:end+1] == ' ':
                word_end = end
        name = tidy(name[:word_end or end])
    return name if legible(name) else ''


def written(word):
    """"Sak", "McDonald" or Thai; not "sak" or "SAK", which read like placeholders on a certificate."""
    letters = [c for c in word if c.isalpha()]
    if not any(c.isupper() or c.islower() for c in letters):
        return True
    return letters[0].isupper() and any(c.islower() for c in letters)


def plain(name):
    """Only words, each a letter followed by letters, marks and JOINS. A staff-list note such as "Nok (Server)",
    "Nok - Server" or "Nok 2" is not plain, and must not print in place of a name."""
    return all(word[0].isalpha() and all(c.isalpha() or c in JOINS or c in JOINERS or unicodedata.category(c)[0] == 'M' for c in word)
               for word in name.split())


def fullness(name):
    """A plain name first, then more words (counting only those with a letter), then more of them written as names."""
    words = [word for word in name.split() if any(c.isalpha() for c in word)]
    return plain(name), len(words), sum(map(written, words))


def suggested(listed, account, email):
    """The name a certificate starts with: the fuller of the staff-list and Google account names.

    Half the staff list holds one word, often a nickname, while Google accounts usually hold a full
    name. A plain name beats one carrying a note or a second person ("Nok (Server)", "Nok & Ploy"), and
    the staff list wins a tie. A staff-list name that is not plain is a manager's note as often as a name,
    so it is used only when no Google name is known. People can change it (rename()), and admins see
    whether they did.
    """
    listed, account = fitted(listed), fitted(account)
    if account and not plain(listed):
        listed = ''
    names = [name for name in (listed, account) if name]
    return max(names, key=fullness) if names else email.split('@')[0][:NAME_LIMIT]


def number(identity, passed_at, *, prefix='PT'):
    """<configured prefix>-<Eastern year of the pass>-<8 hex digits of its attempt's event id>; a restore keeps both."""
    year = datetime.fromtimestamp(passed_at, EASTERN).year
    return f'{prefix}-{year}-{hashlib.sha256(identity.encode()).hexdigest()[:8].upper()}'


def awarded(db, email):
    """(time of the first Final Exam pass, its event id, the Google name it was passed under), or None."""
    row = db.execute('SELECT passed_at FROM quiz_standing WHERE email=? AND chapter=? AND passed_at IS NOT NULL',
                     (email, KEY)).fetchone()
    if row is None:
        return None
    found = db.execute(FIRST_PASS, (email, row[0], KEY)).fetchone()
    if found is None:
        # quiz_standing is folded from these events, so the attempt is there; the email keeps a fallback distinct.
        return row[0], f'{email}:{row[0]!r}', ''
    return row[0], found[0], found[1] if isinstance(found[1], str) else ''


def usable_timestamp(value):
    """Restored dates must also be representable by the summary export's clock."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        if not math.isfinite(value):
            return False
        datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return False
    return True


def chosen(db, email):
    """The name this person last saved for their certificate, if any."""
    for text, timestamp in db.execute('SELECT payload,ts FROM events WHERE email=? AND type=? ORDER BY ts DESC, id DESC',
                              (email, NAMED)):
        if not usable_timestamp(timestamp):
            continue
        try:
            detail = json.loads(text).get('detail')
        except (ValueError, AttributeError):
            continue
        name = fitted(detail.get('name')) if isinstance(detail, dict) and isinstance(detail.get('name'), str) else ''
        if name:
            return name
    return None


def history_ready(db, email):
    """Admin history uses the same current-roster and recovery boundary as Staff progress."""
    if meta(db, 'recovery_mode', False):
        raise Refusal(409, 'history_restoring', 'Training history is being restored. Please try again shortly. / กำลังกู้คืนประวัติการเรียน กรุณาลองใหม่อีกครั้งในอีกสักครู่')
    if not db.execute('SELECT 1 FROM members WHERE email=?', (email,)).fetchone():
        raise Refusal(404, 'certificate_not_found', 'No certificate found for this person on the current staff list. / ไม่พบประกาศนียบัตรของบุคคลนี้ในรายชื่อพนักงานปัจจุบัน')


def name_history(db, email, before=None, *, prefix='PT'):
    """One bounded page of saved choices. A cursor belongs to this person's name events only.

    Timestamps plus exported ids keep tied records stable after restores. Malformed records
    consume page slots instead of extending the page until 50 valid names are found.
    """
    history_ready(db, email)
    current = certificate(db, email, prefix=prefix)
    if current is None:
        raise Refusal(404, 'certificate_not_found', 'This person has no certificate in the available history. / ไม่พบประกาศนียบัตรของบุคคลนี้ในประวัติที่มีอยู่')
    parameters = [email, NAMED]
    boundary = ''
    if before is not None:
        cursor = db.execute('SELECT ts,id FROM events WHERE id=? AND email=? AND type=?', (before, email, NAMED)).fetchone()
        if cursor is None:
            raise Refusal(422, 'invalid_cursor', 'The history page is no longer available. Refresh name history. / ไม่พบหน้าประวัตินี้ กรุณาโหลดประวัติชื่อใหม่')
        boundary = ' AND (ts,id)<(?,?)'
        parameters.extend(cursor)
    records = db.execute('SELECT id,ts,payload FROM events WHERE email=? AND type=?' + boundary +
                         ' ORDER BY ts DESC,id DESC LIMIT 51', parameters).fetchall()
    entries = []
    for identity, timestamp, payload in records[:50]:
        try:
            detail = json.loads(payload).get('detail')
        except (ValueError, AttributeError):
            continue
        # This is evidence of the saved choice, so do not rewrite it with today's normalization.
        name = detail.get('name') if isinstance(detail, dict) else None
        try:
            if isinstance(name, str):
                name.encode('utf-8')
        except UnicodeEncodeError:
            # Escaped lone surrogates can survive JSONL recovery but cannot be sent as JSON.
            continue
        if (isinstance(name, str) and 0 < len(name) <= NAME_LIMIT and legible(name) and
                usable_timestamp(timestamp)):
            saved_at = detail.get('saved_at')
            entries.append({'id': identity, 'name': name,
                            'saved_at': saved_at if usable_timestamp(saved_at) else timestamp})
    return {'certificate': current, 'entries': entries,
            'next_cursor': records[49]['id'] if len(records) > 50 else None,
            'history_incomplete': bool(meta(db, 'history_incomplete', False) or len(entries) < len(records[:50]))}


def describe(award, listed, picked, email, *, prefix='PT'):
    passed_at, identity, account = award
    return {'number': number(identity, passed_at, prefix=prefix), 'name': picked or suggested(listed, account, email),
            'awarded_at': passed_at, 'name_confirmed': picked is not None}


def certificate(db, email, *, prefix='PT'):
    """This person's certificate; None before they pass the Final Exam and while a restore is unverified."""
    if meta(db, 'recovery_mode', False):
        return None
    award = awarded(db, email)
    if award is None:
        return None
    member = db.execute('SELECT name FROM members WHERE email=?', (email,)).fetchone()
    return describe(award, member['name'] if member else '', chosen(db, email), email, prefix=prefix)


def certificates(db, *, prefix='PT'):
    """Certificates of everyone on the current roster, by email, for Staff progress: the same as each person sees."""
    passed = db.execute('SELECT q.email,m.name FROM quiz_standing q JOIN members m ON m.email=q.email '
                        'WHERE q.chapter=? AND q.passed_at IS NOT NULL', (KEY,)).fetchall()
    return {email: describe(awarded(db, email), listed, chosen(db, email), email, prefix=prefix) for email, listed in passed}


def rename(db, ctx, name, *, prefix='PT'):
    """Save the name printed on the caller's certificate: an audit event, so it is backed up and restored."""
    if meta(db, 'recovery_mode', False):
        raise Refusal(409, 'history_restoring', 'Training history is being restored. Please try again shortly. / กำลังกู้คืนประวัติการเรียน กรุณาลองใหม่อีกครั้งในอีกสักครู่')
    if awarded(db, ctx['email']) is None:
        raise Refusal(404, 'certificate_not_found', 'Pass the Final Exam to receive your certificate. / สอบผ่านแบบทดสอบปลายหลักสูตรก่อน จึงจะได้รับประกาศนียบัตร')
    cleaned = tidy(name)
    if not legible(cleaned) or len(cleaned) > NAME_LIMIT:
        raise Refusal(422, 'invalid_name', f'Enter your name in 1 to {NAME_LIMIT} characters, with at least one letter or number. / กรุณาใส่ชื่อความยาว 1 ถึง {NAME_LIMIT} ตัวอักษร และมีตัวอักษรหรือตัวเลขอย่างน้อยหนึ่งตัว')
    now = time.time()
    # Ordering may advance beyond wall time after a restore. Rate limits count
    # actual saves; older events without that field retain their original clock.
    recent = db.execute("SELECT count(*) FROM events WHERE email=? AND type=? AND "
                        "CASE WHEN json_valid(payload) THEN CASE WHEN json_type(payload,'$.detail.saved_at') IN ('integer','real') "
                        "THEN json_extract(payload,'$.detail.saved_at') ELSE ts END ELSE ts END > ?",
                        (ctx['email'], NAMED, now - 86400)).fetchone()[0]
    if recent >= PER_DAY:
        raise Refusal(429, 'rate_limited', 'You have changed this name many times today. Please try again tomorrow. / วันนี้แก้ไขชื่อนี้หลายครั้งแล้ว กรุณาลองใหม่พรุ่งนี้')
    # The writer serializes saves. A clock correction or a future-dated restore
    # must not leave the old name winning; whole seconds also survive coarse logs.
    latest = next((row[0] for row in db.execute('SELECT ts FROM events WHERE email=? AND type=? ORDER BY ts DESC',
                                               (ctx['email'], NAMED)) if usable_timestamp(row[0])), None)
    timestamp = max(now, latest + 1) if latest is not None else now
    if not usable_timestamp(timestamp) or not usable_timestamp(now):
        raise Refusal(503, 'clock_unavailable', 'The saved-name clock needs operator attention. Please try again later. / กรุณาลองใหม่ภายหลัง')
    event(db, NAMED, ctx, detail={'name': cleaned, 'saved_at': now}, timestamp=timestamp)
    return certificate(db, ctx['email'], prefix=prefix)
