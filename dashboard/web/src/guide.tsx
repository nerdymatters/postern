import type { Certificate, Item, Post, QuizStanding } from './api'
import { itemTitle, kindLabel, moduleName, modules } from './contents'
import { CertificateReady } from './honors'
import { findItem } from './quiz'
import { Button, Label, Thai } from './ui'
import { IconArrowRight, IconQuiz } from './PosternIcons'

/** The server's rule for a finished item: every page opened, the photo seen, the video played to
 *  within 2 seconds or 2% of its end. A video without a known length never holds anyone back. */
export function itemFinished(item: Item) {
  if (item.kind === 'video') return videoFinished(item.duration_seconds, item.progress?.watched_seconds ?? 0)
  const total = item.kind === 'photo' ? 1 : item.pages
  return new Set(item.progress?.opened_pages.filter(page => Number.isInteger(page) && page >= 0 && page < total)).size >= total
}

/** The first listed item that can be opened now, else the first still being prepared, which no button
 *  may lead to: it cannot be opened yet, so callers say so instead. Ids this page has not loaded are skipped. */
export function firstUnfinished(posts: Post[], ids: string[] | undefined) {
  const found = (ids ?? []).map(id => findItem(posts, id)).filter(entry => entry !== undefined)
  return found.find(entry => entry.item.derive_status === 'ready') ?? found[0]
}

/** As postern/quiz.py: within END_TOLERANCE seconds or END_FRACTION of the end, whichever is more. */
export const END_TOLERANCE = 2
export const END_FRACTION = .02
export function videoFinished(duration: number | null | undefined, watched: number) {
  return !duration || (watched > 0 && watched >= duration - Math.max(END_TOLERANCE, duration * END_FRACTION))
}

export type Step = ({ kind: 'item' | 'preparing'; post: Post; item: Item } | { kind: 'quiz'; chapter: number }) & {
  /** Staff work in a chapter whose quiz is not passed yet; anything else is optional. */
  required: boolean
}

/** What to do next: first the SOP chapters not yet passed, in order (what is left in each, then its quiz;
 *  for exempt roles every chapter, without quizzes), then material added to a chapter already passed, then
 *  anything left in the optional Modules and other sections. An item still being prepared is a `preparing`
 *  step, never an item to open: in a chapter it keeps the quiz closed, elsewhere it comes only when nothing
 *  else is left. `done` is the item the reader has just reached the end of: its progress may not have
 *  reached the server yet. */
export function nextStep(posts: Post[], quizzes: QuizStanding[], exempt: boolean, done?: string, finalExam?: QuizStanding | null): Step | null {
  // A chapter split across two category names is one chapter.
  const sections = modules(posts).reduce<{ chapter: number | null; posts: Post[] }[]>((all, group) => {
    const last = all.at(-1)
    if (group.chapter !== null && last?.chapter === group.chapter) last.posts.push(...group.posts)
    else all.push({ chapter: group.chapter, posts: [...group.posts] })
    return all
  }, [])
  const standing = (chapter: number | null) => exempt ? undefined : quizzes.find(quiz => quiz.chapter === chapter)
  // The server's list follows the current catalog; it can name items this page has not loaded yet.
  const left = (section: { chapter: number | null; posts: Post[] }) => firstUnfinished(posts, (standing(section.chapter)?.unfinished
    ?? section.posts.flatMap(post => post.items.filter(item => item.derive_status !== 'failed' && !itemFinished(item)).map(item => item.id)))
    .filter(id => id !== done))
  let waiting: Step | undefined
  for (const section of sections) {
    const quiz = standing(section.chapter)
    if (section.chapter === null || quiz?.passed) continue
    // A locked chapter: its blocking quiz comes first (the wall may not have caught up with a pass).
    const blocker = exempt ? null : section.posts.find(post => post.locked_by != null)?.locked_by
    if (blocker != null) return { kind: 'quiz', chapter: blocker, required: true }
    const found = left(section)
    if (found?.item.derive_status === 'ready') return { kind: 'item', ...found, required: !!quiz }
    if (quiz) return found ? { kind: 'preparing', ...found, required: true } : { kind: 'quiz', chapter: quiz.chapter, required: true }
    if (found) waiting ??= { kind: 'preparing', ...found, required: false }
  }
  if (finalExam && !finalExam.optional && !finalExam.passed && finalExam.locked_by === null) return { kind: 'quiz', chapter: 0, required: true }
  for (const section of [...sections.filter(section => standing(section.chapter)?.passed), ...sections.filter(section => section.chapter === null)]) {
    const found = left(section)
    if (found?.item.derive_status === 'ready') return { kind: 'item', ...found, required: false }
    if (found) waiting ??= { kind: 'preparing', ...found, required: false }
  }
  return waiting ?? null
}

