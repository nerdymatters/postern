from __future__ import annotations
import asyncio
import collections
import contextlib
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import time
import uuid
from .auth import Permissions, Refusal, canonical_email, parse_allowlist, token_hash
from .cache import CompositeCache
from .db import AuditUnavailable, Database, event, meta, rows, set_meta
from .cache import SessionInvalidated
from .stamp import stamp
from .derive import Deriver, DeriveError
from .storage import FakeStore, GoogleStore
from .progress import annotate, dashboard, finished_items, unfinished
from .categories import category_key, category_name
from . import branding, exam, honors, quiz, selfie, training


class Service:
    def __init__(self, config, store=None, clock=time.monotonic):
        self.config = config.validate()
        bank, final, tracks = training.load(training.resolve(config.data_dir, config.mode))
        config.branding = branding.load(config.data_dir, required=config.mode == 'google')
        self.store = store or (FakeStore(config.data_dir/'fake-cloud') if config.mode == 'demo' else GoogleStore(config))
        self.db = Database(config.data_dir/'postern.sqlite3')
        self.permissions = Permissions(config.notice_version,clock,ip_notice_version=config.ip_notice_version,
                                       default_group=config.branding.default_group)
        self.permissions.selfie_enabled = config.selfie_enabled
        self.permissions.selfie_notice_version = config.selfie_notice_version
        self.permissions.quizzes = bank
        self.final_exam = final
        self.subtitles = tracks
        self.cache = CompositeCache(config.data_dir/'composites', free_floor=config.free_floor)
        self.deriver = Deriver(config)
        self.tasks = []
        self.derive_tasks = {}
        self.refresh_lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self.selfie_lock = asyncio.Lock()
        # One capture lock per account; the keys are allowlisted emails, so this stays small.
        self.capture_locks = collections.defaultdict(asyncio.Lock)
        # Conversions wait for a converter here, in the event loop. Waiting inside a worker thread would hold the
        # thread pool every API read shares, so a crowd enrolling at once could stall reading.
        self.converter_slots = asyncio.Semaphore(config.converter_workers)
        # Private-storage id allocations wait here the same way: a slow Drive must not hold one thread per capture.
        self.cloud_allocations = asyncio.Semaphore(2)
        # Apply backpressure before upload authorization/audit, and again around each capture's own writes,
        # not after fifty writes have already filled the shared durable writer queue.
        self.selfie_admissions = asyncio.Semaphore(8)
        self.selfie_wakeup = asyncio.Event()
        self.signin_verifiers = asyncio.Semaphore(4)
        self.selfie_cleanup_error = None
        self.last_signin_refresh = 0
        self.export_error = None
        self.limits = {}

    async def write(self, operation, timings=None):
        started=time.monotonic();future=self.db.submit(operation)
        try:
            result=await asyncio.wait_for(asyncio.wrap_future(future),1.5)
        except (asyncio.TimeoutError, AuditUnavailable):
            future.cancel()
            raise Refusal(503,'audit_unavailable','Content is paused because its audit record could not be saved.')
        if timings is not None:
            # Reported per response so the day-one load harness can measure the real app, not only the probe.
            timings.update({'audit_ms':(time.monotonic()-started)*1000,
                            'queue_ms':getattr(future,'queue_ms',0.0),'commit_ms':getattr(future,'commit_ms',0.0)})
        return result

    async def start(self):
        await self.write(lambda db: set_meta(db,'recovery_mode',self.config.recovery_mode or meta(db,'recovery_mode',False)))
        await self.backfill_quiz_seen()
        await self.refresh()
        self.tasks = [asyncio.create_task(self.refresh_loop()), asyncio.create_task(self.background_loop()), asyncio.create_task(self.sweep_loop())]
        if self.config.selfie_enabled:
            self.tasks.append(asyncio.create_task(selfie.loop(self)))
        # Recover queued conversions without resuming a partially written derivative directory.
        pending = await asyncio.to_thread(self.db.read, lambda db: rows(db,"SELECT id FROM items WHERE derive_status IN ('queued','converting')"))
        for item in pending:self.schedule_derive(item['id'])

    async def backfill_quiz_seen(self):
        """Count the questions of attempts made before pools. Only the preference for unseen
        questions depends on it, so a failure never stops the API; the next start tries again."""
        try:
            attempts = await asyncio.to_thread(self.db.read, quiz.earlier_attempts)
            if attempts is not None:
                await self.write(lambda db: quiz.backfill(db, attempts))
        except (Refusal, sqlite3.Error, OSError) as error:
            # Visible in the API's launchd log; the next start tries again.
            print(f'postern: quiz_seen count deferred: {error}', file=sys.stderr)

    async def close(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        # Converter subprocesses are bounded; finish them before closing the writer.
        if self.derive_tasks:
            await asyncio.gather(*list(self.derive_tasks.values()),return_exceptions=True)
        await asyncio.to_thread(self.cache.close)
        await asyncio.to_thread(self.db.close)

    async def refresh(self):
        async with self.refresh_lock:
            try:
                values = await asyncio.to_thread(self.store.allowlist)
                warnings = []
                snapshot = parse_allowlist(values,self.config.email_header,warnings=warnings)
                for email in self.config.administrators:
                    if email in snapshot:snapshot[email]['role']='admin'
                groups = list(values) if isinstance(values, dict) else [self.config.branding.default_group]
                revoked = await self.write(lambda db:self.permissions.apply(db,snapshot,groups=groups,warnings=warnings))
                for identity in revoked:await asyncio.to_thread(self.cache.invalidate_session,identity)
                return True
            except Exception as error:
                message = str(error) if isinstance(error,ValueError) else 'Cannot refresh the allowlist from Google. Check its sharing and credentials.'
                def failure(db):
                    set_meta(db,'allowlist_error',message)
                    if not self.permissions.fresh(db) and not meta(db,'allowlist_paused',True):
                        event(db,'allowlist_unavailable')
                        set_meta(db,'allowlist_paused',True)
                with contextlib.suppress(Exception):await self.write(failure)
                return False

    async def refresh_loop(self):
        while True:
            await asyncio.sleep(30)
            await self.refresh()

    async def sweep_loop(self):
        while True:
            await asyncio.sleep(1)
            await asyncio.to_thread(self.cache.sweep)

    def context(self, request):
        ip = request.client.host if request.client else 'unknown'
        if self.config.trust_loopback_proxy and ip in ('127.0.0.1','::1'):
            try:ip = str(ipaddress.ip_address(request.headers.get('x-forwarded-for','')))
            except ValueError:raise Refusal(400,'invalid_proxy','A trusted client address is required.')
        return {'ip':ip,'user_agent':request.headers.get('user-agent','')[:500]}

    def bearer(self,request):
        value=request.headers.get('authorization','')
        return value[7:] if value.startswith('Bearer ') and len(value)<1024 else ''

    async def execute(self,request,operation=lambda db,ctx:ctx,*,event_name='api_access',timings=None,**policy):
        token=self.bearer(request);request_context=self.context(request)
        def transaction(db):
            context=self.permissions.authorize_content_request(db,token,request_context=request_context,**policy)
            result=operation(db,context)
            if event_name:event(db,event_name,context,post_id=context.get('post',{}).get('id'),item_id=context.get('item',{}).get('id'),view_id=context.get('view',{}).get('id'),summary=False)
            return result
        return await self.write(transaction,timings)

    def rate_limit(self,key,*,limit=300):
        now=time.monotonic()
        # Bound attacker-controlled bucket cardinality and clear expired entries.
        self.limits={key:value for key,value in self.limits.items() if now-value[0]<60}
        since,count=self.limits.get(key,(now,0))
        if count>=limit or (key not in self.limits and len(self.limits)>=4096):
            raise Refusal(429,'rate_limited','Too many sign-in attempts. Please wait a minute.')
        self.limits[key]=(since,count+1)

    async def signin(self,identity,request,client='web'):
        email=canonical_email(identity['email'])
        # A restaurant may share one public IP; keep the tighter account budget
        # after identity verification so changing networks does not reset it.
        self.rate_limit('account:'+email,limit=10)
        device = selfie.device_hash(request.headers.get('x-postern-device')) if self.config.selfie_enabled else None
        if time.monotonic()-self.last_signin_refresh>=15:
            exists=await asyncio.to_thread(self.db.read,lambda db:db.execute('SELECT 1 FROM members WHERE email=?',(email,)).fetchone() is not None)
            if not exists:
                self.last_signin_refresh=time.monotonic();await self.refresh()
        token=secrets.token_urlsafe(32);public=str(uuid.uuid4());now=time.time()
        client=client if client in ('web','desktop') else 'web'
        context={'id':public,'email':email,'name':identity.get('name','')[:200],'client':client,**self.context(request)}
        def create(db):
            self.permissions.require_fresh(db)
            member=db.execute('SELECT * FROM members WHERE email=?',(email,)).fetchone()
            if not member:
                event(db,'signin_denied',context)
                return None
            db.execute('INSERT INTO sessions(id,token_hash,subject,email,name,created_at,expires_at,client) VALUES (?,?,?,?,?,?,?,?)',
                (public,token_hash(token),identity['sub'],email,context['name'],now,now+30*86400,client))
            if device:
                db.execute('INSERT INTO session_devices VALUES (?,?)', (public, device))
            event(db,'signin_ok',context)
            return {**context,'role':member['role'],'consent_required':True,'token':token}
        result=await self.write(create)
        if result is None:raise Refusal(403,'access_denied','Your Google account is not on the staff list.')
        return result

    async def consent(self,request,version,ip_version,monitoring_accepted,ip_accepted):
        if version!=self.config.notice_version or ip_version!=self.config.ip_notice_version:
            raise Refusal(409,'notice_changed','Please refresh this page and review the latest agreements.')
        def accept(db,ctx):
            if not monitoring_accepted or not ip_accepted:
                raise Refusal(422,'agreements_required','Please accept both the activity notice and the intellectual property agreement.')
            db.execute('UPDATE sessions SET consent_version=? WHERE id=?',(version,ctx['id']))
            db.execute('INSERT INTO session_ip_agreements VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET version=excluded.version,accepted_at=excluded.accepted_at', (ctx['id'],ip_version,time.time()))
            event(db,'consent_accepted',ctx,detail={'version':version,'monitoring_accepted':True,'ip_version':ip_version,'ip_accepted':True})
            return {'accepted':True}
        return await self.execute(request,accept,consent=False,selfie=False)

    async def logout(self,request):
        def revoke(db,ctx):
            db.execute('UPDATE sessions SET revoked=1 WHERE id=?',(ctx['id'],));event(db,'signout',ctx);return ctx['id']
        identity=await self.execute(request,revoke,consent=False,selfie=False)
        await asyncio.to_thread(self.cache.invalidate_session,identity)

    def post_document(self,db,post,pending=()):
        return {**dict(post),'title_sync_pending':'title' in pending,'category_sync_pending':'category' in pending,'items':rows(db,'SELECT i.id,i.post_id,i.filename,i.kind,i.pages,i.derive_status,i.derive_error,i.sort,v.duration_seconds FROM items i LEFT JOIN video_metadata v ON v.item_id=i.id WHERE i.post_id=? ORDER BY i.sort',(post['id'],))}

    async def posts(self,request,admin=False):
        def query(db,ctx):
            records=rows(db,'SELECT * FROM posts'+('' if admin else " WHERE status='published'")+' ORDER BY COALESCE(published_at,created_at) DESC')
            if meta(db,'recovery_mode',False):records=[]
            pending = {}
            if admin:
                for op in db.execute("SELECT post_id,kind FROM operations WHERE status='pending' AND kind IN ('title','category')"):
                    pending.setdefault(op['post_id'], set()).add(op['kind'])
            posts=[self.post_document(db,r,pending.get(r['id'],())) for r in records]
            return {'posts':posts if admin else annotate(db,posts,ctx['email'])}
        if admin:
            return await self.execute(request,query,admin=True)
        context = await self.execute(request,event_name=None)
        def snapshot(db):
            db.execute('BEGIN')
            result = query(db,context)
            # Read with the history, off the writer. A stale read can lag a pass or a saved name; the web app
            # reads the reading room again after saving a name (app.tsx).
            result['certificate'] = honors.certificate(db,context['email'], prefix=self.config.branding.certificate_prefix)
            return result
        try:
            result = await asyncio.to_thread(self.db.read,snapshot)
        except (sqlite3.Error, OSError):
            raise Refusal(503, 'catalog_unavailable', 'Your training could not be loaded. Please try again shortly. / โหลดเนื้อหาการเรียนไม่สำเร็จ กรุณาลองใหม่อีกครั้งในอีกสักครู่')
        def disclose(db,ctx):
            # History replay can be slow. Recheck session/enrollment and omit
            # content archived during the read before disclosing the snapshot.
            published={r[0] for r in db.execute("SELECT id FROM posts WHERE status='published'")}
            result['posts']=[] if meta(db,'recovery_mode',False) else [p for p in result['posts'] if p['id'] in published]
            if meta(db,'recovery_mode',False):result['certificate']=None
            # Locks use the role and passes as of this authorized check, not the slower snapshot.
            bank=self.permissions.quizzes
            catalog=quiz.catalog(db);attempts=quiz.records(db,ctx['email'])
            result['quizzes']=quiz.summary(bank,catalog,attempts,ctx['email'],ctx['role'],quiz.unfinished_in(result['posts']))
            passed=quiz.passed_chapters(attempts,ctx['email'])
            for post in result['posts']:
                post['chapter']=quiz.chapter_number(post['category'])
                post['locked_by']=quiz.blocking(bank,catalog,passed,ctx['role'],post['chapter'])
            result['quiz_exempt']=ctx['role'] in quiz.EXEMPT
            result['final_exam']=exam.summary(self.final_exam,catalog,attempts,ctx['email'],ctx['role'])
            return result
        return await self.execute(request,disclose)

    async def rename_category(self, request, category, name):
        name = name.strip()
        if not name or len(category_name(name)) > 200:
            raise Refusal(422, 'invalid_category', 'Enter a category name between 1 and 200 characters.')
        name = category_name(name)
        source, target = category_key(category), category_key(name)
        # Serialize with remote catalog commits and archive; each local rename
        # and its durable backup operations are committed in one transaction.
        async with self.operation_lock:
            def rename(db, ctx):
                if meta(db, 'recovery_mode', False):
                    raise Refusal(403, 'access_denied', 'Content cannot be renamed while history is being restored.')
                posts = rows(db, 'SELECT * FROM posts')
                affected = [post for post in posts if category_key(post['category']) == source]
                if not affected:
                    raise Refusal(409, 'category_changed', 'This category changed. Refresh the list and try again.')
                if source != target and any(category_key(post['category']) == target for post in posts):
                    raise Refusal(409, 'category_exists', 'That category already exists. Choose a different name.')
                for post in affected:
                    if post['status'] == 'publishing' or db.execute(
                        "SELECT 1 FROM operations WHERE post_id=? AND status='pending'", (post['id'],)).fetchone():
                        raise Refusal(409, 'category_busy', 'This category has changes still being saved. Please try again shortly.')
                changed = [post for post in affected if post['category'] != name]
                for post in changed:
                    revision = post['revision'] + 1
                    db.execute('UPDATE posts SET category=?,revision=? WHERE id=?', (name, revision, post['id']))
                    # Drafts are local until published. Existing catalog entries
                    # need a metadata-only write, preserving visibility and IDs.
                    if post['status'] in ('published', 'archived'):
                        identity = str(uuid.uuid4())
                        db.execute('UPDATE posts SET publish_op_id=?,error=NULL WHERE id=?', (identity, post['id']))
                        db.execute("INSERT INTO operations(id,post_id,revision,kind,actor,status,created_at) VALUES (?,?,?,'category',?,'pending',?)",
                                   (identity, post['id'], revision, ctx['email'], time.time()))
                if changed:
                    event(db, 'admin_rename_category', ctx, detail={'from': category_name(category), 'to': name,
                          'post_ids': [post['id'] for post in changed]})
                return {'name': name, 'updated': len(changed)}
            return await self.execute(request, rename, admin=True, event_name=None)

    async def rename_post(self, request, post_id, title):
        title = title.strip()
        if not title or len(title) > 120:
            raise Refusal(422, 'invalid_title', 'Enter a title between 1 and 120 characters.')
        async with self.operation_lock:
            def rename(db, ctx):
                post = ctx['post']
                if post['status'] == 'publishing' or db.execute(
                    "SELECT 1 FROM operations WHERE post_id=? AND status='pending'", (post_id,)).fetchone():
                    raise Refusal(409, 'post_busy', 'This content has changes still being saved. Please try again shortly.')
                if post['title'] != title:
                    revision = post['revision'] + 1
                    db.execute('UPDATE posts SET title=?,revision=? WHERE id=?', (title, revision, post_id))
                    if post['status'] in ('published', 'archived'):
                        identity = str(uuid.uuid4())
                        db.execute('UPDATE posts SET publish_op_id=?,error=NULL WHERE id=?', (identity, post_id))
                        db.execute("INSERT INTO operations(id,post_id,revision,kind,actor,status,created_at) VALUES (?,?,?,'title',?,'pending',?)",
                                   (identity, post_id, revision, ctx['email'], time.time()))
                    event(db, 'admin_rename_post', ctx, post_id=post_id,
                          detail={'from': post['title'], 'to': title})
                pending = [op['kind'] for op in db.execute("SELECT kind FROM operations WHERE post_id=? AND status='pending'", (post_id,))]
                return self.post_document(db, db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone(), pending)
            return await self.execute(request, rename, admin=True, post_id=post_id, event_name=None)

    async def create_post(self,request,title,body,category):
        title,body,category=title.strip(),body.strip(),category.strip()
        if not title or len(title)>120 or len(body)>280 or len(category_name(category))>200:
            raise Refusal(422,'invalid_post','Use a title up to 120 characters, a caption up to 280 characters and a category up to 200 characters.')
        def create(db,ctx):
            identity=str(uuid.uuid4())
            db.execute("INSERT INTO posts(id,title,body,category,status,created_at,created_by) VALUES (?,?,?,?,'draft',?,?)",(identity,title,body,category,time.time(),ctx['email']))
            return self.post_document(db,db.execute('SELECT * FROM posts WHERE id=?',(identity,)).fetchone())
        return await self.execute(request,create,admin=True)

    async def attach(self,request,post_id,upload,video_only=False):
        await self.execute(request,admin=True,post_id=post_id)
        filename=Path(upload.filename or '').name[:200]
        ext=Path(filename).suffix.lower()
        kind='pdf' if ext=='.pdf' else 'video' if ext=='.mp4' else 'photo' if ext in ('.jpg','.jpeg','.png','.heic','.heif') else None
        if video_only and kind != 'video':
            await upload.close()
            raise Refusal(415,'unsupported_file','Choose an H.264/AAC MP4 video.')
        if kind is None:raise Refusal(415,'unsupported_file','Choose a PDF, JPEG, PNG, HEIC or H.264/AAC MP4 file. Export office documents to PDF first.')
        limit = self.config.video_upload_limit if kind == 'video' else self.config.upload_limit
        if shutil.disk_usage(self.config.data_dir).free<self.config.free_floor+limit:
            raise Refusal(507,'storage_low','Uploads are paused because the Mac is low on free space.')
        identity=str(uuid.uuid4());path=self.config.data_dir/'staging'/(identity+ext);size=0;digest=hashlib.sha256()
        try:
            with path.open('xb') as output:
                path.chmod(0o600)
                while True:
                    chunk=await upload.read(1024*1024)
                    if not chunk:break
                    size+=len(chunk)
                    if size>limit:raise Refusal(413,'file_too_large',f'This attachment exceeds its {limit // 1024**2} MB limit.')
                    digest.update(chunk)
                    await asyncio.to_thread(output.write,chunk)
            if size==0:raise Refusal(422,'empty_file','The attachment is empty.')
            def insert(db,ctx):
                if ctx['post']['status']!='draft':raise Refusal(409,'post_locked','This post is already queued or published.')
                count=db.execute('SELECT count(*) FROM items WHERE post_id=?',(post_id,)).fetchone()[0]
                if count>=10:raise Refusal(422,'too_many_files','A post can contain up to ten attachments.')
                db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,derive_status,sort) VALUES (?,?,?,?,?,?,'queued',?)",(identity,post_id,filename,kind,digest.hexdigest(),size,count))
                event(db,'admin_upload',ctx,post_id=post_id,item_id=identity,detail={'filename':filename,'bytes':size})
                return {'id':identity,'derive_status':'queued'}
            result=await self.execute(request,insert,admin=True,post_id=post_id)
        except BaseException:
            path.unlink(missing_ok=True);raise
        finally:await upload.close()
        self.schedule_derive(identity)
        return result

    async def convert(self, source, destination, kind):
        # The one converter bound: Deriver.run itself runs whatever is handed to it.
        async with self.converter_slots:
            return await asyncio.to_thread(self.deriver.run, source, destination, kind)

    def schedule_derive(self,identity):
        if identity in self.derive_tasks:return
        task=asyncio.create_task(self.derive_item(identity))
        self.derive_tasks[identity]=task
        task.add_done_callback(lambda _:self.derive_tasks.pop(identity,None))

    async def derive_item(self,identity):
        item=await asyncio.to_thread(self.db.read,lambda db:dict(db.execute('SELECT * FROM items WHERE id=?',(identity,)).fetchone()))
        path=self.config.data_dir/'staging'/(identity+Path(item['filename']).suffix.lower())
        try:
            await self.write(lambda db:db.execute("UPDATE items SET derive_status='converting',derive_error=NULL WHERE id=?",(identity,)))
            if not path.exists():
                if not item['drive_file_id']:raise DeriveError('The staged original is missing. Upload it again.')
                await asyncio.to_thread(self.store.download,item['drive_file_id'],path,item['sha256'])
            manifest=await self.convert(path,self.config.data_dir/'derived'/identity,item['kind'])
            if item['kind'] == 'video':
                await self.write(lambda db: db.execute('INSERT OR REPLACE INTO video_metadata VALUES (?,?)', (identity, manifest['duration_seconds'])))
            await self.write(lambda db:db.execute("UPDATE items SET derive_status='ready',pages=?,revision=revision+1,derive_error=NULL WHERE id=?",(manifest['pages'],identity)))
        except Exception as error:
            message=str(error) if isinstance(error,DeriveError) else 'The attachment could not be prepared. Please retry.'
            with contextlib.suppress(Exception):await self.write(lambda db:db.execute("UPDATE items SET derive_status='failed',derive_error=? WHERE id=?",(message,identity)))

    async def publish(self,request,post_id):
        def enqueue(db,ctx):
            post=ctx['post']
            if post['status'] in ('publishing','published'):
                return {'operation_id':post['publish_op_id'],'status':post['status']}
            if post['status']!='draft':raise Refusal(409,'post_locked','An archived post cannot be republished.')
            if not db.execute('SELECT 1 FROM items WHERE post_id=?',(post_id,)).fetchone():raise Refusal(422,'attachment_required','Add at least one attachment before publishing.')
            identity=str(uuid.uuid4());now=time.time()
            db.execute("INSERT INTO operations(id,post_id,revision,kind,actor,status,created_at) VALUES (?,?,?,'publish',?,'pending',?)",(identity,post_id,post['revision'],ctx['email'],now))
            db.execute("UPDATE posts SET status='publishing',publish_op_id=?,published_at=? WHERE id=?",(identity,now,post_id))
            return {'operation_id':identity,'status':'publishing'}
        return await self.execute(request,enqueue,admin=True,post_id=post_id)

    async def archive(self,request,post_id):
        # Share the operation lock with remote commits: an old publish never lands after this archive.
        async with self.operation_lock:
            def enqueue(db,ctx):
                post=ctx['post'];identity=str(uuid.uuid4());revision=post['revision']+1
                db.execute("UPDATE operations SET status='cancelled' WHERE post_id=? AND status='pending'",(post_id,))
                db.execute("UPDATE posts SET status='archived',revision=?,publish_op_id=?,error=NULL WHERE id=?",(revision,identity,post_id))
                db.execute("INSERT INTO operations(id,post_id,revision,kind,actor,status,created_at) VALUES (?,?,?,'archive',?,'pending',?)",(identity,post_id,revision,ctx['email'],time.time()))
                event(db,'admin_archive',ctx,post_id=post_id)
                return {'status':'archived'}
            return await self.execute(request,enqueue,admin=True,post_id=post_id)

    async def process_operation(self):
        async with self.operation_lock:
            def select(db):
                self.permissions.require_fresh(db)
                op=db.execute("SELECT * FROM operations WHERE status='pending' AND next_try<=? ORDER BY created_at LIMIT 1",(time.time(),)).fetchone()
                if not op:return None
                op=dict(op);post=dict(db.execute('SELECT * FROM posts WHERE id=?',(op['post_id'],)).fetchone())
                member=db.execute('SELECT role FROM members WHERE email=?',(op['actor'],)).fetchone()
                # Title/category changes already took effect in an authorized transaction;
                # their backup must finish even if that administrator later leaves.
                actor_invalid = op['kind'] not in ('category', 'title') and (not member or member['role'] != 'admin')
                if actor_invalid or post['revision']!=op['revision'] or post['publish_op_id']!=op['id']:
                    db.execute("UPDATE operations SET status='cancelled',error='Permission or revision changed' WHERE id=?",(op['id'],))
                    if op['kind']=='publish' and post['publish_op_id']==op['id'] and post['status']=='publishing':
                        db.execute("UPDATE posts SET status='draft',publish_op_id=NULL,published_at=NULL,error=? WHERE id=?",
                                   ('The account that requested this publication no longer has permission. Publish it again.',post['id']))
                    return None
                items=rows(db,'SELECT * FROM items WHERE post_id=? ORDER BY sort',(post['id'],))
                return op,post,items
            work=await self.write(select)
            if not work:return
            op,post,items=work
            try:
                if op['kind']=='publish':
                    if any(item['derive_status']=='failed' for item in items):raise DeriveError('An attachment failed to prepare. Retry it before publishing.')
                    if any(item['derive_status']!='ready' for item in items):return
                    for item in items:
                        if not item['drive_file_id']:
                            file_id=await asyncio.to_thread(self.store.new_id)
                            await self.write(lambda db,i=item,f=file_id:db.execute('UPDATE items SET drive_file_id=? WHERE id=?',(f,i['id'])))
                            item['drive_file_id']=file_id
                        path=self.config.data_dir/'staging'/(item['id']+Path(item['filename']).suffix.lower())
                        if path.exists():await asyncio.to_thread(self.store.put_file,item['drive_file_id'],path,item['filename'],item['sha256'],{'kind':'original','item_id':item['id']})
                        # No staged copy left: the original must already be acknowledged remotely, not merely allocated an id.
                        else:await asyncio.to_thread(self.store.verify,item['drive_file_id'],item['sha256'])
                    post['status']='published'
                # Recheck immediately before the remote commit after possibly lengthy uploads.
                fresh=await self.write(select)
                if not fresh or fresh[0]['id']!=op['id']:return
                await asyncio.to_thread(self.store.commit_publication,post,items)
                def confirm(db):
                    current=db.execute('SELECT revision,publish_op_id FROM posts WHERE id=?',(post['id'],)).fetchone()
                    if current['revision']!=op['revision'] or current['publish_op_id']!=op['id']:return
                    db.execute('UPDATE posts SET status=?,error=NULL WHERE id=?',(post['status'],post['id']))
                    db.execute("UPDATE operations SET status='confirmed',error=NULL WHERE id=?",(op['id'],))
                    if op['kind']=='publish':event(db,'admin_publish',{'email':op['actor']},post_id=post['id'])
                await self.write(confirm)
                if op['kind'] not in ('category', 'title'):
                    for item in items:(self.config.data_dir/'staging'/(item['id']+Path(item['filename']).suffix.lower())).unlink(missing_ok=True)
            except Exception as error:
                message=str(error) if isinstance(error,DeriveError) else ('Waiting for Google storage. The content changes will retry safely.' if op['kind'] in ('category','title') else 'Waiting for Google storage. The publication will retry safely.')
                def retry(db):
                    db.execute('UPDATE operations SET attempts=attempts+1,next_try=?,error=? WHERE id=?',(time.time()+min(60,2**min(op['attempts']+1,6)),message,op['id']))
                    db.execute('UPDATE posts SET error=? WHERE id=?',(message,post['id']))
                await self.write(retry)

    async def export(self):
        # Sealed immutable bytes + preallocated Drive id make retries idempotent.
        existing=await asyncio.to_thread(self.db.read,lambda db:rows(db,'SELECT * FROM batches WHERE confirmed_at IS NULL OR summary_confirmed=0 ORDER BY created_at LIMIT 1'))
        if not existing:
            events=await asyncio.to_thread(self.db.read,lambda db:rows(db,'SELECT * FROM events WHERE batch_id IS NULL ORDER BY seq LIMIT 500'))
            if not events:return
            identity=str(uuid.uuid4());drive_id=await asyncio.to_thread(self.store.new_id)
            content=''.join(json.dumps({'schema':1,**json.loads(e['payload'])},sort_keys=True)+'\n' for e in events).encode()
            digest=hashlib.sha256(content).hexdigest();path=self.config.data_dir/'exports'/(identity+'.jsonl')
            path.write_bytes(content);path.chmod(0o600)
            def seal(db):
                db.execute('INSERT INTO batches(id,drive_id,sha256,bytes,count,created_at) VALUES (?,?,?,?,?,?)',(identity,drive_id,digest,len(content),len(events),time.time()))
                db.executemany('UPDATE events SET batch_id=? WHERE id=?',[(identity,e['id']) for e in events])
                return dict(db.execute('SELECT * FROM batches WHERE id=?',(identity,)).fetchone())
            batch=await self.write(seal)
        else:batch=existing[0];path=self.config.data_dir/'exports'/(batch['id']+'.jsonl')
        if not batch['confirmed_at']:
            await asyncio.to_thread(self.store.put_file,batch['drive_id'],path,path.name,batch['sha256'],{'kind':'event_batch','schema':'1','count':batch['count'],'bytes':batch['bytes'],'batch_id':batch['id']})
            await self.write(lambda db:db.execute('UPDATE batches SET confirmed_at=? WHERE id=?',(time.time(),batch['id'])))
        if not batch['summary_confirmed']:
            events=await asyncio.to_thread(self.db.read,lambda db:[json.loads(r[0]) for r in db.execute('SELECT payload FROM events WHERE batch_id=? AND summary=1',(batch['id'],))])
            if events:await asyncio.to_thread(self.store.append_summary,events)
            await self.write(lambda db:db.execute('UPDATE batches SET summary_confirmed=1 WHERE id=?',(batch['id'],)))
        self.export_error=None

    async def summarize_views(self):
        def summarize(db):
            for view in rows(db,'SELECT * FROM views WHERE last_served_at<? AND (summarized_at IS NULL OR summarized_at<last_served_at)',(time.time()-30,)):
                event(db,'item_summary',{'email':view['email'],'id':view['session_id']},post_id=view['post_id'],item_id=view['item_id'],view_id=view['id'],detail={'pages':json.loads(view['served_pages']),'highest_page':view['highest_page']})
                db.execute('UPDATE views SET summarized_at=? WHERE id=?',(time.time(),view['id']))
        await self.write(summarize)

    async def background_loop(self):
        while True:
            with contextlib.suppress(Exception):await self.process_operation()
            try:await self.summarize_views();await self.export()
            except Exception:self.export_error='Cloud backup is delayed. Local audit records are retained.'
            await asyncio.sleep(3)

    async def create_view(self,request,item_id,attempt):
        try:uuid.UUID(attempt)
        except (ValueError,TypeError):raise Refusal(422,'invalid_attempt','A valid reading attempt id is required.')
        def create(db,ctx):
            old=db.execute('SELECT * FROM views WHERE session_id=? AND attempt_id=?',(ctx['id'],attempt)).fetchone()
            if old:
                if old['item_id']!=item_id:raise Refusal(409,'attempt_conflict','This reading attempt belongs to another attachment.')
                return dict(old)
            if ctx['item']['derive_status']!='ready':raise Refusal(409,'preparing','This attachment is still preparing.')
            identity=str(uuid.uuid4())
            db.execute('INSERT INTO views(id,session_id,email,item_id,post_id,attempt_id,requested_at) VALUES (?,?,?,?,?,?,?)',(identity,ctx['id'],ctx['email'],item_id,ctx['post']['id'],attempt,time.time()))
            event(db,'view_requested',ctx,post_id=ctx['post']['id'],item_id=item_id,view_id=identity)
            return dict(db.execute('SELECT * FROM views WHERE id=?',(identity,)).fetchone())
        return await self.execute(request,create,item_id=item_id)

    async def confirm_served(self,view_id,page,context):
        def confirm(db):
            view=db.execute('SELECT * FROM views WHERE id=?',(view_id,)).fetchone()
            if not view:return
            now=time.time()
            if view['first_served_at'] is None:
                db.execute('UPDATE views SET first_served_at=? WHERE id=? AND first_served_at IS NULL',(now,view_id))
                event(db,'item_open',context,post_id=view['post_id'],item_id=view['item_id'],view_id=view_id)
            pages=set(json.loads(view['served_pages']));pages.add(page)
            db.execute('UPDATE views SET last_served_at=?,served_pages=?,highest_page=? WHERE id=?',(now,json.dumps(sorted(pages)),max(pages),view_id))
            event(db,'page_served',context,post_id=view['post_id'],item_id=view['item_id'],view_id=view_id,detail={'page':page},summary=False)
        try:
            await self.write(confirm)
        except Refusal:
            # The reader already has the body; record the unconfirmed send rather than fabricating an open.
            with contextlib.suppress(Exception):
                await self.write(lambda db:event(db,'confirm_unavailable',context,view_id=view_id,detail={'page':page},summary=False))

    def composite(self,session,item,page,reader,thumbnail=False,selfie_path=None,selfie_id=None):
        # The cache holds one reader's stamped pixels for one minute; authorization already happened.
        filename='thumb.jpg' if thumbnail else f'page-{page}.jpg'
        def render(minute):
            return self.deriver.read_path(item['id'],filename,lambda path:stamp(path,reader,thumbnail=thumbnail,minute=minute,selfie_path=selfie_path))
        key = [item['id'],item['revision'],filename]
        if selfie_id: key.append(selfie_id)
        body,_=self.cache.get(session,key,render)
        return body

    async def image(self,request,*,view_id=None,item_id=None,post_id=None,page=0,thumbnail=False):
        """Authorize and commit the audit row before any protected pixels exist."""
        def resolve(db,ctx):
            if post_id and not item_id:
                row=db.execute("SELECT * FROM items WHERE post_id=? AND derive_status='ready' ORDER BY sort LIMIT 1",(post_id,)).fetchone()
                if not row:raise Refusal(404,'no_thumbnail','This post has no prepared attachment yet.')
                ctx['item']=dict(row)
            return ctx
        timings={}
        context=await self.execute(request,resolve,event_name='thumbnail_served' if thumbnail else 'page_requested',
                                   view_id=view_id,item_id=item_id,post_id=post_id,timings=timings)
        context['timings']=timings
        item=context['item']
        if item['kind'] == 'video':
            raise Refusal(404,'page_not_found','Open this attachment in the video player.')
        if item['derive_status']!='ready':
            raise Refusal(409,'preparing','This attachment is still preparing.')
        if not thumbnail and not 0<=page<item['pages']:
            raise Refusal(404,'page_not_found','That page is not part of this document.')
        try:
            portrait = None
            if context.get('selfie_id'):
                record = await asyncio.to_thread(self.db.read,lambda db:dict(db.execute('SELECT * FROM selfies WHERE id=?',(context['selfie_id'],)).fetchone()))
                portrait = await selfie.local_photo(self,record)
            arguments=(portrait,context['selfie_id']) if context.get('selfie_id') else ()
            body=await asyncio.to_thread(self.composite,context['id'],item,page,context['email'],thumbnail,*arguments)
            await asyncio.to_thread(self.db.read,lambda db:self.still_authorized(db,context))
            return body,context
        except SessionInvalidated:
            raise Refusal(403,'access_denied','Your access is no longer available.')
        except FileNotFoundError:
            # A rebuildable derivative was evicted; queue it again rather than serving nothing forever.
            await self.write(lambda db:db.execute("UPDATE items SET derive_status='queued' WHERE id=? AND derive_status='ready'",(item['id'],)))
            self.schedule_derive(item['id'])
            raise Refusal(409,'preparing','This attachment is being prepared again. Please try in a moment.')

    def still_authorized(self,db,context):
        # Rendering can outlast a revocation; re-check before the bytes leave the process.
        session=db.execute('SELECT id,revoked,expires_at,consent_version FROM sessions WHERE id=?',(context['id'],)).fetchone()
        if not session or session['expires_at']<=time.time():
            raise Refusal(401,'sign_in_required','Please sign in to continue.')
        self.permissions.require_fresh(db)
        member=db.execute('SELECT role FROM members WHERE email=?',(context['email'],)).fetchone()
        if session['revoked'] or not member:
            raise Refusal(403,'access_denied','Your access is no longer available.')
        if not self.permissions.consent_current(db,session):
            raise Refusal(403,'consent_required','Please review and accept both agreements before entering.')
        if self.config.selfie_enabled:
            selfie.require_enrollment(db,context,self.config.selfie_notice_version)
        post=db.execute('SELECT status,category FROM posts WHERE id=?',(context['post']['id'],)).fetchone()
        if not post or (post['status']!='published' and member['role']!='admin') or meta(db,'recovery_mode',False):
            raise Refusal(403,'access_denied','This post is not available.')
        if self.permissions.quizzes:
            quiz.require_unlocked(db,self.permissions.quizzes,member['role'],context['email'],post['category'])

    async def training_progress(self, request):
        def ready(db, ctx):
            if meta(db, 'recovery_mode', False):
                raise Refusal(409, 'history_restoring', 'Training history is being restored. Please try again shortly.')
        await self.execute(request, ready, admin=True, event_name=None)
        def query(db):
            # Catalog, roster and history come from one WAL snapshot. Replaying
            # history must not occupy the bounded writer used by reading requests.
            db.execute('BEGIN')
            ready(db, None)
            return dashboard(db, self.permissions.quizzes, self.final_exam, default_group=self.config.branding.default_group,
                             certificate_prefix=self.config.branding.certificate_prefix)
        result = await asyncio.to_thread(self.db.read, query)
        # A slow scan must not disclose data after permissions were revoked.
        await self.execute(request, ready, admin=True, event_name='training_progress_viewed')
        return result

    async def completed(self, request):
        """Chapter items the caller has finished, from a read snapshot: history replay must not occupy
        the writer. Events only accumulate, so a stale answer can only keep a quiz closed."""
        context = await self.execute(request, event_name=None)
        # Computed for every role: the writer step alone decides who is exempt, even if the
        # Sheet changes someone's role between the two transactions.
        def snapshot(db):
            db.execute('BEGIN')
            return finished_items(db, context['email'])
        try:
            return await asyncio.to_thread(self.db.read, snapshot)
        except (sqlite3.Error, OSError):
            raise Refusal(503, 'catalog_unavailable', 'Your reading progress could not be checked. Please try again shortly. / ตรวจความคืบหน้าการเรียนไม่สำเร็จ กรุณาลองใหม่อีกครั้งในอีกสักครู่')

    async def quiz(self, request, chapter):
        final = chapter == exam.KEY
        bank = self.final_exam if final else self.permissions.quizzes
        completed = await self.completed(request)
        def start(db, ctx):
            catalog = exam.available(db, ctx['email'], ctx['role']) if final else quiz.available(db, bank, ctx, chapter)
            left = {} if ctx['role'] in quiz.EXEMPT else unfinished(db, completed)
            attempts = quiz.records(db, ctx['email'])
            standing = ([exam.summary(bank, quiz.catalog(db), attempts, ctx['email'], ctx['role'])] if final else
                        quiz.summary(bank, catalog, attempts, ctx['email'], ctx['role'], left))
            ready = not left.get(chapter)
            # Until the chapter is finished, staff see what is left to open rather than the questions.
            dealt = quiz.current_hand(db, bank, ctx['email'], chapter) if ready else []
            event(db, 'quiz_opened', ctx, detail={'chapter': chapter, 'version': bank[chapter]['version'], 'ready': ready,
                                                  'questions': dealt}, summary=False)
            return {'chapter': chapter, 'category': catalog[chapter], 'version': bank[chapter]['version'],
                    'total': bank[chapter]['draw'], 'pool': len(bank[chapter]['questions']), 'pass_mark': bank[chapter]['pass_mark'],
                    'exempt': ctx['role'] == 'admin' if final else ctx['role'] in quiz.EXEMPT, 'ready': ready, 'unfinished': left.get(chapter, []),
                    'questions': quiz.questions(bank, chapter, dealt),
                    'standing': next(state for state in standing if state['chapter'] == chapter)}
        return await self.execute(request, start, event_name=None)

    async def submit_quiz(self, request, chapter, version, answers):
        final = chapter == exam.KEY
        bank = self.final_exam if final else self.permissions.quizzes
        completed = await self.completed(request)
        def submit(db, ctx):
            # Counted per account after authorization, from the durable record: other sessions or
            # anonymous requests cannot share or exhaust it. People need far longer than six seconds
            # a quiz, and the daily cap bounds how much history one account can add.
            now = time.time()
            minute, day = db.execute("SELECT count(*) FILTER (WHERE ts>?),count(*) FROM events "
                                     "WHERE email=? AND type='quiz_attempt' AND ts>?", (now - 60, ctx['email'], now - 86400)).fetchone()
            if minute >= quiz.PER_MINUTE:
                raise Refusal(429, 'rate_limited', 'Too many quiz attempts. Please wait a minute and try again. / ทำแบบทดสอบถี่เกินไป กรุณารอ 1 นาทีแล้วลองใหม่')
            if day >= quiz.PER_DAY:
                raise Refusal(429, 'rate_limited', 'You have reached today’s quiz limit. Please try again tomorrow. / วันนี้ทำแบบทดสอบครบจำนวนครั้งที่กำหนดแล้ว กรุณาลองใหม่พรุ่งนี้')
            catalog = exam.available(db, ctx['email'], ctx['role']) if final else quiz.available(db, bank, ctx, chapter)
            left = {} if ctx['role'] in quiz.EXEMPT else unfinished(db, completed)
            if left.get(chapter):
                raise Refusal(409, quiz.UNFINISHED, 'Read every page and watch every video in this chapter before taking its quiz. / กรุณาอ่านเอกสารให้ครบทุกหน้าและดูวิดีโอให้จบทุกคลิปในบทนี้ก่อนทำแบบทดสอบ')
            dealt = quiz.take(db, bank, ctx['email'], chapter, version, answers)
            correct = quiz.grade(bank, chapter, dealt, answers)
            score, total, mark = len(correct), len(dealt), bank[chapter]['pass_mark']
            passed = score >= mark
            identity = event(db, 'quiz_attempt', ctx, detail={'chapter': chapter, 'category': catalog[chapter], 'version': version,
                             'score': score, 'total': total, 'pass_mark': mark, 'passed': passed, 'answers': answers})
            quiz.record(db, db.execute('SELECT email,ts,seq,payload FROM events WHERE id=?', (identity,)).fetchone())
            attempts = quiz.records(db, ctx['email'])
            chapters = quiz.catalog(db)
            standing = quiz.summary(self.permissions.quizzes, chapters, attempts, ctx['email'], ctx['role'], left)
            following = None if final else next((number for number in catalog if number > chapter), None)
            unlocked = following is not None and quiz.blocking(
                bank, catalog, quiz.passed_chapters(attempts, ctx['email']), ctx['role'], following) is None
            return {'chapter': chapter, 'score': score, 'total': total, 'pass_mark': mark, 'passed': passed,
                    'results': quiz.review(bank, chapter, dealt, answers, passed), 'quizzes': standing,
                    'final_exam': exam.summary(self.final_exam, chapters, attempts, ctx['email'], ctx['role']),
                    'next_chapter': {'chapter': following, 'category': catalog[following], 'unlocked': unlocked} if following is not None else None}
        return await self.execute(request, submit, event_name=None)

    async def name_certificate(self, request, name):
        return await self.execute(request, lambda db, ctx: honors.rename(db, ctx, name, prefix=self.config.branding.certificate_prefix), event_name=None)

    async def report(self,request,post_id):
        def query(db,ctx):
            members=rows(db,'SELECT * FROM members ORDER BY name,email');report=[]
            gap=meta(db,'history_incomplete',False)
            for member in members:
                email=member['email']
                opened=db.execute("SELECT ts,payload FROM events WHERE post_id=? AND email=? AND type='item_open' ORDER BY ts,seq LIMIT 1",(post_id,email)).fetchone()
                first=opened['ts'] if opened else None
                identity=json.loads(opened['payload']).get('detail',{}).get('selfie_id') if opened else None
                photo=db.execute('SELECT * FROM selfies WHERE id=?',(identity,)).fetchone() if identity else None
                identity=identity if self.config.selfie_enabled and photo and selfie.readable(photo,self.config) else None
                attempts=db.execute("SELECT count(*) FROM events WHERE post_id=? AND email=? AND type='view_requested'",(post_id,email)).fetchone()[0]
                report.append({**member,'selfie_id':identity,'status':'opened' if first else 'unknown' if gap else 'not_opened','first_opened_at':first,'attempts':attempts,'seconds_to_open':max(0,first-ctx['post']['published_at']) if first and ctx['post']['published_at'] else None})
            return {'post':self.post_document(db, ctx['post']),'people':report,'opened':sum(r['status']=='opened' for r in report),'total':len(report),'history_incomplete':gap,'meaning':'Opened means a document page was served or video playback was recorded. Loading the player alone does not count. Activity is not proof the material was read or understood.'}
        return await self.execute(request,query,admin=True,post_id=post_id)

    async def operation(self,request,operation_id):
        def query(db,ctx):
            row=db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone()
            if not row:raise Refusal(404,'operation_not_found','That publication is not on record.')
            post=db.execute('SELECT status,error,revision,published_at FROM posts WHERE id=?',(row['post_id'],)).fetchone()
            return {**dict(row),'post':dict(post) if post else None}
        return await self.execute(request,query,admin=True,event_name=None)

    async def denied_signins(self,request,limit=50):
        def query(db,ctx):
            records=rows(db,"SELECT ts,payload FROM events WHERE type='signin_denied' ORDER BY seq DESC LIMIT ?",(min(limit,200),))
            return {'denied':[{'ts':r['ts'],**{k:json.loads(r['payload']).get(k) for k in ('email','ip','user_agent')}} for r in records]}
        return await self.execute(request,query,admin=True,event_name=None)

    async def health(self,private=False):
        def query(db):
            oldest=db.execute('SELECT MIN(e.ts),count(*) FROM events e LEFT JOIN batches b ON b.id=e.batch_id WHERE b.confirmed_at IS NULL').fetchone()
            latest=db.execute('SELECT MAX(confirmed_at) FROM batches').fetchone()[0]
            fresh=self.permissions.fresh(db)
            return {'process':'up','permissions':'ready' if fresh else 'paused','audit':'unavailable' if self.db.failed else 'ready',
                'backup':'delayed' if oldest[0] and time.time()-oldest[0]>60 else 'current','pending_events':oldest[1],
                'oldest_unexported_at':oldest[0],'last_export_at':latest,'allowlist_error':meta(db,'allowlist_error'),
                'recovery_mode':meta(db,'recovery_mode',False)}
        try:result=await asyncio.to_thread(self.db.read,query)
        except Exception:result={'process':'up','permissions':'paused','audit':'unavailable','backup':'unknown'}
        result['storage']='low' if shutil.disk_usage(self.config.data_dir).free<self.config.free_floor+self.config.upload_limit else 'ready'
        if not private:return {k:result[k] for k in ['process','permissions','audit','backup','storage']}
        result['mode']=self.config.mode;result['cache']=self.cache.snapshot();result['sqlite']=self.db.settings
        return result
