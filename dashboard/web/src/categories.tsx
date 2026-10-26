import { useState, type FormEvent } from 'react'
import { api, inline, type Post } from './api'
import { modules } from './contents'
import { Button, Field, Message, Panel, input } from './ui'
import { IconRename } from './PosternIcons'

export function Categories({ posts, onChanged }: { posts: Post[]; onChanged: () => Promise<void> }) {
  const [editing, setEditing] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const groups = modules(posts)

  async function save(event: FormEvent) {
    event.preventDefault()
    if (editing === null || !name.trim() || busy) return
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await api.renameCategory(editing, name.trim())
      setEditing(null)
      setNotice(`Category renamed to “${result.name}”. ${result.updated} post${result.updated === 1 ? '' : 's'} updated.`)
      await onChanged()
    } catch (failure) {
      setError(inline(failure))
      // A lost response can still have committed. Refresh before another attempt.
      await onChanged()
    } finally { setBusy(false) }
  }

  return <Panel>
    <details>
      <summary className="cursor-pointer rounded-md py-2 text-base font-semibold text-neutral-900 outline-offset-4 focus-visible:outline-2 dark:text-neutral-100"><h2 className="inline">Categories ({groups.length})</h2></summary>
      <div className="mt-4 space-y-4">
        <p className="text-sm text-neutral-600 dark:text-neutral-300">Rename a category across all its posts, including drafts and archived posts. Content and reading progress are preserved.</p>
        {groups.length === 0 ? <p className="text-sm text-neutral-500 dark:text-neutral-400">Categories appear after you create a post.</p> : null}
        {groups.map(group => {
          const pending = group.posts.some(post => post.category_sync_pending)
          const blocked = group.posts.some(post => post.category_sync_pending || post.title_sync_pending || post.status === 'publishing')
          return <div key={group.name} className="space-y-3 border-t border-neutral-100 dark:border-neutral-800 pt-3">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="break-words font-medium">{group.name}</p>
                <p className="text-sm text-neutral-500 dark:text-neutral-400">{group.posts.length} post{group.posts.length === 1 ? '' : 's'}{pending ? ' · Saving category changes…' : blocked ? ' · Waiting for content changes to finish…' : ''}</p>
              </div>
              {editing !== group.name ? <Button variant="secondary" disabled={busy || blocked} aria-label={`Rename ${group.name}`} onClick={() => { setEditing(group.name); setName(group.name); setError(''); setNotice('') }}><IconRename className="size-5" />Rename</Button> : null}
            </div>
            {pending && group.posts.some(post => post.error) ? <Message text="The category name has changed. Its backup is delayed and will retry automatically." /> : null}
            {editing === group.name ? <form onSubmit={save} className="space-y-3">
              <Field label="New category name"><input className={input} value={name} maxLength={200} required autoFocus disabled={busy} onChange={event => setName(event.target.value)} /></Field>
              <p className="text-sm text-neutral-600 dark:text-neutral-300">This updates all {group.posts.length} posts in “{group.name}”. Choose a name that is not already used by another category.</p>
              <Message text={error} />
              <div className="flex gap-3">
                <Button type="submit" disabled={busy || blocked || !name.trim()}>{busy ? 'Saving…' : 'Save category name'}</Button>
                <Button type="button" variant="secondary" disabled={busy} onClick={() => { setEditing(null); setError('') }}>Cancel</Button>
              </div>
            </form> : null}
          </div>
        })}
        {error && !groups.some(group => group.name === editing) ? <Message text={error} /> : null}
        {notice ? <p role="status" className="text-sm text-neutral-700 dark:text-neutral-300">{notice}</p> : null}
      </div>
    </details>
  </Panel>
}
