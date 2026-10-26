"""End-of-chapter quizzes for the SOP chapters; the modules have none.

Staff open a chapter's quiz once every page of it is opened and every video is watched to
the end, and must answer 80% of its questions correctly (four of five) before any later
chapter opens. Admins, C-suite and managers may skip quizzes and can still take them at
any time. Each chapter has a pool of questions; until it is passed, every attempt draws five
of them, avoiding the previous attempt's and preferring the ones this person has seen least,
and after a pass, retakes ask only the questions already exposed to them: those of their passes
and of every attempt since their first pass (where a bank edit removed some, others top the hand
up and stay theirs once asked). The draw is kept on the server until it is answered, so reloading
never deals a new hand. Attempts are audit events, so passes are backed up and restored with the
rest of the history. The answer key never leaves the server, options are shuffled each time, a
failed attempt reports only its score, and a pass shows every answer with its explanation.
"""
from __future__ import annotations
import hashlib
import itertools
import json
import math
import random
import re
import time
import unicodedata
from pathlib import Path

from .auth import Refusal
from .categories import CATEGORY_SPACE, category_name
from .db import meta, set_meta

BANK = Path(__file__).with_name('sample_training') / 'quizzes.json'
PASS_FRACTION = .8
EXEMPT = frozenset({'admin', 'c-suite', 'manager'})
SPACE = '[' + re.escape(CATEGORY_SPACE) + ']*'
# The name must start with the chapter: “SOP Chapter 3: …”, “Chapter-3 …” or “บทที่ ๓ …”.
CHAPTER = re.compile(r'^(?:sop' + SPACE + r'[:-]?' + SPACE + r')?(?:chapter|บทที่)' + SPACE + r'[.:#-]?' + SPACE + r'([0-9๐-๙]+)(?![0-9๐-๙])', re.IGNORECASE)
LIMIT = 20
DRAW = 5
FOCUSES = ('conduct', 'mindset')
FOCUS_MIN = 2
# Part of every quiz version: change it with the rules in hands(), so hands dealt under the old rules are dealt again.
RULES = 'focus-2/one-rank/one-letter'
PER_MINUTE = 10
PER_DAY = 100
LOCKED = 'chapter_locked'
UNFINISHED = 'chapter_unfinished'
# Players stop a fraction of a second early (0.06 s seen in production), and stalls or failed
# reports can drop a few seconds of a long video: allow 2 s or 2% of its length, whichever is more.
END_TOLERANCE = 2
END_FRACTION = .02


def chapter_number(category):
    """“SOP Chapter 3: Grooming” is chapter 3. Modules and “Appendix to Chapter 3” are not chapters."""
    # Text pasted from Docs or LINE can carry zero-width characters and full-width digits.
    name = ''.join(c for c in unicodedata.normalize('NFKC', category_name(category)) if unicodedata.category(c) != 'Cf')
    match = CHAPTER.match(name.strip(CATEGORY_SPACE))
    return int(match[1]) if match and int(match[1]) > 0 else None


def load(path=BANK):
    """Validate a bank once at startup; a broken bank must not reach staff.

    A question whose correct answer or options change gets a new id: exposed() counts what a person was asked
    since passing by id on any bank version, so an edit under the old id would retest people on an answer no
    pass showed. Keep the private quiz_ids.lock.json beside the bank to pin shipped IDs.
    """
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    bank = {}
    for entry in data['chapters']:
        number = entry['chapter']
        if type(number) is not int or number <= 0 or number in bank:
            raise ValueError(f'Quiz chapter {number!r} is missing or repeated')
        questions = entry['questions']
        ids = [question['id'] for question in questions]
        # LIMIT also bounds the API's answers object. For a pool it bounds the hands: hands() lists every
        # hand at startup and deal() scans them all on the writer for each quiz opened (12 questions make
        # 600 hands; 20 make about 10,000), so measure a deal before raising it.
        if not 0 < len(questions) <= LIMIT or len(set(ids)) != len(ids) or not all(isinstance(key, str) and key for key in ids):
            raise ValueError(f'Chapter {number} quiz needs 1 to {LIMIT} questions with unique text ids')
        for question in questions:
            options = [option['id'] for option in question['options']]
            texts = [question['prompt'], question['explanation'], *question['options']]
            if (question.get('focus') not in FOCUSES or len(options) < 2 or len(set(options)) != len(options) or question['answer'] not in options
                    or not all(isinstance(key, str) and key for key in options)
                    or not all(isinstance(text.get(key), str) and text[key].strip() for text in texts for key in ('en', 'th'))):
                raise ValueError(f"Chapter {number} question {question['id']} is incomplete")
        size = min(DRAW, len(questions))
        mark = math.ceil(size * PASS_FRACTION - 1e-9)
        allowed, tier = hands(questions, size, mark)
        # The version covers how the chapter is dealt too: a hand dealt before a change to its size or pass
        # mark is dealt again rather than left on a page that can never be submitted.
        canonical = json.dumps({'chapter': entry, 'draw': size, 'pass_mark': mark, 'rules': RULES}, sort_keys=True, ensure_ascii=False).encode()
        bank[number] = {**entry, 'version': hashlib.sha256(canonical).hexdigest()[:16], 'draw': size,
                        'pass_mark': mark, 'hands': allowed, 'tier': tier}
    return bank


