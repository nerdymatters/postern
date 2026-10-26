"""The final assessment uses all 20 configured questions and a separate, durable 80% result."""
import json

import pytest
from conftest import ADMIN, READER, sign_in
from postern import exam, quiz
from postern.db import set_meta
from postern.recover import import_events
from test_quiz import publish, pause_worker, allowlist, MANAGER, EXECUTIVE


def restore(service, email=READER, chapter=9, score=4, total=5, passed=True, identity='prerequisite'):
    service.db.submit(lambda db: import_events(db, [{
        'event_id': identity, 'event': 'quiz_attempt', 'ts': 50, 'email': email,
        'detail': {'chapter': chapter, 'score': score, 'total': total, 'passed': passed},
    }])).result()


def chosen(service, score):
    return {q['id']: q['answer'] if i < score else next(o['id'] for o in q['options'] if o['id'] != q['answer'])
            for i, q in enumerate(service.final_exam[0]['questions'])}


@pytest.mark.parametrize('email', [READER, MANAGER, EXECUTIVE])
def test_non_admin_roles_must_pass_chapter_nine_even_for_direct_submission(client, service, email):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client, email)
    assert client.get('/api/posts', headers=headers).json()['final_exam']['locked_by'] == 9
    assert client.get('/api/quizzes/0', headers=headers).status_code == 423
    response = client.post('/api/quizzes/0/attempts', headers=headers,
                           json={'version': service.final_exam[0]['version'], 'answers': chosen(service, 20)})
    assert response.status_code == 423
    restore(service, email)
    opened = client.get('/api/quizzes/0', headers=headers)
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert (body['ready'], body['exempt'], body['total'], body['pool'], body['pass_mark']) == (True, False, 20, 20, 16)
    assert len(body['questions']) == 20
    assert all('answer' not in q and 'explanation' not in q for q in body['questions'])
    assert all(q['prompt']['en'] and q['prompt']['th'] for q in body['questions'])
    assert {q['id'] for q in client.get('/api/quizzes/0', headers=headers).json()['questions']} == set(chosen(service, 20))


def test_admin_exam_is_optional_without_chapter_nine_pass_and_still_graded(client, service):
    post, _ = publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client, ADMIN)
    state = client.get('/api/posts', headers=headers).json()['final_exam']
    assert state['optional'] is True and state['locked_by'] is None and state['passed'] is False
    for score in (15, 16):
        opened = client.get('/api/quizzes/0', headers=headers).json()
        assert opened['exempt'] is True and opened['ready'] is True
        assert opened['standing']['optional'] is True
        result = client.post('/api/quizzes/0/attempts', headers=headers,
                            json={'version': opened['version'], 'answers': chosen(service, score)}).json()
        assert (result['score'], result['pass_mark'], result['passed']) == (score, 16, score >= 16)
        assert result['final_exam']['optional'] is True
        assert not result['quizzes'][0]['passed']  # Optional does not invent a Chapter 9 pass.
    person = next(p for p in client.get('/api/admin/progress', headers=headers).json()['people'] if p['email'] == ADMIN)
    assert person['final_exam']['optional'] is True and person['final_exam']['best_score'] == 16
    assert person['quizzes_passed'] == 0
    # An existing session loses the bypass when its current server-side role changes.
    service.db.submit(lambda db: db.execute("UPDATE members SET role='reader' WHERE email=?", (ADMIN,))).result()
    assert client.get('/api/quizzes/0', headers=headers).status_code == 423
    assert client.post('/api/quizzes/0/attempts', headers=headers,
                       json={'version': opened['version'], 'answers': chosen(service, 20)}).status_code == 423
    state = client.get('/api/posts', headers=headers).json()['final_exam']
    assert state['optional'] is False and state['locked_by'] == 9 and state['passed'] is True
    # Admin access still requires Chapter 9 to exist in the published catalog.
    service.db.submit(lambda db: db.execute("UPDATE members SET role='admin' WHERE email=?", (ADMIN,))).result()
    service.db.submit(lambda db: db.execute("UPDATE posts SET status='archived' WHERE id=?", (post,))).result()
    assert client.get('/api/quizzes/0', headers=headers).status_code == 404


