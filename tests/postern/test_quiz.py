"""Chapter quizzes: five questions per SOP chapter, 80% to open the next chapter, never for modules."""
import asyncio
import json
import uuid

import pytest

from conftest import ADMIN, READER, events, seed_derived, sign_in
from postern import quiz
from postern.auth import parse_allowlist
from postern.db import event, set_meta
from postern.progress import dashboard
from postern.recover import import_events
from postern.service import Service

MANAGER = 'manager@example.invalid'
EXECUTIVE = 'executive@example.invalid'


@pytest.fixture(autouse=True)
def pause_worker(monkeypatch):
    async def paused(_service):
        await asyncio.Event().wait()
    monkeypatch.setattr(Service, 'background_loop', paused)


@pytest.fixture
def allowlist():
    return [['email', 'name', 'role'], [ADMIN, 'Demo administrator', 'admin'], [READER, 'Demo reader', ''],
            [MANAGER, 'Floor manager', 'Manager'], [EXECUTIVE, 'Chief', 'C-Suite']]


def question(chapter, number, answer='a'):
    text = lambda value: {'en': value, 'th': value + ' (th)'}
    return {'id': f'c{chapter}q{number}', 'focus': 'mindset', 'prompt': text(f'Question {number}?'),
            'options': [{'id': key, **text(f'Option {key}')} for key in 'abcd'],
            'answer': answer, 'explanation': text(f'Because {answer}.')}


def bank_file(directory, chapters=(1, 2)):
    path = directory / 'quizzes.json'
    path.write_text(json.dumps({'chapters': [
        {'chapter': chapter, 'questions': [question(chapter, n, 'abcda'[n - 1]) for n in range(1, 6)]}
        for chapter in chapters]}))
    return path


@pytest.fixture
def bank(service, tmp_path):
    service.permissions.quizzes = quiz.load(bank_file(tmp_path))
    return service.permissions.quizzes


def publish(service, category, status='published'):
    post, item = str(uuid.uuid4()), str(uuid.uuid4())
    def write(db):
        db.execute("INSERT INTO posts(id,title,body,category,status,created_at,created_by,published_at) VALUES (?,?,'',?,?,1,?,2)",
                   (post, category + ' handout', category, status, ADMIN))
        db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) VALUES (?,?,'h.pdf','pdf',?,1,2,'ready',0)",
                   (item, post, 'x' * 64))
    service.db.submit(write).result()
    seed_derived(service, item)
    return post, item


@pytest.fixture
def catalog(service, bank):
    return {name: publish(service, name) for name in (
        'Module 1: Service Excellence', 'SOP Chapter 1: Introduction',
        'SOP Chapter 2: Service Excellence', 'SOP Chapter 3: Professional Appearance and Grooming Standards')}


def answers(chapter, correct):
    """The first `correct` questions answered right, the rest wrong."""
    key = 'abcda'
    return {f'c{chapter}q{n}': key[n - 1] if n <= correct else ('b' if key[n - 1] == 'a' else 'a') for n in range(1, 6)}


def read_chapter(service, email, chapter):
    """Record every page of the chapter's published items as served to this person."""
    def write(db):
        for item in db.execute("SELECT i.id,i.pages,i.post_id,p.category FROM items i JOIN posts p ON p.id=i.post_id WHERE p.status='published'"):
            if quiz.chapter_number(item['category']) == chapter:
                for page in range(item['pages']):
                    event(db, 'page_served', email=email, post_id=item['post_id'], item_id=item['id'], detail={'page': page})
    service.db.submit(write).result()


def submit(client, headers, chapter, chosen, version=None, read=True):
    """Submit as someone who has finished the chapter, unless read=False."""
    if read:
        read_chapter(client.service, client.get('/api/session', headers=headers).json()['email'], chapter)
    version = version or client.get(f'/api/quizzes/{chapter}', headers=headers).json()['version']
    return client.post(f'/api/quizzes/{chapter}/attempts', headers=headers, json={'version': version, 'answers': chosen})


def view(client, headers, item):
    return client.post(f'/api/items/{item}/views', headers=headers, json={'attempt_id': str(uuid.uuid4())})


def standing(client, headers):
    body = client.get('/api/posts', headers=headers).json()
    return {post['category']: post['locked_by'] for post in body['posts']}, body


def test_chapters_are_numbered_categories_and_modules_never_are():
    assert quiz.chapter_number('SOP Chapter 3: Professional Appearance and Grooming Standards') == 3
    assert quiz.chapter_number('  sop chapter 10 ') == 10
    assert quiz.chapter_number('บทที่ 4 มาตรฐานการบริการ') == 4
    assert quiz.chapter_number('บทที่ ๓ การแต่งกาย') == 3
    assert quiz.chapter_number('Chapter-5: Customers') == quiz.chapter_number('SOP chapter: 5') == 5
    for pasted in ('\u200bSOP Chapter 3', 'บทที่\u200b๓ การแต่งกาย', 'SOP Chapter ３', 'SOP-Chapter 3', 'SOP: Chapter 3', '\ufeffChapter 3'):
        assert quiz.chapter_number(pasted) == 3, pasted
    for name in ('Module 1: Service Excellence', 'module 2', 'Module 3 chapter 1', 'General', '', 'Chapters 1-3', 'Live 9/23/26',
                 'Appendix to Chapter 2', 'Live recap: Chapter 3', 'SOP Chapter'):
        assert quiz.chapter_number(name) is None, name


def test_manager_and_c_suite_roles_are_recognized_and_unknown_roles_still_refuse():
    sheet = [['email', 'role'], ['a@example.com', 'Manager'], ['b@example.com', 'C-Suite'], ['c@example.com', 'c suite'],
             ['d@example.com', 'CSuite'], ['e@example.com', 'C-Level'], ['f@example.com', ' Admin '], ['g@example.com', ''],
             ['h@example.com', 'Staff']]
    roles = {email: member['role'] for email, member in parse_allowlist(sheet).items()}
    assert roles == {'a@example.com': 'manager', 'b@example.com': 'c-suite', 'c@example.com': 'c-suite',
                     'd@example.com': 'c-suite', 'e@example.com': 'c-suite', 'f@example.com': 'admin', 'g@example.com': 'reader',
                     'h@example.com': 'reader'}
    with pytest.raises(ValueError, match='Row 2: role must be blank'):
        parse_allowlist([['email', 'role'], ['a@example.com', 'Owner']])


def test_bank_validation_refuses_incomplete_quizzes(tmp_path):
    path = tmp_path / 'bank.json'
    broken = question(1, 1)
    broken['answer'] = 'z'
    for chapters in ([{'chapter': 1, 'questions': [broken]}],
                     [{'chapter': 1, 'questions': [{**question(1, 1), 'prompt': {'en': 'Only English'}}]}],
                     [{'chapter': 1, 'questions': [question(1, 1)]}, {'chapter': 1, 'questions': [question(1, 2)]}],
                     [{'chapter': 1, 'questions': []}],
                     [{'chapter': 1, 'questions': [{**question(1, 1), 'options': [{**o, 'id': n} for n, o in enumerate(question(1, 1)['options'])], 'answer': 0}]}],
                     [{'chapter': 1, 'questions': [{**question(1, 1), 'id': 7}]}],
                     [{'chapter': 1, 'questions': [question(1, n) for n in range(1, 22)]}]):
        path.write_text(json.dumps({'chapters': chapters}))
        with pytest.raises((ValueError, KeyError)):
            quiz.load(path)


def test_sample_bank_has_twelve_valid_questions_for_each_demo_chapter():
    bank = quiz.load()
    assert sorted(bank) == list(range(1, 10))
    for number, entry in bank.items():
        questions = entry['questions']
        assert (len(questions), entry['draw'], entry['pass_mark']) == (12, 5, 4), number
        assert [q['id'] for q in questions] == [f'sample-c{number}q{n}' for n in range(1, 13)]
        assert all([o['id'] for o in q['options']] == list('abcd') for q in questions), number
        assert [q['focus'] for q in questions].count('conduct') == 6, number
        assert entry['tier'] == 'full' and entry['hands'], number


def test_synthetic_pools_never_repeat_the_previous_attempt_and_cover_the_pool_within_four(tmp_path):
    import random
    for number, entry in quiz.load(pool_file(tmp_path)).items():
        hands = [frozenset(hand) for hand in entry['hands']]
        assert len(hands) >= 50, (number, len(hands))
        # Whatever was dealt last, some hand the rules allow shares no question with it.
        assert all(any(not other & hand for other in hands) for hand in hands), number
        for seed in range(100):
            rng, seen, last, covered = random.Random(seed), {}, set(), set()
            for _ in range(4):
                hand = quiz.deal(entry, seen, last, rng)
                assert not set(hand) & last, (number, seed)
                for key in hand:
                    seen[key] = seen.get(key, 0) + 1
                last = set(hand)
                covered |= last
            assert len(covered) == 12, (number, seed)


def test_reader_must_pass_each_quiz_to_open_the_next_chapter(client, service, catalog):
    reader = sign_in(client)
    locks, body = standing(client, reader)
    assert locks == {'Module 1: Service Excellence': None, 'SOP Chapter 1: Introduction': None,
                     'SOP Chapter 2: Service Excellence': 1, 'SOP Chapter 3: Professional Appearance and Grooming Standards': 1}
    assert body['quiz_exempt'] is False
    assert [(q['chapter'], q['passed'], q['locked_by'], q['attempts']) for q in body['quizzes']] == [(1, False, None, 0), (2, False, 1, 0)]
    assert view(client, reader, catalog['Module 1: Service Excellence'][1]).status_code == 200
    assert view(client, reader, catalog['SOP Chapter 1: Introduction'][1]).status_code == 200
    locked = view(client, reader, catalog['SOP Chapter 2: Service Excellence'][1])
    assert locked.status_code == 423
    assert locked.json() == {'error': 'chapter_locked', 'message': 'Finish Chapter 1 and pass its quiz to unlock this chapter. / เรียนบทที่ 1 ให้ครบและสอบผ่านก่อน จึงจะเปิดบทนี้ได้'}
    thumbnail = client.get(f"/api/posts/{catalog['SOP Chapter 2: Service Excellence'][0]}/thumb.jpg", headers=reader)
    assert thumbnail.status_code == 423 and thumbnail.json()['error'] == 'chapter_locked'
    assert client.get('/api/quizzes/2', headers=reader).status_code == 423
    assert client.get('/api/quizzes/3', headers=reader).status_code == 404

    failed = submit(client, reader, 1, answers(1, 3)).json()
    assert (failed['score'], failed['total'], failed['pass_mark'], failed['passed']) == (3, 5, 4, False)
    # A failed attempt reports only its score: no per-question marks to guess from.
    assert failed['results'] == []
    assert view(client, reader, catalog['SOP Chapter 2: Service Excellence'][1]).status_code == 423

    passed = submit(client, reader, 1, answers(1, 4)).json()
    assert (passed['score'], passed['passed']) == (4, True)
    assert {r['id']: r for r in passed['results']}['c1q5'] == {'id': 'c1q5', 'chosen': 'b', 'correct': False, 'answer': 'a',
                                                              'explanation': {'en': 'Because a.', 'th': 'Because a. (th)'}}
    assert passed['next_chapter'] == {'chapter': 2, 'category': 'SOP Chapter 2: Service Excellence', 'unlocked': True}
    assert view(client, reader, catalog['SOP Chapter 2: Service Excellence'][1]).status_code == 200
    locks, body = standing(client, reader)
    assert locks['SOP Chapter 3: Professional Appearance and Grooming Standards'] == 2
    first = body['quizzes'][0]
    assert (first['passed'], first['attempts'], first['best_score'], first['last_score']) == (True, 2, 4, 4)

    # Retaking after a pass never takes the pass away.
    submit(client, reader, 1, answers(1, 0))
    first = standing(client, reader)[1]['quizzes'][0]
    assert (first['passed'], first['attempts'], first['best_score'], first['last_score']) == (True, 3, 4, 0)
    recorded = [json.loads(e['payload'])['detail'] for e in events(service, 'quiz_attempt')]
    assert [(d['chapter'], d['score'], d['passed']) for d in recorded] == [(1, 3, False), (1, 4, True), (1, 0, False)]
    assert recorded[0]['answers'] == answers(1, 3) and recorded[0]['category'] == 'SOP Chapter 1: Introduction'
    assert len(events(service, 'quiz_opened')) >= 3