def length_rank(question, lang):
    """1 when the correct option is the shortest in this language, 4 when it is the longest of four."""
    correct = len(next(option for option in question['options'] if option['id'] == question['answer'])[lang].strip())
    return 1 + sum(len(option[lang].strip()) < correct for option in question['options'])


def hands(questions, size, mark):
    """Every set of questions an attempt may draw, worked out once at startup.

    Each focus in the pool appears at least FOCUS_MIN times (when the pool has that many), no one
    answer letter holds `mark` correct answers (option ids reach the page), and in each language no
    single option-length rank does either, so "always pick b", "always pick the longest", the
    shortest or any other one rank, as length_rank counts it, can never pass.
    Two ranks together may: capping them too forced every hand into one rank pattern, and someone
    who knew three answers could then read the other two off it. A bank that cannot meet the length
    rule drops it rather than refusing to draw, and says which tier it kept: 'full' or 'balanced'
    (the preflight refuses anything but 'full' for the real bank). The focus rule can always be
    met: it never asks for more than the hand holds.

    Two limits are recorded follow-ups. Length is counted in code points, which include Thai vowel
    and tone marks that take no width of their own, so picking the Thai option that looks longest
    can still pass some hands (counting only spacing characters, about one in eight to one in four
    of the bundled Chapter 5's, depending on how equal widths are broken, and about 5% at most
    elsewhere). And a rank is 1 + the options strictly shorter, so two distractors of the same
    length (40 of the 108 bundled questions) share one, and a rule that reads such ties, like "the
    second-longest distinct length", can pass some hands (about 8% in Chapter 1, English); the fix
    there is four distinct option lengths in the bank.
    """
    focus = [question['focus'] for question in questions]
    ranks = [(length_rank(question, 'en'), length_rank(question, 'th')) for question in questions]
    answer = [question['answer'] for question in questions]
    needed = {name: min(FOCUS_MIN, focus.count(name)) for name in FOCUSES}

    def balanced(hand):
        return all(sum(focus[i] == name for i in hand) >= least for name, least in needed.items())

    def unguessable(hand):
        return (all(max(sum(ranks[i][lang] == ranks[j][lang] for i in hand) for j in hand) < mark for lang in (0, 1))
                and max(sum(answer[i] == answer[j] for i in hand) for j in hand) < mark)

    every = [combo for combo in itertools.combinations(range(len(questions)), size) if balanced(combo)]
    full = [combo for combo in every if unguessable(combo)]
    return (full, 'full') if full else (every, 'balanced')


def catalog(db):
    """The chapters staff can see; none while a restore is unverified."""
    return {} if meta(db, 'recovery_mode', False) else published(db)


def published(db):
    """Published chapters in order, with the category name staff see for each."""
    chapters = {}
    for (category,) in db.execute("SELECT DISTINCT category FROM posts WHERE status='published' ORDER BY category"):
        number = chapter_number(category)
        if number is not None:
            chapters.setdefault(number, category_name(category))
    return dict(sorted(chapters.items()))


