"""The Final Exam certificate: a number and a printed name derived from audit events, like every standing."""
import json
import re
import time
from types import SimpleNamespace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import ADMIN, READER, events, sign_in
from postern import honors
from postern.db import Database, set_meta
from postern.recover import import_events
from test_exam import chosen, restore
from test_quiz import MANAGER, allowlist, pause_worker, publish


def take_exam(client, headers, score):
    opened = client.get('/api/quizzes/0', headers=headers).json()
    response = client.post('/api/quizzes/0/attempts', headers=headers,
                           json={'version': opened['version'], 'answers': chosen(client.service, score)})
    assert response.status_code == 200, response.text
    return response.json()


def certificate(client, headers):
    return client.get('/api/posts', headers=headers).json()['certificate']


def rename(client, headers, name):
    return client.post('/api/certificate/name', headers=headers, json={'name': name})


def test_the_first_pass_awards_a_certificate_whose_number_and_date_later_attempts_keep(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client, name='Demo Reader Person')
    restore(service)
    assert certificate(client, headers) is None
    take_exam(client, headers, 15)
    assert certificate(client, headers) is None
    take_exam(client, headers, 16)
    awarded = certificate(client, headers)
    first = next(e for e in events(service, 'quiz_attempt') if '"passed": true' in e['payload'] and '"chapter": 0' in e['payload'])
    state = client.get('/api/posts', headers=headers).json()['final_exam']
    assert awarded == {'number': honors.number(first['id'], first['ts']), 'awarded_at': state['passed_at'],
                       'name': 'Demo Reader Person', 'name_confirmed': False}
    assert re.fullmatch(r'PT-\d{4}-[0-9A-F]{8}', awarded['number'])
    for score in (20, 3):
        take_exam(client, headers, score)
        assert certificate(client, headers) == awarded


