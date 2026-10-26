import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, ApiError, currentToken, inline, type Item, type Post, type Quiz, type QuizResult, type QuizStanding } from './api'
import { coverage, itemTitle, kindLabel, readable } from './contents'
import { badges, Confetti, NewBadge } from './honors'
import { Link } from './routes'
import { Button, buttonClass, Label, Message, Thai } from './ui'
import { IconArrowRight, IconAward, IconCheck, IconPrivacy, IconQuiz, IconRetake } from './PosternIcons'

/** The earliest earlier chapter whose quiz still locks this one; the server enforces the same rule. */
export function lockedBy(chapter: number | null | undefined, quizzes: QuizStanding[], exempt: boolean) {
  if (exempt || chapter == null) return null
  return quizzes.find(quiz => quiz.chapter < chapter && !quiz.passed)?.chapter ?? null
}

/** The first readable post of a chapter, in reading order. */
export function chapterStart(posts: Post[], chapter: number) {
  return readable(posts).find(post => post.chapter === chapter)
}

const TONES = {
  passed: 'border-emerald-200 dark:border-emerald-800 bg-emerald-50 dark:bg-emerald-950 text-emerald-900 dark:text-emerald-200',
  retry: 'border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950 text-amber-900 dark:text-amber-200',
  optional: 'border-sky-200 dark:border-sky-800 bg-sky-50 dark:bg-sky-950 text-sky-900 dark:text-sky-200',
  open: 'border-neutral-300 dark:border-neutral-700 bg-white dark:bg-neutral-900 text-neutral-700 dark:text-neutral-300',
  locked: 'border-neutral-300 dark:border-neutral-700 bg-neutral-100 dark:bg-neutral-800 text-neutral-600 dark:text-neutral-300',
}

function progressNote(item: Item) {
  const { label } = coverage([item])
  return label === 'Unknown' || label === 'Unavailable' ? 'not finished' : `${label} done`
}

/** The post and attachment behind an item id, in reading order. */
export function findItem(posts: Post[], id: string): { post: Post; item: Item } | undefined {
  for (const post of posts) {
    const item = post.items.find(candidate => candidate.id === id)
    if (item) return { post, item }
  }
}

export type QuizState = 'passed' | 'locked' | 'unfinished' | 'retry' | 'optional' | 'open'

export function quizStatus(quiz: QuizStanding, exempt: boolean): { state: QuizState; label: string; tone: keyof typeof TONES } {
  if (quiz.passed) return { state: 'passed', label: 'Passed', tone: 'passed' }
  if (quiz.locked_by !== null) return { state: 'locked', label: 'Locked', tone: 'locked' }
  if (quiz.unfinished?.length) return { state: 'unfinished', label: 'Not ready', tone: 'open' }
  if (quiz.attempts) return { state: 'retry', label: `Best ${quiz.best_score ?? 0}/${quiz.total}`, tone: 'retry' }
  return exempt ? { state: 'optional', label: 'Optional', tone: 'optional' } : { state: 'open', label: 'Not taken', tone: 'open' }
}

export function QuizBadge({ quiz, exempt }: { quiz: QuizStanding; exempt: boolean }) {
  const { label, tone } = quizStatus(quiz, exempt)
  return <span className={`inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-semibold ${TONES[tone]}`}>
    {tone === 'passed' ? <IconCheck className="size-3.5" /> : tone === 'locked' ? <IconPrivacy className="size-3.5" /> : null}{label}
  </span>
}

