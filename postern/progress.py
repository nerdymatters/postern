"""Durable page/video coverage shared by the reading room and admin reporting.

Only unique pages and merged playback intervals contribute to progress. Requests,
thumbnail loads and buffering do not count. Recovered history keeps its uncertainty.
"""
import json
import math
from .db import meta, rows
from .video import intervals
from .categories import category_name
from .quiz import EXEMPT, chapter_number, finished, published, records, summary
from . import exam, honors


def history(db, items, email=None, coverage=False):
    """Scan once for a reader, or once for the entire current roster.

    coverage=True reads only page and playback evidence for the given items, which is all
    that completion needs, through the (email, type, item_id) index.
    """
    result = {}
    sql = "SELECT e.email,e.item_id,e.type,e.ts,e.payload FROM events e "
    # Recovery can annotate an individual reader before the roster is restored.
    # Only the all-staff dashboard limits its scan to current members.
    if email is None:
        sql += "JOIN members m ON m.email=e.email "
    if coverage:
        sql += "WHERE e.type IN ('page_served','item_summary','video_progress') AND e.item_id IN (%s)" % ','.join('?' * len(items))
    else:
        sql += "WHERE e.type IN ('page_served','item_summary','video_progress','item_open','view_requested')"
    values = list(items) if coverage else []
    if email is not None:
        sql += ' AND e.email=?'
        values.append(email)
    for row in db.execute(sql, values):
        identity = row['item_id']
        if identity not in items:
            continue
        item = items[identity]
        state = result.setdefault((row['email'], identity), {
            'pages': set(), 'ranges': [], 'opened': False, 'attempts': 0, 'last_activity_at': None,
        })
        kind = row['type']
        if kind == 'view_requested':
            state['attempts'] += 1
            continue
        detail = json.loads(row['payload']).get('detail', {})
        recorded = kind == 'item_open'
        if kind == 'item_open':
            state['opened'] = True
        elif kind == 'video_progress' and item['kind'] == 'video':
            pairs = [pair for pair in detail.get('ranges', []) if isinstance(pair, list)
                     and len(pair) == 2 and all(type(v) in (int, float) for v in pair)]
            valid = intervals(pairs, item.get('duration_seconds') or 0)
            state['ranges'].extend(valid)
            recorded = bool(valid)
        elif kind in ('page_served', 'item_summary') and item['kind'] != 'video':
            pages = [detail.get('page')] if kind == 'page_served' else detail.get('pages', [])
            total = 1 if item['kind'] == 'photo' else item['pages']
            valid = {page for page in pages if type(page) is int and 0 <= page < total}
            state['pages'].update(valid)
            # Summaries are emitted on view close, not when pages were opened.
            recorded = bool(valid) and kind == 'page_served'
        if recorded:
            state['last_activity_at'] = max(state['last_activity_at'] or row['ts'], row['ts'])
    for (email, identity), state in result.items():
        item = items[identity]
        state['watched_seconds'] = sum(b-a for a, b in intervals(state.pop('ranges'), item.get('duration_seconds') or 0))
    return result


def chapter_items(db):
    """Items of each published chapter in reading order: a cheap catalog read.

    Publication requires every attachment to be ready, so only a failed re-preparation drops an
    item; one being prepared again after its cache was evicted still has to be finished.
    """
    result = {}
    for item in rows(db, "SELECT i.id,i.kind,i.pages,v.duration_seconds,p.category FROM items i JOIN posts p ON p.id=i.post_id "
                         "LEFT JOIN video_metadata v ON v.item_id=i.id WHERE p.status='published' AND i.derive_status!='failed' "
                         "ORDER BY COALESCE(p.published_at,p.created_at),p.id,i.sort,i.id"):
        number = chapter_number(item['category'])
        if number is not None:
            result.setdefault(number, []).append(item)
    return result


def done(item, state):
    return finished(item, len(state.get('pages', ())), state.get('watched_seconds', 0))


def finished_items(db, email):
    """Chapter items this reader has finished. Replays history: run it on a read snapshot, never in the writer."""
    items = {item['id']: item for group in chapter_items(db).values() for item in group}
    recorded = history(db, items, email, coverage=True) if items else {}
    return {identity for identity, item in items.items() if done(item, recorded.get((email, identity), {}))}


def unfinished(db, completed):
    """Each published chapter's ready items not in completed, in reading order.

    Items published after completed was computed count as unfinished, so a stale snapshot can only
    keep a quiz closed, never open it early.
    """
    result = {}
    for number, group in chapter_items(db).items():
        left = [item['id'] for item in group if item['id'] not in completed]
        if left:
            result[number] = left
    return result


