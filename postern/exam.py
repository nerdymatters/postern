"""The full, bilingual final assessment. Quiz key 0 is reserved for the final exam.

It shares the durable quiz attempt/restore machinery, but has no question pool:
every attempt contains all 20 questions. Admins may take it optionally; other
roles must first pass Chapter 9.
The nine chapter standings and their unlock rules remain separate.
"""
import hashlib
import json
from pathlib import Path

from . import quiz
from .auth import Refusal

KEY = 0
PREREQUISITE = 9
BANK = Path(__file__).with_name('sample_training') / 'final_exam.json'


def load(path=BANK):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    questions = data['questions']
    if len(questions) != 20 or len({q['id'] for q in questions}) != 20 or data['pass_mark'] != 16:
        raise ValueError('The final exam must have 20 unique questions and a pass mark of 16')
    for q in questions:
        ids = [option['id'] for option in q['options']]
        if (not isinstance(q['id'], str) or not q['id'] or len(ids) != 4 or len(set(ids)) != 4
                or q['answer'] not in ids or not all(isinstance(key, str) and key for key in ids)
                or not all(isinstance(text.get(lang), str) and text[lang].strip()
                           for text in (q['prompt'], q['explanation'], *q['options']) for lang in ('en', 'th'))):
            raise ValueError('Every final exam question needs bilingual text and a valid answer')
    version = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return {KEY: {**data, 'version': version, 'draw': 20, 'hands': [tuple(range(20))]}}


def available(db, email, role='reader'):
    if PREREQUISITE not in quiz.catalog(db):
        raise Refusal(404, 'quiz_not_found', 'The final exam is not available. / ยังไม่มีแบบทดสอบปลายหลักสูตร')
    if role != 'admin' and PREREQUISITE not in quiz.passes(db, email):
        raise Refusal(423, 'chapter_locked', 'Pass the Chapter 9 quiz to unlock the final exam. / ผ่านแบบทดสอบบทที่ 9 ก่อน จึงจะทำแบบทดสอบปลายหลักสูตรได้')
    return {KEY: 'Final Exam'}


def summary(bank, chapters, attempts, email, role='reader'):
    if PREREQUISITE not in chapters:
        return None
    state = quiz.summary(bank, {KEY: 'Final Exam'}, attempts, email, 'reader')[0]
    state['optional'] = role == 'admin'
    state['locked_by'] = None if state['optional'] or attempts.get((email, PREREQUISITE), {}).get('passed') else PREREQUISITE
    return state
