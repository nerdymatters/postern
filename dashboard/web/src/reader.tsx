import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { api, inline, type Item, type Post, type QuizStanding, type View } from './api'
import { hold, release } from './blobs'
import { Button, Label, Message, Thai } from './ui'
import { openView } from './views'
import { Video } from './video'
import { Contents, itemTitle, moduleName, readable } from './contents'
import { IconArrowRight, IconPrivacy, IconQuiz } from './PosternIcons'
import { EndOfContent, firstUnfinished, nextStep, Preparing, Unavailable } from './guide'

export function Reader({ post, posts = [post], onBack, onOpen, onPageDisplayed, selectedItem, quizzes = [], exempt = false, onQuiz, finalExam }: {
  selectedItem?: string; post: Post; posts?: Post[]; onBack: () => void; onOpen?: (post: Post, item?: Item) => void; onPageDisplayed?: () => void
  finalExam?: QuizStanding | null; quizzes?: QuizStanding[]; exempt?: boolean; onQuiz?: (chapter: number) => void
}) {
  const ready = post.items.filter(item => item.derive_status === 'ready')
  // Opened on an item not ready (-1): say it is being prepared, or that it failed, rather than show another in its place.
  const [picked, setChosen] = useState(() => post.items.some(item => item.id === selectedItem && item.derive_status !== 'ready') ? -1 : Math.max(0, ready.findIndex(item => item.id === selectedItem)))
  const [contentsOpen, setContentsOpen] = useState(() => window.innerWidth >= 768)
  const chosen = picked < 0 ? ready.findIndex(item => item.id === selectedItem) : picked
  const item = ready[chosen]
  const unprepared = chosen < 0 ? post.items.find(item => item.id === selectedItem) : undefined
  const contents = useRef<HTMLElement>(null)
  const activeItem = item?.id ?? unprepared?.id
  useLayoutEffect(() => {
    if (!contentsOpen) return
    const panel = contents.current
    const selected = panel?.querySelector<HTMLElement>('[aria-current="page"]')
    if (!panel || !selected) return
    // Scroll only the desktop sidebar: scrollIntoView also moves the document and hides its title.
    // Run on selection/opening, not progress refreshes, so playback never steals keyboard focus.
    if (window.innerWidth >= 768) {
      const bounds = panel.getBoundingClientRect()
      const row = selected.getBoundingClientRect()
      if (row.top < bounds.top) panel.scrollTop += row.top - bounds.top
      else if (row.bottom > bounds.bottom) panel.scrollTop += row.bottom - bounds.bottom
    } else {
      selected.scrollIntoView?.({ block: 'nearest' })
    }
    selected.focus({ preventScroll: true })
  }, [activeItem, contentsOpen])
  // A failed item never becomes ready without a new upload, so it is not called "being prepared".
  const Unready = unprepared?.derive_status === 'failed' ? Unavailable : Preparing
  const ordered = readable(posts)
  const following = ordered[ordered.findIndex(candidate => candidate.id === post.id) + 1]
  // A later chapter can be locked by one still being prepared; do not lead the reader into it.
  const nextPost = following?.locked_by != null && following.locked_by !== post.chapter ? undefined : following
  const nextItem = item && ready[chosen + 1]
  // This chapter or module; a chapter split across two category names is still one chapter.
  const along = (other: Post) => post.chapter != null ? other.chapter === post.chapter : moduleName(other.category).toLowerCase() === moduleName(post.category).toLowerCase()
  const locked = post.locked_by ?? null
  const blocker = quizzes.find(candidate => candidate.chapter === locked)
  // A chapter ends with its quiz until it is passed; later chapters stay locked until then.
  const quiz = quizzes.find(candidate => candidate.chapter === post.chapter)
  // For staff the server's list says what is left in the chapter. The current item may only just have
  // finished, so the end of any item leads to what is still open, and to the quiz once nothing is. An item
  // still being prepared is named instead, and keeps the quiz closed.
  const staffQuiz = !exempt && quiz && !quiz.passed && onQuiz && quiz.unfinished ? quiz : undefined
  const left = staffQuiz ? firstUnfinished(posts, staffQuiz.unfinished!.filter(id => id !== item?.id)) : undefined
  const pending = left?.item.derive_status === 'ready' ? left : undefined
  const quizNext = staffQuiz ? (left ? undefined : staffQuiz)
    : !nextItem && (!nextPost || nextPost.chapter !== post.chapter) && quiz && !quiz.passed && onQuiz ? quiz : undefined
  // Leaving a section (the last chapter for the Modules, or the last section of all), staff go to required
  // work left elsewhere (another chapter's items or quiz); otherwise reading order goes on, and after the
  // last section, anything still open. Nothing is required of exempt roles or on a site without quizzes,
  // so only the end of the last section leads elsewhere. Chapter to chapter and Module to Module, the
  // Next button follows reading order.
  const leaving = !left && !quizNext && !nextItem && (!nextPost || (nextPost.chapter == null) !== (post.chapter == null))
  const step = leaving ? nextStep(posts, quizzes, exempt, item?.id, finalExam) : null
  const guided = step?.kind === 'quiz' ? (step.chapter !== post.chapter && onQuiz ? step : undefined)
    : step && (step.required ? !along(step.post) : !nextPost) && (onOpen || step.kind === 'preparing') ? step : undefined
  const waiting = left && !pending ? left : guided?.kind === 'preparing' ? guided : undefined
  const onward = guided ? undefined : nextPost
  // Another chapter is named, and says it ends with a quiz until it is begun; Modules are optional.
  const into = (target: Post): [string, string] => {
    const name = moduleName(target.category)
    if (target.chapter == null) return quizzes.length ? [`Next: ${name} (optional)`, `ถัดไป: ${name} (ไม่บังคับ)`] : [`Next module: ${target.title}`, 'โมดูลถัดไป']
    const standing = exempt ? undefined : quizzes.find(candidate => candidate.chapter === target.chapter)
    const begun = posts.some(candidate => candidate.chapter === target.chapter && candidate.items.some(entry => entry.progress?.opened_pages.length || entry.progress?.watched_seconds))
    return begun || standing?.passed ? [`Continue ${name}`, `เรียนบทที่ ${target.chapter} ต่อ`]
      : [`Start ${name}${standing ? ' (ends with a quiz)' : ''}`, `เริ่มบทที่ ${target.chapter}${standing ? ' (มีแบบทดสอบท้ายบท)' : ''}`]
  }
  const [nextLabel, nextLabelTh] = pending ? [`Continue the chapter: ${itemTitle(pending.post, pending.item)}`, 'เรียนบทนี้ต่อ']
    : waiting ? ['', ''] : quizNext ? [`Take the Chapter ${quizNext.chapter} quiz${exempt ? ' (optional)' : ''}`, `ทำแบบทดสอบบทที่ ${quizNext.chapter}${exempt ? ' (ไม่บังคับ)' : ''}`]
      : nextItem ? [`Next document: ${nextItem.filename}`, 'ถัดไป']
        : guided?.kind === 'quiz' ? [guided.chapter === 0 ? "Take the Final Exam" : `Take the Chapter ${guided.chapter} quiz`, guided.chapter === 0 ? "ทำแบบทดสอบปลายหลักสูตร" : `ทำแบบทดสอบบทที่ ${guided.chapter}`]
          : guided?.kind === 'item' ? along(guided.post) ? [`Next document: ${itemTitle(guided.post, guided.item)}`, 'ถัดไป'] : into(guided.post)
            : onward && onOpen ? along(onward) ? [`Next document: ${onward.title}`, 'ถัดไป'] : into(onward) : ['', '']
  // A skipped video is not finished: say so at its end instead of offering the quiz.
  const requireFinish = !!quizNext && !exempt && item?.kind === 'video'
  // The blocking chapter's next unfinished item, so a locked chapter leads back to the reading first.
  const blockerItem = blocker && !blocker.passed ? firstUnfinished(posts, blocker.unfinished) : undefined
  // Name the quiz just before this chapter, as the contents do: "Chapter 1" for every chapter reads as one gate.
  const previous = locked !== null ? Math.max(locked, ...quizzes.filter(candidate => candidate.chapter < (post.chapter ?? 0) && !candidate.passed).map(candidate => candidate.chapter)) : null
  const advance = () => { if (pending) onOpen?.(pending.post, pending.item); else if (quizNext) onQuiz?.(quizNext.chapter); else if (nextItem) { setChosen(chosen + 1); onOpen?.(post, nextItem) }
    else if (guided?.kind === 'quiz') onQuiz?.(guided.chapter); else if (guided?.kind === 'item') onOpen?.(guided.post, guided.item); else if (onward && onOpen) onOpen(onward) }
  const end = waiting ? <Preparing post={waiting.post} item={waiting.item} className="text-center" /> : undefined

  return (
    <main className="mx-auto grid w-full max-w-6xl gap-6 px-4 py-4 md:grid-cols-[14rem_minmax(0,1fr)]">
      <aside ref={contents} id="reader-contents" className="min-w-0 md:sticky md:top-20 md:max-h-[calc(100vh-6rem)] md:self-start md:overflow-y-auto">
        <details open={contentsOpen} onToggle={event => setContentsOpen(event.currentTarget.open)} className="group rounded-xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-4">
          <summary className="cursor-pointer text-sm font-semibold group-open:mb-4">Contents <span lang="th" className="font-normal text-neutral-500 dark:text-neutral-400">· สารบัญ</span></summary>
          <Contents finalExam={finalExam} posts={posts} active={activeItem} quizzes={quizzes} exempt={exempt} onQuiz={onQuiz} headingLevel={2} onOpen={(candidate, attachment) => { if (candidate.id === post.id) setChosen(Math.max(0, ready.findIndex(item => item.id === attachment?.id))); onOpen?.(candidate, attachment) }} />
        </details>
      </aside>
      <div className="min-w-0">
      <div className="flex items-center gap-3 pb-3">
        <Button variant="quiet" onClick={onBack}><IconArrowRight className="size-5 rotate-180" />Back</Button>
        <div className="min-w-0 flex-1 text-center">
          {/* Titles are often file names; the chapter or module says where this item belongs. */}
          {post.category.trim() ? <p className="line-clamp-2 text-xs font-medium text-neutral-500 dark:text-neutral-400">{moduleName(post.category)}</p> : null}
          <h1 className="truncate text-base font-semibold text-neutral-900 dark:text-neutral-100">{post.title}</h1>
        </div>
        <span className="w-12" />
      </div>
      {ready.length > 1 && locked === null ? (
        <div className="flex gap-2 overflow-x-auto pb-3">
          {ready.map((candidate, index) => (
            <button
              key={candidate.id}
              onClick={() => { setChosen(index); onOpen?.(post, candidate) }}
              className={`shrink-0 rounded-lg border px-3 py-2 text-xs ${index === chosen ? 'border-neutral-900 dark:border-neutral-100 text-neutral-900 dark:text-neutral-100' : 'border-neutral-300 dark:border-neutral-700 text-neutral-500 dark:text-neutral-400'}`}
            >
              {candidate.filename}
            </button>
          ))}
        </div>
      ) : null}
      {locked !== null ? (
        <section className="space-y-4 rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-6 text-center">
          <IconPrivacy className="mx-auto size-8 text-neutral-500 dark:text-neutral-400" />
          <h2 className="text-lg font-semibold">This chapter is locked<Thai tone="inherit" size="text-base" className="font-medium">บทนี้ยังล็อกอยู่</Thai></h2>
          {previous !== locked ? <p className="text-sm text-neutral-600 dark:text-neutral-300">Chapters open one at a time, in order. {post.category} opens after Chapter {previous}. You are on Chapter {locked}.
            <Thai className="mt-1">{`บทเรียนจะเปิดให้ทีละบทตามลำดับ บทนี้จะเปิดหลังจากผ่านบทที่ ${previous} ตอนนี้คุณอยู่ที่บทที่ ${locked}`}</Thai></p>
            : <p className="text-sm text-neutral-600 dark:text-neutral-300">Finish Chapter {locked} and pass its quiz to unlock {post.category}. {blocker ? `Answer at least ${blocker.pass_mark} of ${blocker.total} questions correctly.` : ''}
            <Thai className="mt-1">{blocker ? `เรียนบทที่ ${locked} ให้ครบ และทำแบบทดสอบให้ผ่าน (ตอบถูกอย่างน้อย ${blocker.pass_mark} จาก ${blocker.total} ข้อ) จึงจะเปิดบทนี้ได้` : `เรียนบทที่ ${locked} ให้ครบ และทำแบบทดสอบให้ผ่าน จึงจะเปิดบทนี้ได้`}</Thai></p>}
          {blockerItem && blockerItem.item.derive_status !== 'ready' ? <Preparing post={blockerItem.post} item={blockerItem.item} />
            : blockerItem && onOpen ? <Button onClick={() => onOpen(blockerItem.post, blockerItem.item)} className="w-full sm:w-auto"><Label en={`Continue Chapter ${locked}`} th={`เรียนบทที่ ${locked} ต่อ`} /><IconArrowRight className="size-5" /></Button>
            : onQuiz ? <Button onClick={() => onQuiz(locked)} className="w-full sm:w-auto"><IconQuiz className="size-5" /><Label en={`Take the Chapter ${locked} quiz`} th={`ทำแบบทดสอบบทที่ ${locked}`} /></Button> : null}
        </section>
      ) : item
        ? <>
          {quiz && post.chapter != null && !exempt ? <ChapterNote chapter={post.chapter} quiz={quiz} onQuiz={onQuiz} /> : null}
          {item.kind === 'video' ? <Video key={item.id} item={item} nextLabel={nextLabel} nextLabelTh={nextLabelTh} end={end} onNext={advance} onProgress={onPageDisplayed} requireFinish={requireFinish} /> : <Document key={item.id} item={item} nextLabel={nextLabel} nextLabelTh={nextLabelTh} end={end} onNext={advance} onPageDisplayed={onPageDisplayed} />}
        </>
        : unprepared ? <Unready post={post} item={unprepared} className="rounded-2xl border border-neutral-200 bg-white p-6 text-center dark:border-neutral-800 dark:bg-neutral-900" />
        : post.items.length && post.items.every(entry => entry.derive_status === 'failed') ? <Message text="This post is not available. An admin needs to upload it again. / โพสต์นี้เปิดดูไม่ได้ ผู้ดูแลระบบต้องอัปโหลดใหม่" />
        : <Message text="This post is still being prepared. Please try again in a moment. / โพสต์นี้ยังเตรียมไม่เสร็จ กรุณาลองใหม่อีกครั้งในภายหลัง" />}
      <p className="pt-4 text-center text-xs text-neutral-500 dark:text-neutral-400">
        Pages opened and video playback are recorded with your name, time and device.
      </p>
      </div>
    </main>
  )
}