def test_questions_never_carry_the_answer_key(client, catalog):
    reader = sign_in(client)
    body = client.get('/api/quizzes/1', headers=reader).json()
    assert (body['chapter'], body['category'], body['total'], body['pass_mark'], body['exempt']) == (1, 'SOP Chapter 1: Introduction', 5, 4, False)
    assert body['standing']['passed'] is False
    text = json.dumps(body)
    assert '"answer"' not in text and 'explanation' not in text and 'Because' not in text
    assert all(sorted(option['id'] for option in q['options']) == list('abcd') for q in body['questions'])


def test_incomplete_or_stale_answers_are_refused_without_recording_an_attempt(client, service, catalog):
    reader = sign_in(client)
    partial = answers(1, 5)
    partial.pop('c1q5')
    assert submit(client, reader, 1, partial).json()['error'] == 'answers_required'
    assert submit(client, reader, 1, {**answers(1, 5), 'c1q5': 'z'}).status_code == 422
    assert submit(client, reader, 1, {**answers(1, 5), 'extra': 'a'}).status_code == 422
    stale = submit(client, reader, 1, answers(1, 5), version='0000000000000000')
    assert stale.status_code == 409 and stale.json()['error'] == 'quiz_changed'
    assert events(service, 'quiz_attempt') == []


@pytest.mark.parametrize('email', [ADMIN, MANAGER, EXECUTIVE])
def test_admins_managers_and_c_suite_may_skip_quizzes_but_still_take_them(client, catalog, email):
    leader = sign_in(client, email)
    locks, body = standing(client, leader)
    assert set(locks.values()) == {None} and body['quiz_exempt'] is True
    assert all(q['locked_by'] is None for q in body['quizzes'])
    assert view(client, leader, catalog['SOP Chapter 3: Professional Appearance and Grooming Standards'][1]).status_code == 200
    assert client.get('/api/quizzes/2', headers=leader).json()['exempt'] is True
    result = submit(client, leader, 2, answers(2, 5)).json()
    assert result['passed'] is True and result['next_chapter']['unlocked'] is True


def test_manager_demoted_to_staff_is_locked_again_on_every_content_route(client, service, catalog, store, allowlist):
    from postern.auth import token_hash
    import secrets
    import time
    manager = sign_in(client, MANAGER)
    opened = view(client, manager, catalog['SOP Chapter 2: Service Excellence'][1])
    assert opened.status_code == 200
    identity = opened.json()['id']
    assert client.get(f'/api/views/{identity}/page/0.jpg', headers=manager).status_code == 200
    ticket = secrets.token_urlsafe(32)
    service.db.submit(lambda db: db.execute('INSERT INTO video_tickets VALUES (?,?,?)', (token_hash(ticket), identity, time.time() + 600))).result()
    store.write_allowlist([row if row[0] != MANAGER else [MANAGER, 'Floor manager', ''] for row in allowlist])
    asyncio.run(service.refresh())
    for response in (view(client, manager, catalog['SOP Chapter 2: Service Excellence'][1]),
                     client.get(f'/api/views/{identity}/page/0.jpg', headers=manager),
                     client.get(f'/api/views/{identity}/photo.jpg', headers=manager),
                     client.post(f'/api/views/{identity}/video', headers=manager),
                     client.post(f'/api/views/{identity}/video-progress', headers=manager, json={'ranges': []}),
                     client.get(f'/api/video/{ticket}', headers={'Range': 'bytes=0-1'}),
                     client.get(f"/api/posts/{catalog['SOP Chapter 2: Service Excellence'][0]}/thumb.jpg", headers=manager),
                     client.get('/api/quizzes/2', headers=manager),
                     client.post('/api/quizzes/2/attempts', headers=manager, json={'version': service.permissions.quizzes[2]['version'], 'answers': answers(2, 5)})):
        assert response.status_code == 423, response.text
        assert response.json() == {'error': 'chapter_locked', 'message': 'Finish Chapter 1 and pass its quiz to unlock this chapter. / เรียนบทที่ 1 ให้ครบและสอบผ่านก่อน จึงจะเปิดบทนี้ได้'}
    assert events(service, 'quiz_attempt') == []


def test_unpublished_chapters_do_not_block_later_ones(client, service, catalog):
    reader = sign_in(client)
    def archive(db):
        db.execute("UPDATE posts SET status='archived' WHERE category='SOP Chapter 1: Introduction'")
    service.db.submit(archive).result()
    locks, body = standing(client, reader)
    assert locks['SOP Chapter 2: Service Excellence'] is None
    assert locks['SOP Chapter 3: Professional Appearance and Grooming Standards'] == 2
    assert [q['chapter'] for q in body['quizzes']] == [2]
    assert client.get('/api/quizzes/1', headers=reader).status_code == 404


def test_restored_attempts_keep_passes_and_recovery_hides_quizzes(client, service, catalog):
    reader = sign_in(client)
    def restore(db):
        import_events(db, [{'event_id': 'restored-pass', 'event': 'quiz_attempt', 'ts': 50, 'email': READER,
                            'detail': {'chapter': 1, 'score': 5, 'total': 5, 'passed': True}},
                           {'event_id': 'restored-fail', 'event': 'quiz_attempt', 'ts': 40, 'email': READER,
                            'detail': {'chapter': 1, 'score': 2, 'total': 5, 'passed': False}}])
    service.db.submit(restore).result()
    locks, body = standing(client, reader)
    assert locks['SOP Chapter 2: Service Excellence'] is None
    first = body['quizzes'][0]
    assert (first['attempts'], first['best_score'], first['last_score'], first['passed_at']) == (2, 5, 5, 50)
    service.db.submit(lambda db: set_meta(db, 'recovery_mode', True)).result()
    assert client.get('/api/posts', headers=reader).json() == {'posts': [], 'quizzes': [], 'quiz_exempt': False, 'final_exam': None, 'certificate': None}
    assert client.get('/api/quizzes/1', headers=reader).status_code == 404


def test_admin_progress_reports_quiz_standing_from_the_single_history_scan(client, service, catalog):
    admin = sign_in(client, ADMIN)
    reader = sign_in(client)
    submit(client, reader, 1, answers(1, 2))
    submit(client, reader, 1, answers(1, 5))
    report = client.get('/api/admin/progress', headers=admin).json()
    assert report['quiz_total'] == 2
    people = {person['email']: person for person in report['people']}
    person = people[READER]
    assert (person['quizzes_passed'], person['quiz_exempt']) == (1, False)
    assert [(q['chapter'], q['passed'], q['attempts'], q['best_score'], q['locked_by']) for q in person['quizzes']] == [
        (1, True, 2, 5, None), (2, False, 0, None, None)]
    assert people[MANAGER]['quiz_exempt'] is True and people[MANAGER]['quizzes_passed'] == 0
    statements = []
    def inspect(db):
        db.set_trace_callback(statements.append)
        return dashboard(db, service.permissions.quizzes)
    result = service.db.read(inspect)
    assert len([s for s in statements if 'FROM events' in s]) == 1
    assert {p['email']: p['quizzes_passed'] for p in result['people']}[READER] == 1


def test_quiz_attempts_are_rate_limited_per_account_after_authorization(client, service, catalog):
    # Anonymous requests never reach the limiter, so they cannot crowd out sign-in buckets.
    for _ in range(50):
        anonymous = client.post('/api/quizzes/1/attempts', headers={'Authorization': f'Bearer {uuid.uuid4()}'},
                                json={'version': 'x', 'answers': {}})
        assert anonymous.status_code == 401
    assert not [key for key in service.limits if key.startswith('quiz:')]
    reader, again = sign_in(client), sign_in(client)
    read_chapter(service, READER, 1)
    def attempt(headers):
        version = client.get('/api/quizzes/1', headers=headers).json()['version']
        return client.post('/api/quizzes/1/attempts', headers=headers, json={'version': version, 'answers': answers(1, 0)})
    assert [attempt(reader).status_code for _ in range(10)] == [200] * 10
    refused = attempt(again)
    assert refused.status_code == 429
    assert refused.json() == {'error': 'rate_limited', 'message': 'Too many quiz attempts. Please wait a minute and try again. / ทำแบบทดสอบถี่เกินไป กรุณารอ 1 นาทีแล้วลองใหม่'}
    assert len(events(service, 'quiz_attempt')) == 10
    assert attempt(sign_in(client, MANAGER)).status_code == 200


def test_malformed_restored_attempts_are_ignored_rather_than_breaking_reading(client, service, catalog):
    admin = sign_in(client, ADMIN)
    reader = sign_in(client)
    def restore(db):
        import_events(db, [{'event_id': f'odd-{n}', 'event': 'quiz_attempt', 'ts': 10 + n, 'email': READER, 'detail': detail}
                           for n, detail in enumerate([None, [1], 'passed', {'chapter': '1', 'score': 5, 'passed': True},
                                                       {'chapter': 1, 'score': 5, 'passed': 'yes'}, {'chapter': 1, 'passed': True},
                                                       {'chapter': 1, 'score': None, 'passed': True}])])
    service.db.submit(restore).result()
    locks, body = standing(client, reader)
    assert locks['SOP Chapter 2: Service Excellence'] == 1 and body['quizzes'][0]['attempts'] == 1
    # The lock the reading room shows is the lock the server enforces.
    assert view(client, reader, catalog['SOP Chapter 2: Service Excellence'][1]).status_code == 423
    assert service.db.read(lambda db: (quiz.passes(db, READER), quiz.passed_chapters(quiz.records(db, READER), READER))) == (set(), set())
    assert client.get('/api/admin/progress', headers=admin).status_code == 200
    # Value: protects=restored attempts with malformed answers still fold into standing and count no questions; fails_when=remember() loosens its shape guard so a number raises mid-restore or a string/list/oversized map adds seen rows; why_new=the rows above never reach remember(); seam=none
    def malformed_answers(db):
        import_events(db, [{'event_id': f'odd-answers-{n}', 'event': 'quiz_attempt', 'ts': 30 + n, 'email': READER,
                            'detail': {'chapter': 1, 'score': 0, 'passed': False, 'answers': chosen}}
                           for n, chosen in enumerate([5, 'c1q1', ['c1q1'], {f'c1q{k}': 'a' for k in range(1, quiz.LIMIT + 2)}])])
    service.db.submit(malformed_answers).result()
    assert service.db.read(lambda db: db.execute('SELECT count(*) FROM quiz_seen').fetchone()[0]) == 0
    assert standing(client, reader)[1]['quizzes'][0]['attempts'] == 5


def test_a_demotion_during_the_wall_snapshot_is_reflected_in_its_locks(client, service, catalog, monkeypatch):
    import postern.service as service_module
    manager = sign_in(client, MANAGER)
    original = service_module.annotate
    def demote_then_annotate(db, posts, email):
        service.db.submit(lambda writer: writer.execute("UPDATE members SET role='reader' WHERE email=?", (MANAGER,))).result()
        return original(db, posts, email)
    monkeypatch.setattr(service_module, 'annotate', demote_then_annotate)
    locks, body = standing(client, manager)
    assert body['quiz_exempt'] is False and locks['SOP Chapter 2: Service Excellence'] == 1