def record(db, row):
    """Fold one stored quiz_attempt event into quiz_standing and quiz_seen, in any order.

    Submission and restore both call this in the transaction that inserts the event, so
    standing never needs a replay of a person's full attempt history.
    """
    payload = json.loads(row['payload'])
    detail = payload.get('detail') if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        return
    chapter, score = detail.get('chapter'), detail.get('score')
    if type(chapter) is not int or type(score) is not int or not row['email']:
        return
    passed = row['ts'] if detail.get('passed') is True else None
    # Restored events keep their original time but receive new sequence numbers.
    db.execute('''INSERT INTO quiz_standing(email,chapter,attempts,best_score,last_score,last_ts,last_seq,passed_at)
                  VALUES (?,?,1,?,?,?,?,?) ON CONFLICT(email,chapter) DO UPDATE SET
                  attempts=attempts+1, best_score=max(best_score,excluded.best_score),
                  last_score=CASE WHEN (excluded.last_ts,excluded.last_seq)>(last_ts,last_seq) THEN excluded.last_score ELSE last_score END,
                  last_seq=CASE WHEN (excluded.last_ts,excluded.last_seq)>(last_ts,last_seq) THEN excluded.last_seq ELSE last_seq END,
                  last_ts=max(last_ts,excluded.last_ts),
                  passed_at=CASE WHEN passed_at IS NULL THEN excluded.passed_at
                                 ELSE min(passed_at,coalesce(excluded.passed_at,passed_at)) END''',
               (row['email'], chapter, score, score, row['ts'], row['seq'], passed))
    if passed is not None:
        # A hand dealt before a pass was not dealt from permitted(); a submission has spent it already, a
        # restore has not. Hands kept after a pass therefore need no history to check (see kept()).
        db.execute('DELETE FROM quiz_draws WHERE email=? AND chapter=?', (row['email'], chapter))
    remember(db, row['email'], chapter, detail.get('answers'), row['ts'], row['seq'])


def remember(db, email, chapter, answers, ts, seq):
    """Count the questions one attempt answered, so later draws prefer the others."""
    if not isinstance(answers, dict) or not 0 < len(answers) <= LIMIT or not all(isinstance(key, str) for key in answers):
        return
    db.executemany('''INSERT INTO quiz_seen(email,chapter,question,times,last_ts,last_seq) VALUES (?,?,?,1,?,?)
                      ON CONFLICT(email,chapter,question) DO UPDATE SET times=times+1,
                      last_seq=CASE WHEN (excluded.last_ts,excluded.last_seq)>(last_ts,last_seq) THEN excluded.last_seq ELSE last_seq END,
                      last_ts=max(last_ts,excluded.last_ts)''',
                   [(email, chapter, key, ts, seq) for key in answers])


def earlier_attempts(db):
    """Every recorded attempt, read on a snapshot: the history scan must not occupy the writer."""
    if meta(db, 'quiz_seen_ready', False):
        return None
    attempts = []
    for row in db.execute("SELECT email,ts,seq,payload FROM events WHERE type='quiz_attempt'"):
        try:
            payload = json.loads(row['payload'])
        except ValueError:
            continue
        detail = payload.get('detail') if isinstance(payload, dict) else None
        # The same attempts record() folds, so a recount matches what live recording built.
        if isinstance(detail, dict) and type(detail.get('chapter')) is int and type(detail.get('score')) is int and row['email']:
            attempts.append((row['email'], detail['chapter'], detail.get('answers'), row['ts'], row['seq']))
    return attempts


def backfill(db, attempts):
    """Once, for attempts made before question pools: count what each person has already answered."""
    if attempts is None or meta(db, 'quiz_seen_ready', False):
        return
    db.execute('DELETE FROM quiz_seen')
    # Hands dealt before the recount (for example before a rollback and roll-forward) ignore it.
    db.execute('DELETE FROM quiz_draws')
    for attempt in attempts:
        remember(db, *attempt)
    set_meta(db, 'quiz_seen_ready', True)


def records(db, email=None):
    """Standing per (email, chapter) for one reader, or for everyone on the current roster."""
    sql = 'SELECT q.* FROM quiz_standing q '
    sql += 'WHERE q.email=?' if email is not None else 'JOIN members m ON m.email=q.email'
    return {(row['email'], row['chapter']): {
        'attempts': row['attempts'], 'best_score': row['best_score'], 'last_score': row['last_score'],
        'passed': row['passed_at'] is not None, 'passed_at': row['passed_at'], 'last_attempt_at': row['last_ts']}
        for row in db.execute(sql, (email,) if email is not None else ())}


