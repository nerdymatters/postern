import { useEffect, useState } from 'react'
import { SelfieImage } from './selfie'
import { api, inline, type Denial, type Person, type Report as Opened } from './api'
import { Link } from './routes'
import { copyText } from './clipboard'
import { day, moment } from './format'
import { Button, Message, Panel } from './ui'
import { IconArrowRight } from './PosternIcons'

export function Report({ postId, onBack }: { postId: string; onBack: () => void }) {
  const [report, setReport] = useState<Opened | null>(null)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let live = true
    api.report(postId).then(loaded => { if (live) setReport(loaded) })
      .catch(failure => { if (live) setError(inline(failure)) })
    return () => { live = false }
  }, [postId])

  if (!report) {
    return (
      <main className="mx-auto w-full max-w-2xl space-y-4 px-4 py-6">
        <Button variant="quiet" onClick={onBack}><IconArrowRight className="size-5 rotate-180" />Back</Button>
        <Message text={error} />
        {error ? null : <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…</p>}
      </main>
    )
  }

  const videoOnly = report.post.items?.length > 0 && report.post.items.every(item => item.kind === 'video')
  const documentsOnly = report.post.items?.length > 0 && report.post.items.every(item => item.kind !== 'video')
  const attemptNote = videoOnly ? 'Tried to open · no playback recorded' : documentsOnly ? 'Tried to open · no pages loaded' : 'Tried to open · no viewing recorded'
  const opened = report.people.filter(person => person.status === 'opened')
  const waiting = report.people.filter(person => person.status === 'not_opened')
  const unknown = report.people.filter(person => person.status === 'unknown')
  const share = Math.round((report.opened / Math.max(1, report.total)) * 100)

  return (
    <main className="mx-auto w-full max-w-2xl space-y-4 px-4 py-6">
      <div className="flex items-center gap-3">
        <Button variant="quiet" onClick={onBack}><IconArrowRight className="size-5 rotate-180" />Back</Button>
        <h1 className="flex-1 truncate text-center text-base font-semibold text-neutral-900 dark:text-neutral-100">{report.post.title}</h1>
        <span className="w-12" />
      </div>

      <Panel className="space-y-3">
        <p className="text-3xl font-semibold text-neutral-900 dark:text-neutral-100">
          {report.opened} of {report.total} <span className="text-lg font-normal text-neutral-500 dark:text-neutral-400">opened</span>
        </p>
        <div className="h-2 w-full overflow-hidden rounded bg-neutral-200 dark:bg-neutral-700">
          <div className="h-full bg-neutral-900 dark:bg-neutral-100" style={{ width: `${share}%` }} />
        </div>
        <p className="text-sm text-neutral-600 dark:text-neutral-300">
          Posted {day(report.post.published_at)} · {waiting.length} have not opened it
          {unknown.length ? ` · ${unknown.length} unknown` : ''}
        </p>
        {report.history_incomplete ? (
          <Message text="Some history is missing, so people marked unknown may have opened this before the gap." />
        ) : null}
      </Panel>

      <Panel className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">Not opened yet ({waiting.length})</h2>
          <Button
            variant="secondary"
            disabled={!waiting.length}
            onClick={async () => setCopied(await copyText(waiting.map(person => person.email).join(', ')))}
          >
            {copied ? 'Copied' : 'Copy emails'}
          </Button>
        </div>
        {waiting.map(person => (
          <Row key={person.email} person={person} note={person.attempts ? attemptNote : 'Not opened yet'} />
        ))}
        {waiting.length ? null : <p className="text-sm text-neutral-500 dark:text-neutral-400">{unknown.length ? 'No confirmed unopened readers.' : 'Everyone has opened it.'}</p>}
      </Panel>

      {unknown.length ? (
        <Panel className="space-y-3">
          <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">Unknown ({unknown.length})</h2>
          {unknown.map(person => (
            <Row key={person.email} person={person} note="Opening history is incomplete" />
          ))}
        </Panel>
      ) : null}

      <Panel className="space-y-3">
        <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-100">Opened ({opened.length})</h2>
        {opened.map(person => (
          <Row key={person.email} person={person} note={moment(person.first_opened_at)} />
        ))}
        {opened.length ? null : <p className="text-sm text-neutral-500 dark:text-neutral-400">{unknown.length ? 'No confirmed opens.' : 'Nobody has opened it yet.'}</p>}
      </Panel>

      <p className="text-sm text-neutral-500 dark:text-neutral-400">{report.meaning}</p>
      <Panel className="space-y-2">
        <Link to="/admin/progress" className="inline-flex items-center gap-1.5 font-medium text-neutral-700 dark:text-neutral-300 underline">View staff progress<IconArrowRight className="size-4" /></Link>
        <p className="text-base text-neutral-600 dark:text-neutral-300">Compare overall progress and see how much of each document or video a person has viewed.</p>
      </Panel>
    </main>
  )
}

function Row({ person, note }: { person: Person; note: string }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-neutral-100 dark:border-neutral-800 pt-3 text-sm first:border-0 first:pt-0">
      {person.selfie_id ? <SelfieImage id={person.selfie_id} className="size-9 shrink-0" /> : null}
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-neutral-900 dark:text-neutral-100">{person.name || person.email}</p>
        <p className="truncate text-neutral-500 dark:text-neutral-400">{person.email}</p>
      </div>
      <span className="max-w-full text-neutral-500 dark:text-neutral-400 sm:max-w-56 sm:text-right">{note}</span>
    </div>
  )
}

export function Denials({ onBack }: { onBack: () => void }) {
  const [denied, setDenied] = useState<Denial[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    api.denials().then(result => { if (live) setDenied(result.denied) })
      .catch(failure => { if (live) setError(inline(failure)) })
    return () => { live = false }
  }, [])

  return (
    <main className="mx-auto w-full max-w-2xl space-y-4 px-4 py-6">
      <div className="flex items-center gap-3">
        <Button variant="quiet" onClick={onBack}><IconArrowRight className="size-5 rotate-180" />Back</Button>
        <h1 className="flex-1 truncate text-center text-base font-semibold text-neutral-900 dark:text-neutral-100">Denied sign-ins</h1>
        <span className="w-12" />
      </div>
      <Message text={error} />
      <Panel className="space-y-3">
        {denied === null && !error ? <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…</p> : null}
        {denied?.length === 0 ? <p className="text-sm text-neutral-500 dark:text-neutral-400">Nobody has been turned away.</p> : null}
        {denied?.map((attempt, index) => (
          <div key={`${attempt.ts}-${index}`} className="border-t border-neutral-100 dark:border-neutral-800 pt-3 text-sm first:border-0 first:pt-0">
            <p className="font-medium text-neutral-900 dark:text-neutral-100">{attempt.email ?? 'unknown address'}</p>
            <p className="text-neutral-500 dark:text-neutral-400">{moment(attempt.ts)} · {attempt.ip ?? 'no address'}</p>
            <p className="truncate text-xs text-neutral-400 dark:text-neutral-500">{attempt.user_agent}</p>
          </div>
        ))}
      </Panel>
      <p className="text-xs text-neutral-500 dark:text-neutral-400">
        These accounts tried to sign in and are not on the staff list. Add them to the list to let them in.
      </p>
    </main>
  )
}