@pytest.mark.parametrize('email', [MANAGER, EXECUTIVE])
def test_managers_and_c_suite_skip_quizzes_but_never_reach_admin_routes(client, catalog, email):
    leader = sign_in(client, email)
    assert client.get('/api/session', headers=leader).json()['role'] == ('manager' if email == MANAGER else 'c-suite')
    post = catalog['SOP Chapter 1: Introduction'][0]
    for response in (client.get('/api/admin/progress', headers=leader), client.get('/api/admin/posts', headers=leader),
                     client.get('/api/admin/health', headers=leader), client.get(f'/api/admin/posts/{post}/report', headers=leader),
                     client.post('/api/admin/posts', headers=leader, json={'title': 'Probe', 'body': '', 'category': 'SOP Chapter 1'}),
                     client.post(f'/api/admin/posts/{post}/archive', headers=leader)):
        assert response.status_code == 403, response.text
        assert response.json() == {'error': 'access_denied', 'message': 'Administrator access is required. / ต้องมีสิทธิ์ผู้ดูแลระบบ'}


def test_attempts_older_than_a_minute_no_longer_count_toward_the_limit(client, service, catalog):
    import time
    reader = sign_in(client)
    now = time.time()
    def restore(db):
        import_events(db, [{'event_id': f'old-{n}', 'event': 'quiz_attempt', 'ts': now - 61, 'email': READER,
                            'detail': {'chapter': 1, 'score': 0, 'total': 5, 'passed': False}} for n in range(10)] +
                          [{'event_id': f'recent-{n}', 'event': 'quiz_attempt', 'ts': now - 30, 'email': READER,
                            'detail': {'chapter': 1, 'score': 0, 'total': 5, 'passed': False}} for n in range(9)])
    service.db.submit(restore).result()
    assert submit(client, reader, 1, answers(1, 0)).status_code == 200
    refused = submit(client, reader, 1, answers(1, 0))
    assert refused.status_code == 429 and refused.json()['error'] == 'rate_limited'


def test_a_chapter_without_a_quiz_never_locks_and_the_last_quiz_names_no_next_chapter(client, service, catalog):
    reader = sign_in(client)
    fourth = publish(service, 'SOP Chapter 4: Bar Service')
    assert standing(client, reader)[0]['SOP Chapter 4: Bar Service'] == 1
    failed = submit(client, reader, 1, answers(1, 3)).json()
    assert failed['next_chapter'] == {'chapter': 2, 'category': 'SOP Chapter 2: Service Excellence', 'unlocked': False}
    submit(client, reader, 1, answers(1, 4))
    assert standing(client, reader)[0]['SOP Chapter 4: Bar Service'] == 2
    # Chapter 3 has no quiz, so passing Chapter 2 opens both Chapter 3 and Chapter 4.
    second = submit(client, reader, 2, answers(2, 5)).json()
    assert second['next_chapter'] == {'chapter': 3, 'category': 'SOP Chapter 3: Professional Appearance and Grooming Standards', 'unlocked': True}
    assert [(q['chapter'], q['passed'], q['locked_by']) for q in second['quizzes']] == [(1, True, None), (2, True, None)]
    assert set(standing(client, reader)[0].values()) == {None}
    assert view(client, reader, fourth[1]).status_code == 200
    def archive(db):
        db.execute("UPDATE posts SET status='archived' WHERE category LIKE 'SOP Chapter 3%' OR category LIKE 'SOP Chapter 4%'")
    service.db.submit(archive).result()
    assert submit(client, reader, 2, answers(2, 5)).json()['next_chapter'] is None


def test_malformed_submissions_are_refused_before_grading_and_record_nothing(client, service, catalog):
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    version = client.get('/api/quizzes/1', headers=reader).json()['version']
    attempt = lambda body, chapter='1': client.post(f'/api/quizzes/{chapter}/attempts', headers=reader, json=body)
    for body in ({'version': version, 'answers': {f'c1q{n}': 'a' for n in range(1, 22)}},
                 {'version': version, 'answers': {**answers(1, 5), 'c1q1': 1}},
                 {'version': version, 'answers': list(answers(1, 5).values())},
                 {'version': 'v' * 65, 'answers': answers(1, 5)},
                 {'answers': answers(1, 5)}):
        assert attempt(body).status_code == 422, body
    assert attempt({'version': version, 'answers': answers(1, 5)}, chapter='one').status_code == 422
    for chapter in (0, -1, 99):
        assert client.get(f'/api/quizzes/{chapter}', headers=reader).json()['error'] == 'quiz_not_found'
        assert attempt({'version': version, 'answers': answers(1, 5)}, chapter=chapter).status_code == 404
    assert events(service, 'quiz_attempt') == []


def test_bank_validation_refuses_every_malformed_shape_and_rounds_the_pass_mark_up(tmp_path):
    path = tmp_path / 'bank.json'
    first = question(1, 1)
    # Value: protects=load() refuses a question with no known focus, so a typo cannot weaken the focus balance of every hand; fails_when=the FOCUSES check is dropped from load(); why_new=no malformed case varied focus; seam=none
    for chapters in ([{'chapter': 1, 'questions': [{**first, 'focus': 'grooming'}]}],
                     [{'chapter': 1, 'questions': [{key: value for key, value in first.items() if key != 'focus'}]}],
                     [{'chapter': '1', 'questions': [first]}],
                     [{'chapter': True, 'questions': [first]}],
                     [{'chapter': 1, 'questions': [first, question(1, 1)]}],
                     [{'chapter': 1, 'questions': [{**first, 'id': ''}]}],
                     [{'chapter': 1, 'questions': [{**first, 'options': first['options'][:1]}]}],
                     [{'chapter': 1, 'questions': [{**first, 'options': [first['options'][0]] * 2}]}],
                     [{'chapter': 1, 'questions': [{**first, 'explanation': {'en': '  ', 'th': 'th'}}]}],
                     [{'chapter': 1, 'questions': [{**first, 'options': [{**first['options'][0], 'th': 5}, *first['options'][1:]]}]}]):
        path.write_text(json.dumps({'chapters': chapters}))
        with pytest.raises(ValueError):
            quiz.load(path)
    # Attempts deal five questions however large the pool, so the mark stays four of five.
    for count, mark in ((1, 1), (3, 3), (4, 4), (5, 4), (10, 4), (15, 4), (20, 4)):
        path.write_text(json.dumps({'chapters': [{'chapter': 1, 'questions': [question(1, n) for n in range(1, count + 1)]}]}))
        assert quiz.load(path)[1]['pass_mark'] == mark, count
    # The version is the content: unchanged text keeps it, any edit to the key changes it.
    bank = bank_file(tmp_path)
    before = quiz.load(bank)
    assert quiz.load(bank)[1]['version'] == before[1]['version']
    data = json.loads(bank.read_text())
    data['chapters'][0]['questions'][0]['answer'] = 'b'
    bank.write_text(json.dumps(data))
    changed = quiz.load(bank)
    assert changed[1]['version'] != before[1]['version'] and changed[2]['version'] == before[2]['version']


def test_standing_is_the_same_whatever_order_restored_attempts_arrive_in():
    import itertools
    import sqlite3
    from postern.db import SCHEMA
    def row(seq, ts, score, passed):
        return {'email': READER, 'ts': ts, 'seq': seq,
                'payload': json.dumps({'detail': {'chapter': 1, 'score': score, 'passed': passed}})}
    attempts = [row(4, 30, 3, False), row(1, 20, 4, True), row(3, 30, 5, True), row(2, 10, 1, False)]
    for order in itertools.permutations(attempts):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        for entry in (*order, {'email': READER, 'ts': 99, 'seq': 9, 'payload': '[1]'}):
            quiz.record(db, entry)
        state = quiz.records(db, READER)[(READER, 1)]
        # Equal times fall back to sequence for the latest score; the pass dates from the earliest pass.
        assert (state['attempts'], state['best_score'], state['last_score'], state['last_attempt_at'],
                state['passed'], state['passed_at']) == (4, 5, 3, 30, True, 20)


def test_setup_check_counts_the_managers_and_c_suite_who_may_skip_quizzes(monkeypatch, config, allowlist):
    from postern import bootstrap
    class Sheet:
        def __init__(self, _config):
            pass
        def allowlist(self):
            return Sheet.values
    monkeypatch.setattr(bootstrap, 'GoogleStore', Sheet)
    Sheet.values = allowlist
    report = []
    assert bootstrap.check_allowlist(config, report) is True
    assert report[0] == 'allowlist          4 member(s), 1 administrator(s)'
    assert report[-1] == '                   2 manager/C-suite member(s) may skip chapter quizzes'
    Sheet.values = [*allowlist, ['owner@example.invalid', 'Owner', 'Owner']]
    report = []
    assert bootstrap.check_allowlist(config, report) is False
    assert report == ['allowlist          INVALID — Row 6: role must be blank (staff), Admin, Manager or C-Suite']


def test_without_a_quiz_bank_chapters_stay_open_and_progress_lists_no_quizzes(client, service, catalog):
    service.permissions.quizzes = {}
    reader = sign_in(client)
    locks, body = standing(client, reader)
    assert set(locks.values()) == {None} and body['quizzes'] == []
    assert view(client, reader, catalog['SOP Chapter 3: Professional Appearance and Grooming Standards'][1]).status_code == 200
    assert client.get('/api/quizzes/1', headers=reader).status_code == 404
    result = service.db.read(lambda db: dashboard(db))
    assert result['quiz_total'] == 0
    assert all((p['quizzes'], p['quizzes_passed']) == ([], 0) for p in result['people'])


def test_a_chapter_published_under_two_names_has_one_quiz_and_both_open_together(client, service, catalog):
    reader = sign_in(client)
    publish(service, 'SOP Chapter 2: Service Excellence (Thai)')
    locks, body = standing(client, reader)
    assert locks['SOP Chapter 2: Service Excellence (Thai)'] == locks['SOP Chapter 2: Service Excellence'] == 1
    assert [(q['chapter'], q['category']) for q in body['quizzes']] == [(1, 'SOP Chapter 1: Introduction'), (2, 'SOP Chapter 2: Service Excellence')]
    submit(client, reader, 1, answers(1, 5))
    locks, _ = standing(client, reader)
    assert locks['SOP Chapter 2: Service Excellence (Thai)'] is None and locks['SOP Chapter 3: Professional Appearance and Grooming Standards'] == 2


def test_a_demotion_while_a_page_renders_is_refused_before_the_bytes_leave(client, service, catalog, store, allowlist):
    from postern.auth import Refusal
    manager = sign_in(client, MANAGER)
    identity = view(client, manager, catalog['SOP Chapter 2: Service Excellence'][1]).json()['id']
    token = manager['Authorization'].removeprefix('Bearer ')
    context = service.db.submit(lambda db: service.permissions.authorize_content_request(db, token, view_id=identity)).result()
    service.db.read(lambda db: service.still_authorized(db, context))
    store.write_allowlist([row if row[0] != MANAGER else [MANAGER, 'Floor manager', ''] for row in allowlist])
    asyncio.run(service.refresh())
    with pytest.raises(Refusal) as refused:
        service.db.read(lambda db: service.still_authorized(db, context))
    assert (refused.value.status, refused.value.code) == (423, 'chapter_locked')


def test_the_daily_cap_bounds_how_much_quiz_history_one_account_can_add(client, service, catalog):
    import time
    reader = sign_in(client)
    version = client.get('/api/quizzes/1', headers=reader).json()['version']
    def earlier(db):
        for n in range(quiz.PER_DAY):
            event(db, 'quiz_attempt', email=READER, detail={'chapter': 1, 'score': 0, 'passed': False}, timestamp=time.time() - 3600 - n)
    service.db.submit(earlier).result()
    refused = client.post('/api/quizzes/1/attempts', headers=reader, json={'version': version, 'answers': answers(1, 5)})
    assert refused.status_code == 429 and 'today' in refused.json()['message']