def test_a_chapter_nine_pass_alone_earns_no_certificate(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    assert client.get('/api/posts', headers=headers).json()['quizzes'][0]['passed'] is True
    assert certificate(client, headers) is None


def test_an_admin_who_passes_the_optional_exam_receives_a_certificate(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client, ADMIN)
    take_exam(client, headers, 17)
    assert certificate(client, headers)['name'] == 'Demo administrator'


@pytest.mark.parametrize('listed, account, expected', [
    ('Ari', 'Ari Exampleton', 'Ari Exampleton'),          # A nickname on the staff list.
    ('ari exampleton', 'Ari Exampleton', 'Ari Exampleton'),  # Same words, one case.
    ('Ari exampleton', 'Ari Exampleton', 'Ari Exampleton'),  # One word not written as a name.
    ('Ari Exampleton', 'ARI EXAMPLETON', 'Ari Exampleton'),
    ('ARI', 'ari', 'ARI'),                                      # Neither is better: the staff list wins.
    ('Ari Exampleton', 'Ari E', 'Ari Exampleton'),
    ('นก', 'สมชาย ใจดี', 'สมชาย ใจดี'),                          # More words win, in Thai as in English.
    # Value: protects=written() decides between names of equal length: one case loses, Thai counts as written, an initial
    #   is not a written surname; fails_when=written() accepts all caps or initials, or rejects caseless scripts;
    #   why_new=the rows above are all settled by word count or the staff-list tie-break; seam=none
    ('ARI EXAMPLETON', 'Ari Exampleton', 'Ari Exampleton'),
    ('สมชาย ใจดี', 'Somchai Jaidee', 'สมชาย ใจดี'),              # Thai is written: the tie keeps the staff list.
    ('Ari E', 'Ari Exampleton', 'Ari Exampleton'),
    ('  Maria​   Santos ', '', 'Maria Santos'),
    # Value: protects=a line break inside a staff Sheet cell (Alt+Enter) still separates words in the suggested name;
    #   fails_when=tidy() drops whitespace control characters along with the other control characters, suggesting
    #   "MariaSantos"; why_new=the suite's only line break sits next to a space, so dropping it changed nothing; seam=none
    ('Maria\nSantos', 'Maria', 'Maria Santos'),
    # Value: protects=a manager's note or a second person in either name never prints in place of a plain name;
    #   fails_when=fullness() counts separators, digits or bracketed notes as words; why_new=red team showed
    #   "Nok - Server" (three "words") beating "Nok Saelim"; seam=none
    ('Nok - Server', 'Nok Saelim', 'Nok Saelim'),
    ('Nok (Server)', 'Nok Saelim', 'Nok Saelim'),
    ('Nok / นก', 'Nok Saelim', 'Nok Saelim'),
    ('Nok 2', 'Nok', 'Nok'),
    ('NOK', 'Nok & Ploy Saelim', 'NOK'),
    ('Mary (Sales)', "Mary-Jane O'Brien", "Mary-Jane O'Brien"),  # Hyphens and apostrophes join a plain name.
    ('Mary', 'Mary O\u2019Brien', 'Mary O\u2019Brien'),          # So does the curly apostrophe iPhones type,
    ('Maria', 'Maria J. Santos', 'Maria J. Santos'),             # and the period after an initial.
    # Value: protects=a manager's note in the staff list never reaches the person while any Google name is known, even one
    #   with an emoji, a digit or a second person; fails_when=suggested() compares a note-carrying staff-list name with a
    #   Google name that is not plain; why_new=security showed 'Nok (Server)' beating 'Nok' plus an emoji; seam=none
    ('Nok (Server)', 'Nok \U0001F338', 'Nok \U0001F338'),
    ('Nok - Server', 'Nok2', 'Nok2'),
    ('Nok (Server)', 'Nok & Ploy', 'Nok & Ploy'),
    ('Nok (Server)', '', 'Nok (Server)'),                         # The only name known still beats the email.
    ('- . -', 'nok', 'nok'),                                    # A name needs a letter or digit to be suggested.
    ('...', '', 'reader'),
    ('', '', 'reader'),
])
def test_the_suggested_name_is_the_fuller_of_the_staff_list_and_google_names(listed, account, expected):
    assert honors.suggested(listed, account, 'reader@example.invalid') == expected


def test_a_person_chooses_the_printed_name_and_admins_see_it(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    take_exam(client, headers, 18)
    saved = rename(client, headers, '  Maria ​Santos\n Rivera ')
    assert saved.status_code == 200, saved.text
    assert (saved.json()['name'], saved.json()['name_confirmed']) == ('Maria Santos Rivera', True)
    assert certificate(client, headers) == saved.json()
    recorded = events(service, honors.NAMED)
    assert len(recorded) == 1 and recorded[0]['summary'] == 1 and recorded[0]['email'] == READER
    # The latest choice wins, and Staff progress shows the same certificate.
    assert rename(client, headers, 'มาเรีย ซานโตส').json()['name'] == 'มาเรีย ซานโตส'
    people = client.get('/api/admin/progress', headers=sign_in(client, ADMIN)).json()['people']
    assert next(p for p in people if p['email'] == READER)['certificate'] == certificate(client, headers)
    assert next(p for p in people if p['email'] == ADMIN)['certificate'] is None


def test_new_names_win_after_the_clock_moves_back_without_bypassing_the_daily_cap(client, service, monkeypatch):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service, chapter=0, score=16, total=20, identity='final-pass')
    now = time.time()
    future = now + 86400
    service.db.submit(lambda db: import_events(db, [
        {'event_id': 'future-name', 'event': honors.NAMED, 'ts': future, 'email': READER, 'detail': {'name': 'Old Name'}},
        {'event_id': 'other-future-name', 'event': honors.NAMED, 'ts': future + 86400, 'email': ADMIN, 'detail': {'name': 'Other Person'}},
    ])).result()
    # Equal clock readings must also preserve save order, regardless of random event ids.
    monkeypatch.setattr(honors, 'time', SimpleNamespace(time=lambda: now))
    for attempt in range(honors.PER_DAY - 1):
        name = f'New Name {attempt}'
        saved = rename(client, headers, name)
        assert saved.status_code == 200, saved.text
        assert saved.json()['name'] == name
        assert certificate(client, headers) == saved.json()
    stamped = [row['ts'] for row in events(service, honors.NAMED) if row['email'] == READER]
    assert all(earlier < later for earlier, later in zip(stamped, stamped[1:]))
    assert stamped[-1] < future + 86400  # Another account cannot advance this account's clock.
    assert rename(client, headers, 'One More Name').status_code == 429


def test_restored_invalid_date_cannot_poison_new_saved_name_or_summary(client, service):
    headers = sign_in(client)
    restore(service, chapter=0, score=16, total=20, identity='final-pass')
    restored = {'event_id': 'invalid-date', 'event': honors.NAMED, 'ts': 1e30,
                'email': READER, 'detail': {'name': 'Unusable Date'}}
    service.db.submit(lambda db: import_events(db, [restored])).result()
    saved = rename(client, headers, 'Corrected Name')
    assert saved.status_code == 200 and saved.json()['name'] == 'Corrected Name'
    records = events(service, honors.NAMED)
    new = next(row for row in records if row['id'] != 'invalid-date')
    assert datetime.fromtimestamp(new['ts'], timezone.utc).year == datetime.now(timezone.utc).year
    assert next(row for row in records if row['id'] == 'invalid-date')['ts'] == 1e30
    history = service.db.read(lambda db: honors.name_history(db, READER))
    assert history['history_incomplete'] is True
    assert [entry['name'] for entry in history['entries']] == ['Corrected Name']


def test_future_restored_date_does_not_extend_daily_rename_limit(client, service, monkeypatch):
    headers = sign_in(client)
    restore(service, chapter=0, score=16, total=20, identity='final-pass')
    now = time.time()
    future = now + 30 * 86400
    service.db.submit(lambda db: import_events(db, [
        {'event_id': 'future-name', 'event': honors.NAMED, 'ts': future, 'email': READER,
         'detail': {'name': 'Restored Name'}}])).result()
    monkeypatch.setattr(honors, 'time', SimpleNamespace(time=lambda: now))
    for attempt in range(honors.PER_DAY - 1):
        assert rename(client, headers, f'Correction {attempt}').status_code == 200
    assert rename(client, headers, 'Too Soon').status_code == 429
    now += 2 * 86400
    saved = rename(client, headers, 'Later Correction')
    assert saved.status_code == 200 and saved.json()['name'] == 'Later Correction'
    history = service.db.read(lambda db: honors.name_history(db, READER))
    assert history['entries'][0]['saved_at'] == now


@pytest.mark.parametrize('name', ['علی\u200cرضا رضایی', 'ශ්\u200dරී පෙරේරා', 'क्\u200dष कुमार'], ids=['persian', 'sinhala', 'devanagari'])
def test_joined_names_survive_saving_suggestions_and_restore(client, service, tmp_path, name):
    headers = sign_in(client)
    restore(service, chapter=0, score=16, total=20, identity='final-pass')
    saved = rename(client, headers, name)
    assert saved.status_code == 200, saved.text
    assert saved.json()['name'] == name
    assert honors.suggested(name, 'Ari', READER) == name
    records = [json.loads(row['payload']) for row in events(service)
               if row['type'] in ('quiz_attempt', honors.NAMED)]
    restored = Database(tmp_path/'joined-name.sqlite3')
    try:
        restored.submit(lambda db: import_events(db, records)).result()
        assert restored.read(lambda db: honors.certificate(db, READER))['name'] == name
    finally:
        restored.close()


@pytest.mark.parametrize('raw, expected', [
    ('\u200dAri\u200c', 'Ari'),
    ('Ari\u200d Smith', 'Ari Smith'),
    ('Ari \u200cSmith', 'Ari Smith'),
    ('A\u200d\u200cB', 'AB'),
    ('\u3164\u200d\u2800', ''),
    ('A\u200d\u3164', 'A'),
    ('A\u0301\u200dB', 'Á\u200dB'),
    ('q\u0301\u0301\u0301\u200d\u0301B', 'q\u0301\u0301\u0301\u200dB'),
    ('a\u200b\u0301', 'á'),
    ('q' + '\U0001171e' * 5 + '\u200dB', 'q' + '\U0001171e' * 5 + '\u200dB'),
])
def test_name_joiners_need_printable_neighbors_and_do_not_bypass_mark_limits(raw, expected):
    assert honors.tidy(raw) == expected
    assert honors.tidy(expected) == expected


@pytest.mark.parametrize('raw, expected', [
    ('Maria ' + 'Longsurname' * 8, 'Maria'),
    ('x' * 81, 'x' * 80),
    ('ก' * 79 + 'ขิ่', 'ก' * 79),
    ('अ' * 79 + 'कि', 'अ' * 79),
    ('a' * 78 + 'क्\u200dष', 'a' * 78),
    ('a' * 79 + '👍🏽', 'a' * 79),
    ('a' * 79 + '🇹🇭', 'a' * 79),
    ('a' * 78 + 'ขิ่', 'a' * 78),
    ('a' * 77 + 'ขิ่', 'a' * 77 + 'ขิ่'),
])
def test_long_suggestions_end_at_whole_words_or_graphemes(raw, expected):
    assert honors.fitted(raw) == expected


@pytest.mark.parametrize('kind', ['quiz_attempt', honors.NAMED])
def test_tied_certificate_events_survive_reversed_restore_order(service, tmp_path, kind):
    passes = [
        {'event_id': f'{letter}-pass', 'event': 'quiz_attempt', 'ts': 70, 'email': READER,
         'name': f'{name} Person Example', 'detail': {'chapter': 0, 'score': 16, 'total': 20, 'passed': True}}
        for letter, name in [('a', 'Alpha'), ('z', 'Zulu')]
    ]
    named = [
        {'event_id': f'{letter}-name', 'event': honors.NAMED, 'ts': 80, 'email': READER, 'detail': {'name': name}}
        for letter, name in [('a', 'Alpha Name'), ('z', 'Zulu Name')]
    ]
    records = passes if kind == 'quiz_attempt' else passes[:1] + named
    service.db.submit(lambda db: import_events(db, records)).result()
    before = service.db.read(lambda db: honors.certificate(db, READER))
    assert before['number'] == honors.number('a-pass', 70)
    assert before['name'] == ('Alpha Person Example' if kind == 'quiz_attempt' else 'Zulu Name')
    restored = Database(tmp_path/'restored.sqlite3')
    try:
        restored.submit(lambda db: import_events(db, list(reversed(records)))).result()
        assert restored.read(lambda db: honors.certificate(db, READER)) == before
    finally:
        restored.close()


# Value: protects=a certificate never prints a blank, invisible or symbol-only name; fails_when=tidy() keeps Hangul,
#   Braille or hieroglyph fillers (category Lo/So) or the letter-or-digit check goes; why_new=QA reproduced U+3164 being
#   saved as confirmed, and security found the Egyptian hieroglyph blanks U+13441/U+13442 (category Lo); seam=none
@pytest.mark.parametrize('name', ['', '   ', '\u200b\u200b', 'x' * (honors.NAME_LIMIT + 1), '\u3164', '\u2800', '\u115f\u1160',
                                  '\uffa0 \u034f', '\u0301\u0301', '- . -', '\U00013441', '\U00013442 \U00013441'])
def test_a_name_must_be_one_to_eighty_printable_characters(client, service, name):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    take_exam(client, headers, 16)
    response = rename(client, headers, name)
    assert response.status_code == 422 and response.json()['error'] == 'invalid_name'
    assert 'at least one letter or number' in response.json()['message']
    assert events(service, honors.NAMED) == []


# Value: protects=the longest name the form allows (80) is saved, and a longer one that tidies to 80 or less;
#   fails_when=the length check becomes >= NAME_LIMIT or measures the name before tidying;
#   why_new=only the refusing side (81 characters) was tested;
#   seam=none
def test_the_longest_name_the_form_allows_is_accepted(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    take_exam(client, headers, 16)
    # Thai keeps its vowel and tone marks; a pile of accents on one letter keeps three; fillers around a name go.
    # Value: protects=a Thai name with marks on several letters (a vowel and tone mark stacked, five marks in all) prints
    #   every mark; fails_when=tidy() counts combining marks across the whole name instead of per letter, dropping
    #   the fourth on; why_new=the other Thai names carry one mark in all and the accent pile sits on one letter; seam=none
    for typed, printed in (('x' * honors.NAME_LIMIT, 'x' * honors.NAME_LIMIT), ('Maria' + ' ' * 100 + 'Santos', 'Maria Santos'),
                           ('สมชาย ใจดี', 'สมชาย ใจดี'), ('Zq' + '\u0301' * 20 + 'e', 'Zq' + '\u0301' * 3 + 'e'),
                           ('\u3164Nok\u2800 Saelim', 'Nok Saelim'), ('น้ำผึ้ง ศรีสุข', 'น้ำผึ้ง ศรีสุข')):
        saved = rename(client, headers, typed)
        assert saved.status_code == 200, saved.text
        assert saved.json()['name'] == printed


# Value: protects=the number, date and suggested name come from the earliest pass even when a restore imports a later one first;
#   fails_when=FIRST_PASS stops matching quiz_standing.passed_at (batches restore in batch-id order, not time order);
#   why_new=every other restore case imports a single Final Exam pass;
#   seam=none
def test_the_earliest_pass_names_the_certificate_whatever_order_a_restore_imports(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    service.db.submit(lambda db: import_events(db, [
        {'event_id': 'later-pass', 'event': 'quiz_attempt', 'ts': 90, 'email': READER, 'name': 'Later Name Here',
         'detail': {'chapter': 0, 'score': 18, 'total': 20, 'passed': True}},
        {'event_id': 'first-pass', 'event': 'quiz_attempt', 'ts': 70, 'email': READER, 'name': 'First Name Here',
         'detail': {'chapter': 0, 'score': 16, 'total': 20, 'passed': True}},
    ])).result()
    assert certificate(client, headers) == {'number': honors.number('first-pass', 70), 'awarded_at': 70,
                                            'name': 'First Name Here', 'name_confirmed': False}


# Value: protects=the certificate number and suggested name come from the attempt quiz_standing counted; fails_when=FIRST_PASS
#   accepts a passing event quiz.record() skips (a non-integer score) at the same time; why_new=every restored pass had an
#   integer score; seam=none
def test_the_number_comes_from_the_attempt_quiz_standing_counted(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    # Same time; the uncounted event (a score kept as text, as a hand-edited summary log row can) is imported first.
    service.db.submit(lambda db: import_events(db, [
        {'event_id': 'uncounted', 'event': 'quiz_attempt', 'ts': 70, 'email': READER, 'name': 'Wrong Name',
         'detail': {'chapter': 0, 'score': '16', 'total': 20, 'passed': True}},
        {'event_id': 'counted', 'event': 'quiz_attempt', 'ts': 70, 'email': READER, 'name': 'Right Name',
         'detail': {'chapter': 0, 'score': 16, 'total': 20, 'passed': True}},
    ])).result()
    awarded = certificate(client, headers)
    assert (awarded['number'], awarded['name']) == (honors.number('counted', 70), 'Right Name')


def test_naming_needs_a_passed_exam(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    response = rename(client, headers, 'Maria Santos')
    assert response.status_code == 404 and response.json()['error'] == 'certificate_not_found'
    assert client.post('/api/certificate/name', json={'name': 'Maria Santos'}).status_code == 401


# Value: protects=the 20-a-day name cap counts only this person's own changes from the last 24 hours;
#   fails_when=the cap's count drops its email or 24-hour filter, refusing names after colleagues' or day-old changes;
#   why_new=the existing case has one person whose changes are all fresh, so either filter could go unnoticed;
#   seam=none
def test_name_changes_are_limited_per_day(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    take_exam(client, headers, 16)
    # Neither a colleague's changes today nor this person's own from two days ago use up today's changes.
    now = time.time()
    service.db.submit(lambda db: import_events(db, [
        {'event_id': f'named-{email}-{n}', 'event': honors.NAMED, 'ts': ts, 'email': email, 'detail': {'name': f'Name {n}'}}
        for email, ts in ((ADMIN, now), (READER, now - 2 * 86400)) for n in range(honors.PER_DAY)])).result()
    for attempt in range(honors.PER_DAY):
        assert rename(client, headers, f'Maria Santos {attempt}').status_code == 200
    response = rename(client, headers, 'Maria Santos')
    assert response.status_code == 429 and response.json()['error'] == 'rate_limited'


# Value: protects=a newer saved-name event with no usable name never blanks or breaks the certificate; the latest usable name prints;
#   fails_when=chosen() stops skipping empty, non-text or non-object details (a 500, or the suggested name comes back);
#   why_new=restore cases imported only well-formed certificate_name events;
#   seam=none
def test_a_restore_brings_back_the_same_number_and_chosen_name(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    # The summary log or a batch carries the original event ids and details.
    restore(service, chapter=0, score=16, total=20, identity='final-pass')
    service.db.submit(lambda db: import_events(db, [
        {'event_id': 'named-1', 'event': honors.NAMED, 'ts': 60, 'email': READER, 'detail': {'name': 'Maria Santos'}},
        {'event_id': 'named-0', 'event': honors.NAMED, 'ts': 55, 'email': READER, 'detail': {'name': 'Older Name'}},
        # Later rows without a usable name, as an emptied or hand-edited summary log cell restores them.
        *({'event_id': f'unusable-{n}', 'event': honors.NAMED, 'ts': 65 + n, 'email': READER, 'detail': detail}
          for n, detail in enumerate([{}, {'name': 123}, {'name': '​ '}, 'Someone Else', {'name': '- . -'}, {'name': '\U00013441'}])),
    ])).result()
    assert certificate(client, headers) == {'number': honors.number('final-pass', 50), 'awarded_at': 50,
                                            'name': 'Maria Santos', 'name_confirmed': True}


# Value: protects=a restored name longer than the limit prints cut at a word, with no trailing space on the certificate,
#   in Staff progress or in the Change name field; fails_when=chosen() cuts without stripping; why_new=only typed names,
#   which are refused past the limit, were tested; seam=none
def test_a_restored_name_cut_to_the_limit_keeps_no_trailing_space(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    restore(service, chapter=0, score=16, total=20, identity='final-pass')
    service.db.submit(lambda db: import_events(db, [{'event_id': 'named-long', 'event': honors.NAMED, 'ts': 60,
        'email': READER, 'detail': {'name': 'x' * (honors.NAME_LIMIT - 1) + ' yz'}}])).result()
    assert certificate(client, headers)['name'] == 'x' * (honors.NAME_LIMIT - 1)


def test_the_suggested_name_is_the_one_passed_under_for_the_person_admins_and_a_restore(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    restore(service)
    # A restored first pass carries the Google name it was taken under; no session is needed to read it.
    service.db.submit(lambda db: import_events(db, [{'event_id': 'final-pass', 'event': 'quiz_attempt', 'ts': 70,
        'email': READER, 'name': 'Maria Santos Rivera', 'detail': {'chapter': 0, 'score': 17, 'total': 20, 'passed': True}}])).result()
    admin = sign_in(client, ADMIN)
    person = next(p for p in client.get('/api/admin/progress', headers=admin).json()['people'] if p['email'] == READER)
    assert person['certificate']['name'] == 'Maria Santos Rivera'
    # Signing in later under another Google name changes neither view.
    headers = sign_in(client, name='M S')
    assert certificate(client, headers) == person['certificate']


# Value: protects=each holder's printed name and number come from their own events; fails_when=chosen() or FIRST_PASS
#   drops its email filter; why_new=no test had two certificate holders; seam=none
def test_two_holders_each_keep_their_own_number_and_chosen_name(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    restore(service)
    # Two first passes that share a timestamp, as restored batches can.
    restore(service, chapter=0, score=16, total=20, identity='reader-final')
    restore(service, ADMIN, chapter=0, score=17, total=20, identity='admin-final')
    reader, admin = sign_in(client), sign_in(client, ADMIN)
    assert rename(client, reader, 'Maria Santos').status_code == 200
    assert rename(client, admin, 'Somchai Jaidee').status_code == 200
    mine, theirs = certificate(client, reader), certificate(client, admin)
    assert (mine['number'], mine['name']) == (honors.number('reader-final', 50), 'Maria Santos')
    assert (theirs['number'], theirs['name']) == (honors.number('admin-final', 50), 'Somchai Jaidee')
    people = {p['email']: p['certificate'] for p in client.get('/api/admin/progress', headers=admin).json()['people']}
    assert people[READER] == mine and people[ADMIN] == theirs


# Value: protects=a certificate outlives the catalog (README: it does not depend on it); fails_when=/api/posts or Staff
#   progress hides a certificate whenever the Final Exam is unavailable; why_new=every other test keeps Chapter 9
#   published; seam=none
def test_archiving_chapter_nine_keeps_every_certificate(client, service):
    post, _ = publish(service, 'SOP Chapter 9: Safety')
    restore(service)
    restore(service, chapter=0, score=16, total=20, identity='final-pass')
    reader = sign_in(client)
    before = certificate(client, reader)
    assert before is not None
    service.db.submit(lambda db: db.execute("UPDATE posts SET status='archived' WHERE id=?", (post,))).result()
    wall = client.get('/api/posts', headers=reader).json()
    assert wall['final_exam'] is None and wall['certificate'] == before
    people = client.get('/api/admin/progress', headers=sign_in(client, ADMIN)).json()['people']
    person = next(p for p in people if p['email'] == READER)
    assert person['final_exam'] is None and person['certificate'] == before


def test_a_certificate_belongs_to_its_holder_alone(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    restore(service)
    restore(service, chapter=0, score=16, total=20, identity='final-pass')
    assert certificate(client, sign_in(client)) is not None
    other = sign_in(client, ADMIN)
    assert certificate(client, other) is None
    assert rename(client, other, 'Someone Else').status_code == 404
    assert certificate(client, sign_in(client))['name'] != 'Someone Else'


@pytest.fixture
def outside_eastern(monkeypatch):
    """A machine outside Eastern, so its local time and Eastern disagree around midnight on any runner."""
    monkeypatch.setenv('TZ', 'Asia/Bangkok')
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# Value: protects=a printed certificate number stays the same across releases for the same first passing attempt;
#   fails_when=honors.number() changes its digest, digit count, letter case or the source of its digits, or takes the
#   year in local time; why_new=other tests compare with honors.number() itself or check only the PT-YYYY-XXXXXXXX shape;
#   seam=none
def test_the_number_carries_the_eastern_year_of_the_pass(outside_eastern):
    new_years_eve = datetime(2026, 12, 31, 20, 30, tzinfo=ZoneInfo('America/New_York')).timestamp()
    assert honors.number('final-pass', new_years_eve).startswith('PT-2026-')
    assert honors.number('final-pass', new_years_eve + 4 * 3600).startswith('PT-2027-')
    # Numbers already on paper must still match after an upgrade: the digits depend only on the attempt id.
    assert honors.number('final-pass', new_years_eve) == 'PT-2026-CA6CC84B'
    assert honors.number('final-pass', new_years_eve + 4 * 3600) == 'PT-2027-CA6CC84B'


def test_an_unverified_restore_hides_certificates_and_refuses_new_names(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    take_exam(client, headers, 16)
    service.db.submit(lambda db: set_meta(db, 'recovery_mode', True)).result()
    assert certificate(client, headers) is None
    response = rename(client, headers, 'Maria Santos')
    assert response.status_code == 409 and response.json()['error'] == 'history_restoring'


def test_staff_progress_suggests_the_staff_list_name_for_a_passer_who_never_signed_in(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    restore(service, MANAGER)
    restore(service, MANAGER, chapter=0, score=19, total=20, identity='manager-final')
    admin = sign_in(client, ADMIN)
    person = next(p for p in client.get('/api/admin/progress', headers=admin).json()['people'] if p['email'] == MANAGER)
    # The manager never signed in and the restored pass names no Google account: the staff list's name is the only one known.
    assert person['certificate'] == {'number': honors.number('manager-final', 50), 'awarded_at': 50,
                                     'name': 'Floor manager', 'name_confirmed': False}
