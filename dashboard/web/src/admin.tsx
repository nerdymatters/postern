import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import { PostStatus } from './post-status'
import { PostTitle } from './post-title'
import { Categories } from './categories'
import { SelfieHistory } from './selfie'
import { api, inline, type AdminHealth, type Post } from './api'
import { copyText } from './clipboard'
import { moment, when } from './format'
import { Link, navigate } from './routes'
import { Button, Field, Message, Panel, input } from './ui'
import { IconArchive, IconArrowRight, IconManage, IconNewPost, IconNotOnList, IconReport, IconSearch, IconStaff } from './PosternIcons'

const ACCEPTED = '.pdf,.jpg,.jpeg,.png,.heic,.heif,.mp4'
const pause = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

export function Admin({ selfieEnabled = false, onMetadataChanged }: { selfieEnabled?: boolean; onMetadataChanged?: () => void }) {
  const [posts, setPosts] = useState<Post[] | null>(null)
  const [selected, setSelected] = useState('')
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [error, setError] = useState('')

  const reload = useCallback(async () => {
    try {
      setPosts((await api.adminPosts()).posts)
    } catch (failure) {
      setError(inline(failure))
    }
  }, [])

  useEffect(() => { void reload() }, [reload])

  const savingMetadata = posts?.some(post => post.category_sync_pending || post.title_sync_pending || post.status === 'publishing')
  useEffect(() => {
    if (!savingMetadata) return
    const timer = window.setInterval(() => { void reload() }, 3000)
    return () => window.clearInterval(timer)
  }, [savingMetadata, reload])

  const matches = (candidate: Post) => `${candidate.title} ${candidate.category}`.toLowerCase().includes(search.trim().toLowerCase())
  const active = posts?.filter(candidate => candidate.status !== 'archived') ?? []
  const archived = posts?.filter(candidate => candidate.status === 'archived') ?? []
  const visible = active.filter(candidate => matches(candidate) && (status === 'all' || candidate.status === status))
  const post = posts?.find(candidate => candidate.id === selected) ?? null
  return (
    <main className="mx-auto w-full max-w-2xl space-y-5 px-4 py-6">
      <h1 className="text-xl font-semibold text-neutral-900 dark:text-neutral-100">Admin</h1>
      <Message text={error} />
      <Panel className="space-y-2">
        <Link to="/admin/progress" className="inline-flex items-center gap-2 text-lg font-semibold text-neutral-700 dark:text-neutral-300 underline"><IconStaff className="size-5" />Staff progress<IconArrowRight className="size-5" /></Link>
        <p className="text-base text-neutral-600 dark:text-neutral-300">See each person’s overall progress, modules and individual documents or videos.</p>
      </Panel>
      <NewPost onCreated={async created => { await reload(); setSelected(created.id) }} />
      {post ? <Preparation key={post.id} post={post} onChanged={reload} onRenamed={async () => { await reload(); onMetadataChanged?.() }} /> : null}
      {posts ? <Categories posts={posts} onChanged={async () => { await reload(); onMetadataChanged?.() }} /> : null}
      <Panel className="space-y-4">
        <h2 className="text-lg font-semibold text-neutral-900 dark:text-neutral-100">Posts ({active.length})</h2>
        <Field label="Find a post"><span className="relative block"><IconSearch className="pointer-events-none absolute left-4 top-1/2 size-5 -translate-y-1/2 text-neutral-500 dark:text-neutral-400" /><input type="search" className={`${input} pl-12`} value={search} onChange={event => setSearch(event.target.value)} placeholder="Search title or category, including archives" /></span></Field>
        <Field label="Post status"><select className={input} value={status} onChange={event => setStatus(event.target.value)}>
          <option value="all">All active posts ({active.length})</option>
          <option value="published">Published ({active.filter(p => p.status === 'published').length})</option>
          <option value="draft">Drafts ({active.filter(p => p.status === 'draft').length})</option>
          <option value="publishing">Publishing ({active.filter(p => p.status === 'publishing').length})</option>
        </select></Field>
        {posts === null ? <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…</p> : <>
          <PostRows posts={visible} selected={selected} onSelect={setSelected} />
          {!visible.length ? <p className="text-sm text-neutral-500 dark:text-neutral-400">{active.length ? 'No active posts match these filters.' : 'No active posts yet.'}</p> : null}
        </>}
      </Panel>
      <Panel>
        <details open={search.trim() ? archived.some(matches) : undefined}>
          <summary className="cursor-pointer py-2 text-lg font-semibold text-neutral-700 dark:text-neutral-300"><IconArchive className="mr-2 inline size-5 align-[-3px]" />Archived ({search.trim() ? `${archived.filter(matches).length} of ${archived.length}` : archived.length})</summary>
          <p className="my-3 text-sm text-neutral-500 dark:text-neutral-400">Archived posts are hidden from readers and excluded from current training progress.</p>
          <PostRows posts={archived.filter(matches)} selected={selected} onSelect={setSelected} />
          {!archived.filter(matches).length ? <p className="text-sm text-neutral-500 dark:text-neutral-400">{archived.length ? 'No archived posts match your search.' : 'No archived posts.'}</p> : null}
        </details>
      </Panel>
      {selfieEnabled ? <SelfieHistory /> : null}
      <Health />
      <p className="text-sm">
        <Link to="/admin/denials" className="inline-flex items-center gap-1.5 text-neutral-600 dark:text-neutral-300 underline"><IconNotOnList className="size-4 shrink-0" />Tried to sign in but not on the list<IconArrowRight className="size-4 shrink-0" /></Link>
      </p>
    </main>
  )
}