def annotate(db, posts, email):
    items = {item['id']: item for post in posts for item in post['items']}
    recorded = history(db, items, email)
    incomplete = meta(db, 'history_incomplete', False)
    for identity, item in items.items():
        state = recorded.get((email, identity), {})
        item['progress'] = {'opened_pages': sorted(state.get('pages', [])), 'history_incomplete': incomplete}
        if item['kind'] == 'video':
            item['progress']['watched_seconds'] = state.get('watched_seconds', 0)
    return posts


def summarize(items, incomplete):
    total = len(items)
    available = all(item['fraction'] is not None for item in items)
    fraction = sum(item['fraction'] or 0 for item in items) / total if total else 0
    percent = math.floor(fraction * 100) if total and available else None
    complete = sum(item['fraction'] == 1 for item in items)
    started = any(item['opened'] or (item['fraction'] or 0) > 0 for item in items)
    status = ('no_content' if not total else 'unavailable' if not available else
              'complete' if complete == total else 'in_progress' if started else
              'unknown' if incomplete else 'not_started')
    return {'percent': percent, 'status': status, 'completed_items': complete,
            'total_items': total, 'opened_items': sum(item['opened'] or (item['fraction'] or 0) > 0 for item in items),
            'last_activity_at': max((item['last_activity_at'] for item in items if item['last_activity_at'] is not None), default=None)}


def dashboard(db, bank=None, final_exam=None, *, default_group='Staff', certificate_prefix='PT'):
    """Current published material and current members; no hidden-post denominator."""
    bank = bank or {}
    incomplete = meta(db, 'history_incomplete', False)
    chapters = published(db)
    catalog = rows(db, "SELECT i.id,i.post_id,i.filename,i.kind,i.pages,v.duration_seconds,"
                      "p.title,p.category FROM items i JOIN posts p ON p.id=i.post_id "
                      "LEFT JOIN video_metadata v ON v.item_id=i.id WHERE p.status='published' "
                      "ORDER BY COALESCE(p.published_at,p.created_at),p.id,i.sort,i.id")
    items = {item['id']: item for item in catalog}
    recorded = history(db, items)
    attempts = records(db)
    reading = chapter_items(db)
    mine = {}
    for key, state in attempts.items():
        mine.setdefault(key[0], {})[key] = state
    people = []
    membership = meta(db, 'member_groups', {})
    certificates = honors.certificates(db, prefix=certificate_prefix)
    for member in rows(db, 'SELECT email,name,role FROM members ORDER BY name COLLATE NOCASE,email'):
        activity = []
        for item in catalog:
            state = recorded.get((member['email'], item['id']), {})
            pages = len(state.get('pages', []))
            watched = state.get('watched_seconds', 0)
            total = item['duration_seconds'] if item['kind'] == 'video' else 1 if item['kind'] == 'photo' else item['pages']
            amount = watched if item['kind'] == 'video' else pages
            fraction = min(1, amount / total) if total and total > 0 else None
            activity.append({**item, 'category': category_name(item['category']), 'opened_pages': pages,
                             'watched_seconds': watched, 'fraction': fraction, 'opened': state.get('opened', False),
                             'attempts': state.get('attempts', 0), 'last_activity_at': state.get('last_activity_at')})
        left = {}
        for number, group in reading.items():
            ids = [item['id'] for item in group if not done(item, recorded.get((member['email'], item['id']), {}))]
            if ids:
                left[number] = ids
        quizzes = summary(bank, chapters, mine.get(member['email'], {}), member['email'], member['role'], left)
        people.append({**member, 'groups': membership.get(member['email'], [default_group]),
                       **summarize(activity, incomplete), 'items': activity, 'quizzes': quizzes,
                       'quizzes_passed': sum(quiz['passed'] for quiz in quizzes), 'quiz_exempt': member['role'] in EXEMPT,
                       'final_exam': exam.summary(final_exam, chapters, mine.get(member['email'], {}), member['email'], member['role']) if final_exam else None,
                       'certificate': certificates.get(member['email'])})
    return {'people': people, 'total_items': len(catalog), 'history_incomplete': incomplete,
            'groups': meta(db, 'staff_groups', [default_group]), 'roster_warnings': meta(db, 'allowlist_warnings', []),
            'quiz_total': sum(number in bank for number in chapters)}