def test_locks_and_standing_never_replay_a_long_attempt_history(client, service, catalog):
    reader = sign_in(client)
    def history(db):
        for n in range(300):
            row_id = event(db, 'quiz_attempt', email=READER, detail={'chapter': 1, 'score': n % 5, 'passed': n == 7}, timestamp=1000 + n)
            quiz.record(db, db.execute('SELECT email,ts,seq,payload FROM events WHERE id=?', (row_id,)).fetchone())
    service.db.submit(history).result()
    statements = []
    def inspect(db):
        db.set_trace_callback(statements.append)
        quiz.require_unlocked(db, service.permissions.quizzes, 'reader', READER, 'SOP Chapter 2: Service Excellence')
        return quiz.records(db, READER)
    standing = service.db.read(inspect)[(READER, 1)]
    assert not [s for s in statements if 'FROM events' in s]
    assert (standing['attempts'], standing['best_score'], standing['passed_at']) == (300, 4, 1007)
    assert view(client, reader, catalog['SOP Chapter 2: Service Excellence'][1]).status_code == 200


def test_staff_open_a_chapter_quiz_only_after_opening_every_page(client, service, catalog):
    reader = sign_in(client)
    post, item = catalog['SOP Chapter 1: Introduction']
    body = client.get('/api/quizzes/1', headers=reader).json()
    assert (body['ready'], body['unfinished'], body['questions']) == (False, [item], [])
    assert standing(client, reader)[1]['quizzes'][0]['unfinished'] == [item]
    refused = submit(client, reader, 1, answers(1, 5), read=False)
    assert refused.status_code == 409 and refused.json()['error'] == 'chapter_unfinished'
    assert events(service, 'quiz_attempt') == []
    page = lambda n: service.db.submit(lambda db: event(db, 'page_served', email=READER, post_id=post, item_id=item, detail={'page': n})).result()
    page(0)
    assert client.get('/api/quizzes/1', headers=reader).json()['ready'] is False
    page(1)
    body = client.get('/api/quizzes/1', headers=reader).json()
    assert (body['ready'], body['unfinished'], len(body['questions'])) == (True, [], 5)
    assert standing(client, reader)[1]['quizzes'][0]['unfinished'] == []
    assert submit(client, reader, 1, answers(1, 5), read=False).json()['passed'] is True


def test_a_video_counts_as_watched_within_two_seconds_of_its_end(client, service, bank):
    reader = sign_in(client)
    post, item = str(uuid.uuid4()), str(uuid.uuid4())
    def write(db):
        db.execute("INSERT INTO posts(id,title,body,category,status,created_at,created_by,published_at) VALUES (?,'Video','','SOP Chapter 1: Introduction','published',1,?,2)", (post, ADMIN))
        db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) VALUES (?,?,'v.mp4','video',?,1,0,'ready',0)", (item, post, 'x' * 64))
        db.execute('INSERT INTO video_metadata VALUES (?,100)', (item,))
        # Failed to prepare: it cannot be opened, so it does not hold the quiz back.
        db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) VALUES (?,?,'w.pdf','pdf',?,1,2,'failed',1)", (str(uuid.uuid4()), post, 'x' * 64))
    service.db.submit(write).result()
    watch = lambda end: service.db.submit(lambda db: event(db, 'video_progress', email=READER, post_id=post, item_id=item, detail={'ranges': [[0, end]]})).result()
    watch(97.5)
    assert client.get('/api/quizzes/1', headers=reader).json()['unfinished'] == [item]
    watch(98.5)
    assert client.get('/api/quizzes/1', headers=reader).json()['ready'] is True
    # No recorded length: progress cannot be measured, so it never holds the quiz back.
    assert quiz.finished({'kind': 'video', 'duration_seconds': None}, 0, 0) is True
    # A very short video still has to be played, not skipped because it is under the tolerance.
    assert quiz.finished({'kind': 'video', 'duration_seconds': 1.5}, 0, 0) is False
    assert quiz.finished({'kind': 'video', 'duration_seconds': 1.5}, 0, 0.5) is True


@pytest.mark.parametrize('email', [ADMIN, MANAGER, EXECUTIVE])
def test_admins_managers_and_c_suite_can_open_any_quiz_without_reading(client, catalog, email):
    leader = sign_in(client, email)
    body = client.get('/api/quizzes/1', headers=leader).json()
    assert (body['ready'], body['unfinished'], len(body['questions'])) == (True, [], 5)
    assert all(q['unfinished'] == [] for q in standing(client, leader)[1]['quizzes'])
    assert submit(client, leader, 1, answers(1, 5), read=False).json()['passed'] is True


def test_a_question_id_keeps_its_answer_and_options():
    """quiz_ids.lock.json pins every id ever shipped; removed ids stay so they are never reused."""
    # Value: protects=retakes after a pass and 0.7.x passes pick questions by id on any bank version; fails_when=a bank edit changes or reuses an id with a different answer or options; why_new=load() only documented the rule; seam=none
    import hashlib
    from pathlib import Path
    lock = json.loads((Path(__file__).parent / 'quiz_ids.lock.json').read_text())
    for entry in quiz.load().values():
        for question in entry['questions']:
            digest = hashlib.sha256(json.dumps([question['answer'], question['options']], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
            assert question['id'] in lock, f"{question['id']} is new: add \"{question['id']}\": \"{digest}\" to tests/postern/quiz_ids.lock.json"
            assert lock[question['id']] == digest, f"{question['id']} changed its answer or options: give it a new id and keep the old one in the lock"


def test_no_single_length_rank_or_answer_letter_passes_a_sample_hand():
    for number, entry in quiz.load().items():
        assert entry['tier'] == 'full', number
        for hand in entry['hands']:
            questions = [entry['questions'][i] for i in hand]
            for lang in ('en', 'th'):
                assert max(sum(quiz.length_rank(q, lang) == rank for q in questions)
                           for rank in range(1, 5)) < entry['pass_mark']
            assert max(sum(q['answer'] == letter for q in questions)
                       for letter in 'abcd') < entry['pass_mark']


def attachment(service, post, kind, status='ready', pages=0, duration=None, sort=1):
    """Add one attachment to an existing post, with a video duration when given."""
    item = str(uuid.uuid4())
    def write(db):
        db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort) VALUES (?,?,?,?,?,1,?,?,?)",
                   (item, post, f'{item[:8]}.{ {"video": "mp4", "photo": "jpg"}.get(kind, "pdf") }', kind, 'x' * 64, pages, status, sort))
        if duration is not None:
            db.execute('INSERT INTO video_metadata VALUES (?,?)', (item, duration))
    service.db.submit(write).result()
    return item


def record(service, kind, post, item, email=READER, **detail):
    service.db.submit(lambda db: event(db, kind, email=email, post_id=post, item_id=item, detail=detail)).result()


def test_skipping_to_the_end_of_a_video_is_not_watching_it(client, service, bank):
    reader = sign_in(client)
    post, _ = publish(service, 'SOP Chapter 1: Introduction')
    read_chapter(service, READER, 1)
    video = attachment(service, post, 'video', duration=100)
    # The first second and the last two: the player reached the end, but 97 seconds were never played.
    record(service, 'video_progress', post, video, ranges=[[0, 1], [98, 100]])
    assert client.get('/api/quizzes/1', headers=reader).json()['unfinished'] == [video]
    assert submit(client, reader, 1, answers(1, 5), read=False).json()['error'] == 'chapter_unfinished'
    # Ranges from separate sessions merge; exactly END_TOLERANCE short of the end is enough.
    record(service, 'video_progress', post, video, ranges=[[1, 96]])
    body = client.get('/api/quizzes/1', headers=reader).json()
    assert (body['ready'], body['unfinished'], len(body['questions'])) == (True, [], 5)


def test_photos_count_once_seen_and_restored_page_summaries_count_like_pages(client, service, bank):
    reader = sign_in(client)
    post, handout = publish(service, 'SOP Chapter 1: Introduction')
    photo = attachment(service, post, 'photo', pages=1)
    left = lambda: client.get('/api/quizzes/1', headers=reader).json()['unfinished']
    assert left() == [handout, photo]
    record(service, 'page_served', post, photo, page=0)
    assert left() == [handout]
    # A summary recorded when the view closed (or restored from the summary log) covers both pages.
    record(service, 'item_summary', post, handout, pages=[0, 1])
    assert left() == []
    assert standing(client, reader)[1]['quizzes'][0]['unfinished'] == []


def test_only_the_readers_own_pages_open_their_quiz(client, service, catalog):
    reader = sign_in(client)
    for email in (ADMIN, MANAGER, EXECUTIVE):
        read_chapter(service, email, 1)
    item = catalog['SOP Chapter 1: Introduction'][1]
    assert client.get('/api/quizzes/1', headers=reader).json()['unfinished'] == [item]
    assert submit(client, reader, 1, answers(1, 5), read=False).status_code == 409
    # Pages out of range or of another chapter never count toward this one.
    record(service, 'page_served', catalog['SOP Chapter 1: Introduction'][0], item, page=7)
    read_chapter(service, READER, 2)
    assert client.get('/api/quizzes/1', headers=reader).json()['unfinished'] == [item]


def test_archived_draft_and_failed_items_never_hold_a_quiz_back_but_re_preparing_ones_do(client, service, catalog):
    reader = sign_in(client)
    publish(service, 'SOP Chapter 1: Introduction', status='archived')
    publish(service, 'SOP Chapter 1: Introduction', status='draft')
    post = catalog['SOP Chapter 1: Introduction'][0]
    # Content that would block the quiz if it counted: only the failed filter keeps it out.
    attachment(service, post, 'pdf', status='failed', pages=2)
    read_chapter(service, READER, 1)
    assert client.get('/api/quizzes/1', headers=reader).json()['ready'] is True
    # The reading room computes the same thing from its own annotated posts.
    assert [q['unfinished'] for q in standing(client, reader)[1]['quizzes']] == [[], [catalog['SOP Chapter 2: Service Excellence'][1]]]
    # An evicted cache being prepared again is not a way around finishing the chapter.
    video = attachment(service, post, 'video', status='converting', duration=60, sort=2)
    assert client.get('/api/quizzes/1', headers=reader).json()['unfinished'] == [video]
    assert standing(client, reader)[1]['quizzes'][0]['unfinished'] == [video]


def test_the_reading_room_and_the_quiz_page_agree_on_a_half_watched_video(client, service, catalog):
    reader = sign_in(client)
    post = catalog['SOP Chapter 1: Introduction'][0]
    read_chapter(service, READER, 1)
    video = attachment(service, post, 'video', duration=60)
    record(service, 'video_progress', post, video, ranges=[[0, 30]])
    assert standing(client, reader)[1]['quizzes'][0]['unfinished'] == [video]
    assert client.get('/api/quizzes/1', headers=reader).json()['unfinished'] == [video]
    record(service, 'video_progress', post, video, ranges=[[30, 58]])
    assert standing(client, reader)[1]['quizzes'][0]['unfinished'] == []
    assert client.get('/api/quizzes/1', headers=reader).json()['ready'] is True


def test_opening_an_unfinished_quiz_is_recorded_and_its_standing_lists_what_is_left(client, service, catalog):
    reader = sign_in(client)
    item = catalog['SOP Chapter 1: Introduction'][1]
    body = client.get('/api/quizzes/1', headers=reader).json()
    assert body['standing']['unfinished'] == [item]
    # The unfinished check comes before the version and the answers are looked at.
    for version, chosen in (('0000000000000000', answers(1, 5)), (None, {'c1q1': 'a'})):
        refused = submit(client, reader, 1, chosen, version=version, read=False)
        assert (refused.status_code, refused.json()['error']) == (409, 'chapter_unfinished')
    read_chapter(service, READER, 1)
    assert client.get('/api/quizzes/1', headers=reader).json()['standing']['unfinished'] == []
    opened = [json.loads(e['payload'])['detail'] for e in events(service, 'quiz_opened')]
    assert [(d['chapter'], d['ready']) for d in opened][0] == (1, False) and opened[-1]['ready'] is True
    assert events(service, 'quiz_attempt') == []