/** In place of a way into an item still being prepared. */
export function Preparing({ post, item, className = '' }: { post: Post; item: Item; className?: string }) {
  const title = itemTitle(post, item)
  return <p className={`text-sm text-neutral-700 dark:text-neutral-200 ${className}`}>{title} is still being prepared. Check back soon.
    <Thai>{`${title} ยังเตรียมไม่เสร็จ กรุณากลับมาดูอีกครั้งในภายหลัง`}</Thai></p>
}

/** In place of an item that failed to prepare: it stays unavailable until an admin uploads it again. */
export function Unavailable({ post, item, className = '' }: { post: Post; item: Item; className?: string }) {
  const title = itemTitle(post, item)
  return <p className={`text-sm text-neutral-700 dark:text-neutral-200 ${className}`}>{title} is not available. An admin needs to upload it again.
    <Thai>{`${title} เปิดดูไม่ได้ ผู้ดูแลระบบต้องอัปโหลดไฟล์นี้ใหม่`}</Thai></p>
}

/** The end of an item with nowhere further to go. */
export function EndOfContent() {
  return <p className="text-center text-sm text-neutral-500 dark:text-neutral-400">You’ve reached the end of the available content.
    <Thai className="mt-1">นี่คือเนื้อหาส่วนสุดท้ายที่มีในตอนนี้</Thai></p>
}