def passes(db, email):
    """Chapters this person has passed; one indexed lookup on every content request."""
    return {row[0] for row in db.execute('SELECT chapter FROM quiz_standing WHERE email=? AND passed_at IS NOT NULL', (email,))}


def exposed(db, email, number):
    """The questions of the chapter a retake may ask this person again, by id on any bank version: those of
    every passing attempt and of every attempt, failed ones included, from their first pass on.

    Each attempt after a pass was dealt from permitted(), so its answers or its score were already theirs;
    counting a failed one keeps the top-ups it asked fixed when a later bank edit reorders or adds questions.
    Failed attempts before the first pass showed only a score and count for nothing. Restored events keep
    their original time, so the first pass is the earliest passing attempt by time, whatever the restore order.
    Before 0.8.0 every pass showed its answers and every hand was the chapter's original five
    questions, which keep their ids in the pool, so a 0.7.x pass counts as showing those five.
    """
    attempts = []
    # Only this chapter's attempts reach Python: this runs on the writer for every quiz a passer opens or submits.
    for ts, text in db.execute("SELECT ts,payload FROM events WHERE email=? AND type='quiz_attempt' "
                               "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.detail.chapter') END=?", (email, number)):
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        detail = payload.get('detail') if isinstance(payload, dict) else None
        # The attempts record() folds, as earlier_attempts reads them.
        if isinstance(detail, dict) and type(detail.get('chapter')) is int and detail['chapter'] == number and type(detail.get('score')) is int:
            attempts.append((ts, detail.get('passed') is True, detail.get('answers')))
    first = min((ts for ts, passed, _ in attempts if passed), default=None)
    if first is None:
        return set()
    return {key for ts, _, answers in attempts if ts >= first and isinstance(answers, dict) for key in answers}


def finished(item, pages_opened, watched_seconds):
    """Every page opened, the photo seen, or the video played to within its END_TOLERANCE/END_FRACTION allowance.

    A video without a recorded length cannot report progress, so it never holds a quiz back.
    """
    if item['kind'] == 'video':
        duration = item.get('duration_seconds')
        return not duration or (watched_seconds > 0 and watched_seconds >= duration - max(END_TOLERANCE, duration * END_FRACTION))
    return pages_opened >= (1 if item['kind'] == 'photo' else item['pages'])


def unfinished_in(posts):
    """Items per chapter this reader has not finished (failed ones excluded), in reading order."""
    result = {}
    # The wall lists newest first; the checklist and the reader's next step follow reading order.
    for post in sorted(posts, key=lambda post: (post['published_at'] or post['created_at'], post['id'])):  # as progress.chapter_items
        number = chapter_number(post['category'])
        for item in sorted(post['items'], key=lambda item: (item['sort'], item['id'])) if number is not None else ():
            progress = item.get('progress') or {}
            if item['derive_status'] != 'failed' and not finished(
                    item, len(progress.get('opened_pages', [])), progress.get('watched_seconds', 0)):
                result.setdefault(number, []).append(item['id'])
    return result


def blocking(bank, catalog, passed, role, number):
    """The earliest earlier chapter whose quiz this person still has to pass, if any."""
    if role in EXEMPT or number is None:
        return None
    return next((n for n in catalog if n < number and n in bank and n not in passed), None)


def passed_chapters(attempts, email):
    return {chapter for (who, chapter), state in attempts.items() if who == email and state['passed']}


def refuse_if_locked(db, bank, chapters, role, email, number):
    blocker = blocking(bank, chapters, passes(db, email), role, number)
    if blocker is not None:
        raise Refusal(423, LOCKED, f'Finish Chapter {blocker} and pass its quiz to unlock this chapter. / เรียนบทที่ {blocker} ให้ครบและสอบผ่านก่อน จึงจะเปิดบทนี้ได้')


def require_unlocked(db, bank, role, email, category):
    number = chapter_number(category)
    if role in EXEMPT or number is None:
        return
    chapters = published(db)
    if any(n < number and n in bank for n in chapters):
        refuse_if_locked(db, bank, chapters, role, email, number)