def test_a_pass_reports_what_is_left_in_the_next_chapter(client, service, catalog):
    reader = sign_in(client)
    result = submit(client, reader, 1, answers(1, 5)).json()
    assert result['next_chapter']['unlocked'] is True
    assert [(q['chapter'], q['passed'], q['unfinished']) for q in result['quizzes']] == [
        (1, True, []), (2, False, [catalog['SOP Chapter 2: Service Excellence'][1]])]


def test_new_material_in_a_passed_chapter_waits_for_a_retake_but_never_relocks_the_next(client, service, catalog):
    reader = sign_in(client)
    assert submit(client, reader, 1, answers(1, 5)).json()['passed'] is True
    _, added = publish(service, 'SOP Chapter 1: Introduction')
    locks, body = standing(client, reader)
    assert (body['quizzes'][0]['passed'], body['quizzes'][0]['unfinished']) == (True, [added])
    assert locks['SOP Chapter 2: Service Excellence'] is None
    assert view(client, reader, catalog['SOP Chapter 2: Service Excellence'][1]).status_code == 200
    retake = submit(client, reader, 1, answers(1, 0), read=False)
    assert retake.status_code == 409 and retake.json()['error'] == 'chapter_unfinished'
    assert standing(client, reader)[1]['quizzes'][0]['attempts'] == 1


def test_unfinished_items_follow_reading_order_on_the_wall_and_the_quiz_page(client, service, bank):
    reader = sign_in(client)
    first = publish(service, 'SOP Chapter 1: Introduction')
    second = publish(service, 'SOP Chapter 1: Introduction')
    service.db.submit(lambda db: db.execute('UPDATE posts SET published_at=published_at+10 WHERE id=?', (second[0],))).result()
    wall = standing(client, reader)[1]['quizzes'][0]['unfinished']
    page = client.get('/api/quizzes/1', headers=reader).json()['unfinished']
    assert wall == page == [first[1], second[1]]


def test_quiz_routes_replay_reading_history_only_on_a_read_snapshot(client, service, catalog, monkeypatch):
    import postern.service as service_module
    reader = sign_in(client)
    seen = []
    original = service_module.finished_items
    def checked(db, email):
        seen.append((db.execute('PRAGMA query_only').fetchone()[0], db.in_transaction))
        return original(db, email)
    monkeypatch.setattr(service_module, 'finished_items', checked)
    client.get('/api/quizzes/1', headers=reader)
    submit(client, reader, 1, answers(1, 5))
    assert seen and all(entry == (1, True) for entry in seen)
    # Computed for every role on the snapshot; the writer alone decides who is exempt.
    seen.clear()
    client.get('/api/quizzes/1', headers=sign_in(client, MANAGER))
    assert seen == [(1, True)]


def test_material_published_after_the_snapshot_keeps_the_quiz_closed(service, catalog):
    from postern.progress import chapter_items, unfinished
    snapshot = service.db.read(lambda db: {item['id'] for group in chapter_items(db).values() for item in group})
    late = publish(service, 'SOP Chapter 1: Introduction')
    left = service.db.read(lambda db: unfinished(db, snapshot))
    assert left == {1: [late[1]]}


def test_staff_progress_shows_which_chapters_staff_still_have_to_finish(client, service, catalog):
    admin = sign_in(client, ADMIN)
    read_chapter(service, READER, 1)
    people = {person['email']: person for person in client.get('/api/admin/progress', headers=admin).json()['people']}
    staff = {q['chapter']: q['unfinished'] for q in people[READER]['quizzes']}
    assert staff == {1: [], 2: [catalog['SOP Chapter 2: Service Excellence'][1]]}
    assert all(q['unfinished'] == [] for q in people[MANAGER]['quizzes'])


def test_a_long_video_allows_two_percent_at_the_end():
    long = {'kind': 'video', 'duration_seconds': 1000}
    assert quiz.finished(long, 0, 980) is True
    assert quiz.finished(long, 0, 979.5) is False
    short = {'kind': 'video', 'duration_seconds': 60}
    assert quiz.finished(short, 0, 58) is True and quiz.finished(short, 0, 57.9) is False


def pooled(chapter, number):
    """Pool questions whose correct option ranks 1..4 by length in turn, alternating focus."""
    answer = 'abcd'[(number - 1) % 4]
    text = lambda value: {'en': value, 'th': value + ' (th)'}
    return {'id': f'c{chapter}q{number}', 'focus': quiz.FOCUSES[number % 2], 'prompt': text(f'Question {number}?'),
            'options': [{'id': key, **text('Option ' + key * (n + 1))} for n, key in enumerate('abcd')],
            'answer': answer, 'explanation': text(f'Because {answer}.')}


def pool_file(directory, size=12, chapters=(1, 2)):
    path = directory / 'pools.json'
    path.write_text(json.dumps({'chapters': [
        {'chapter': chapter, 'questions': [pooled(chapter, n) for n in range(1, size + 1)]} for chapter in chapters]}))
    return path


@pytest.fixture
def pools(service, tmp_path):
    service.permissions.quizzes = quiz.load(pool_file(tmp_path))
    return {name: publish(service, name) for name in ('SOP Chapter 1: Introduction', 'SOP Chapter 2: Service Excellence')}


def reply(body, correct):
    """Answer the first `correct` dealt questions right and the rest wrong."""
    right = lambda key: 'abcd'[(int(key.split('q')[1]) - 1) % 4]
    return {q['id']: right(q['id']) if n < correct else ('b' if right(q['id']) == 'a' else 'a')
            for n, q in enumerate(body['questions'])}


def deal(client, headers, chapter=1):
    body = client.get(f'/api/quizzes/{chapter}', headers=headers).json()
    return body, [q['id'] for q in body['questions']]


def test_each_attempt_deals_five_of_the_pool_and_keeps_them_until_answered(client, service, pools):
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, hand = deal(client, reader)
    assert (body['total'], body['pool'], body['pass_mark'], len(hand), len(set(hand))) == (5, 12, 4, 5, 5)
    # Standing everywhere counts the questions of an attempt, and says how large the pool is.
    assert (body['standing']['total'], body['standing']['pool']) == (5, 12)
    assert [(q['total'], q['pool']) for q in client.get('/api/posts', headers=reader).json()['quizzes']] == [(5, 12), (5, 12)]
    assert set(hand) <= {f'c1q{n}' for n in range(1, 13)}
    # At least two of each focus, and no option-length rank holds four correct answers.
    pool = {q['id']: q for q in service.permissions.quizzes[1]['questions']}
    assert all(sum(pool[key]['focus'] == focus for key in hand) >= 2 for focus in quiz.FOCUSES)
    assert max(sum(quiz.length_rank(pool[key], 'en') == rank for key in hand) for rank in range(1, 5)) < 4
    # Reloading, or opening it on another device, shows the same questions.
    assert deal(client, reader)[1] == hand and deal(client, sign_in(client))[1] == hand
    assert [json.loads(e['payload'])['detail']['questions'] for e in events(service, 'quiz_opened')] == [hand] * 3
    # Answers to pool questions that were not dealt mean a stale page; unknown ones are incomplete.
    # Both are refused and keep the hand.
    other = next(f'c1q{n}' for n in range(1, 13) if f'c1q{n}' not in hand)
    swapped = {**reply(body, 5), other: 'a'}
    swapped.pop(hand[0])
    attempt = lambda chosen: client.post('/api/quizzes/1/attempts', headers=reader, json={'version': body['version'], 'answers': chosen})
    assert attempt(swapped).json()['error'] == 'quiz_changed'
    unknown = {**reply(body, 5), 'c9q99': 'a'}
    unknown.pop(hand[0])
    assert attempt(unknown).json()['error'] == 'answers_required'
    assert deal(client, reader)[1] == hand
    result = attempt(reply(body, 4)).json()
    assert (result['score'], result['total'], result['passed']) == (4, 5, True)
    assert [r['id'] for r in result['results']] == hand
    # The hand is spent: sending the same answers again records nothing.
    again = attempt(reply(body, 5))
    assert again.status_code == 409 and again.json()['error'] == 'quiz_changed'
    assert len(events(service, 'quiz_attempt')) == 1


def test_retakes_never_repeat_the_previous_questions_and_soon_cover_the_pool(client, service, pools):
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    hands = []
    for _ in range(6):
        body, hand = deal(client, reader)
        hands.append(hand)
        assert client.post('/api/quizzes/1/attempts', headers=reader, json={'version': body['version'], 'answers': reply(body, 1)}).status_code == 200
    assert all(not set(before) & set(after) for before, after in zip(hands, hands[1:]))
    assert len(set(hands[0] + hands[1] + hands[2])) == 12


def test_restored_and_earlier_attempts_steer_the_next_hand(client, service, pools):
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    seen = [f'c1q{n}' for n in (1, 2, 3, 4, 6)]
    def restore(db):
        import_events(db, [{'event_id': 'restored', 'event': 'quiz_attempt', 'ts': 50, 'email': READER,
                            'detail': {'chapter': 1, 'score': 2, 'total': 5, 'passed': False, 'answers': {key: 'a' for key in seen}}}])
    service.db.submit(restore).result()
    assert not set(deal(client, reader)[1]) & set(seen)
    # Attempts made before pools existed are counted once, at startup.
    def earlier(db):
        # As 0.7.x stored and folded it; a fail, so the recount steers someone who has not passed yet.
        identity = event(db, 'quiz_attempt', email=MANAGER, detail={'chapter': 1, 'score': 2, 'passed': False, 'answers': {key: 'a' for key in seen}})
        quiz.record(db, db.execute('SELECT email,ts,seq,payload FROM events WHERE id=?', (identity,)).fetchone())
        db.execute('DELETE FROM quiz_seen')
        set_meta(db, 'quiz_seen_ready', False)
    service.db.submit(earlier).result()
    for _ in range(2):
        attempts = service.db.read(quiz.earlier_attempts)
        service.db.submit(lambda db: quiz.backfill(db, attempts)).result()
    counted = service.db.read(lambda db: db.execute('SELECT email,count(*),sum(times) FROM quiz_seen GROUP BY email ORDER BY email').fetchall())
    assert [tuple(row) for row in counted] == [(MANAGER, 5, 5), (READER, 5, 5)]
    assert not set(deal(client, sign_in(client, MANAGER))[1]) & set(seen)


def test_an_updated_pool_deals_a_new_hand_and_refuses_the_old_one(client, service, pools, tmp_path):
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    before, hand = deal(client, reader)
    data = json.loads(pool_file(tmp_path).read_text())
    data['chapters'][0]['questions'][0]['prompt']['en'] = 'Question 1, reworded?'
    (tmp_path / 'updated.json').write_text(json.dumps(data))
    service.permissions.quizzes = quiz.load(tmp_path / 'updated.json')
    stale = client.post('/api/quizzes/1/attempts', headers=reader, json={'version': before['version'], 'answers': reply(before, 5)})
    assert stale.status_code == 409 and stale.json()['error'] == 'quiz_changed'
    after = deal(client, reader)[0]
    assert after['version'] != before['version']
    assert client.post('/api/quizzes/1/attempts', headers=reader, json={'version': after['version'], 'answers': reply(after, 5)}).json()['passed'] is True


def test_dealing_prefers_unseen_questions_then_the_least_seen(tmp_path):
    import random
    entry = quiz.load(pool_file(tmp_path))[1]
    rng = random.Random(7)
    last = {'c1q1', 'c1q2', 'c1q3', 'c1q4', 'c1q5'}
    seen = {**{key: 1 for key in last}, 'c1q6': 3, 'c1q7': 3}
    for _ in range(50):
        hand = quiz.deal(entry, seen, last, rng)
        assert not set(hand) & last
        # Of the seven questions outside the last hand, the five least seen win.
        assert sum(seen.get(key, 0) for key in hand) == min(
            sum(seen.get(entry['questions'][i]['id'], 0) for i in h) for h in entry['hands']
            if not {entry['questions'][i]['id'] for i in h} & last)