/** The first thing new staff see: what to do next, and the rules in English and Thai. */
export function TrainingGuide({ posts, quizzes, exempt, onOpen, onQuiz, finalExam, certificate }: {
  posts: Post[]; quizzes: QuizStanding[]; exempt: boolean; finalExam?: QuizStanding | null; certificate?: Certificate | null
  onOpen: (post: Post, item?: Item) => void; onQuiz?: (chapter: number) => void
}) {
  const step = nextStep(posts, quizzes, exempt, undefined, finalExam)
  const first = quizzes[0]
  const uniform = quizzes.every(quiz => quiz.total === first?.total && quiz.pass_mark === first?.pass_mark)
  const total = uniform ? first?.total : undefined
  const mark = uniform ? first?.pass_mark : undefined
  const hasModules = modules(posts).some(group => group.chapter === null)
  // Promise new questions on a retake only when the pool is larger than an attempt.
  const fresh = !!first?.pool && first.pool > (first.total ?? 0)
  // Every chapter passed: what is left in the optional Modules is offered, not asked for.
  const optional = !exempt && quizzes.length > 0 && quizzes.every(quiz => quiz.passed) && !!step && step.kind !== 'quiz' && step.post.chapter == null
  // Required work comes first. Otherwise the certificate leads: ahead of optional reading, which becomes the secondary
  // button, and after the line saying everything is done when nothing is left.
  const leads = !!certificate && !!step && !step.required && !optional
  return <div className="space-y-4">
    {leads ? <CertificateReady /> : null}
    {optional ? <div className="space-y-3">
      <p className="text-sm text-neutral-700 dark:text-neutral-200">You have finished every chapter and passed every quiz.<Thai>คุณเรียนครบและสอบผ่านทุกบทแล้ว</Thai></p>
      {certificate ? <CertificateReady /> : null}
      {step.kind === 'item' ? <Button variant="secondary" onClick={() => onOpen(step.post, step.item)} className="w-full sm:w-auto">
        <Label en="Explore the optional Modules" th="ดูโมดูลเสริม (ไม่บังคับ)" /><IconArrowRight className="size-5 shrink-0" />
      </Button> : null}
    </div> : step?.kind === 'item' ? <div className="space-y-2">
      <Button variant={leads ? 'secondary' : 'primary'} onClick={() => onOpen(step.post, step.item)} className="w-full text-left">
        <span className="flex w-full items-center justify-between gap-3"><Label en="Continue training" th="เรียนต่อ" /><IconArrowRight className="size-5 shrink-0" /></span>
      </Button>
      <p className="break-words text-sm text-neutral-600 dark:text-neutral-300">Next <span lang="th">· ถัดไป</span>: {moduleName(step.post.category)} · {itemTitle(step.post, step.item)} <span className="text-neutral-500 dark:text-neutral-400">({kindLabel(step.item)})</span></p>
    </div> : step?.kind === 'quiz' && onQuiz ? <Button onClick={() => onQuiz(step.chapter)} className="w-full text-left">
      <span className="flex w-full items-center justify-between gap-3"><span className="flex min-w-0 items-center gap-3"><IconQuiz className="size-5 shrink-0" /><Label en={step.chapter === 0 ? "Take the Final Exam" : `Take the Chapter ${step.chapter} quiz`} th={step.chapter === 0 ? "ทำแบบทดสอบปลายหลักสูตร" : `ทำแบบทดสอบบทที่ ${step.chapter}`} /></span><IconArrowRight className="size-5 shrink-0" /></span>
    </Button> : step?.kind === 'preparing' ? <Preparing post={step.post} item={step.item} /> : !step && posts.length ? <p className="text-sm text-neutral-700 dark:text-neutral-200">{quizzes.length && !exempt ? 'You have finished every chapter and passed every quiz.' : 'You have opened everything available.'}
      <Thai>{quizzes.length && !exempt ? 'คุณเรียนครบและสอบผ่านทุกบทแล้ว' : 'คุณเปิดดูเนื้อหาครบทุกรายการแล้ว'}</Thai></p> : null}
    {certificate && !step ? <CertificateReady /> : null}
    {quizzes.length ? <details open className="group rounded-xl border-2 border-neutral-300 bg-neutral-50 p-4 dark:border-neutral-600 dark:bg-neutral-950">
      <summary className="cursor-pointer text-2xl font-semibold text-neutral-900 dark:text-neutral-100"><span className="inline-block align-top">How your training works<Thai tone="inherit" size="text-xl" className="font-medium">ขั้นตอนการเรียน</Thai></span></summary>
      <ol className="mt-4 space-y-4 text-lg leading-relaxed text-neutral-800 dark:text-neutral-200">
        <Rule n={1} en="Do the SOP chapters in order, one at a time, starting with Chapter 1. In each chapter, read every page of the handout and watch every video to the end. Skipped parts of a video do not count."
          th="เรียนบท SOP ตามลำดับทีละบท เริ่มจากบทที่ 1 ในแต่ละบท อ่านเอกสารให้ครบทุกหน้า และดูวิดีโอให้จบทุกคลิป ช่วงที่กรอข้ามจะไม่นับ" />
        <Rule n={2} en={total && mark ? `Then take the chapter quiz: ${total} questions. Get at least ${mark} right to unlock the next chapter. If you do not pass, review and try again${fresh ? ' with new questions' : ''}.` : 'Then pass the chapter quiz to unlock the next chapter. If you do not pass, review and try again.'}
          th={total && mark ? `จากนั้นทำแบบทดสอบท้ายบท ${total} ข้อ ต้องตอบถูกอย่างน้อย ${mark} ข้อจึงจะเปิดบทถัดไปได้ ถ้ายังไม่ผ่าน ทบทวนแล้วทำใหม่ได้${fresh ? ' โดยจะได้คำถามชุดใหม่' : ''}` : 'จากนั้นทำแบบทดสอบท้ายบทให้ผ่านเพื่อเปิดบทถัดไป ถ้ายังไม่ผ่าน ทบทวนแล้วทำใหม่ได้'} />
        {finalExam && !finalExam.optional ? <Rule n={3} en={`After passing Chapter 9, take the Final Exam: ${finalExam.total} questions. Answer at least ${finalExam.pass_mark} correctly to pass. Your score is saved in Postern.`} th={`หลังผ่านแบบทดสอบบทที่ 9 ทำแบบทดสอบปลายหลักสูตร ${finalExam.total} ข้อ ต้องตอบถูกอย่างน้อย ${finalExam.pass_mark} ข้อจึงจะผ่าน ระบบจะบันทึกคะแนนไว้ใน Postern`} /> : null}
      </ol>
      {hasModules ? <p className="mt-4 text-lg leading-relaxed text-neutral-700 dark:text-neutral-200">The Modules are optional background lessons. They have no quiz.<Thai size="text-lg">โมดูลเป็นบทเรียนเสริม ไม่บังคับ และไม่มีแบบทดสอบ</Thai></p> : null}
      {exempt ? <p className="mt-4 text-lg leading-relaxed text-neutral-600 dark:text-neutral-300">{finalExam?.optional ? 'Chapter quizzes and the Final Exam are optional for Admins. You can open the Final Exam from Contents without passing Chapter 9.' : finalExam ? 'Chapter quizzes are optional for your role. To take the Final Exam, you must first pass Chapter 9.' : 'Quizzes are optional for your role, but staff must follow these steps.'}<Thai size="text-lg">{finalExam?.optional ? 'แบบทดสอบท้ายบทและแบบทดสอบปลายหลักสูตรไม่บังคับสำหรับผู้ดูแลระบบ เปิดแบบทดสอบปลายหลักสูตรจากสารบัญได้โดยไม่ต้องผ่านบทที่ 9 ก่อน' : finalExam ? 'แบบทดสอบท้ายบทไม่บังคับสำหรับตำแหน่งของคุณ หากต้องการทำแบบทดสอบปลายหลักสูตร ต้องผ่านแบบทดสอบบทที่ 9 ก่อน' : 'แบบทดสอบไม่บังคับสำหรับตำแหน่งของคุณ แต่พนักงานทั่วไปต้องเรียนตามขั้นตอนนี้'}</Thai></p> : null}
    </details> : null}
  </div>
}

function Rule({ n, en, th }: { n: number; en: string; th: string }) {
  return <li className="flex gap-3">
    <span aria-hidden="true" className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-full bg-neutral-900 text-sm font-semibold text-white dark:bg-neutral-100 dark:text-neutral-900">{n}</span>
    <span className="min-w-0">{en}<Thai size="text-lg" className="mt-1 leading-relaxed">{th}</Thai></span>
  </li>
}