def summary(bank, catalog, attempts, email, role, unfinished=None):
    """Every published chapter that has a quiz, in order, with this person's standing."""
    passed = passed_chapters(attempts, email)
    result = []
    for number, category in catalog.items():
        if number not in bank:
            continue
        state = attempts.get((email, number), {})
        result.append({'chapter': number, 'category': category, 'total': bank[number]['draw'], 'pool': len(bank[number]['questions']),
                       'pass_mark': bank[number]['pass_mark'], 'attempts': state.get('attempts', 0),
                       'best_score': state.get('best_score'), 'last_score': state.get('last_score'),
                       'passed': state.get('passed', False), 'passed_at': state.get('passed_at'),
                       'last_attempt_at': state.get('last_attempt_at'),
                       'locked_by': blocking(bank, catalog, passed, role, number),
                       'unfinished': [] if role in EXEMPT else (unfinished or {}).get(number, [])})
    return result


def available(db, bank, ctx, number):
    """The published catalog, once this person may take the chapter's quiz."""
    chapters = catalog(db)
    if number not in chapters or number not in bank:
        raise Refusal(404, 'quiz_not_found', 'This quiz is not available. / ไม่พบแบบทดสอบนี้')
    refuse_if_locked(db, bank, chapters, ctx['role'], ctx['email'], number)
    return chapters


def deal(quiz, seen, last, rng=random.SystemRandom()):
    """Question ids for a new attempt: as few from the previous attempt as the rules allow, then the least seen."""
    ids = [question['id'] for question in quiz['questions']]
    costs = [(sum(ids[i] in last for i in combo), sum(seen.get(ids[i], 0) for i in combo)) for combo in quiz['hands']]
    best = min(costs)
    chosen = list(rng.choice([combo for combo, cost in zip(quiz['hands'], costs) if cost == best]))
    rng.shuffle(chosen)
    return [ids[i] for i in chosen]


def permitted(quiz, exposed):
    """The questions a retake may ask someone who has passed: their exposed ones still in the pool, in pool
    order, and only when fewer than a hand of those are left (a bank edit removed some, or a pass was restored
    without its answers), the first others in pool order. A top-up never depends on what was seen, and the
    attempt that asks it exposes it, so later edits that reorder or add questions keep it: a new one appears
    only when an edit removes an exposed question from the pool."""
    ids = [question['id'] for question in quiz['questions']]
    held = [key for key in ids if key in exposed]
    return held + [key for key in ids if key not in exposed][:max(0, quiz['draw'] - len(held))]


def retake(quiz, exposed, seen, rng=random.SystemRandom()):
    """Question ids for someone who has passed: the least seen of those permitted, at random between equals."""
    chosen = sorted(permitted(quiz, exposed), key=lambda key: (seen.get(key, 0), rng.random()))[:quiz['draw']]
    rng.shuffle(chosen)
    return chosen


def known(db, bank, email, number):
    """None until this person passes the chapter; then their exposed() questions still in the pool."""
    if number not in passes(db, email):
        return None
    return exposed(db, email, number) & {question['id'] for question in bank[number]['questions']}


def kept(db, bank, email, number):
    """The stored hand while it still applies: the same bank version, a full hand, and after a pass only
    questions permitted() allows.

    Recording a pass drops older hands (record()), so a hand dealt after the pass came from permitted() and
    is reused without reading any history: reopening a passed quiz costs one lookup. Only a hand older than
    the pass, which a rollback to 0.7.x can leave behind, is checked against what the person was shown.
    """
    row = db.execute('SELECT version,questions,drawn_at FROM quiz_draws WHERE email=? AND chapter=?', (email, number)).fetchone()
    if not row or row['version'] != bank[number]['version']:
        return None
    hand = json.loads(row['questions'])
    if len(hand) != bank[number]['draw']:
        return None
    passed = db.execute('SELECT passed_at FROM quiz_standing WHERE email=? AND chapter=? AND passed_at IS NOT NULL', (email, number)).fetchone()
    if passed is None or row['drawn_at'] >= passed[0]:
        return hand
    return hand if set(hand) <= set(permitted(bank[number], known(db, bank, email, number))) else None


