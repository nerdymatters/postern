import type { Item, Post, QuizStanding } from './api'
import { FinalExamEntry, QuizBadge, quizStatus } from './quiz'
import { IconPdf, IconPrivacy, IconQuiz, IconVideo } from './PosternIcons'
import { Thai } from './ui'

/** How an attachment is named in lists: the post title when it is the post's only attachment. */
export function itemTitle(post: Post, item: Item) {
  return post.items.length === 1 ? post.title : item.filename
}

export function kindLabel(item: Item) {
  return item.kind === 'video' ? 'Video' : item.kind === 'pdf' ? `PDF · ${item.pages} pages` : 'Photo'
}

export function moduleName(category: string) {
  return category.trim().replace(/^module\s*(\d+)$/i, 'Module $1') || 'General'
}

/** The SOP chapters by the number the server read from their names, then the optional Modules and
 *  other categories by name. */
export function modules(posts: Post[]) {
  const groups = new Map<string, { name: string; chapter: number | null; posts: Post[] }>()
  for (const post of posts) {
    const name = moduleName(post.category)
    const key = name.toLowerCase()
    if (!groups.has(key)) groups.set(key, { name, chapter: post.chapter ?? null, posts: [] })
    groups.get(key)!.posts.push(post)
  }
  return [...groups.values()].sort((a, b) => Number(a.chapter === null) - Number(b.chapter === null)
    || (a.chapter ?? 0) - (b.chapter ?? 0) || a.name.localeCompare(b.name, undefined, { numeric: true }))
    .map(group => ({ ...group, posts: [...group.posts].sort((a, b) =>
      (a.published_at ?? a.created_at) - (b.published_at ?? b.created_at) || a.id.localeCompare(b.id)) }))
}

/** Posts with something to open, in reading order. */
export function readable(posts: Post[]) {
  return modules(posts).flatMap(group => group.posts).filter(post => post.items.some(item => item.derive_status === 'ready'))
}

/** Each document contributes equally; a 69-page handout cannot swamp a short one. */
export function coverage(items: Item[]) {
  const counted = items.filter(item => item.kind === 'photo' || item.kind === 'video' || item.pages > 0)
  let fraction = 0
  let incomplete = false
  let unavailable = false
  for (const item of counted) {
    const total = item.kind === 'photo' ? 1 : item.pages
    if (!item.progress) unavailable = true
    const pages = new Set(item.progress?.opened_pages.filter(page => Number.isInteger(page) && page >= 0 && page < total))
    fraction += item.kind === 'video' ? Math.min(1, (item.progress?.watched_seconds ?? 0) / (item.duration_seconds || Infinity)) : pages.size / total
    incomplete ||= item.progress?.history_incomplete ?? false
  }
  const value = counted.length ? Math.floor(fraction / counted.length * 100) : 0
  return { value, label: unavailable ? 'Unavailable' : incomplete && value < 100 ? (value ? `At least ${value}%` : 'Unknown') : `${value}%`, count: counted.length }
}

export function Progress({ items, label, th }: { items: Item[]; label: string; th?: string }) {
  const progress = coverage(items)
  const known = progress.label !== 'Unknown' && progress.label !== 'Unavailable'
  return (
    <div className="space-y-2">
      <div className="flex justify-between gap-3 text-sm"><span>{label}{th ? <Thai>{th}</Thai> : null}</span><span className="font-medium tabular-nums">{progress.label}</span></div>
      {known ? <div role="progressbar" aria-label={label} aria-valuenow={progress.value} aria-valuemin={0} aria-valuemax={100} className="h-1.5 overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-700">
        <div className="h-full rounded-full bg-emerald-700 dark:bg-emerald-500" style={{ width: `${progress.value}%` }} />
      </div> : null}
    </div>
  )
}