def test_hands_follow_the_focus_and_length_rules_and_relax_only_when_a_pool_cannot(tmp_path):
    entry = quiz.load(pool_file(tmp_path))[1]
    questions = entry['questions']
    assert len(entry['hands']) > 100
    for hand in entry['hands']:
        assert all(sum(questions[i]['focus'] == focus for i in hand) >= 2 for focus in quiz.FOCUSES)
        assert max(sum(quiz.length_rank(questions[i], 'en') == r for i in hand) for r in range(1, 5)) < 4
    assert entry['tier'] == 'full'
    # Value: protects=only one rank holding the pass mark is refused, in each language on its own; fails_when=hands() checks one language or caps two ranks together again; why_new=owner decision P2-D5 A; seam=none
    # The first six answers are the longest option in one language only. A hand with four of them is
    # refused; one with three of them, and two ranks of the other language holding two answers each, is dealt.
    for lang, letter in (('th', 'ก'), ('en', 'x')):
        skewed = [pooled(1, n) for n in range(1, 13)]
        for q in skewed[:6]:
            for option in q['options']:
                option[lang] = letter * (9 if option['id'] == q['answer'] else 3 + 'abcd'.index(option['id']))
        allowed, tier = quiz.hands(skewed, 5, 4)
        assert tier == 'full' and (0, 1, 2, 6, 9) in allowed and (0, 1, 2, 3, 6) not in allowed, lang
        assert all(max(sum(quiz.length_rank(skewed[i], other) == r for i in hand) for r in range(1, 5)) < 4
                   for hand in allowed for other in ('en', 'th')), lang
    # Value: protects=no answer letter carries a hand, since option ids reach the page; fails_when=hands() stops checking letters; why_new=adversarial review (native) of the letter tell; seam=none
    # Seven answers are "b" (the first six relabelled, lengths unchanged): no hand holds four of them.
    lettered = [pooled(1, n) for n in range(1, 13)]
    for q in lettered[:6]:
        for option in q['options']:
            option['id'] = {q['answer']: 'b', 'b': q['answer']}.get(option['id'], option['id'])
        q['answer'] = 'b'
    allowed, tier = quiz.hands(lettered, 5, 4)
    assert tier == 'full' and all(sum(lettered[i]['answer'] == 'b' for i in hand) < 4 for hand in allowed)
    assert any(sum(lettered[i]['answer'] == 'b' for i in hand) == 3 for hand in allowed)
    # Every option the same length and every question the same focus: the draw still works, and says so.
    plain = [question(1, n) for n in range(1, 8)]
    allowed, tier = quiz.hands(plain, 5, 4)
    assert (len(allowed), tier) == (21, 'balanced')


def test_a_page_still_showing_an_answered_hand_is_asked_to_start_again(client, service, pools):
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    phone, first = deal(client, reader)
    # The same person answers on another device, then is dealt the next hand there (a fail: after a
    # pass the next hand asks the same questions again).
    assert client.post('/api/quizzes/1/attempts', headers=reader, json={'version': phone['version'], 'answers': reply(phone, 3)}).status_code == 200
    second = deal(client, sign_in(client))[1]
    assert not set(first) & set(second)
    stale = client.post('/api/quizzes/1/attempts', headers=reader, json={'version': phone['version'], 'answers': reply(phone, 5)})
    assert stale.status_code == 409 and stale.json()['error'] == 'quiz_changed'
    assert len(events(service, 'quiz_attempt')) == 1 and deal(client, reader)[1] == second
    # Leaving a question of the current hand unanswered is still "answer every question".
    partial = {key: 'a' for key in second[:4]}
    assert client.post('/api/quizzes/1/attempts', headers=reader, json={'version': phone['version'], 'answers': partial}).json()['error'] == 'answers_required'


def test_the_api_counts_earlier_attempts_once_when_it_starts(config, store, clock, tmp_path):
    from fastapi.testclient import TestClient
    from postern.app import create_app
    service = Service(config, store=store, clock=clock)
    def earlier(db):
        event(db, 'quiz_attempt', email=READER, detail={'chapter': 1, 'score': 1, 'passed': False, 'answers': {'c1q1': 'a', 'c1q2': 'b'}}, timestamp=10)
        event(db, 'quiz_attempt', email=READER, detail={'chapter': 1, 'score': 1, 'passed': False, 'answers': {'c1q2': 'a', 'c1q3': 'b'}}, timestamp=20)
        db.execute("INSERT INTO events(id,ts,type,email,summary,payload) VALUES ('malformed',30,'quiz_attempt',?,1,'[1]')", (READER,))
    service.db.submit(earlier).result()
    seen = lambda: service.db.read(lambda db: sorted(tuple(row) for row in db.execute('SELECT question,times,last_ts FROM quiz_seen')))
    with TestClient(create_app(config, service=service)):
        assert seen() == [('c1q1', 1, 10), ('c1q2', 2, 20), ('c1q3', 1, 20)]
        # Counted once: later starts leave the table to the attempts themselves.
        assert service.db.read(quiz.earlier_attempts) is None


def test_a_failed_count_at_startup_never_stops_the_api_and_the_next_start_counts(config, store, clock, monkeypatch):
    # Value: protects=the API starts when the one-time quiz_seen count fails, and the next start still counts; fails_when=backfill_quiz_seen loses its except or marks quiz_seen_ready on failure; why_new=only a successful startup count is tested; seam=none
    import sqlite3
    from fastapi.testclient import TestClient
    from postern.app import create_app
    first = Service(config, store=store, clock=clock)
    first.db.submit(lambda db: event(db, 'quiz_attempt', email=READER, timestamp=10,
                                     detail={'chapter': 1, 'score': 1, 'passed': False, 'answers': {'c1q1': 'a', 'c1q2': 'b'}})).result()
    def locked(db):
        raise sqlite3.OperationalError('database is locked')
    with monkeypatch.context() as patch:
        patch.setattr(quiz, 'earlier_attempts', locked)
        with TestClient(create_app(config, service=first)) as client:
            assert client.get('/api/session', headers=sign_in(client)).status_code == 200
            assert first.db.read(lambda db: db.execute('SELECT count(*) FROM quiz_seen').fetchone()[0]) == 0
    second = Service(config, store=store, clock=clock)
    with TestClient(create_app(config, service=second)):
        assert second.db.read(lambda db: sorted(tuple(row) for row in db.execute('SELECT question,times FROM quiz_seen'))) == [('c1q1', 1), ('c1q2', 1)]


def test_seen_counts_and_the_last_hand_do_not_depend_on_restore_order():
    import itertools
    import sqlite3
    from postern.db import SCHEMA
    def row(seq, ts, keys):
        return {'email': READER, 'ts': ts, 'seq': seq,
                'payload': json.dumps({'detail': {'chapter': 1, 'score': 0, 'passed': False, 'answers': {key: 'a' for key in keys}}})}
    attempts = [row(1, 10, ['c1q1', 'c1q2']), row(2, 30, ['c1q2', 'c1q3']), row(3, 20, ['c1q3', 'c1q4']), row(4, 30, ['c1q5'])]
    for order in itertools.permutations(attempts):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        for entry in order:
            quiz.record(db, entry)
        counts = dict(db.execute('SELECT question,times FROM quiz_seen').fetchall())
        assert counts == {'c1q1': 1, 'c1q2': 2, 'c1q3': 2, 'c1q4': 1, 'c1q5': 1}
        # Equal times fall back to sequence: the latest attempt is seq 4.
        latest = max(db.execute('SELECT last_ts,last_seq FROM quiz_seen').fetchall(), key=tuple)
        assert tuple(latest) == (30, 4)
        assert {row[0] for row in db.execute('SELECT question FROM quiz_seen WHERE last_ts=30 AND last_seq=4')} == {'c1q5'}


def test_hands_are_dealt_at_random_among_the_least_seen(tmp_path):
    import random
    entry = quiz.load(pool_file(tmp_path))[1]
    rng = random.Random(3)
    hands = {tuple(sorted(quiz.deal(entry, {}, set(), rng))) for _ in range(200)}
    assert len(hands) > 30
    orders = {tuple(quiz.deal(entry, {}, set(), rng)) for _ in range(50)}
    assert len(orders) == 50


def test_a_submit_that_commits_after_the_deadline_keeps_its_pass(client, service, pools, monkeypatch):
    # Value: protects=a pass saved behind a 503 still counts, a retry is told to start again, and the next hand re-asks that pass's questions with answers; fails_when=take() stops spending the hand, the attempt is lost, or a lost first pass hides its answers for good; why_new=no test drives the commit-then-deadline path for quizzes; seam=none
    import time
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, hand = deal(client, reader)
    original = quiz.review
    def slow(*args, **kwargs):
        time.sleep(service.db.deadline + .1)
        return original(*args, **kwargs)
    monkeypatch.setattr(quiz, 'review', slow)
    send = lambda: client.post('/api/quizzes/1/attempts', headers=reader, json={'version': body['version'], 'answers': reply(body, 5)})
    assert send().status_code == 503
    monkeypatch.setattr(quiz, 'review', original)
    # The writer finishes and commits after the response gave up waiting: the pass is recorded once.
    for _ in range(50):
        if events(service, 'quiz_attempt'):
            break
        time.sleep(.05)
    assert len(events(service, 'quiz_attempt')) == 1
    retry = send()
    assert retry.status_code == 409 and retry.json()['error'] == 'quiz_changed'
    assert standing(client, reader)[1]['quizzes'][0]['passed'] is True
    # The lost reply is not lost answers (owner decision P2-D2): the next hand asks exactly the
    # questions of that pass again, and passing it shows them.
    again, same = deal(client, reader)
    assert sorted(same) == sorted(hand)
    result = client.post('/api/quizzes/1/attempts', headers=reader, json={'version': again['version'], 'answers': reply(again, 4)}).json()
    assert result['passed'] is True and [r['id'] for r in result['results']] == same


def test_a_recount_matches_live_recording_and_forgets_old_hands(service):
    # Value: protects=the startup recount counts what record() counts, skips unreadable rows and clears stale hands; fails_when=earlier_attempts accepts a non-int score, raises on bad JSON, or keeps quiz_draws; why_new=recount was only tested with well-formed rows; seam=none
    def seed(db):
        db.execute("INSERT INTO events(id,ts,type,email,summary,payload) VALUES ('bad-json',10,'quiz_attempt',?,1,'{not json')", (READER,))
        db.execute("INSERT INTO events(id,ts,type,email,summary,payload) VALUES ('text-score',11,'quiz_attempt',?,1,?)",
                   (READER, json.dumps({'detail': {'chapter': 1, 'score': '4', 'passed': False, 'answers': {'c1q1': 'a'}}})))
        event(db, 'quiz_attempt', email=READER, detail={'chapter': 1, 'score': 2, 'passed': False, 'answers': {'c1q2': 'a'}}, timestamp=12)
        db.execute("INSERT INTO quiz_draws VALUES (?,1,'v','[\"c1q3\"]',1)", (READER,))
        set_meta(db, 'quiz_seen_ready', False)
    service.db.submit(seed).result()
    attempts = service.db.read(quiz.earlier_attempts)
    service.db.submit(lambda db: quiz.backfill(db, attempts)).result()
    assert service.db.read(lambda db: [tuple(r) for r in db.execute('SELECT question,times FROM quiz_seen')]) == [('c1q2', 1)]
    assert service.db.read(lambda db: db.execute('SELECT count(*) FROM quiz_draws').fetchone()[0]) == 0


def answer(client, headers, body, chosen, chapter=1):
    return client.post(f'/api/quizzes/{chapter}/attempts', headers=headers, json={'version': body['version'], 'answers': chosen}).json()