/** One badge per chapter quiz: passed, open or locked. */
export function QuizTrail({ quizzes, exempt, onQuiz }: { quizzes: QuizStanding[]; exempt: boolean; onQuiz: (chapter: number) => void }) {
  const passed = quizzes.filter(quiz => quiz.passed).length
  return <div className="space-y-3">
    <div className="flex justify-between gap-3 text-sm"><span>SOP chapter quizzes<Thai>แบบทดสอบท้ายบท SOP</Thai></span>
      <span className="text-right"><span className="block font-medium tabular-nums">{passed} of {quizzes.length} passed</span><Thai>ผ่านแล้ว {passed} จาก {quizzes.length} บท</Thai></span></div>
    <ol aria-label="Chapter quizzes" className="flex flex-wrap gap-2">
      {quizzes.map(quiz => {
        const locked = quiz.locked_by !== null
        const style = quiz.passed ? 'border-emerald-700 bg-emerald-700 text-white dark:border-emerald-500 dark:bg-emerald-500 dark:text-neutral-950'
          : locked ? 'border-dashed border-neutral-300 text-neutral-400 dark:border-neutral-700 dark:text-neutral-500'
            : 'border-neutral-900 text-neutral-900 hover:bg-neutral-100 dark:border-neutral-100 dark:text-neutral-100 dark:hover:bg-neutral-800'
        return <li key={quiz.chapter}>
          <button type="button" disabled={locked} onClick={() => onQuiz(quiz.chapter)} aria-label={`Chapter ${quiz.chapter} quiz: ${quizStatus(quiz, exempt).label}`}
            className={`grid size-11 place-items-center rounded-full border-2 text-sm font-semibold tabular-nums ${style}`}>
            {quiz.passed ? <IconCheck className="size-5" /> : locked ? <IconPrivacy className="size-4" /> : quiz.chapter}
          </button>
        </li>
      })}
    </ol>
    {/* Staff read the rules in "How your training works" just above, so here only what the circles mean;
        exempt roles get the staff rule here too, as the staff they help never see the optional view. */}
    <p className="text-sm leading-relaxed text-neutral-600 dark:text-neutral-300">{exempt
      ? 'Quizzes are optional for your role, so every chapter is open. You can still take them, and your results are recorded. Staff must read every page and watch every video in a chapter, then pass its quiz, before the next chapter opens.'
      : 'Tap a number to open that chapter’s quiz. A lock means an earlier quiz comes first.'}
      <Thai className="mt-1">{exempt ? 'แบบทดสอบไม่บังคับสำหรับตำแหน่งของคุณ คุณจึงเปิดได้ทุกบท แต่ยังทำแบบทดสอบได้ และระบบจะบันทึกผลไว้ ส่วนพนักงานทั่วไปต้องอ่านเอกสารให้ครบทุกหน้าและดูวิดีโอให้จบทุกคลิปในบทนั้น แล้วทำแบบทดสอบให้ผ่าน จึงจะเปิดบทถัดไปได้'
        : 'แตะหมายเลขเพื่อเปิดแบบทดสอบของบทนั้น รูปกุญแจหมายถึงต้องผ่านแบบทดสอบบทก่อนหน้าก่อน'}</Thai></p>
  </div>
}

export function FinalExamEntry({ exam, onQuiz }: { exam: QuizStanding; onQuiz: (chapter: number) => void }) {
  return <section aria-label="Final Exam" className="space-y-2 border-t border-neutral-200 pt-4 dark:border-neutral-800">
    <button disabled={exam.locked_by !== null} onClick={() => onQuiz(0)} className="flex w-full items-start gap-3 rounded-lg px-3 py-3 text-left text-sm text-neutral-900 enabled:hover:bg-neutral-100 disabled:text-neutral-500 dark:text-neutral-100 dark:enabled:hover:bg-neutral-800">
      <IconQuiz className="mt-0.5 size-4 shrink-0" />
      <span className="min-w-0 flex-1 space-y-2">
        <span className="flex flex-wrap items-center justify-between gap-2"><span className="font-semibold">Final Exam<Thai>แบบทดสอบปลายหลักสูตร</Thai></span><QuizBadge quiz={exam} exempt={exam.optional ?? false} /></span>
        <span className="block text-xs text-neutral-600 dark:text-neutral-300">{exam.total} questions · Pass with {exam.pass_mark}<Thai>{exam.total} ข้อ · ตอบถูก {exam.pass_mark} ข้อจึงจะผ่าน</Thai></span>
        {exam.optional ? <span className="block text-xs text-neutral-600 dark:text-neutral-300">Optional for Admins. You can take it without passing Chapter 9.<Thai>ไม่บังคับสำหรับผู้ดูแลระบบ ทำได้โดยไม่ต้องผ่านแบบทดสอบบทที่ 9 ก่อน</Thai></span> : null}
        {exam.locked_by !== null ? <span className="block text-xs">Pass the Chapter 9 quiz to unlock.<Thai>ผ่านแบบทดสอบบทที่ 9 ก่อน จึงจะเปิดได้</Thai></span> : null}
      </span>
    </button>
  </section>
}