/** Where this item sits in its chapter: what is left before the quiz, or that it is passed. */
function ChapterNote({ chapter, quiz, onQuiz }: { chapter: number; quiz: QuizStanding; onQuiz?: (chapter: number) => void }) {
  const left = quiz.unfinished?.length ?? 0
  return <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm dark:border-neutral-800 dark:bg-neutral-950">
    <p className="min-w-0 flex-1 basis-48 text-neutral-800 dark:text-neutral-200">{quiz.passed
      ? <>Chapter {chapter} quiz passed.<Thai>ผ่านแบบทดสอบบทที่ {chapter} แล้ว</Thai></>
      : left
        ? <>Chapter {chapter} · {left} item{left === 1 ? '' : 's'} left, then the quiz<Thai>บทที่ {chapter} · เหลืออีก {left} รายการ แล้วทำแบบทดสอบ</Thai></>
        : <>Chapter {chapter} is finished. Take the quiz to unlock the next chapter.<Thai>เรียนบทที่ {chapter} ครบแล้ว ทำแบบทดสอบเพื่อเปิดบทถัดไป</Thai></>}</p>
    {!quiz.passed && !left && onQuiz ? <Button variant="secondary" onClick={() => onQuiz(chapter)}><IconQuiz className="size-5" /><Label en="Take the quiz" th="ทำแบบทดสอบ" /></Button> : null}
  </div>
}