function PostRows({ posts, selected, onSelect }: { posts: Post[]; selected: string; onSelect: (id: string) => void }) {
  return <div className="space-y-4">{posts.map(candidate => <article key={candidate.id} className="space-y-3 border-t border-neutral-100 dark:border-neutral-800 pt-4 first:border-0 first:pt-0">
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="break-words font-medium text-neutral-900 dark:text-neutral-100">{candidate.title}</p>
        <p className="mt-1 break-words text-sm text-neutral-500 dark:text-neutral-400">{candidate.category || 'General'} · {candidate.items.length} file(s)</p>
      </div>
      <PostStatus status={candidate.status} />
    </div>
    {candidate.error ? <p className="text-sm text-amber-900 dark:text-amber-200">{candidate.error}</p> : null}
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
      <span className="mr-auto text-neutral-500 dark:text-neutral-400">{when(candidate.published_at ?? candidate.created_at)}</span>
      {candidate.id === selected ? <span className="text-neutral-500 dark:text-neutral-400">Managing</span> : <Button variant="quiet" className="text-sm" onClick={() => onSelect(candidate.id)}><IconManage className="size-4" />Manage</Button>}
      <Link to={`/admin/posts/${candidate.id}`} className="inline-flex items-center gap-1.5 text-neutral-700 dark:text-neutral-300 underline"><IconReport className="size-4" />Report</Link>
    </div>
  </article>)}</div>
}

function Health() {
  const [health, setHealth] = useState<AdminHealth | null>(null)
  useEffect(() => {
    let live = true
    // Health is a diagnostic strip: if it cannot be read, the screen simply omits it.
    api.health().then(loaded => { if (live) setHealth(loaded) }).catch(() => {})
    return () => { live = false }
  }, [])
  if (!health) return null
  return (
    <Panel className="space-y-2 text-sm">
      <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">Service</h2>
      <p className="text-neutral-600 dark:text-neutral-300">
        {health.mode} mode · permissions {health.permissions} · audit {health.audit} · backup {health.backup} · storage {health.storage}
      </p>
      <p className="text-neutral-600 dark:text-neutral-300">
        Last export {moment(health.last_export_at)} · {health.pending_events} event(s) waiting
        {health.oldest_unexported_at ? `, oldest ${moment(health.oldest_unexported_at)}` : ''}
      </p>
      <Message text={health.allowlist_error ?? health.export_error ?? (health.recovery_mode ? 'Recovery mode: readers see nothing until the history is restored.' : '')} />
    </Panel>
  )
}