def test_retakes_after_a_pass_ask_only_what_the_pass_showed(client, service, pools, monkeypatch):
    # Value: protects=retakes after a pass reveal nothing new, by their score or their answers; fails_when=current_hand deals someone who has passed questions no pass showed, or review() stops showing a passing retake's answers; why_new=owner decision P2-D1 A (2026-10-04) replaces first-pass-only answers; seam=none
    monkeypatch.setattr(quiz, 'PER_MINUTE', 100)
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, hand = deal(client, reader)
    assert answer(client, reader, body, reply(body, 3))['results'] == []
    body, first = deal(client, reader)
    passed = answer(client, reader, body, reply(body, 4))
    assert passed['passed'] is True and [r['id'] for r in passed['results']] == first
    for correct in (5, 0, 4, 3, 5, 1, 4, 2, 5, 3):
        body, hand = deal(client, reader)
        assert sorted(hand) == sorted(first), correct
        result = answer(client, reader, body, reply(body, correct))
        assert (result['score'], result['passed']) == (correct, correct >= 4)
        assert [r['id'] for r in result['results']] == (hand if correct >= 4 else []), correct


def test_unreadable_attempts_never_move_a_first_pass_or_break_the_quiz(client, service, pools):
    # Value: protects=a passer with an unreadable or score-less attempt row can still open the quiz, and such rows never move their first pass; fails_when=exposed() drops its json_valid guard (the quiz route fails for that person) or stops requiring an int score as record() does; why_new=only earlier_attempts() met bad rows; seam=none
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, first = deal(client, reader)
    assert answer(client, reader, body, reply(body, 4))['passed'] is True
    def junk(db):
        db.execute("INSERT INTO events(id,ts,type,email,summary,payload) VALUES ('bad-json',1,'quiz_attempt',?,1,'{not json')", (READER,))
        db.execute("INSERT INTO events(id,ts,type,email,summary,payload) VALUES ('no-score',2,'quiz_attempt',?,1,?)",
                   (READER, json.dumps({'detail': {'chapter': 1, 'passed': True, 'answers': {'c1q12': 'a'}}})))
    service.db.submit(junk).result()
    assert service.db.read(lambda db: quiz.exposed(db, READER, 1)) == set(first)
    response = client.get('/api/quizzes/1', headers=reader)
    assert response.status_code == 200 and sorted(q['id'] for q in response.json()['questions']) == sorted(first)


def test_retakes_after_a_0_7_pass_ask_the_five_questions_it_showed(client, service, pools):
    # Value: protects=people who passed before pools (every pass showed the chapter's original five) are retested on those five only; fails_when=exposed() skips passes taken on an older bank version, or a passed person is dealt from the whole pool; why_new=owner decision P2-D1 A; seam=none
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    original = [f'c1q{n}' for n in range(1, 6)]
    def earlier(db):
        # As 0.7.1 recorded and folded it: every question of the chapter's quiz, on that bank's version.
        identity = event(db, 'quiz_attempt', email=READER, timestamp=100, detail={
            'chapter': 1, 'category': 'SOP Chapter 1: Introduction', 'version': '0123456789abcdef', 'score': 4, 'total': 5,
            'pass_mark': 4, 'passed': True, 'answers': {key: 'a' for key in original}})
        quiz.record(db, db.execute('SELECT email,ts,seq,payload FROM events WHERE id=?', (identity,)).fetchone())
    service.db.submit(earlier).result()
    for correct in (5, 2, 4):
        body, hand = deal(client, reader)
        assert sorted(hand) == original
        assert [r['id'] for r in answer(client, reader, body, reply(body, correct))['results']] == (hand if correct >= 4 else [])


def test_a_bank_edit_that_drops_shown_questions_tops_the_retake_up(client, service, pools, tmp_path):
    # Value: protects=a retake stays a full hand when a bank edit removed questions a pass showed, and adds only as many new ones; fails_when=retake() deals a short hand, drops a remaining shown question, or refills from the whole pool; why_new=owner decision P2-D1 A; seam=none
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, first = deal(client, reader)
    assert answer(client, reader, body, reply(body, 5))['passed'] is True
    data = json.loads(pool_file(tmp_path).read_text())
    data['chapters'][0]['questions'] = [q for q in data['chapters'][0]['questions'] if q['id'] not in first[:2]]
    (tmp_path / 'edited.json').write_text(json.dumps(data))
    service.permissions.quizzes = quiz.load(tmp_path / 'edited.json')
    body, hand = deal(client, reader)
    others = [q['id'] for q in data['chapters'][0]['questions'] if q['id'] not in first]
    assert len(hand) == 5 and set(first[2:]) <= set(hand) and set(hand) - set(first) == set(others[:2])
    assert [r['id'] for r in answer(client, reader, body, reply(body, 4))['results']] == hand
    # That pass showed the two new ones, so the next retake asks the same five.
    assert sorted(deal(client, reader)[1]) == sorted(hand)


def edit_pool(service, tmp_path, name, questions):
    """Load a bank whose Chapter 1 pool is `questions`, as an API restart after a bank edit does."""
    data = json.loads(pool_file(tmp_path).read_text())
    data['chapters'][0]['questions'] = questions
    (tmp_path / name).write_text(json.dumps(data))
    service.permissions.quizzes = quiz.load(tmp_path / name)
    return service.permissions.quizzes[1]['version']


def probe(client, headers, key, guess):
    """A failed retake that reads its score: the hand and how many guesses at questions outside `key` were right.

    Enough known answers wrong that even every guess right stays below the pass mark, so no pass shows the
    new answers and the score counts the right guesses (two right, two wrong for one new question).
    """
    body, hand = deal(client, headers)
    right = body['pass_mark'] - 1 - sum(qid not in key for qid in hand)
    known = [q for q in body['questions'] if q['id'] in key]
    chosen = {q['id']: key[q['id']] if n < right else next(o['id'] for o in q['options'] if o['id'] != key[q['id']])
              for n, q in enumerate(known)}
    chosen.update({q['id']: guess for q in body['questions'] if q['id'] not in key})
    result = answer(client, headers, body, chosen)
    assert (result['passed'], result['results']) == (False, []), hand
    return hand, result['score'] - right


@pytest.mark.parametrize('removed', [1, 2])
def test_reading_failed_scores_after_a_bank_edit_reaches_only_the_fixed_top_ups(client, service, pools, tmp_path, monkeypatch, removed):
    # Value: protects=after a bank edit, failed retakes deal the same top-ups, so reading their scores learns at most as many new answers as the edit removed; fails_when=retake() picks top-ups by seen count or at random, so each failed retake brings another unshown question; why_new=a verifier read 7 of 7 unshown answers off 21 failed retakes; seam=none
    monkeypatch.setattr(quiz, 'PER_MINUTE', 100)
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, first = deal(client, reader)
    key = {r['id']: r['answer'] for r in answer(client, reader, body, reply(body, 5))['results']}
    data = json.loads(pool_file(tmp_path).read_text())
    data['chapters'][0]['questions'] = [q for q in data['chapters'][0]['questions'] if q['id'] not in first[:removed]]
    (tmp_path / 'edited.json').write_text(json.dumps(data))
    service.permissions.quizzes = quiz.load(tmp_path / 'edited.json')
    shown, dealt = set(first[removed:]), set()
    for attempt in range(30):
        dealt |= set(probe(client, reader, key, 'abcd'[attempt % 4])[0]) - shown
    others = [q['id'] for q in data['chapters'][0]['questions'] if q['id'] not in shown]
    assert dealt == set(others[:quiz.DRAW - len(shown)]) and len(dealt) == removed


def test_a_failed_top_up_stays_the_same_when_later_edits_reorder_or_add_questions(client, service, pools, tmp_path, monkeypatch):
    # Value: protects=a top-up a failed retake asked stays that person's, so a bank that reorders the pool or inserts a question ahead of it deals no other unshown question; fails_when=exposed() counts only passes, so each version's pool order picks the top-up afresh; why_new=a verifier learned 2 unshown answers after 1 removal across two bank versions; seam=none
    monkeypatch.setattr(quiz, 'PER_MINUTE', 100)
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, first = deal(client, reader)
    key = {r['id']: r['answer'] for r in answer(client, reader, body, reply(body, 5))['results']}
    v2 = [pooled(1, n) for n in range(1, 13) if f'c1q{n}' != first[0]]
    top_up = next(q['id'] for q in v2 if q['id'] not in key)
    versions = set()
    for name, questions in (('v2.json', v2), ('v3.json', v2[::-1]), ('v4.json', [pooled(1, 13)] + v2)):
        versions.add(edit_pool(service, tmp_path, name, questions))
        for guess in 'abc':
            assert sorted(probe(client, reader, key, guess)[0]) == sorted(first[1:] + [top_up]), name
    assert len(versions) == 3


def test_rotating_the_pool_through_seven_versions_learns_only_the_one_top_up(client, service, pools, tmp_path, monkeypatch):
    # Value: protects=reading failed scores across many bank versions learns only the one answer the removal of one shown question allows; fails_when=a top-up is chosen afresh from each version's pool order; why_new=a verifier read 4 of 7 unshown answers across seven rotations; seam=none
    monkeypatch.setattr(quiz, 'PER_MINUTE', 100)
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, first = deal(client, reader)
    key = {r['id']: r['answer'] for r in answer(client, reader, body, reply(body, 5))['results']}
    v2 = [pooled(1, n) for n in range(1, 13) if f'c1q{n}' != first[0]]
    versions, dealt, learned = set(), set(), {}
    for turn in range(7):
        versions.add(edit_pool(service, tmp_path, f'rotated{turn}.json', v2[turn:] + v2[:turn]))
        for guess in 'abcd':
            hand, right = probe(client, reader, key, guess)
            new = set(hand) - set(key)
            dealt |= new
            learned.update(dict.fromkeys(new, guess) if right else {})
    assert len(versions) == 7 and len(dealt) == 1 and set(learned) == dealt


def test_questions_failed_before_the_first_pass_are_retaken_only_as_pool_order_top_ups(client, service, pools, tmp_path, monkeypatch):
    # Value: protects=attempts before the first pass expose nothing, so after it their questions come back only as pool-order top-ups and a pass never shows their answers otherwise; fails_when=exposed() counts every attempt rather than those from the first pass on, or orders restored attempts by sequence rather than time; why_new=failed retakes after a pass now expose their questions, and that must not reach back before the pass; seam=none
    monkeypatch.setattr(quiz, 'PER_MINUTE', 100)
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, failed = deal(client, reader)
    assert answer(client, reader, body, reply(body, 3))['results'] == []
    body, passed = deal(client, reader)
    assert not set(failed) & set(passed)
    key = {r['id']: r['answer'] for r in answer(client, reader, body, reply(body, 4))['results']}
    for correct in (2, 5, 0):
        body, hand = deal(client, reader)
        assert sorted(hand) == sorted(passed), correct
        answer(client, reader, body, reply(body, correct))
    assert service.db.read(lambda db: quiz.exposed(db, READER, 1)) == set(passed)
    # An edit removes one passed question: the top-up is the first other in pool order, failed before the
    # pass or not, and never the rest of the failed hand, though it was seen least.
    v2 = [pooled(1, n) for n in range(1, 13) if f'c1q{n}' != passed[0]]
    edit_pool(service, tmp_path, 'edited.json', v2)
    top_up = next(q['id'] for q in v2 if q['id'] not in passed)
    for guess in 'ab':
        assert sorted(probe(client, reader, key, guess)[0]) == sorted(passed[1:] + [top_up])
    # Restored attempts arrive in any order at their original times: time decides which follow the first pass.
    restored = [(40, True, 'c2q4'), (30, False, 'c2q3'), (20, True, 'c2q2'), (10, False, 'c2q1'), (50, False, 'c1q1')]
    service.db.submit(lambda db: import_events(db, [{'event_id': f'restored-{ts}', 'event': 'quiz_attempt', 'ts': ts, 'email': MANAGER,
        'detail': {'chapter': int(qid[1]), 'score': 5 if won else 1, 'total': 5, 'passed': won, 'answers': {qid: 'a'}}}
        for ts, won, qid in restored])).result()
    assert service.db.read(lambda db: quiz.exposed(db, MANAGER, 2)) == {'c2q2', 'c2q3', 'c2q4'}


