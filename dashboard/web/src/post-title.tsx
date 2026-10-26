import { useState, type FormEvent } from 'react'
import { PostStatus } from './post-status'
import { api, inline, type Post } from './api'
import { Button, Field, Message, input } from './ui'
import { IconRename } from './PosternIcons'

export function PostTitle({ post, onChanged }: { post: Post; onChanged: () => Promise<void> }) {
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(post.title)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const pending = post.title_sync_pending || post.category_sync_pending || post.status === 'publishing'

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!title.trim() || busy || pending) return
    setBusy(true); setError(''); setNotice('')
    try {
      await api.renamePost(post.id, title.trim())
      setEditing(false)
      setNotice('Content title saved.')
      await onChanged()
    } catch (failure) {
      setError(inline(failure))
      // Refresh even after a lost response: the rename may have committed.
      await onChanged()
    } finally { setBusy(false) }
  }

  return <div className="space-y-3 pb-3">
    <div className="flex items-start justify-between gap-3">
      <h2 className="min-w-0 break-words text-base font-semibold text-neutral-900 dark:text-neutral-100">{post.title}</h2>
      {!editing ? <Button variant="secondary" disabled={busy || pending} onClick={() => { setTitle(post.title); setEditing(true); setError(''); setNotice('') }}><IconRename className="size-5" />Rename title</Button> : null}
    </div>
    <div className="flex flex-wrap items-center gap-2"><PostStatus status={post.status} />{post.error && !post.title_sync_pending ? <p className="text-sm text-amber-900 dark:text-amber-200">{post.error}</p> : null}</div>
    {pending && !post.title_sync_pending ? <p role="status" className="text-sm text-neutral-600 dark:text-neutral-300">Waiting for content changes to finish before renaming…</p> : null}
    {editing ? <form onSubmit={save} className="space-y-3">
      <Field label="New content title"><input className={input} value={title} maxLength={120} required autoFocus disabled={busy} onChange={event => setTitle(event.target.value)} /></Field>
      <p className="text-sm text-neutral-600 dark:text-neutral-300">Files, links and reading progress stay the same.</p>
      <div className="flex gap-3">
        <Button type="submit" disabled={busy || pending || !title.trim()}>{busy ? 'Saving…' : 'Save title'}</Button>
        <Button type="button" variant="secondary" disabled={busy} onClick={() => { setEditing(false); setError('') }}>Cancel</Button>
      </div>
    </form> : null}
    <Message text={error} />
    {post.title_sync_pending ? <p role="status" className="text-sm text-neutral-600 dark:text-neutral-300">{post.error ? 'The title has changed. Its backup is delayed and will retry automatically.' : 'Saving title backup…'}</p> : notice ? <p role="status" className="text-sm text-neutral-600 dark:text-neutral-300">{notice}</p> : null}
  </div>
}