def test_fifteen_fails_sixteen_passes_and_a_failed_retake_keeps_the_pass(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    for score in (15, 16, 0):
        opened = client.get('/api/quizzes/0', headers=headers).json()
        response = client.post('/api/quizzes/0/attempts', headers=headers,
                               json={'version': opened['version'], 'answers': chosen(service, score)})
        assert response.status_code == 200, response.text
        result = response.json()
        assert (result['score'], result['total'], result['pass_mark'], result['passed']) == (score, 20, 16, score >= 16)
        assert len(result['results']) == (20 if score >= 16 else 0)
        assert result['next_chapter'] is None
        assert [q['chapter'] for q in result['quizzes']] == [9]
    state = client.get('/api/posts', headers=headers).json()['final_exam']
    assert (state['passed'], state['best_score'], state['last_score'], state['attempts']) == (True, 16, 0, 3)
    admin = sign_in(client, ADMIN)
    report = client.get('/api/admin/progress', headers=admin)
    assert report.status_code == 200, report.text
    person = next(p for p in report.json()['people'] if p['email'] == READER)
    assert person['final_exam']['passed'] is True
    assert [q['chapter'] for q in person['quizzes']] == [9]


def test_restored_final_result_and_prerequisite_removal(client, service):
    post, _ = publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    restore(service, chapter=0, score=16, total=20, identity='final-pass')
    assert client.get('/api/posts', headers=headers).json()['final_exam']['passed'] is True
    service.db.submit(lambda db: db.execute("UPDATE posts SET status='archived' WHERE id=?", (post,))).result()
    assert client.get('/api/quizzes/0', headers=headers).status_code == 404
    assert client.get('/api/posts', headers=headers).json()['final_exam'] is None
    service.db.submit(lambda db: set_meta(db, 'recovery_mode', True)).result()
    assert client.get('/api/posts', headers=headers).json()['final_exam'] is None


def test_final_attempt_rejects_missing_answers_without_recording_a_score(client, service):
    publish(service, 'SOP Chapter 9: Safety')
    headers = sign_in(client)
    restore(service)
    opened = client.get('/api/quizzes/0', headers=headers).json()
    answers = chosen(service, 20)
    answers.pop(next(iter(answers)))
    response = client.post('/api/quizzes/0/attempts', headers=headers, json={'version': opened['version'], 'answers': answers})
    assert response.status_code == 422
    assert client.get('/api/posts', headers=headers).json()['final_exam']['attempts'] == 0


def test_sample_exam_has_twenty_distinct_ids_and_requires_both_languages(tmp_path):
    entry = exam.load()[0]
    assert {q['id'] for q in entry['questions']} == {f'sample-final-q{n}' for n in range(1, 21)}
    data = json.loads(exam.BANK.read_text())
    data['questions'][0]['prompt']['en'] = ''
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='bilingual'):
        exam.load(path)


def test_zero_is_reserved_for_the_exam_not_a_published_chapter():
    assert quiz.chapter_number('SOP Chapter 0') is None
    assert quiz.chapter_number('SOP Chapter 9') == 9


@pytest.mark.parametrize('change', ['count', 'duplicate', 'mark', 'answer'])
def test_final_bank_refuses_changes_that_make_grading_ambiguous(tmp_path, change):
    data = json.loads(exam.BANK.read_text())
    if change == 'count': data['questions'].pop()
    elif change == 'duplicate': data['questions'][1]['id'] = data['questions'][0]['id']
    elif change == 'mark': data['pass_mark'] = 15
    else: data['questions'][0]['answer'] = 'missing'
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError): exam.load(path)


def test_chapter_bank_cannot_take_the_final_exams_reserved_key(tmp_path):
    path = tmp_path / 'bank.json'
    path.write_text(json.dumps({'chapters': [{'chapter': 0, 'questions': []}]}))
    with pytest.raises(ValueError): quiz.load(path)