function NewPost({ onCreated }: { onCreated: (post: Post) => Promise<void> }) {
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [category, setCategory] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function create(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const created = await api.createPost({ title: title.trim(), body: body.trim(), category: category.trim() })
      setTitle(''); setBody(''); setCategory('')
      await onCreated(created)
    } catch (failure) {
      setError(inline(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel className="space-y-4">
      <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">New post</h2>
      <form className="space-y-4" onSubmit={create}>
        <Field label="Title">
          <input className={input} value={title} maxLength={120} required onChange={event => setTitle(event.target.value)} />
        </Field>
        <Field label="Caption" hint="Plain text describing the content. Nothing confidential here — that belongs inside the PDF.">
          <textarea className={input} rows={3} maxLength={280} value={body} onChange={event => setBody(event.target.value)} />
        </Field>
        <p className="-mt-2 text-right text-xs text-neutral-500 dark:text-neutral-400">{body.length}/280</p>
        <Field label="Category">
          <input className={input} value={category} maxLength={200} onChange={event => setCategory(event.target.value)} />
        </Field>
        <Message text={error} />
        <Button type="submit" disabled={busy || !title.trim()}><IconNewPost className="size-5" />Create draft</Button>
      </form>
    </Panel>
  )
}

function Preparation({ post, onChanged, onRenamed }: { post: Post; onChanged: () => Promise<void>; onRenamed: () => Promise<void> }) {
  const [progress, setProgress] = useState(-1)
  const [error, setError] = useState('')
  const [publishing, setPublishing] = useState(false)
  const [note, setNote] = useState('')
  const [copied, setCopied] = useState(false)
  const mounted = useRef(true)
  useEffect(() => () => { mounted.current = false }, [])

  const preparing = post.items.some(item => item.derive_status === 'queued' || item.derive_status === 'converting')
  useEffect(() => {
    if (!preparing) return
    const timer = setInterval(() => { void onChanged() }, 2000)
    return () => clearInterval(timer)
  }, [preparing, onChanged])

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setError('')
    setProgress(0)
    try {
      await api.attach(post.id, file, setProgress)
      await onChanged()
    } catch (failure) {
      setError(inline(failure))
    } finally {
      setProgress(-1)
    }
  }

  /** Publication is a queued operation: poll its status URL rather than block (ENG-D4). */
  async function publish() {
    setError('')
    setPublishing(true)
    setNote('Publishing…')
    try {
      const started = await api.publish(post.id)
      for (let tries = 0; tries < 120 && mounted.current; tries++) {
        const operation = await api.operation(started.status_url)
        if (operation.status === 'confirmed') {
          setNote('')
          await onChanged()
          return
        }
        if (operation.status === 'cancelled') {
          setError(operation.error ?? 'The publication was cancelled.')
          setNote('')
          return
        }
        // A pending operation carries the reason it is retrying, if there is one.
        setNote(operation.error ?? 'Publishing…')
        await pause(1000)
      }
      if (mounted.current) {
        setError('Still publishing. It finishes on its own — check this post again shortly.')
        setNote('')
      }
    } catch (failure) {
      setError(inline(failure))
      setNote('')
    } finally {
      if (mounted.current) setPublishing(false)
    }
  }

  async function archive() {
    if (!window.confirm('Archive this post? Readers lose access to it immediately.')) return
    setError('')
    try {
      await api.archive(post.id)
      await onChanged()
    } catch (failure) {
      setError(inline(failure))
    }
  }

  const link = `${window.location.origin}/post/${post.id}`
  return (
    <Panel className="space-y-4">
      <PostTitle post={post} onChanged={onRenamed} />

      <div className="space-y-2">
        {post.items.map(item => (
          <div key={item.id} className="flex items-center justify-between gap-3 text-sm">
            <span className="min-w-0 flex-1 truncate text-neutral-800 dark:text-neutral-200">{item.filename}</span>
            <span className={item.derive_status === 'failed' ? 'text-red-700 dark:text-red-400' : 'text-neutral-500 dark:text-neutral-400'}>
              {item.derive_status === 'ready'
                ? (item.kind === 'pdf' ? `${item.pages} pages ready` : item.kind === 'video' ? 'video ready' : 'photo ready')
                : item.derive_status === 'failed' ? (item.derive_error ?? 'failed') : 'preparing…'}
            </span>
          </div>
        ))}
        {post.status === 'draft' ? (
          <>
            <input type="file" accept={ACCEPTED} onChange={upload} className="block w-full text-sm" />
            <p className="text-xs text-neutral-500 dark:text-neutral-400">PDF or photos up to 100 MB; MP4 video (H.264/AAC) up to 2 GB and 3 hours. Export office documents to PDF first.</p>
            {progress >= 0 ? (
              <div className="h-2 w-full overflow-hidden rounded bg-neutral-200 dark:bg-neutral-700">
                <div className="h-full bg-neutral-900 dark:bg-neutral-100" style={{ width: `${Math.round(progress * 100)}%` }} />
              </div>
            ) : null}
          </>
        ) : null}
      </div>

      <Message text={error} />
      {note ? <p className="text-sm text-neutral-600 dark:text-neutral-300">{note}</p> : null}

      <div className="flex flex-wrap gap-3">
        {post.status === 'draft' ? (
          <Button onClick={publish} disabled={!post.items.length || preparing || publishing}>Publish</Button>
        ) : null}
        {post.status === 'published' ? (
          <Button variant="secondary" onClick={async () => setCopied(await copyText(link))}>
            {copied ? 'Link copied' : 'Copy link'}
          </Button>
        ) : null}
        {post.status !== 'archived' ? <Button variant="secondary" onClick={archive}>Archive</Button> : null}
        <Button variant="quiet" onClick={() => navigate(`/admin/posts/${post.id}`)}>Who opened it</Button>
      </div>
      {post.status === 'published' ? (
        <p className="break-all rounded-xl bg-neutral-100 dark:bg-neutral-800 px-4 py-3 font-mono text-xs text-neutral-700 dark:text-neutral-300">{link}</p>
      ) : null}
      <p className="text-xs text-neutral-500 dark:text-neutral-400">Documents are stamped page images. Videos show a personal overlay in the player.</p>
    </Panel>
  )
}