export function QuizPage({ chapter, posts, exempt, onFinished, onOpen, onBack, onStale, onQuiz }: {
  chapter: number; posts: Post[]; exempt: boolean; onQuiz?: (chapter: number) => void; onStale?: () => void
  onFinished: (result: QuizResult) => void; onOpen: (post: Post, item?: Item) => void; onBack: () => void
}) {
  const final = chapter === 0
  const [quiz, setQuiz] = useState<Quiz | null>(null)
  const [chosen, setChosen] = useState<Record<string, string>>({})
  const [result, setResult] = useState<QuizResult | null>(null)
  const [error, setError] = useState('')
  const [stale, setStale] = useState(false)
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [round, setRound] = useState(0)
  // "Check again" that finds the chapter still unfinished says so, rather than looking like nothing happened.
  const [checked, setChecked] = useState(false)
  const [checking, setChecking] = useState(false)
  const top = useRef<HTMLDivElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const outcome = useRef<HTMLHeadingElement>(null)
  const mounted = useRef(true)
  const postsRef = useRef(posts)
  postsRef.current = posts
  const onStaleRef = useRef(onStale)
  onStaleRef.current = onStale
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  useEffect(() => {
    let live = true
    // Hide the previous copy: a retry reshuffles options, and an updated quiz has new questions.
    setLoading(true); setError(''); setStale(false); setResult(null); setChosen({}); setQuiz(null); setChecked(false)
    api.quiz(chapter).then(loaded => {
      if (!live) return
      setQuiz(loaded)
      if (round) heading.current?.focus()
      // Material the page has not loaded yet: ask for a fresh reading room so the checklist can link it.
      if ((loaded.unfinished ?? []).some(id => !findItem(postsRef.current, id))) onStaleRef.current?.()
    })
      .catch(failure => { if (live) { setQuiz(null); setError(inline(failure) || 'This quiz could not be loaded.') } })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [chapter, round])

  // Progress saved after this page loaded (a video's last report, a page just read) may have
  // finished the chapter: ask again whenever the reading room refreshes.
  const waiting = quiz?.ready === false
  useEffect(() => {
    if (!waiting) return
    let live = true
    api.quiz(chapter).then(loaded => { if (live) setQuiz(loaded) }).catch(() => { /* the checklist stays */ })
    return () => { live = false }
  }, [posts]) // eslint-disable-line react-hooks/exhaustive-deps

  // Keyboard and screen-reader users land on their score, not at the top of the page.
  useEffect(() => { if (result) outcome.current?.focus() }, [result])
  const landing = useRef(false)
  useEffect(() => { if (landing.current) { landing.current = false; heading.current?.focus() } }, [quiz])

  const answered = quiz ? quiz.questions.filter(question => chosen[question.id]).length : 0
  const start = chapterStart(posts, chapter)
  const next = result?.next_chapter
  const following = next?.unlocked ? chapterStart(posts, next.chapter) : undefined
  // A failed retake never removes an earlier pass.
  const kept = !!result && !result.passed && !!(final ? result.final_exam : result.quizzes.find(standing => standing.chapter === chapter))?.passed
  // The first pass earns this quiz's badge (the Final Exam's brings the certificate); retakes after a pass earn nothing new.
  const earned = result?.passed && quiz && !quiz.standing.passed
    ? badges(result.quizzes, result.final_exam).find(badge => final ? badge.kind === 'final' : badge.chapter === chapter) : undefined
  const everyChapter = !!earned && !final && result!.quizzes.length > 1 && result!.quizzes.every(standing => standing.passed)
  const title = final ? "Final Exam" : quiz?.category ?? start?.category ?? `Chapter ${chapter}`
  const upcoming = !final && readable(posts).find(post => post.chapter != null && post.chapter > chapter)
  // Each attempt deals new questions from a larger pool until the chapter is passed; after a pass the
  // server asks only questions a pass has already shown the answers to, topped up from the pool with the
  // same fixed few only when a bank edit removed some.
  const fresh = !!quiz && (quiz.pool ?? 0) > quiz.total && !quiz.standing.passed && !result?.passed
  const retry = () => { setRound(value => value + 1); top.current?.scrollIntoView?.({ block: 'start' }) }
  const recheck = () => {
    if (checking) return
    // Cleared first, so the status below is announced again even when the count has not changed.
    setChecking(true); setNotice(''); setChecked(false)
    api.quiz(chapter).then(loaded => {
      if (!mounted.current) return
      // Still unfinished: say so. Now ready: the questions replace the checklist, so focus moves to the title.
      setQuiz(loaded); setChecked(loaded.ready === false); landing.current = loaded.ready !== false
      if ((loaded.unfinished ?? []).some(id => !findItem(postsRef.current, id))) onStaleRef.current?.()
    })
      .catch(failure => { if (mounted.current) setNotice(inline(failure) || 'Your progress could not be checked. Please try again.') })
      .finally(() => { if (mounted.current) setChecking(false) })
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!quiz || busy || answered < quiz.total) return
    setBusy(true); setError('')
    const identity = currentToken()
    try {
      const graded = await api.submitQuiz(chapter, quiz.version, Object.fromEntries(quiz.questions.map(question => [question.id, chosen[question.id]])))
      // A late answer must not land on a page that closed or on the next person's session.
      if (!mounted.current || currentToken() !== identity) return
      setResult(graded)
      onFinished(graded)
      top.current?.scrollIntoView?.({ block: 'start' })
    } catch (failure) {
      if (!mounted.current) return
      // Finished-chapter checks can disagree with a stale page: reload to show what is left.
      if (failure instanceof ApiError && failure.code === 'chapter_unfinished') {
        setNotice('Your answers were not submitted. Finish the items below first, then take the quiz again. / ยังไม่ได้ส่งคำตอบ กรุณาเรียนรายการด้านล่างให้ครบก่อน แล้วทำแบบทดสอบอีกครั้ง')
        retry()
        return
      }
      setError(inline(failure))
      const changed = failure instanceof ApiError && failure.code === 'quiz_changed'
      setStale(changed)
      if (changed) onStaleRef.current?.()
    } finally { if (mounted.current) setBusy(false) }
  }

  return <main className="mx-auto w-full max-w-2xl space-y-5 px-4 py-6">
    <div ref={top} className="scroll-mt-20">
      <Button variant="quiet" onClick={onBack}><IconArrowRight className="size-5 rotate-180" />Back</Button>
    </div>
    <header className="space-y-2">
      <p className="flex items-center gap-2 text-sm font-medium text-neutral-600 dark:text-neutral-300"><IconQuiz className="size-4" />{final ? <>Final Exam · <span lang="th">แบบทดสอบปลายหลักสูตร</span></> : `Chapter ${chapter} quiz`}</p>
      <h1 ref={heading} tabIndex={-1} className="break-words text-xl font-semibold text-neutral-900 outline-none dark:text-neutral-100">{title}</h1>
      {quiz ? <p className="text-sm text-neutral-600 dark:text-neutral-300">
        {quiz.total} questions · Answer {quiz.pass_mark} correctly ({Math.round(quiz.pass_mark / quiz.total * 100)}%) to pass{exempt || final ? '.' : ' and unlock the next chapter.'}
        {fresh ? ' Every attempt has a new set of questions.' : ''}
        <Thai className="mt-1">{`${quiz.total} ข้อ · ตอบถูกอย่างน้อย ${quiz.pass_mark} ข้อ (${Math.round(quiz.pass_mark / quiz.total * 100)}%) จึงจะผ่าน${exempt || final ? '' : 'และเปิดบทถัดไป'}`}{fresh ? ' ทำแบบทดสอบแต่ละครั้งจะได้คำถามชุดใหม่' : ''}</Thai>
      </p> : null}
      {exempt && quiz ? <p className="text-sm text-neutral-600 dark:text-neutral-300">This quiz is optional for your role. Your result is still recorded.<Thai>แบบทดสอบนี้ไม่บังคับสำหรับตำแหน่งของคุณ แต่ระบบยังบันทึกผลไว้</Thai></p> : null}
      {exempt && quiz && upcoming && !result ? <Button variant="secondary" onClick={() => onOpen(upcoming)}><Label en={`Skip to Chapter ${upcoming.chapter}`} th={`ข้ามไปบทที่ ${upcoming.chapter}`} /><IconArrowRight className="size-5" /></Button> : null}
      {quiz?.standing.passed && !result ? <p className="text-sm text-neutral-600 dark:text-neutral-300">You passed this quiz before. Taking it again keeps your pass and asks the questions from your pass again.<Thai>คุณเคยสอบผ่านแบบทดสอบนี้แล้ว ทำซ้ำได้ และผลที่ผ่านแล้วยังนับอยู่ โดยจะได้คำถามเดิมจากครั้งที่สอบผ่าน</Thai></p> : null}
    </header>
    {loading ? <p className="text-neutral-600 dark:text-neutral-300">Loading…</p> : null}
    <Message text={error} />
    {stale ? <Button onClick={retry}><IconRetake className="size-5" /><Label en="Start again" th="เริ่มใหม่" /></Button> : null}
    {!quiz && !loading ? <div className="flex flex-col gap-3 sm:flex-row">
      {error ? <Button onClick={retry}><IconRetake className="size-5" /><Label en="Try again" th="ลองใหม่" /></Button> : null}
      <Button variant="secondary" onClick={onBack}><Label en="Back to your training" th="กลับไปหน้าการเรียน" /></Button>
    </div> : null}

    {quiz && result ? <>
      <section className={`relative space-y-4 overflow-hidden rounded-2xl border p-5 ${result.passed || kept ? TONES.passed : TONES.retry}`}>
        {earned ? <Confetti /> : null}
        <div className="flex items-center gap-4">
          <span aria-hidden="true" className={`grid size-16 shrink-0 place-items-center rounded-full text-lg font-semibold tabular-nums ${result.passed ? 'bg-emerald-700 text-white dark:bg-emerald-500 dark:text-neutral-950' : 'bg-white text-amber-900 dark:bg-amber-900 dark:text-amber-100'}`}>
            {result.score}/{result.total}
          </span>
          <div className="min-w-0 space-y-1">
            <h2 ref={outcome} tabIndex={-1} className="text-lg font-semibold outline-none">{result.passed ? 'You passed!' : kept ? 'Practice result' : 'Not passed yet'}<span className="sr-only"> {result.score} of {result.total} correct.</span><span lang="th" className="block text-base font-normal">{result.passed ? 'คุณสอบผ่านแล้ว!' : kept ? 'ผลการฝึกทำ' : 'ยังไม่ผ่าน'}</span></h2>
            <p className="text-sm">{result.passed
              ? final ? 'Well done. You passed the Final Exam. Your result has been saved.' : next?.unlocked && !exempt ? `Chapter ${next.chapter} is unlocked. It works the same way: read every page, watch every video, then pass its quiz.` : next ? 'Well done.' : 'Well done. You have reached the last chapter quiz.'
              : kept ? `${result.score} of ${result.total} correct this time. Your earlier pass still counts.`
                : `${result.score} of ${result.total} correct. You need ${result.pass_mark} to pass. Review the ${final ? "training materials" : "chapter"}, then try again${fresh ? ' with a new set of questions' : ''}.`}
              <Thai tone="inherit" className="mt-1 opacity-90">{result.passed
                ? final ? 'เยี่ยมมาก คุณผ่านแบบทดสอบปลายหลักสูตรแล้ว ระบบบันทึกผลไว้แล้ว' : next?.unlocked && !exempt ? `บทที่ ${next.chapter} เปิดแล้ว และมีขั้นตอนเหมือนเดิม คืออ่านเอกสารให้ครบทุกหน้า ดูวิดีโอให้จบทุกคลิป แล้วทำแบบทดสอบให้ผ่าน` : next ? 'เยี่ยมมาก' : 'เยี่ยมมาก นี่คือแบบทดสอบบทสุดท้ายแล้ว'
                : kept ? `ครั้งนี้ตอบถูก ${result.score} จาก ${result.total} ข้อ ผลที่สอบผ่านก่อนหน้ายังนับอยู่`
                  : `ตอบถูก ${result.score} จาก ${result.total} ข้อ ต้องตอบถูกอย่างน้อย ${result.pass_mark} ข้อ ทบทวนบทเรียนแล้วลองใหม่อีกครั้ง${fresh ? ' โดยจะได้คำถามชุดใหม่' : ''}`}</Thai></p>
          </div>
        </div>
        {earned ? <NewBadge badge={earned} alsoAll={everyChapter} /> : null}
        <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap">
          {final && (result.passed || kept) ? <Link to="/certificate" className={buttonClass('primary')}><IconAward className="size-5" /><Label en="View your certificate" th="ดูประกาศนียบัตรของคุณ" /></Link> : null}
          {result.passed || kept
            ? following ? <Button onClick={() => onOpen(following)}><Label en={`Continue to Chapter ${next!.chapter}`} th={`ไปต่อบทที่ ${next!.chapter}`} /><IconArrowRight className="size-5" /></Button> : <Button variant={final ? 'secondary' : 'primary'} onClick={onBack}><Label en="Back to your training" th="กลับไปหน้าการเรียน" /></Button>
            : start ? <Button onClick={() => onOpen(start)}><Label en={`Review Chapter ${chapter}`} th={`ทบทวนบทที่ ${chapter}`} /></Button> : null}
          {chapter === 9 && (result.passed || kept) && result.final_exam?.locked_by === null && onQuiz ? <Button onClick={() => onQuiz(0)}><Label en="Take the Final Exam" th="ทำแบบทดสอบปลายหลักสูตร" /><IconArrowRight className="size-5" /></Button> : null}
          {kept || (!result.passed && start) ? <Button variant="secondary" onClick={retry}><IconRetake className="size-5" /><Label en="Try again" th="ลองใหม่" /></Button>
            : !result.passed ? <Button onClick={retry}><IconRetake className="size-5" /><Label en="Try again" th="ลองใหม่" /></Button> : null}
          {kept && start ? <Button variant="secondary" onClick={() => onOpen(start)}><Label en={`Review Chapter ${chapter}`} th={`ทบทวนบทที่ ${chapter}`} /></Button> : null}
          {!result.passed && !kept && exempt && following ? <Button variant="secondary" onClick={() => onOpen(following)}><Label en={`Skip to Chapter ${next!.chapter}`} th={`ข้ามไปบทที่ ${next!.chapter}`} /></Button> : null}
        </div>
      </section>
      {/* Every pass shows its answers: retakes after a pass ask only questions a pass has already answered,
        and fixed top-ups from the pool when a bank edit removed some. */}
      {result.passed ? result.results.length ? <section className="space-y-3">
        <h2 className="font-semibold">Your answers</h2>
        <ol className="space-y-3">{quiz.questions.map((question, index) => {
          const graded = result.results.find(entry => entry.id === question.id)
          const choice = question.options.find(option => option.id === graded?.chosen)
          const answer = question.options.find(option => option.id === graded?.answer)
          return <li key={question.id} className="space-y-2 rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-4">
            <div className="flex items-start justify-between gap-3">
              <p className="min-w-0 font-medium">{index + 1}. {question.prompt.en}</p>
              <span className={`inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-semibold ${graded?.correct ? TONES.passed : TONES.retry}`}>{graded?.correct ? <><IconCheck className="size-3.5" />Correct</> : 'Incorrect'}</span>
            </div>
            <p lang="th" className="leading-relaxed text-neutral-800 dark:text-neutral-100">{question.prompt.th}</p>
            {choice ? <p className="text-sm text-neutral-700 dark:text-neutral-300">Your answer: {choice.en}<span lang="th" className="block">คำตอบของคุณ: {choice.th}</span></p> : null}
            {answer && !graded?.correct ? <p className="text-sm text-neutral-900 dark:text-neutral-100">Correct answer: {answer.en} <span lang="th" className="block text-neutral-600 dark:text-neutral-300">คำตอบที่ถูก: {answer.th}</span></p> : null}
            {graded?.explanation ? <p className="border-t border-neutral-100 dark:border-neutral-800 pt-2 text-sm text-neutral-600 dark:text-neutral-300">{graded.explanation.en}<span lang="th" className="mt-1 block">{graded.explanation.th}</span></p> : null}
          </li>
        })}</ol>
      </section> : null
        : <p className="text-sm text-neutral-500 dark:text-neutral-400">{kept ? 'Practice attempts show only the score. Pass again to see the answers.' : 'Correct answers and explanations appear after you pass.'}<Thai className="mt-1">{kept ? 'การฝึกทำซ้ำจะแสดงเฉพาะคะแนน สอบผ่านอีกครั้งเพื่อดูเฉลย' : 'เฉลยและคำอธิบายจะแสดงหลังจากสอบผ่าน'}</Thai></p>}
    </> : null}

    {quiz && quiz.ready === false ? <section className="space-y-4 rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-5">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold">Finish the chapter first<Thai tone="inherit" size="text-base" className="font-medium">เรียนบทนี้ให้ครบก่อน</Thai></h2>
        <p className="text-sm text-neutral-600 dark:text-neutral-300">The quiz opens when you have read every page and watched every video to the end. Skipped parts of a video do not count.
          <Thai className="mt-1">แบบทดสอบจะเปิดให้ทำเมื่อคุณอ่านครบทุกหน้าและดูวิดีโอจนจบทุกคลิป ช่วงที่กรอข้ามจะไม่นับ</Thai></p>
      </div>
      {notice ? <Message text={notice} /> : null}
      {(() => {
        const missing = (quiz.unfinished ?? []).filter(id => !findItem(posts, id)).length
        return missing ? <p className="text-sm text-neutral-600 dark:text-neutral-300">{missing === 1 ? 'One more item was' : `${missing} more items were`} added to this chapter. Go back to your training to see {missing === 1 ? 'it' : 'them'}.</p> : null
      })()}
      <ul className="divide-y divide-neutral-100 dark:divide-neutral-800">{(quiz.unfinished ?? []).map(id => {
        const found = findItem(posts, id)
        if (!found) return null
        const { post, item } = found
        return <li key={id} className="flex flex-wrap items-center justify-between gap-3 py-3">
          <div className="min-w-0 flex-1 basis-48">
            <p className="break-words font-medium">{itemTitle(post, item)}</p>
            <p className="text-sm text-neutral-500 dark:text-neutral-400">{kindLabel(item)} · {progressNote(item)}</p>
          </div>
          {item.derive_status === 'ready' ? <Button variant="secondary" onClick={() => onOpen(post, item)}><Label en="Open" th="เปิด" /><IconArrowRight className="size-5" /></Button>
            // guide.tsx's Preparing note repeats the title shown just above; this row only needs the state.
            : <p className="text-sm text-neutral-600 dark:text-neutral-300">Still being prepared. Check back soon.<Thai>ยังเตรียมไม่เสร็จ กรุณากลับมาดูอีกครั้งในภายหลัง</Thai></p>}
        </li>
      })}</ul>
      {/* Mounted while the checklist shows, so screen readers hear each result as an update. */}
      <p role="status" className="text-sm text-neutral-700 empty:sr-only dark:text-neutral-200">{checked ? <>Checked just now: {(quiz.unfinished ?? []).length === 1 ? 'one item is' : `${(quiz.unfinished ?? []).length} items are`} still not finished.<Thai>ตรวจเมื่อสักครู่: ยังเรียนไม่ครบอีก {(quiz.unfinished ?? []).length} รายการ</Thai></> : null}</p>
      {/* Refetches in place, unlike a retry after a result: the checklist stays and focus stays here.
          Not disabled while checking, which would drop keyboard focus. */}
      <Button variant="secondary" aria-busy={checking} onClick={recheck}><IconRetake className="size-5" />{checking ? <Label en="Checking…" th="กำลังตรวจ…" /> : <Label en="Check again" th="ตรวจอีกครั้ง" />}</Button>
    </section> : null}

    {quiz && quiz.ready !== false && !result ? <form onSubmit={submit} className="space-y-4">
      {quiz.questions.map((question, index) => <div key={question.id} className="rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 p-5">
        <fieldset className="space-y-3" disabled={busy}>
          <legend className="space-y-1">
            <span className="block text-sm font-medium text-neutral-500 dark:text-neutral-400">Question {index + 1} of {quiz.total} <span lang="th">· ข้อ {index + 1} จาก {quiz.total}</span></span>
            <span className="block text-base font-medium text-neutral-900 dark:text-neutral-100">{question.prompt.en}</span>
            <span lang="th" className="mt-1 block text-base leading-relaxed text-neutral-800 dark:text-neutral-100">{question.prompt.th}</span>
          </legend>
          <div className="space-y-2">{question.options.map(option => {
            const selected = chosen[question.id] === option.id
            return <label key={option.id} className={`flex cursor-pointer items-start gap-3 rounded-xl border px-4 py-3 ${selected ? 'border-neutral-900 bg-neutral-50 dark:border-neutral-100 dark:bg-neutral-800' : 'border-neutral-300 dark:border-neutral-700 hover:bg-neutral-50 dark:hover:bg-neutral-800'}`}>
              <input type="radio" name={question.id} value={option.id} checked={selected} onChange={() => setChosen(current => ({ ...current, [question.id]: option.id }))} className="mt-1 size-4 shrink-0 accent-neutral-900 dark:accent-neutral-100" />
              <span className="min-w-0"><span className="block text-base">{option.en}</span><span lang="th" className="mt-0.5 block text-base leading-relaxed text-neutral-700 dark:text-neutral-200">{option.th}</span></span>
            </label>
          })}</div>
        </fieldset>
      </div>)}
      <Button type="submit" disabled={busy || answered < quiz.total} className="w-full">
        {busy ? 'Checking…' : answered < quiz.total ? <Label en={`Answer all ${quiz.total} questions (${answered} of ${quiz.total})`} th={`ตอบให้ครบ ${quiz.total} ข้อ (ตอบแล้ว ${answered} ข้อ)`} /> : <Label en="Submit answers" th="ส่งคำตอบ" />}
      </Button>
    </form> : null}
  </main>
}
