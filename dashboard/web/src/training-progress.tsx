import { Fragment, useEffect, useState } from 'react'
import { api, inline, type QuizStanding, type TrainingItem, type TrainingPerson, type TrainingProgress } from './api'
import { moduleName } from './contents'
import { dated, EASTERN, moment } from './format'
import { CertificateCard, PrintableCertificate } from './honors'
import { Link } from './routes'
import { QuizBadge } from './quiz'
import { Button, Field, Message, Panel, input } from './ui'
import { IconArrowRight, IconAward, IconPrint } from './PosternIcons'

const statusNames: Record<TrainingPerson['status'], string> = {
  complete: 'Completed', in_progress: 'In progress', not_started: 'Not started',
  unknown: 'History incomplete', unavailable: 'Progress unavailable', no_content: 'No content',
}

const roleNames: Record<string, string> = { admin: 'Admin', manager: 'Manager', 'c-suite': 'C-suite' }

function quizNote(quiz: QuizStanding, incomplete: boolean) {
  const tries = `${quiz.attempts} attempt${quiz.attempts === 1 ? '' : 's'}`
  if (quiz.passed) return `Best ${quiz.best_score}/${quiz.total} · passed ${moment(quiz.passed_at ?? 0)} · ${tries}`
  if (quiz.attempts) return `Best ${quiz.best_score}/${quiz.total} · needs ${quiz.pass_mark} · ${tries} · last ${moment(quiz.last_attempt_at ?? 0)}`
  if (incomplete) return 'No attempt in available history'
  return quiz.locked_by !== null ? `Opens after the Chapter ${quiz.locked_by} quiz` : 'No attempts yet'
}

function percentage(value: number | null, incomplete: boolean) {
  if (value === null) return 'Unavailable'
  if (incomplete && value < 100) return value > 0 ? `At least ${value}%` : 'Unknown'
  return `${value}%`
}

function Meter({ value, incomplete, label }: { value: number | null; incomplete: boolean; label: string }) {
  const known = value !== null && (!incomplete || value > 0)
  return <div className="min-w-28 space-y-1.5">
    <p className="text-sm font-semibold tabular-nums text-neutral-900 dark:text-neutral-100">{percentage(value, incomplete)}</p>
    {known ? <div role="progressbar" aria-label={label} aria-valuenow={value} aria-valuemin={0} aria-valuemax={100} className="h-1.5 overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-700">
      <div className="h-full rounded-full bg-emerald-700 dark:bg-emerald-500" style={{ width: `${value}%` }} />
    </div> : null}
  </div>
}

function grouped(items: TrainingItem[]) {
  const groups = new Map<string, { name: string; items: TrainingItem[] }>()
  for (const item of items) {
    const name = moduleName(item.category), key = name.toLowerCase()
    if (!groups.has(key)) groups.set(key, { name, items: [] })
    groups.get(key)!.items.push(item)
  }
  return [...groups.values()].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }))
}