export function Contents({ posts, active, onOpen, quizzes = [], exempt = false, onQuiz, finalExam, headingLevel = 3 }: {
  posts: Post[]; active?: string; onOpen: (post: Post, item?: Item) => void
  finalExam?: QuizStanding | null; quizzes?: QuizStanding[]; exempt?: boolean; onQuiz?: (chapter: number) => void
  /** Under the home card's h2 the section headings are h3; in the reader's sidebar, h2. */
  headingLevel?: 2 | 3
}) {
  const groups = modules(posts)
  const Heading = headingLevel === 2 ? 'h2' : 'h3'
  // Modules and quiz chapters look alike; head the chapters, then say where the optional Modules start.
  const labelled = quizzes.length > 0 && groups.some(group => group.chapter === null) && groups.some(group => group.chapter !== null)
  return <nav aria-label="Table of contents" className="space-y-5">
    {groups.map((group, index) => {
      const locked = group.posts[0].locked_by ?? null
      // A chapter split across two category names lists its quiz once, after its last group.
      const last = groups[index + 1]?.chapter !== group.chapter
      const quiz = last ? quizzes.find(candidate => candidate.chapter === group.chapter) : undefined
      // Name the nearest quiz still to pass before this chapter: "Chapter 1" under every later chapter reads as one gate.
      const previous = locked !== null ? Math.max(locked, ...quizzes.filter(candidate => candidate.chapter < (group.chapter ?? 0) && !candidate.passed).map(candidate => candidate.chapter)) : null
      const heading = labelled && (index === 0 || (groups[index - 1].chapter === null) !== (group.chapter === null))
      return <div key={group.name} className="space-y-5"><section className="space-y-2">
        {heading ? <div className="border-b border-neutral-200 pb-2 dark:border-neutral-800">
          <Heading className="text-sm font-semibold text-neutral-900 dark:text-neutral-100">{group.chapter === null
            ? <>Modules · optional, no quiz<Thai className="font-normal">โมดูล · ไม่บังคับ ไม่มีแบบทดสอบ</Thai></>
            : <>SOP chapters · each ends with a quiz<Thai className="font-normal">บท SOP · ทุกบทมีแบบทดสอบท้ายบท</Thai></>}</Heading>
          {group.chapter !== null ? <p className="mt-1 text-xs text-neutral-600 dark:text-neutral-300">{exempt ? 'Optional for your role.' : 'They open one at a time, in order.'}
            <Thai>{exempt ? 'ไม่บังคับสำหรับตำแหน่งของคุณ' : 'เปิดให้ทีละบทตามลำดับ'}</Thai></p> : null}
        </div> : null}
        <Progress items={group.posts.flatMap(post => post.items)} label={group.name} />
        {previous !== null ? <p className="flex items-start gap-1.5 text-xs text-neutral-600 dark:text-neutral-300"><IconPrivacy className="mt-px size-3.5 shrink-0" />{previous === locked
          ? <span>Finish Chapter {locked} and pass its quiz to unlock<span lang="th" className="block text-sm">เรียนบทที่ {locked} ให้ครบและสอบผ่านก่อน จึงจะเปิดบทนี้ได้</span></span>
          : <span>Opens after Chapter {previous}<span lang="th" className="block text-sm">จะเปิดหลังจากผ่านบทที่ {previous}</span></span>}</p> : null}
        <ol className="space-y-1">
          {group.posts.flatMap(post => post.items.map(item => <li key={item.id}>
            <button aria-label={`${itemTitle(post, item)}${post.locked_by != null ? `, locked until the Chapter ${previous ?? post.locked_by} quiz is passed` : ''}`} aria-current={item.id === active ? 'page' : undefined} onClick={() => onOpen(post, item)} className={`flex w-full items-start justify-between gap-3 rounded-lg px-3 py-3 text-left text-sm leading-snug ${item.id === active ? 'bg-neutral-100 dark:bg-neutral-800 font-medium text-neutral-900 dark:text-neutral-100' : post.locked_by != null ? 'text-neutral-500 dark:text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800' : 'text-neutral-700 dark:text-neutral-300 hover:bg-neutral-100 dark:hover:bg-neutral-800'}`}>
              <span className="flex min-w-0 gap-3">{item.kind === 'video' ? <IconVideo className="mt-px size-4 shrink-0" /> : item.kind === 'pdf' ? <IconPdf className="mt-px size-4 shrink-0" /> : <span className="size-4 shrink-0" />}<span className="min-w-0"><span className="block [overflow-wrap:anywhere]">{itemTitle(post, item)}</span><span className="mt-1 block text-xs font-normal text-neutral-500 dark:text-neutral-400">{kindLabel(item)}{post.items.length === 1 ? ` · ${item.filename}` : ''}</span></span></span>
              <span className="shrink-0 text-xs tabular-nums text-neutral-500 dark:text-neutral-400">{post.locked_by != null ? 'Locked' : item.derive_status === 'ready' ? coverage([item]).label : item.derive_status === 'failed' ? 'Unavailable' : 'Preparing'}</span>
            </button>
          </li>))}
          {quiz && onQuiz ? <li>
            <button disabled={quiz.locked_by !== null} onClick={() => onQuiz(quiz.chapter)} className="flex w-full items-start gap-3 rounded-lg px-3 py-3 text-left text-sm leading-snug text-neutral-700 enabled:hover:bg-neutral-100 disabled:text-neutral-400 dark:text-neutral-300 dark:enabled:hover:bg-neutral-800 dark:disabled:text-neutral-500">
              <IconQuiz className="mt-px size-4 shrink-0" />
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1"><span>Chapter {quiz.chapter} quiz</span><QuizBadge quiz={quiz} exempt={exempt} /></span>
                <span className="mt-1 block text-xs font-normal text-neutral-500 dark:text-neutral-400">{quizStatus(quiz, exempt).state === 'unfinished' ? `Finish ${quiz.unfinished!.length} more item${quiz.unfinished!.length === 1 ? '' : 's'} first` : `${quiz.total} questions · Pass with ${quiz.pass_mark}`}<span lang="th" className="block text-sm">{quizStatus(quiz, exempt).state === 'unfinished' ? `เรียนอีก ${quiz.unfinished!.length} รายการให้ครบก่อน` : `${quiz.total} ข้อ · ตอบถูก ${quiz.pass_mark} ข้อจึงจะผ่าน`}</span></span>
              </span>
            </button>
          </li> : null}
        </ol>
      </section>
        {group.chapter === 9 && last && finalExam && onQuiz ? <FinalExamEntry exam={finalExam} onQuiz={onQuiz} /> : null}
      </div>
    })}
  </nav>
}