def current_hand(db, bank, email, number):
    """This person's current questions for the chapter, dealt once and kept until they are answered.

    After a pass every hand comes from permitted(): the questions exposed to them, and fixed top-ups only
    when fewer than a hand of those are left, so a retake's score or answers teach nothing beyond the top-ups.
    """
    quiz = bank[number]
    hand = kept(db, bank, email, number)
    if hand is not None:
        return hand
    # Only a new hand reads history, and a new hand needs a spent one first (attempts are rate limited).
    exposed = known(db, bank, email, number)
    seen, latest, last = {}, None, set()
    for question, times, ts, seq in db.execute('SELECT question,times,last_ts,last_seq FROM quiz_seen WHERE email=? AND chapter=?', (email, number)):
        seen[question] = times
        if latest is None or (ts, seq) > latest:
            latest, last = (ts, seq), {question}
        elif (ts, seq) == latest:
            last.add(question)
    dealt = deal(quiz, seen, last) if exposed is None else retake(quiz, exposed, seen)
    db.execute('INSERT OR REPLACE INTO quiz_draws(email,chapter,version,questions,drawn_at) VALUES (?,?,?,?,?)',
               (email, number, quiz['version'], json.dumps(dealt), time.time()))
    return dealt


def take(db, bank, email, number, version, answers):
    """The questions an attempt answers; the hand is spent by the attempt that grades it."""
    if version != bank[number]['version']:
        raise Refusal(409, 'quiz_changed', 'This quiz was updated. Please start it again. / แบบทดสอบนี้มีการปรับปรุง กรุณาเริ่มทำใหม่อีกครั้ง')
    dealt = kept(db, bank, email, number)
    pool = {question['id'] for question in bank[number]['questions']}
    # Answers to other pool questions come from a page still showing a hand that another tab or
    # device has already answered: start again, rather than "answer every question" forever.
    if dealt is None or any(key in pool and key not in dealt for key in answers):
        raise Refusal(409, 'quiz_changed', 'These questions were already answered or are no longer open. Please start the quiz again. / คำถามชุดนี้ตอบไปแล้วหรือปิดไปแล้ว กรุณาเริ่มทำแบบทดสอบใหม่')
    db.execute('DELETE FROM quiz_draws WHERE email=? AND chapter=?', (email, number))
    return dealt


def questions(bank, number, dealt):
    """The dealt questions without the answer key or explanations, options in a fresh order."""
    shuffle = random.SystemRandom().shuffle
    pool = {question['id']: question for question in bank[number]['questions']}
    result = []
    for qid in dealt:
        options = [{field: option[field] for field in ('id', 'en', 'th')} for option in pool[qid]['options']]
        shuffle(options)
        result.append({'id': qid, 'prompt': pool[qid]['prompt'], 'options': options})
    return result


def grade(bank, number, dealt, answers):
    pool = {question['id']: question for question in bank[number]['questions']}
    expected = {key: {option['id'] for option in pool[key]['options']} for key in dealt}
    if set(answers) != set(expected) or any(answers[key] not in options for key, options in expected.items()):
        raise Refusal(422, 'answers_required', 'Answer every question before submitting. / กรุณาตอบให้ครบทุกข้อก่อนส่งคำตอบ')
    return [key for key in dealt if answers[key] == pool[key]['answer']]


def review(bank, number, dealt, answers, passed):
    """Nothing but the score after a failed attempt; every answer and its reason after any pass.

    Showing them after every pass is safe: once a person has passed, current_hand deals only from
    permitted(), so passes show answers only for exposed questions plus the top-ups needed because exposed
    questions were removed from the pool. The attempt that asks a top-up exposes it, so reordering or adding
    questions never deals another, and the answers a passer can learn after their first pass, by scores or
    by passes, are at most as many as the exposed questions that bank edits removed. The one addition is a
    pass that showed fewer than a hand: one restored without its answers counts as showing none, so its
    retakes ask the pool's first hand.
    """
    if not passed:
        return []
    pool = {question['id']: question for question in bank[number]['questions']}
    return [{'id': key, 'chosen': answers[key], 'correct': answers[key] == pool[key]['answer'],
             'answer': pool[key]['answer'], 'explanation': pool[key]['explanation']} for key in dealt]