function Document({ item, nextLabel, nextLabelTh = '', end, onNext, onPageDisplayed }: { item: Item; nextLabel: string; nextLabelTh?: string; end?: ReactNode; onNext: () => void; onPageDisplayed?: () => void }) {
  const [view, setView] = useState<View | null>(null)
  const [page, setPage] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [displayedPage, setDisplayedPage] = useState<number | null>(null)
  const surface = useRef<HTMLCanvasElement>(null)
  const held = useRef(new Set<string>())
  const pages = item.kind === 'photo' ? 1 : item.pages
  const displayed = useRef(onPageDisplayed)
  displayed.current = onPageDisplayed
  // Only a page the reader turned to scrolls the page into view; opening the item keeps the title on screen.
  const turned = useRef(false)

  useEffect(() => {
    let live = true
    openView(item.id).then(opened => { if (live) setView(opened) })
      .catch(failure => { if (live) { setError(inline(failure) || 'This attachment could not be opened.'); setLoading(false) } })
    return () => { live = false }
  }, [item.id])

  useEffect(() => {
    if (!view) return
    let live = true
    setError('')
    setLoading(true)
    const wanted = item.kind === 'photo' ? api.photo(view.id) : api.page(view.id, page)
    wanted.then(blob => {
      if (!live) return
      const url = hold(blob)
      held.current.add(url)
      const picture = new Image()
      const done = () => { held.current.delete(url); release(url) }
      picture.onload = () => {
        done()
        if (!live) return
        paint(surface.current, picture)
        if (turned.current) surface.current?.scrollIntoView?.({ block: 'start', behavior: 'instant' })
        setDisplayedPage(page)
        setLoading(false)
        displayed.current?.()
      }
      picture.onerror = () => {
        done()
        if (live) { setError('That page could not be displayed. Try again.'); setLoading(false) }
      }
      picture.src = url
    }).catch(failure => {
      if (live) { setError(inline(failure) || 'That page is not available.'); setLoading(false) }
    })
    return () => { live = false }
  }, [view, page, item.id, item.kind])

  // Leaving the viewer: no page pixels and no object URLs outlive the screen.
  useEffect(() => {
    const canvas = surface.current
    const urls = held.current
    return () => {
      for (const url of urls) release(url)
      urls.clear()
      if (canvas) wipe(canvas)
    }
  }, [])

  return (
    <div className="space-y-3">
      <div className="protected relative overflow-hidden rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900">
        <canvas ref={surface} aria-label={`${item.filename}, page ${page + 1}`} className="block w-full scroll-mt-20" />
        {/* Above the pixels: a long press reaches this, not the canvas. */}
        <div className="absolute inset-0" onContextMenu={event => event.preventDefault()} />
        {loading ? <p className="absolute inset-0 grid place-items-center text-sm text-neutral-500 dark:text-neutral-400">Loading…</p> : null}
      </div>
      <Message text={error} />
      {item.kind === 'pdf' ? (
        <div className="flex items-center justify-between">
          <Button variant="secondary" disabled={page === 0} onClick={() => { turned.current = true; setPage(page - 1) }}><IconArrowRight className="size-5 rotate-180" />Prev</Button>
          <span className="text-sm text-neutral-600 dark:text-neutral-300">Page {page + 1} of {pages}</span>
          <Button variant="secondary" disabled={page + 1 >= pages} onClick={() => { turned.current = true; setPage(page + 1) }}>Next<IconArrowRight className="size-5" /></Button>
        </div>
      ) : null}
      {page + 1 >= pages && displayedPage === page && !loading && !error ? (
        <div className="border-t border-neutral-200 dark:border-neutral-800 pt-4">
          {nextLabel ? <Button onClick={onNext} className="w-full"><Label en={nextLabel} th={nextLabelTh} /><IconArrowRight className="size-5 shrink-0" /></Button> : end ?? <EndOfContent />}
        </div>
      ) : null}
    </div>
  )
}

function paint(canvas: HTMLCanvasElement | null, picture: HTMLImageElement) {
  if (!canvas) return
  canvas.width = picture.naturalWidth
  canvas.height = picture.naturalHeight
  canvas.getContext('2d')?.drawImage(picture, 0, 0)
}

function wipe(canvas: HTMLCanvasElement) {
  canvas.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height)
  canvas.width = 0
  canvas.height = 0
}