function itemNote(item: TrainingItem, incomplete: boolean) {
  if (item.fraction === null) return 'Progress unavailable'
  if (incomplete && !item.fraction) return 'No viewing record in available history'
  if (item.kind === 'video') {
    const minutes = (seconds: number) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`
    return `${minutes(item.watched_seconds)} of ${minutes(item.duration_seconds ?? 0)} watched`
  }
  if (item.kind === 'photo') return item.opened_pages ? 'Photo opened' : 'Photo not opened'
  return `${item.opened_pages} of ${item.pages} pages opened`
}

function PersonDetails({ person, incomplete }: { person: TrainingPerson; incomplete: boolean }) {
  const quizzes = person.quizzes ?? []
  // One person's details are open at a time, so at most one certificate is ever ready to print.
  const [showCertificate, setShowCertificate] = useState(false)
  const certificate = person.certificate
  return <div id={`details-${encodeURIComponent(person.email)}`} className="space-y-4 px-1 py-2">
    {quizzes.length ? <section className="rounded-xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-neutral-100 dark:border-neutral-800 pb-3">
        <h2 className="text-base font-semibold">Chapter quizzes for {person.name || person.email}</h2>
        <p className="text-sm tabular-nums text-neutral-600 dark:text-neutral-300">{person.quizzes_passed ?? 0} of {quizzes.length} passed{person.quiz_exempt ? ' · Optional for this role' : ''}</p>
      </div>
      <ul className="divide-y divide-neutral-100 dark:divide-neutral-800">{quizzes.map(quiz => <li key={quiz.chapter} className="flex flex-wrap items-start justify-between gap-3 py-3 last:pb-0">
        <div className="min-w-0 flex-1 basis-48 space-y-1">
          <p className="break-words font-medium text-neutral-900 dark:text-neutral-100">Chapter {quiz.chapter} quiz</p>
          <p className="break-words text-sm text-neutral-500 dark:text-neutral-400">{quiz.category}</p>
          <p className="text-sm text-neutral-600 dark:text-neutral-300">{quizNote(quiz, incomplete)}</p>
        </div>
        <QuizBadge quiz={quiz} exempt={person.quiz_exempt ?? false} />
      </li>)}</ul>
    </section> : null}
    {person.final_exam || certificate ? <section className="space-y-3 rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h2 className="font-semibold">Final Exam <span lang="th">· แบบทดสอบปลายหลักสูตร</span></h2>
          {person.final_exam ? <p className="text-sm text-neutral-600 dark:text-neutral-300">{quizNote(person.final_exam, incomplete)}</p> : null}
          {certificate ? <p className="break-words text-sm text-neutral-600 dark:text-neutral-300">Certificate {certificate.number} · awarded {dated(certificate.awarded_at, 'short', EASTERN)} · {certificate.name_confirmed ? `printed name “${certificate.name}”` : `printed name “${certificate.name}” (suggested, not yet confirmed)`}</p> : null}
        </div>
        {person.final_exam ? <span><QuizBadge quiz={person.final_exam} exempt={person.final_exam.optional ?? false} />{person.final_exam.optional && person.final_exam.attempts > 0 ? <span className="mt-1 block text-xs text-neutral-500 dark:text-neutral-400">Optional</span> : null}</span> : null}
      </div>
      {certificate ? <div className="space-y-3">
        <Button variant="secondary" aria-expanded={showCertificate} onClick={() => setShowCertificate(value => !value)}><IconAward className="size-5" />{showCertificate ? 'Hide certificate' : 'View certificate'}</Button>
        {showCertificate ? <>
          <div className="max-w-2xl"><CertificateCard certificate={certificate} level={3} /></div>
          <Button onClick={() => window.print()}><IconPrint className="size-5" />Print certificate</Button>
          <PrintableCertificate certificate={certificate} />
        </> : null}
      </div> : null}
    </section> : null}
    <h2 className="text-base font-semibold">Content progress for {person.name || person.email}</h2>
    {grouped(person.items).map(group => {
      const percent = group.items.every(item => item.fraction !== null) ? Math.floor(group.items.reduce((sum, item) => sum + (item.fraction ?? 0), 0) / group.items.length * 100) : null
      return <section key={group.name} className="rounded-xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-4">
        <div className="flex flex-wrap items-center justify-between gap-4 border-b border-neutral-100 dark:border-neutral-800 pb-3">
          <h3 className="min-w-0 flex-1 break-words font-semibold">{group.name}</h3>
          <Meter value={percent} incomplete={incomplete} label={`${person.name || person.email}: ${group.name}`} />
        </div>
        <ul className="divide-y divide-neutral-100 dark:divide-neutral-800">{group.items.map(item => <li key={item.id} className="flex flex-wrap items-start gap-4 py-3 last:pb-0">
          <div className="min-w-0 flex-1 basis-48 space-y-1">
            <Link to={`/admin/posts/${item.post_id}`} className="break-words font-medium text-neutral-900 dark:text-neutral-100 underline">{item.title}</Link>
            <p className="break-words text-sm text-neutral-500 dark:text-neutral-400">{item.kind === 'video' ? 'Video' : item.kind === 'pdf' ? 'PDF' : 'Photo'} · {item.filename}</p>
            <p className="text-sm text-neutral-600 dark:text-neutral-300">{itemNote(item, incomplete)}</p>
            {!item.opened && !item.fraction && item.attempts > 0 && !incomplete ? <p className="text-sm text-amber-900 dark:text-amber-200">{item.kind === 'video' ? 'Tried to open · no playback recorded' : 'Tried to open · no pages loaded'}</p> : null}
          </div>
          <Meter value={item.fraction === null ? null : Math.floor(item.fraction * 100)} incomplete={incomplete} label={`${person.name || person.email}: ${item.filename}`} />
        </li>)}</ul>
      </section>
    })}
  </div>
}

export function TrainingDashboard() {
  const [report, setReport] = useState<TrainingProgress | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(true)
  const [refresh, setRefresh] = useState(0)
  const [updated, setUpdated] = useState<number | null>(null)
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [sort, setSort] = useState('name')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [selectedGroup, setSelectedGroup] = useState('')
  useEffect(() => {
    let live = true
    setBusy(true); setError('')
    api.trainingProgress().then(result => { if (live) { setReport(result); setUpdated(Date.now() / 1000) } })
      .catch(failure => { if (live) setError(inline(failure)) }).finally(() => { if (live) setBusy(false) })
    return () => { live = false }
  }, [refresh])
  const groups = report?.groups?.length ? report.groups : ['Staff']
  const activeGroup = groups.includes(selectedGroup) ? selectedGroup : groups[0]
  const people = (report?.people ?? []).filter(person => (person.groups ?? [groups[0]]).includes(activeGroup))
  const chooseGroup = (group: string) => {
    setSelectedGroup(group); setExpanded(null); setSearch(''); setStatus('all')
  }
  const visible = people.filter(person => `${person.name} ${person.email}`.toLowerCase().includes(search.trim().toLowerCase()) && (status === 'all' || person.status === status))
    .sort((a, b) => sort === 'quizzes' ? (a.quizzes_passed ?? 0) - (b.quizzes_passed ?? 0) || (a.name || a.email).localeCompare(b.name || b.email) : sort === 'low' ? (a.percent ?? -1) - (b.percent ?? -1) : sort === 'high' ? (b.percent ?? -1) - (a.percent ?? -1) : sort === 'recent' ? (b.last_activity_at ?? 0) - (a.last_activity_at ?? 0) : (a.name || a.email).localeCompare(b.name || b.email))
  const incomplete = report?.history_incomplete ?? false
  const quizTotal = report?.quiz_total ?? 0
  const hasFinal = people.some(person => person.final_exam)
  const average = people.length && people.every(person => person.percent !== null)
    ? Math.floor(people.reduce((sum, person) => sum + person.items.reduce((total, item) => total + (item.fraction ?? 0), 0) / Math.max(1, person.total_items), 0) / people.length * 100) : null
  return <main className="mx-auto w-full max-w-6xl space-y-5 px-4 py-6">
    <Link to="/admin" className="inline-flex items-center gap-1.5 text-sm text-neutral-600 dark:text-neutral-300 underline"><IconArrowRight className="size-4 rotate-180" />Admin</Link>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h1 className="text-2xl font-semibold text-neutral-900 dark:text-neutral-100">Staff progress</h1><p className="mt-1 text-sm text-neutral-500 dark:text-neutral-400">{updated ? `Updated ${moment(updated)}` : 'Current training activity'}</p></div>
      <Button variant="secondary" disabled={busy} onClick={() => setRefresh(value => value + 1)}>{busy ? 'Loading…' : error ? 'Try again' : 'Refresh'}</Button>
    </div>
    <Message text={error} />
    {report ? <>
      <p className="text-base leading-relaxed text-neutral-600 dark:text-neutral-300">Current staff list, including admins · Published content only. Documents count unique pages opened; videos count unique sections watched. Each item has equal weight. Activity does not confirm understanding.{quizTotal ? ' Staff must pass each chapter quiz to unlock the next chapter; admins, managers and C-suite may skip them.' : ''}</p>
      {incomplete ? <Message text={`Some viewing history is missing. Percentages are minimum recorded progress; people without records are unknown, not confirmed as unstarted.${quizTotal ? ' Quiz passes in the missing history are not shown, and those chapters stay locked until the quiz is passed again.' : ''}`} /> : null}
      <div role="tablist" aria-label="Staff locations" className="flex gap-2 overflow-x-auto border-b border-neutral-200 pb-2 dark:border-neutral-800">
        {groups.map((group, index) => <button key={group} type="button" role="tab" id={`staff-group-${index}`} aria-selected={group === activeGroup} aria-controls="staff-group-panel" tabIndex={group === activeGroup ? 0 : -1}
          className={`min-h-11 shrink-0 rounded-lg px-3 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-700 ${group === activeGroup ? 'bg-neutral-900 text-white dark:bg-neutral-100 dark:text-neutral-900' : 'text-neutral-600 hover:bg-neutral-100 dark:text-neutral-300 dark:hover:bg-neutral-800'}`}
          onClick={() => chooseGroup(group)} onKeyDown={event => {
            const target = event.key === 'ArrowRight' ? (index + 1) % groups.length : event.key === 'ArrowLeft' ? (index - 1 + groups.length) % groups.length : event.key === 'Home' ? 0 : event.key === 'End' ? groups.length - 1 : null
            if (target === null) return
            event.preventDefault(); chooseGroup(groups[target])
            const tab = document.getElementById(`staff-group-${target}`)
            tab?.focus(); tab?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
          }}>{group}</button>)}
      </div>
      <section id="staff-group-panel" role="tabpanel" aria-labelledby={`staff-group-${groups.indexOf(activeGroup)}`} className="space-y-4">
      {(report.roster_warnings ?? []).filter(warning => warning.group === activeGroup).map(warning => <p key={warning.row} className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100">Spreadsheet row {warning.row}: {warning.message}. This row has no login access. Add an email address in the spreadsheet to include this person.</p>)}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Panel><p className="text-sm text-neutral-600 dark:text-neutral-300">Staff</p><p className="mt-2 text-2xl font-semibold tabular-nums">{people.length}</p></Panel>
        <Panel><p className="text-sm text-neutral-600 dark:text-neutral-300">Overall progress</p><p className="mt-2 text-2xl font-semibold tabular-nums">{report.total_items && people.length ? percentage(average, incomplete) : '—'}</p><p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">Average across staff in this tab</p></Panel>
        <Panel><p className="text-sm text-neutral-600 dark:text-neutral-300">All items completed</p><p className="mt-2 text-2xl font-semibold tabular-nums">{people.filter(person => person.status === 'complete').length}</p></Panel>
        <Panel><p className="text-sm text-neutral-600 dark:text-neutral-300">{incomplete ? 'Progress unknown' : 'Not started'}</p><p className="mt-2 text-2xl font-semibold tabular-nums">{people.filter(person => person.status === (incomplete ? 'unknown' : 'not_started')).length}</p></Panel>
      </div>
      {!report.total_items ? <Panel><p>No published training content yet. Publish a document or video to start tracking progress here.</p></Panel> : !people.length ? <Panel><p>No staff are on the current list.</p></Panel> : <Panel className="space-y-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-48 flex-1"><Field label="Find a person"><input type="search" className={input} placeholder="Name or email" value={search} onChange={event => setSearch(event.target.value)} /></Field></div>
          <Field label="Progress status"><select className={input} value={status} onChange={event => setStatus(event.target.value)}><option value="all">All progress</option><option value="not_started">Not started</option><option value="in_progress">In progress</option><option value="complete">Completed</option><option value="unknown">History incomplete</option><option value="unavailable">Progress unavailable</option></select></Field>
          <Field label="Sort by"><select className={input} value={sort} onChange={event => setSort(event.target.value)}><option value="name">Name</option><option value="low">Lowest progress</option><option value="high">Highest progress</option><option value="recent">Recent activity</option>{quizTotal ? <option value="quizzes">Fewest quizzes passed</option> : null}</select></Field>
        </div>
        <p className="text-sm text-neutral-500 dark:text-neutral-400">{visible.length} of {people.length} people · {report.total_items} training items</p>
        {!visible.length ? <p className="py-4 text-neutral-600 dark:text-neutral-300">No people match these filters.</p> : <div className="overflow-x-auto">
          <table role="table" className="block w-full text-left text-sm md:table">
            <caption className="sr-only">Staff training progress. Expand a person for chapter quiz, Final Exam, module and content details.</caption>
            <thead role="rowgroup" className="hidden md:table-header-group"><tr role="row" className="border-b border-neutral-200 dark:border-neutral-800 text-neutral-600 dark:text-neutral-300"><th role="columnheader" scope="col" className="py-3 pr-4">Person</th><th role="columnheader" scope="col" className="p-3">Overall progress</th><th role="columnheader" scope="col" className="p-3">Items completed</th>{quizTotal ? <th role="columnheader" scope="col" className="p-3">{hasFinal ? 'Chapter quizzes passed' : 'Quizzes passed'}</th> : null}{hasFinal ? <th role="columnheader" scope="col" className="p-3">Final Exam</th> : null}<th role="columnheader" scope="col" className="p-3">Last viewed</th><th role="columnheader" scope="col" className="py-3 pl-3"><span className="sr-only">Details</span></th></tr></thead>
            <tbody role="rowgroup" className="block md:table-row-group">{visible.map(person => <Fragment key={person.email}>
              <tr role="row" className="grid grid-cols-2 items-center gap-x-4 border-b border-neutral-200 dark:border-neutral-800 py-4 md:table-row md:border-neutral-100 md:py-0">
                <th role="rowheader" scope="row" className="col-span-2 pb-3 font-normal md:max-w-64 md:py-4 md:pr-4"><p className="break-words font-semibold text-neutral-900 dark:text-neutral-100">{person.name || person.email}</p><p className="break-all text-neutral-500 dark:text-neutral-400">{person.email}</p>{roleNames[person.role] ? <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400">{roleNames[person.role]}</p> : null}</th>
                <td role="cell" className="py-2 md:p-3"><p className="mb-1 text-xs text-neutral-500 dark:text-neutral-400 md:hidden">Overall progress</p><Meter value={person.percent} incomplete={incomplete} label={`${person.name || person.email}: overall progress`} /><p className="mt-1.5 text-xs text-neutral-500 dark:text-neutral-400">{statusNames[person.status]}</p></td>
                <td role="cell" className="py-2 tabular-nums md:p-3"><p className="mb-1 text-xs text-neutral-500 dark:text-neutral-400 md:hidden">Items completed</p>{person.completed_items} / {person.total_items}{incomplete ? <span className="block text-xs text-neutral-500 dark:text-neutral-400">Recorded</span> : null}</td>
                {quizTotal ? <td role="cell" className="py-2 tabular-nums md:p-3"><p className="mb-1 text-xs text-neutral-500 dark:text-neutral-400 md:hidden">{hasFinal ? 'Chapter quizzes passed' : 'Quizzes passed'}</p>{person.quizzes_passed ?? 0} / {quizTotal}{person.quiz_exempt ? <span className="block text-xs text-neutral-500 dark:text-neutral-400">Optional</span> : null}</td> : null}
                {hasFinal ? <td role="cell" className="py-2 md:p-3"><p className="mb-1 text-xs text-neutral-500 dark:text-neutral-400 md:hidden">Final Exam</p>{person.final_exam ? <span><QuizBadge quiz={person.final_exam} exempt={person.final_exam.optional ?? false} />{person.final_exam.optional && person.final_exam.attempts > 0 ? <span className="mt-1 block text-xs text-neutral-500 dark:text-neutral-400">Optional</span> : null}</span> : "—"}</td> : null}
                <td role="cell" className="py-2 text-neutral-500 dark:text-neutral-400 md:p-3"><span className="block text-xs md:hidden">Last viewed</span>{person.last_activity_at === null ? 'No record' : moment(person.last_activity_at)}</td>
                <td role="cell" className="py-2 text-right md:py-3 md:pl-3"><Button variant="secondary" aria-expanded={expanded === person.email} aria-controls={expanded === person.email ? `details-${encodeURIComponent(person.email)}` : undefined} aria-label={`${expanded === person.email ? 'Hide' : 'View'} progress for ${person.name || person.email}`} onClick={() => setExpanded(expanded === person.email ? null : person.email)}>{expanded === person.email ? 'Hide' : 'Details'}</Button></td>
              </tr>
              {expanded === person.email ? <tr role="row" className="block md:table-row"><td role="cell" colSpan={5 + Number(quizTotal > 0) + Number(hasFinal)} className="block bg-neutral-50 dark:bg-neutral-800 p-3 md:table-cell md:p-4"><PersonDetails person={person} incomplete={incomplete} /></td></tr> : null}
            </Fragment>)}</tbody>
          </table>
        </div>}
      </Panel>}
      </section>
    </> : null}
  </main>
}