def test_a_pass_recorded_without_its_answers_retakes_one_fixed_hand(client, service, pools, monkeypatch):
    # Value: protects=a pass with no answers counts as showing none: retakes ask the pool's first hand every time, and a hand dealt before the pass cannot be answered; fails_when=kept() keeps any stored hand when no pass recorded answers, or retake() picks the top-ups by seen count; why_new=a verifier found an empty shown set kept the pre-pass hand; seam=none
    monkeypatch.setattr(quiz, 'PER_MINUTE', 100)
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, _ = deal(client, reader)
    # Whatever was dealt, the page left open before the pass shows a hand retakes never deal.
    later = [f'c1q{n}' for n in range(6, 11)]
    service.db.submit(lambda db: db.execute('UPDATE quiz_draws SET questions=? WHERE email=? AND chapter=1', (json.dumps(later), READER))).result()
    service.db.submit(lambda db: import_events(db, [{'event_id': 'restored-pass', 'event': 'quiz_attempt', 'ts': 50, 'email': READER,
        'detail': {'chapter': 1, 'score': 5, 'total': 5, 'passed': True}}])).result()
    stale = client.post('/api/quizzes/1/attempts', headers=reader, json={'version': body['version'], 'answers': {key: 'a' for key in later}})
    assert stale.status_code == 409 and stale.json()['error'] == 'quiz_changed'
    pool_start = [f'c1q{n}' for n in range(1, 6)]
    for correct in (3, 0, 2):
        body, hand = deal(client, reader)
        assert sorted(hand) == pool_start, correct
        assert answer(client, reader, body, reply(body, correct))['results'] == []
    body, hand = deal(client, reader)
    assert sorted(hand) == pool_start and [r['id'] for r in answer(client, reader, body, reply(body, 4))['results']] == hand
    assert sorted(deal(client, reader)[1]) == pool_start


def test_a_kept_hand_holding_questions_no_pass_showed_is_dealt_again(client, service, pools):
    # Value: protects=a hand dealt before a restored pass, or left from before a pass by a rollback to 0.7.x, cannot be answered to see new questions; fails_when=record() keeps hands when it folds a pass, or kept() trusts a hand older than the pass; why_new=owner decision P2-D1 A; seam=none
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, before = deal(client, reader)
    shown = [f'c1q{n}' for n in range(1, 13) if f'c1q{n}' not in before][:5]
    service.db.submit(lambda db: import_events(db, [{'event_id': 'restored-pass', 'event': 'quiz_attempt', 'ts': 50, 'email': READER,
        'detail': {'chapter': 1, 'score': 5, 'total': 5, 'passed': True, 'answers': {key: 'a' for key in shown}}}])).result()
    # A page still showing the earlier hand is asked to start again.
    stale = client.post('/api/quizzes/1/attempts', headers=reader, json={'version': body['version'], 'answers': reply(body, 5)})
    assert stale.status_code == 409 and stale.json()['error'] == 'quiz_changed'
    assert sorted(deal(client, reader)[1]) == sorted(shown)
    # As a rollback to 0.7.x leaves it: a hand dealt before the pass that 0.7.x then recorded.
    keep = lambda chapter, keys: service.db.submit(lambda db: db.execute(
        'UPDATE quiz_draws SET questions=?,drawn_at=1 WHERE email=? AND chapter=?', (json.dumps(keys), READER, chapter))).result()
    keep(1, shown[:4] + [before[0]])
    assert sorted(deal(client, reader)[1]) == sorted(shown)
    # A bank edit left three shown questions in Chapter 2: the hand is those three and the first two
    # others in the pool. A stored hand of exactly those is kept as it is; one holding any other
    # question is dealt again.
    read_chapter(service, READER, 2)
    three, fixed = ['c2q1', 'c2q2', 'c2q3'], [f'c2q{n}' for n in range(1, 6)]
    service.db.submit(lambda db: import_events(db, [{'event_id': 'restored-edited', 'event': 'quiz_attempt', 'ts': 60, 'email': READER,
        'detail': {'chapter': 2, 'score': 4, 'total': 5, 'passed': True, 'answers': {key: 'a' for key in three + ['c2q98', 'c2q99']}}}])).result()
    assert sorted(deal(client, reader, 2)[1]) == fixed
    keep(2, ['c2q5', 'c2q1', 'c2q4', 'c2q3', 'c2q2'])
    assert deal(client, reader, 2)[1] == ['c2q5', 'c2q1', 'c2q4', 'c2q3', 'c2q2']
    for stored in (three + ['c2q11', 'c2q12'], ['c2q1', 'c2q2', 'c2q4', 'c2q5', 'c2q10']):
        keep(2, stored)
        assert sorted(deal(client, reader, 2)[1]) == fixed, stored


def test_reopening_a_passed_quiz_reads_no_history_until_a_new_hand_is_dealt(client, service, pools, monkeypatch):
    # Value: protects=reloading a passed chapter's quiz costs one lookup on the single writer, whatever the person's history; fails_when=current_hand or take() call exposed() for a hand already dealt after the pass; why_new=adversarial review (native and Codex) of the per-reload history scan; seam=none
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, hand = deal(client, reader)
    assert answer(client, reader, body, reply(body, 5))['passed'] is True
    reads = []
    real = quiz.exposed
    monkeypatch.setattr(quiz, 'exposed', lambda *args: reads.append(args) or real(*args))
    body, again = deal(client, reader)
    assert len(reads) == 1 and sorted(again) == sorted(hand)
    for _ in range(3):
        assert deal(client, reader)[1] == again
    assert len(reads) == 1
    assert answer(client, reader, body, reply(body, 5))['passed'] is True
    assert len(reads) == 1
    deal(client, reader)
    assert len(reads) == 2


def test_a_change_to_how_a_chapter_is_dealt_deals_open_hands_again(client, service, pools, monkeypatch, tmp_path):
    # Value: protects=a page never shows fewer questions than the quiz asks for, which would leave Submit disabled for good; fails_when=the version covers only the questions, or kept() keeps a hand of the wrong size; why_new=adversarial review of a DRAW-only change; seam=none
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, hand = deal(client, reader)
    monkeypatch.setattr(quiz, 'DRAW', 4)
    smaller = quiz.load(pool_file(tmp_path))
    assert smaller[1]['version'] != body['version'] and smaller[1]['draw'] == 4
    service.permissions.quizzes = smaller
    fresh = client.get('/api/quizzes/1', headers=reader).json()
    assert (fresh['total'], len(fresh['questions'])) == (4, 4)
    # A stored hand of the wrong size for the current version is dealt again too.
    service.db.submit(lambda db: db.execute('UPDATE quiz_draws SET questions=? WHERE email=? AND chapter=1',
                                            (json.dumps(hand), READER))).result()
    assert len(client.get('/api/quizzes/1', headers=reader).json()['questions']) == 4


def test_answer_elimination_after_a_pass_never_reaches_a_question_the_pass_did_not_show(client, service, pools, monkeypatch):
    # Value: protects=the security review's attack: after a pass, answer the known questions right and cycle each new question's options, reading the score; fails_when=any retake after a pass deals a question outside what a pass showed; why_new=regression test for the P2 security finding behind owner decision P2-D1 A; seam=none
    monkeypatch.setattr(quiz, 'PER_MINUTE', 100)
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    body, hand = deal(client, reader)
    key = {r['id']: r['answer'] for r in answer(client, reader, body, reply(body, 4))['results']}
    shown, left = set(key), {}
    for _ in range(20):
        body, hand = deal(client, reader)
        assert set(hand) <= shown
        guesses = {}
        for q in body['questions']:
            if q['id'] not in key:
                guesses[q['id']] = left.setdefault(q['id'], [option['id'] for option in q['options']])[0]
        result = answer(client, reader, body, {qid: key.get(qid) or guesses[qid] for qid in hand})
        key.update({r['id']: r['answer'] for r in result['results']})
        # Every known answer is right, so with one guess the score says whether it was.
        if len(guesses) == 1:
            (qid, option), = guesses.items()
            if result['score'] == len(hand):
                key[qid] = option
            else:
                left[qid].remove(option)
    assert set(key) == shown and left == {}


def test_a_retake_after_a_pass_prefers_the_least_seen_shown_questions(tmp_path):
    import random
    entry = quiz.load(pool_file(tmp_path))[1]
    rng = random.Random(5)
    shown = {f'c1q{n}' for n in range(1, 9)}
    seen = {**{key: 2 for key in shown}, 'c1q1': 1, 'c1q2': 1, 'c1q3': 1}
    hands = [quiz.retake(entry, shown, seen, rng) for _ in range(50)]
    # The three seen once, then two of the five seen twice, at random; the order is shuffled too.
    assert all(len(hand) == 5 and {'c1q1', 'c1q2', 'c1q3'} <= set(hand) <= shown for hand in hands)
    assert len({tuple(sorted(hand)) for hand in hands}) > 5 and len({tuple(hand) for hand in hands}) > 40
    # Fewer shown than a hand: every one of them, then the first others in pool order however often
    # they were seen, so failed retakes ask the same top-ups again.
    seen = {**{f'c1q{n}': 1 for n in range(3, 13)}, 'c1q9': 0, 'c1q10': 0, 'c1q11': 0}
    assert {tuple(sorted(quiz.retake(entry, {'c1q2', 'c1q7'}, seen, rng))) for _ in range(20)} == {('c1q1', 'c1q2', 'c1q3', 'c1q4', 'c1q7')}
    assert sorted(quiz.retake(entry, set(), seen, rng)) == [f'c1q{n}' for n in range(1, 6)]


def test_a_recount_the_writer_refuses_never_stops_the_api(config, store, clock, monkeypatch, capsys):
    # Value: protects=start() survives a recount the writer refuses (Refusal) and says so in the log; fails_when=Refusal leaves the except tuple or the failure is silent; why_new=only the read-side sqlite3.Error was tested; seam=none
    import sqlite3
    from fastapi.testclient import TestClient
    from postern.app import create_app
    service = Service(config, store=store, clock=clock)
    def broken(db, attempts):
        raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(quiz, 'backfill', broken)
    with TestClient(create_app(config, service=service)) as client:
        assert client.get('/api/session', headers=sign_in(client)).status_code == 200
    assert service.db.read(quiz.earlier_attempts) is not None
    assert 'quiz_seen count deferred' in capsys.readouterr().err


@pytest.mark.parametrize('setting,value', [('PASS_FRACTION', .6), ('RULES', 'new-dealing-rule')])
def test_mark_or_rules_changes_invalidate_open_quizzes(client, service, pools, monkeypatch, tmp_path, setting, value):
    # Value: protects=mark-only and rule-only releases invalidate open quiz pages; fails_when=version hashing omits pass_mark or RULES; why_new=the existing invalidation test only changes DRAW; seam=none
    reader = sign_in(client)
    read_chapter(service, READER, 1)
    before, _ = deal(client, reader)
    monkeypatch.setattr(quiz, setting, value)
    service.permissions.quizzes = quiz.load(pool_file(tmp_path))
    after, hand = deal(client, reader)
    assert after['version'] != before['version']
    stale = client.post('/api/quizzes/1/attempts', headers=reader,
                        json={'version': before['version'], 'answers': reply(before, 5)})
    assert stale.status_code == 409 and stale.json()['error'] == 'quiz_changed'
    assert deal(client, reader)[1] == hand
    result = answer(client, reader, after, reply(after, 5))
    assert result['passed'] is True and result['total'] == 5
