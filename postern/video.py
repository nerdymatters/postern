"""Private native video playback and durable, per-reader watched intervals.

Media tickets grant only one view for 20 minutes, renewed by authenticated player
heartbeats. Every bounded byte response rechecks the owning session and allowlist.
Originals are never published; the player overlay is deliberately not DRM.
"""
import asyncio
import json
import math
import re
import secrets
import time
from datetime import datetime, timezone

from .auth import Refusal, token_hash
from .db import event

CHUNK = 4 * 1024**2
TTL = 20 * 60


def intervals(ranges, duration):
    valid = sorted((max(0, a), min(duration, b)) for a, b in ranges
                   if math.isfinite(a) and math.isfinite(b) and 0 <= a < b <= duration + .5)
    merged = []
    for start, end in valid:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def subtract(ranges, covered):
    """The parts of merged ranges not already in merged covered ranges."""
    fresh = []
    for start, end in ranges:
        for low, high in covered:
            if high <= start or low >= end:
                continue
            if low > start:
                fresh.append([start, low])
            start = max(start, high)
            if start >= end:
                break
        if start < end:
            fresh.append([start, end])
    return fresh


def byte_range(header, size):
    match = re.fullmatch(r'bytes=(\d{0,20})-(\d{0,20})', header or '')
    if not match or not any(match.groups()):
        raise Refusal(416, 'invalid_range', 'A single byte range is required.')
    first, last = match.groups()
    if not first:
        start, end = max(0, size - int(last)), size - 1
    else:
        start, end = int(first), min(size - 1, int(last)) if last else size - 1
    if start >= size or start > end or start < 0:
        raise Refusal(416, 'invalid_range', 'That byte range is not available.')
    return start, min(end, start + CHUNK - 1)


def require_video(ctx):
    if ctx['item']['kind'] != 'video' or ctx['item']['derive_status'] != 'ready':
        raise Refusal(409, 'video_unavailable', 'This video is not ready to play.')


async def prepare(service, request, view_id):
    ticket = secrets.token_urlsafe(32)
    def issue(db, ctx):
        require_video(ctx)
        db.execute('DELETE FROM video_tickets WHERE expires_at<? OR view_id=?', (time.time(), view_id))
        db.execute('INSERT INTO video_tickets VALUES (?,?,?)', (token_hash(ticket), view_id, time.time() + TTL))
        db.execute('INSERT OR IGNORE INTO video_checkpoints VALUES (?,?)', (view_id, time.time()))
        return {'path': f'/api/video/{ticket}', 'email': ctx['email'],
                'stamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
                **({'subtitles': service.subtitles[ctx['item']['sha256']]} if ctx['item']['sha256'] in service.subtitles else {}),
                **({'selfie_id': ctx['selfie_id']} if ctx.get('selfie_id') else {})}
    return await service.execute(request, issue, view_id=view_id)


async def progress(service, request, view_id, ranges):
    def record(db, ctx):
        require_video(ctx)
        metadata = db.execute('SELECT duration_seconds FROM video_metadata WHERE item_id=?', (ctx['item']['id'],)).fetchone()
        if not metadata:
            raise Refusal(409, 'video_unavailable', 'This video is being prepared.')
        duration = metadata[0]
        if any(not math.isfinite(v) or v < 0 or v > duration + .5 for pair in ranges for v in pair):
            raise Refusal(422, 'invalid_progress', 'Invalid playback interval.')
        if any(b <= a for a, b in ranges):
            raise Refusal(422, 'invalid_progress', 'Invalid playback interval.')
        merged = intervals(ranges, duration)
        now = time.time()
        checkpoint = db.execute('SELECT reported_at FROM video_checkpoints WHERE view_id=?', (view_id,)).fetchone()
        if not checkpoint:
            raise Refusal(409, 'video_unavailable', 'Open the video player first.')
        # Up to 2x playback, with a small allowance for network/scheduling jitter.
        # Client-reported playback is activity evidence, never proof of attention.
        budget = min(65, max(0, now - checkpoint[0]) * 2 + 3)
        # Only seconds this player has not reported before count, so re-sending a report whose
        # response was lost costs nothing and cannot crowd out new playback.
        stored = db.execute('SELECT ranges FROM video_coverage WHERE view_id=?', (view_id,)).fetchone()
        covered = json.loads(stored[0]) if stored else []
        if sum(b-a for a, b in subtract(merged, covered)) > budget:
            raise Refusal(422, 'invalid_progress', 'Playback intervals exceed elapsed viewing time.')
        db.execute('INSERT INTO video_coverage VALUES (?,?) ON CONFLICT(view_id) DO UPDATE SET ranges=excluded.ranges',
                   (view_id, json.dumps(intervals(covered + merged, duration))))
        db.execute('UPDATE video_checkpoints SET reported_at=? WHERE view_id=?', (now, view_id))
        db.execute('UPDATE video_tickets SET expires_at=? WHERE view_id=?', (now + TTL, view_id))
        if merged:
            view = ctx['view']
            if view['first_served_at'] is None:
                db.execute('UPDATE views SET first_served_at=? WHERE id=?', (now, view_id))
                event(db, 'item_open', ctx, post_id=view['post_id'], item_id=view['item_id'], view_id=view_id)
            db.execute('UPDATE views SET last_served_at=? WHERE id=?', (now, view_id))
            event(db, 'video_progress', ctx, post_id=view['post_id'], item_id=view['item_id'], view_id=view_id,
                  detail={'ranges': merged}, summary=True)
        return {'saved': True}
    return await service.execute(request, record, view_id=view_id, event_name=None)


async def content(service, request, ticket):
    if len(ticket) != 43:
        raise Refusal(401, 'sign_in_required', 'Reopen the video to continue.')
    def authorize(db):
        row = db.execute('SELECT t.view_id,s.token_hash FROM video_tickets t JOIN views v ON v.id=t.view_id '
                         'JOIN sessions s ON s.id=v.session_id WHERE t.token_hash=? AND t.expires_at>?',
                         (token_hash(ticket), time.time())).fetchone()
        if not row:
            raise Refusal(401, 'sign_in_required', 'Reopen the video to continue.')
        ctx = service.permissions.authorize_content_request(db, '', token_digest=row['token_hash'],
                view_id=row['view_id'], request_context=service.context(request))
        require_video(ctx)
        event(db, 'video_bytes_requested', ctx, post_id=ctx['post']['id'], item_id=ctx['item']['id'],
              view_id=row['view_id'], summary=False)
        return ctx
    ctx = await service.write(authorize)
    def read(path):
        size = path.stat().st_size
        first, last = byte_range(request.headers.get('range'), size)
        with path.open('rb') as handle:
            handle.seek(first)
            return handle.read(last-first+1), first, last, size
    try:
        result = await asyncio.to_thread(service.deriver.read_path, ctx['item']['id'], 'video.mp4', read)
    except FileNotFoundError:
        service.schedule_derive(ctx['item']['id'])
        raise Refusal(409, 'preparing', 'This video is being prepared again. Please reopen it shortly.')
    await asyncio.to_thread(service.db.read, lambda db: service.still_authorized(db, ctx))
    return result
