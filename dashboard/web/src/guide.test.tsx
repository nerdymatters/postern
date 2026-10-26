import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { api, type Item, type Post, type QuizStanding } from './api'
import { Contents } from './contents'
import { Grid } from './grid'
import { quizStatus, QuizPage } from './quiz'
import { Message, Thai } from './ui'
import { itemFinished, nextStep } from './guide'
import { Reader } from './reader'
import { calls, installApi, post, world } from './testing'
import { navigate } from './routes'
import { clock, merge, SERVER_FIGURE_WAIT_MS } from './video'

// The onboarding fixes from the 0.8.0 usability study: what new staff are told, and where they are led next.

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

const category = (chapter: number) => `SOP Chapter ${chapter}: Topic ${chapter}`

function module(id: string, opened: number[] = []): Post {
  const lesson = { ...post(id, `${id} handout`, 2), category: 'Module 1', chapter: null, locked_by: null, published_at: 1 }
  lesson.items[0].progress = { opened_pages: opened, history_incomplete: false }
  return lesson
}

function chapter(n: number, locked: number | null = null): Post {
  const handout = { ...post(`c${n}`, `Chapter ${n} handout`, 2), category: category(n), chapter: n, locked_by: locked, published_at: 100 + n }
  handout.items.push({ ...handout.items[0], id: `video-c${n}`, filename: `c${n}.mp4`, kind: 'video', pages: 0, duration_seconds: 100, sort: 1 })
  return handout
}

function standing(n: number, overrides: Partial<QuizStanding> = {}): QuizStanding {
  return { chapter: n, category: category(n), total: 5, pass_mark: 4, attempts: 0, best_score: null, last_score: null,
    passed: false, passed_at: null, last_attempt_at: null, locked_by: null, unfinished: [], ...overrides }
}

// A chapter of one single-page handout, so its end shows at once.
function short(n: number): Post {
  const handout = chapter(n)
  handout.items = [{ ...handout.items[0], pages: 1 }]
  return handout
}

const video = (watched: number, duration = 300): Item => ({ ...post('v', 'Video').items[0], kind: 'video', pages: 0, duration_seconds: duration,
  progress: { opened_pages: [], watched_seconds: watched, history_incomplete: false } })

// --- What counts as finished, and the next step ---

test('an item is finished by the server’s rule: every page, the photo, or the video to within 2 s or 2% of its end', () => {
  expect(itemFinished(video(293))).toBe(false)
  expect(itemFinished(video(294))).toBe(true)
  expect(itemFinished(video(7, 10))).toBe(false)
  expect(itemFinished(video(8, 10))).toBe(true)
  expect(itemFinished({ ...video(0), duration_seconds: undefined })).toBe(true)
  const handout = module('m', [0])
  expect(itemFinished(handout.items[0])).toBe(false)
  handout.items[0].progress = { opened_pages: [0, 1, 7], history_incomplete: false }
  expect(itemFinished(handout.items[0])).toBe(true)
})

test('the next step starts at Chapter 1: each chapter’s items, then its quiz, and the optional Modules last', () => {
  const first = module('m1', [0])
  const posts = [first, chapter(1), chapter(2, 1)]
  const quizzes = [standing(1, { unfinished: ['item-c1', 'video-c1'] }), standing(2, { locked_by: 1, unfinished: ['item-c2'] })]
  // The Modules are optional: an unfinished one never comes before Chapter 1.
  expect(nextStep(posts, quizzes, false)).toMatchObject({ kind: 'item', item: { id: 'item-c1' } })
  // The server's list decides a chapter's order and what counts as finished.
  quizzes[0].unfinished = ['video-c1']
  expect(nextStep(posts, quizzes, false)).toMatchObject({ kind: 'item', item: { id: 'video-c1' } })
  quizzes[0].unfinished = []
  expect(nextStep(posts, quizzes, false)).toEqual({ kind: 'quiz', chapter: 1, required: true })
  // A locked chapter is never offered: until the wall shows the pass, its blocking quiz is the next step.
  quizzes[0].passed = true
  expect(nextStep(posts, quizzes, false)).toEqual({ kind: 'quiz', chapter: 1, required: true })
  posts[2] = chapter(2)
  expect(nextStep(posts, quizzes, false)).toMatchObject({ kind: 'item', item: { id: 'item-c2' }, required: true })
  // With every chapter done, what is left in the Modules comes next, and is optional.
  quizzes[1] = standing(2, { passed: true })
  expect(nextStep(posts, quizzes, false)).toEqual({ kind: 'item', post: first, item: first.items[0], required: false })
  // Quizzes are optional for exempt roles: their next step is only what is left to open.
  const exempt = [module('m1', [0, 1]), chapter(1)]
  exempt[1].items.forEach(item => { item.progress = { opened_pages: [0, 1], watched_seconds: 100, history_incomplete: false } })
  expect(nextStep(exempt, [standing(1)], true)).toBeNull()
})

test('the next step skips an item still being prepared while another can be opened, and never offers it or the quiz', () => {
  const first = chapter(1)
  first.items[0].derive_status = 'converting'
  expect(nextStep([first], [standing(1, { unfinished: ['item-c1', 'video-c1'] })], false)).toMatchObject({ kind: 'item', item: { id: 'video-c1' } })
  // Value: protects=no button leads to an item the reader cannot show, and the quiz stays closed while the server lists it; fails_when=nextStep falls back to the unready item as an item step or offers the quiz; why_new=the fallback returned it as an item to open; seam=none
  expect(nextStep([first], [standing(1, { unfinished: ['item-c1'] })], false)).toEqual({ kind: 'preparing', post: first, item: first.items[0], required: true })
})

// --- The home card ---

test('staff see what to do next and expanded rules in English and Thai even after passing the first quiz', () => {
  const posts = [module('m1'), chapter(1), chapter(2, 1)]
  const onOpen = vi.fn()
  const view = render(<Grid posts={posts} quizzes={[standing(1, { unfinished: ['item-c1', 'video-c1'] }), standing(2, { locked_by: 1 })]} onOpen={onOpen} onQuiz={() => {}} />)
  const next = screen.getByRole('button', { name: /^Continue training/ })
  // Value: protects=Thai-first staff on a screen reader hear the button's Thai label, voiced as Thai; fails_when=Label hides its Thai line from assistive tech (aria-hidden) or drops lang="th"; why_new=every button test matches only the English prefix, which passes either way; seam=none
  expect(screen.getByRole('button', { name: /^Continue training\s*เรียนต่อ$/ })).toBe(next)
  expect(within(next).getByText('เรียนต่อ').getAttribute('lang')).toBe('th')
  fireEvent.click(next)
  expect(onOpen).toHaveBeenCalledWith(posts[1], posts[1].items[0])
  screen.getByText(/SOP Chapter 1: Topic 1 · c1\.pdf/)
  const guide = screen.getByText('How your training works').closest('details')!
  expect(guide.open).toBe(true)
  // The chapters come first, from Chapter 1, which is where Continue starts; the Modules are optional.
  within(guide).getByText(/^Do the SOP chapters in order, one at a time, starting with Chapter 1\./)
  within(guide).getByText('The Modules are optional background lessons. They have no quiz.')
  within(guide).getByText('โมดูลเป็นบทเรียนเสริม ไม่บังคับ และไม่มีแบบทดสอบ')
  // No pool size from this API: no promise of new questions.
  within(guide).getByText('Then take the chapter quiz: 5 questions. Get at least 4 right to unlock the next chapter. If you do not pass, review and try again.')
  // Modules and chapters are labelled where the quizzes start.
  screen.getByText('Modules · optional, no quiz')
  screen.getByText('SOP chapters · each ends with a quiz')
  screen.getByText('They open one at a time, in order.')
  view.unmount()

  // Passing the first quiz does not collapse the guide on the next visit.
  posts[2] = chapter(2)
  render(<Grid posts={posts} quizzes={[standing(1, { passed: true }), standing(2)]} onOpen={() => {}} onQuiz={() => {}} />)
  expect(screen.getByText('How your training works').closest('details')!.open).toBe(true)
})

test('a finished chapter’s next step is its quiz, and exempt roles are told the staff rule', () => {
  const posts = [chapter(1)]
  const onQuiz = vi.fn()
  const view = render(<Grid posts={posts} quizzes={[standing(1)]} onOpen={() => {}} onQuiz={onQuiz} />)
  fireEvent.click(screen.getByRole('button', { name: /^Take the Chapter 1 quiz/ }))
  expect(onQuiz).toHaveBeenCalledWith(1)
  view.unmount()
  render(<Grid posts={posts} quizzes={[standing(1)]} exempt onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByText('Quizzes are optional for your role, but staff must follow these steps.')
})

test('once every chapter is passed the home card says so, and an exempt role is only told it has opened everything', () => {
  // Value: protects=the end of training reads as done for staff and never claims quiz passes for an exempt role; fails_when=the all-done line is dropped or its staff/exempt condition flips; why_new=no test renders the guide with nothing left to do; seam=none
  const done = chapter(1)
  done.items.forEach(item => { item.progress = { opened_pages: [0, 1], watched_seconds: 100, history_incomplete: false } })
  const view = render(<Grid posts={[done]} quizzes={[standing(1, { passed: true, attempts: 1, best_score: 5 })]} onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByText('You have finished every chapter and passed every quiz.')
  screen.getByText('คุณเรียนครบและสอบผ่านทุกบทแล้ว')
  expect(screen.queryByRole('button', { name: /^(Continue training|Take the Chapter)/ })).toBeNull()
  view.unmount()
  render(<Grid posts={[done]} quizzes={[standing(1)]} exempt onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByText('You have opened everything available.')
  expect(screen.queryByText(/passed every quiz/)).toBeNull()
})

test('with every chapter passed, unread Modules are offered as optional rather than as training still to do', () => {
  // Value: protects=staff who passed every chapter are told they are done, and the Modules read as optional; fails_when=the big Continue training button returns for optional Modules after all passes; why_new=owner decision (2026-10-04) on the Codex design finding; seam=none
  const done = chapter(1)
  done.items.forEach(item => { item.progress = { opened_pages: [0, 1], watched_seconds: 100, history_incomplete: false } })
  const lesson = module('m1')
  const onOpen = vi.fn()
  const view = render(<Grid posts={[lesson, done]} quizzes={[standing(1, { passed: true, attempts: 1, best_score: 5 })]} onOpen={onOpen} onQuiz={() => {}} />)
  screen.getByText('You have finished every chapter and passed every quiz.')
  expect(screen.queryByRole('button', { name: /^Continue training/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /^Explore the optional Modules\s*ดูโมดูลเสริม \(ไม่บังคับ\)$/ }))
  expect(onOpen).toHaveBeenCalledWith(lesson, lesson.items[0])
  view.unmount()
  // Before every chapter is passed, Continue training still leads, and through the chapters first.
  render(<Grid posts={[lesson, chapter(1)]} quizzes={[standing(1, { unfinished: ['item-c1'] })]} onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByRole('button', { name: /^Continue training/ })
  expect(screen.queryByRole('button', { name: /^Explore the optional Modules/ })).toBeNull()
})

test('the rules promise new questions only when the pool is larger than an attempt', () => {
  // Value: protects=the home card promises new questions exactly when the API can deal them; fails_when=the pool > total check breaks; why_new=only the no-pool case was asserted; seam=none
  render(<Grid posts={[chapter(1)]} quizzes={[standing(1, { pool: 12 })]} onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByText('Then take the chapter quiz: 5 questions. Get at least 4 right to unlock the next chapter. If you do not pass, review and try again with new questions.')
})

test('when all that is left is still being prepared, the home card says so and offers neither the item nor the quiz', () => {
  // Value: protects=Continue training never opens an item still being prepared, which the reader would swap for another; fails_when=the guide renders a preparing step as an item or the quiz; why_new=the step fell back to the unready item; seam=none
  const first = chapter(1)
  first.items[1].derive_status = 'converting'
  render(<Grid posts={[first]} quizzes={[standing(1, { unfinished: ['video-c1'] })]} onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByText('c1.mp4 is still being prepared. Check back soon.')
  screen.getByText('c1.mp4 ยังเตรียมไม่เสร็จ กรุณากลับมาดูอีกครั้งในภายหลัง')
  expect(screen.queryByRole('button', { name: /^(Continue training|Take the Chapter)/ })).toBeNull()
})

// --- Locks ---

test('locked chapters name the one quiz in the way, and later ones the chapter before them', () => {
  render(<Contents posts={[chapter(1), chapter(2, 1), chapter(3, 1)]} quizzes={[standing(1), standing(2, { locked_by: 1 }), standing(3, { locked_by: 1 })]} onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByText('Finish Chapter 1 and pass its quiz to unlock')
  screen.getByText('Opens after Chapter 2')
  screen.getByText('จะเปิดหลังจากผ่านบทที่ 2')
  // Value: protects=screen-reader users hear the same gate the lock line shows; fails_when=a locked item's aria-label names post.locked_by instead of the nearest unpassed chapter; why_new=only a Chapter 2 item locked by Chapter 1 was named, where both agree; seam=none
  screen.getByRole('button', { name: 'c2.pdf, locked until the Chapter 1 quiz is passed' })
  screen.getByRole('button', { name: 'c3.pdf, locked until the Chapter 2 quiz is passed' })
  screen.getByRole('button', { name: 'c3.mp4, locked until the Chapter 2 quiz is passed' })
})

test('a locked chapter leads back to what is left in the chapter before it, then to its quiz', () => {
  const first = chapter(1)
  const onOpen = vi.fn()
  const onQuiz = vi.fn()
  const view = render(<Reader post={chapter(2, 1)} posts={[first, chapter(2, 1)]} quizzes={[standing(1, { unfinished: ['video-c1'] }), standing(2, { locked_by: 1 })]} onQuiz={onQuiz} onOpen={onOpen} onBack={() => {}} />)
  screen.getByText(`Finish Chapter 1 and pass its quiz to unlock ${category(2)}. Answer at least 4 of 5 questions correctly.`)
  fireEvent.click(screen.getByRole('button', { name: /Continue Chapter 1/ }))
  expect(onOpen).toHaveBeenCalledWith(first, first.items[1])
  expect(onQuiz).not.toHaveBeenCalled()
  view.unmount()
  const ready = render(<Reader post={chapter(2, 1)} posts={[first, chapter(2, 1)]} quizzes={[standing(1), standing(2, { locked_by: 1 })]} onQuiz={onQuiz} onOpen={onOpen} onBack={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: /Take the Chapter 1 quiz/ }))
  expect(onQuiz).toHaveBeenCalledWith(1)
  ready.unmount()
  // Value: protects=the lock card never leads into an item still being prepared, nor to a quiz it keeps closed; fails_when=blockerItem is opened or the quiz offered while the server lists an unready item; why_new=the lock card fell back to the unready item; seam=none
  const preparing = chapter(1)
  preparing.items[1].derive_status = 'converting'
  render(<Reader post={chapter(2, 1)} posts={[preparing, chapter(2, 1)]} quizzes={[standing(1, { unfinished: ['video-c1'] }), standing(2, { locked_by: 1 })]} onQuiz={onQuiz} onOpen={onOpen} onBack={() => {}} />)
  screen.getByText('c1.mp4 is still being prepared. Check back soon.')
  expect(screen.queryByRole('button', { name: /^(Continue Chapter 1|Take the Chapter 1 quiz)/ })).toBeNull()
})

test('a later chapter’s lock says chapters open one at a time and where the reader is', () => {
  render(<Reader post={chapter(3, 1)} posts={[chapter(1), chapter(2, 1), chapter(3, 1)]} quizzes={[standing(1), standing(2, { locked_by: 1 }), standing(3, { locked_by: 1 })]} onQuiz={() => {}} onOpen={() => {}} onBack={() => {}} />)
  screen.getByText(`Chapters open one at a time, in order. ${category(3)} opens after Chapter 2. You are on Chapter 1.`)
  screen.getByText('บทเรียนจะเปิดให้ทีละบทตามลำดับ บทนี้จะเปิดหลังจากผ่านบทที่ 2 ตอนนี้คุณอยู่ที่บทที่ 1')
})

// --- Chapter pages and hand-offs ---

test('every page of a chapter says what is left before its quiz, then offers the quiz, then says it is passed', async () => {
  installApi(world({ posts: [chapter(1)] }))
  const onQuiz = vi.fn()
  const view = render(<Reader post={chapter(1)} posts={[chapter(1)]} quizzes={[standing(1, { unfinished: ['item-c1', 'video-c1'] })]} onQuiz={onQuiz} onBack={() => {}} onOpen={() => {}} />)
  await screen.findByText('Chapter 1 · 2 items left, then the quiz')
  // The chapter's name heads the page, whatever the file is called.
  screen.getByText(category(1), { selector: 'p' })
  view.unmount()
  const ready = render(<Reader post={chapter(1)} posts={[chapter(1)]} quizzes={[standing(1)]} onQuiz={onQuiz} onBack={() => {}} onOpen={() => {}} />)
  screen.getByText('Chapter 1 is finished. Take the quiz to unlock the next chapter.')
  fireEvent.click(screen.getByRole('button', { name: /^Take the quiz/ }))
  expect(onQuiz).toHaveBeenCalledWith(1)
  ready.unmount()
  render(<Reader post={chapter(1)} posts={[chapter(1)]} quizzes={[standing(1, { passed: true })]} onQuiz={onQuiz} onBack={() => {}} onOpen={() => {}} />)
  screen.getByText('Chapter 1 quiz passed.')
})

test('the end of the last SOP chapter, once passed, leads on to the optional Modules', async () => {
  // Value: protects=passing the last chapter offers the optional Modules instead of the end of the content; fails_when=reading order puts the Modules first again or the hand-off drops reading order once nothing is left; why_new=owner decision P2-D3 A puts the chapters first; seam=none
  const last = short(1)
  const lesson = module('m1')
  installApi(world({ posts: [lesson, last] }))
  const onOpen = vi.fn()
  const view = render(<Reader post={last} posts={[lesson, last]} quizzes={[standing(1, { passed: true })]} onQuiz={() => {}} onBack={() => {}} onOpen={onOpen} />)
  const next = await screen.findByRole('button', { name: /^Next: Module 1 \(optional\)/ })
  within(next).getByText('ถัดไป: Module 1 (ไม่บังคับ)')
  fireEvent.click(next)
  expect(onOpen).toHaveBeenCalledWith(lesson)
  view.unmount()
  // Exempt roles browse on in reading order, and are told the same.
  const browsing = render(<Reader post={last} posts={[lesson, last]} quizzes={[standing(1, { passed: true })]} exempt onQuiz={() => {}} onBack={() => {}} onOpen={onOpen} />)
  fireEvent.click(await screen.findByRole('button', { name: /^Next: Module 1 \(optional\)/ }))
  expect(onOpen).toHaveBeenLastCalledWith(lesson)
  browsing.unmount()
  // With the Modules read too, reading order still goes on to them.
  const read = module('m1', [0, 1])
  render(<Reader post={last} posts={[read, last]} quizzes={[standing(1, { passed: true })]} onQuiz={() => {}} onBack={() => {}} onOpen={onOpen} />)
  fireEvent.click(await screen.findByRole('button', { name: /^Next: Module 1 \(optional\)/ }))
  expect(onOpen).toHaveBeenLastCalledWith(read)
})

test('the end of a chapter leads on to the next chapter in reading order, with a Module unread or everything done', async () => {
  // Value: protects=staff who have finished or skipped ahead keep a Next button through the chapters; fails_when=the end of a chapter follows nextStep into the Modules or drops the button when nothing is left; why_new=the hand-off threw reading order away whenever nextStep had nothing required; seam=none
  const posts = [short(1), short(2), short(3), module('m1')]
  const passed = [1, 2, 3].map(n => standing(n, { passed: true, attempts: 1, best_score: 5 }))
  installApi(world({ posts }))
  const onOpen = vi.fn()
  const view = render(<Reader post={posts[1]} posts={posts} quizzes={passed} onQuiz={() => {}} onBack={() => {}} onOpen={onOpen} />)
  const next = await screen.findByRole('button', { name: /^Continue SOP Chapter 3: Topic 3/ })
  within(next).getByText('เรียนบทที่ 3 ต่อ')
  fireEvent.click(next)
  expect(onOpen).toHaveBeenCalledWith(posts[2])
  view.unmount()
  posts[3] = module('m1', [0, 1])
  render(<Reader post={posts[1]} posts={posts} quizzes={passed} onQuiz={() => {}} onBack={() => {}} onOpen={onOpen} />)
  fireEvent.click(await screen.findByRole('button', { name: /^Continue SOP Chapter 3: Topic 3/ }))
  expect(onOpen).toHaveBeenLastCalledWith(posts[2])
})

test('the end of the last Module leads to the reader’s real next step, never back to a chapter already passed', async () => {
  // Value: protects=a Module read out of order hands staff back to where they are in the chapters; fails_when=the hand-off follows reading order or names Chapter 1 to someone who passed it; why_new=the Modules now come last, so their end needs a destination; seam=none
  const last = module('m1')
  last.items[0].pages = 1
  const onOpen = vi.fn()
  const onQuiz = vi.fn()
  const show = (posts: Post[], quizzes: QuizStanding[]) => render(<Reader post={last} posts={posts} quizzes={quizzes} onQuiz={onQuiz} onBack={() => {}} onOpen={onOpen} />)
  // New staff: Chapter 1, which ends with a quiz.
  const fresh = [chapter(1), chapter(2, 1), last]
  installApi(world({ posts: fresh }))
  let view = show(fresh, [standing(1, { unfinished: ['item-c1', 'video-c1'] }), standing(2, { locked_by: 1, unfinished: ['item-c2', 'video-c2'] })])
  const start = await screen.findByRole('button', { name: /^Start SOP Chapter 1: Topic 1 \(ends with a quiz\)/ })
  within(start).getByText('เริ่มบทที่ 1 (มีแบบทดสอบท้ายบท)')
  fireEvent.click(start)
  expect(onOpen).toHaveBeenCalledWith(fresh[0], fresh[0].items[0])
  view.unmount()
  // Chapters 1 to 3 passed: Chapter 4, and nothing about Chapter 1.
  const posts = [chapter(1), chapter(2), chapter(3), chapter(4), last]
  const passed = [1, 2, 3].map(n => standing(n, { passed: true, attempts: 1, best_score: 5 }))
  view = show(posts, [...passed, standing(4, { unfinished: ['item-c4', 'video-c4'] })])
  fireEvent.click(await screen.findByRole('button', { name: /^Start SOP Chapter 4: Topic 4 \(ends with a quiz\)/ }))
  expect(onOpen).toHaveBeenLastCalledWith(posts[3], posts[3].items[0])
  expect(screen.queryByRole('button', { name: /^(Start|Continue) SOP Chapter [1-3]/ })).toBeNull()
  view.unmount()
  // Begun: continue it, at the item still left.
  posts[3].items[0].progress = { opened_pages: [0, 1], history_incomplete: false }
  view = show(posts, [...passed, standing(4, { unfinished: ['video-c4'] })])
  const resume = await screen.findByRole('button', { name: /^Continue SOP Chapter 4: Topic 4/ })
  within(resume).getByText('เรียนบทที่ 4 ต่อ')
  fireEvent.click(resume)
  expect(onOpen).toHaveBeenLastCalledWith(posts[3], posts[3].items[1])
  view.unmount()
  // Finished: its quiz.
  view = show(posts, [...passed, standing(4)])
  fireEvent.click(await screen.findByRole('button', { name: /^Take the Chapter 4 quiz/ }))
  expect(onQuiz).toHaveBeenCalledWith(4)
  view.unmount()
  // Value: protects=material added to a passed chapter never comes before the chapter in the way; fails_when=nextStep takes the server's unfinished list of a passed chapter before the first unpassed one; why_new=the passed-chapter case had no new material; seam=none
  const added = [standing(1, { passed: true, attempts: 1, best_score: 5, unfinished: ['video-c1'] }), ...passed.slice(1)]
  expect(nextStep(posts, [...added, standing(4, { unfinished: ['video-c4'] })], false)).toMatchObject({ kind: 'item', item: { id: 'video-c4' }, required: true })
  view = show(posts, [...added, standing(4, { unfinished: ['video-c4'] })])
  fireEvent.click(await screen.findByRole('button', { name: /^Continue SOP Chapter 4: Topic 4/ }))
  expect(onOpen).toHaveBeenLastCalledWith(posts[3], posts[3].items[1])
  expect(screen.queryByRole('button', { name: /^(Start|Continue) SOP Chapter [1-3]/ })).toBeNull()
  view.unmount()
  // Once every chapter is passed, the new material is an optional step after the last section.
  view = show(posts, [...added, standing(4, { passed: true })])
  fireEvent.click(await screen.findByRole('button', { name: /^Continue SOP Chapter 1: Topic 1/ }))
  expect(onOpen).toHaveBeenLastCalledWith(posts[0], posts[0].items[1])
  view.unmount()
  // With nothing left, that is the end.
  show(posts, [...passed, standing(4, { passed: true })])
  await screen.findByText('You’ve reached the end of the available content.')
  screen.getByText('นี่คือเนื้อหาส่วนสุดท้ายที่มีในตอนนี้')
})

test('the end of the last Module leads exempt roles, and staff on a site without quizzes, to the chapters left unopened', async () => {
  // Value: protects=the Modules coming last never end the reading with chapters unopened; fails_when=the section-leaving rule is skipped for exempt roles or a site without quizzes; why_new=0.7.1 led on to Chapter 1 here, and moving the Modules last lost that for them; seam=none
  const last = module('m1')
  last.items[0].pages = 1
  const posts = [chapter(1), chapter(2), last]
  installApi(world({ posts }))
  const onOpen = vi.fn()
  // Exempt: Chapter 1, with no quiz to mention.
  let view = render(<Reader post={last} posts={posts} quizzes={[standing(1), standing(2)]} exempt onQuiz={() => {}} onBack={() => {}} onOpen={onOpen} />)
  let start = await screen.findByRole('button', { name: /^Start SOP Chapter 1: Topic 1/ })
  within(start).getByText('Start SOP Chapter 1: Topic 1')
  within(start).getByText('เริ่มบทที่ 1')
  expect(screen.queryByText('You’ve reached the end of the available content.')).toBeNull()
  fireEvent.click(start)
  expect(onOpen).toHaveBeenCalledWith(posts[0], posts[0].items[0])
  view.unmount()
  // No quizzes at all: the same.
  view = render(<Reader post={last} posts={posts} onBack={() => {}} onOpen={onOpen} />)
  start = await screen.findByRole('button', { name: /^Start SOP Chapter 1: Topic 1/ })
  within(start).getByText('Start SOP Chapter 1: Topic 1')
  within(start).getByText('เริ่มบทที่ 1')
  fireEvent.click(start)
  expect(onOpen).toHaveBeenLastCalledWith(posts[0], posts[0].items[0])
  view.unmount()
  // Everything else read: that is the end.
  for (const handout of posts.slice(0, 2)) handout.items.forEach(item => { item.progress = { opened_pages: [0, 1], watched_seconds: 100, history_incomplete: false } })
  render(<Reader post={last} posts={posts} quizzes={[standing(1), standing(2)]} exempt onQuiz={() => {}} onBack={() => {}} onOpen={onOpen} />)
  await screen.findByText('You’ve reached the end of the available content.')
  screen.getByText('นี่คือเนื้อหาส่วนสุดท้ายที่มีในตอนนี้')
})

test('the end of a Module leads to the next Module, and the end of the last one to an optional Module left unread', async () => {
  // Value: protects=the Modules read in order, and an unread one is offered after the last; fails_when=Module to Module follows nextStep back to a chapter, or the leftover is dropped; why_new=the boundary rule now follows reading order between Modules; seam=none
  const first = module('m1')
  first.items[0].pages = 1
  const second = { ...module('m2'), category: 'Module 2' }
  second.items[0].pages = 1
  const posts = [chapter(1), first, second]
  installApi(world({ posts }))
  const onOpen = vi.fn()
  // Chapter 1 is still to do, but the Modules go on in order.
  const view = render(<Reader post={first} posts={posts} quizzes={[standing(1, { unfinished: ['item-c1', 'video-c1'] })]} onQuiz={() => {}} onBack={() => {}} onOpen={onOpen} />)
  fireEvent.click(await screen.findByRole('button', { name: /^Next: Module 2 \(optional\)/ }))
  expect(onOpen).toHaveBeenCalledWith(second)
  view.unmount()
  // Chapter 1 passed: the end of Module 2 offers Module 1, still unread.
  render(<Reader post={second} posts={posts} quizzes={[standing(1, { passed: true })]} onQuiz={() => {}} onBack={() => {}} onOpen={onOpen} />)
  fireEvent.click(await screen.findByRole('button', { name: /^Next: Module 1 \(optional\)/ }))
  expect(onOpen).toHaveBeenLastCalledWith(first, first.items[0])
})

test('the end of any item leads to what is left in the chapter, then to its quiz', async () => {
  const handout = chapter(1)
  installApi(world({ posts: [handout] }))
  const onQuiz = vi.fn()
  const onOpen = vi.fn()
  // Reading the handout last: the video, finished earlier, does not stand in the way of the quiz.
  const view = render(<Reader post={handout} posts={[handout]} selectedItem="item-c1" quizzes={[standing(1, { unfinished: ['item-c1'] })]} onQuiz={onQuiz} onOpen={onOpen} onBack={() => {}} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Next' }))
  fireEvent.click(await screen.findByRole('button', { name: /^Take the Chapter 1 quiz/ }))
  expect(onQuiz).toHaveBeenCalledWith(1)
  view.unmount()
  // A video skipped earlier is named at the end of the handout instead of the quiz.
  render(<Reader post={handout} posts={[handout]} selectedItem="item-c1" quizzes={[standing(1, { unfinished: ['item-c1', 'video-c1'] })]} onQuiz={onQuiz} onOpen={onOpen} onBack={() => {}} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Next' }))
  fireEvent.click(await screen.findByRole('button', { name: /^Continue the chapter: c1\.mp4/ }))
  expect(onOpen).toHaveBeenCalledWith(handout, handout.items[1])
})

test('when what is left of a chapter is still being prepared, the end of an item says so, and a link to it does not show another item', async () => {
  // Value: protects=no Next button loops back through an item the reader cannot show, and a deep link to one says why; fails_when=pending falls back to the unready item, the quiz is offered while the server lists it, or the reader silently shows ready[0]; why_new=all three led to the handout in place of the video; seam=none
  const handout = chapter(1)
  handout.items[0].pages = 1
  handout.items[1].derive_status = 'converting'
  installApi(world({ posts: [handout] }))
  const quizzes = [standing(1, { unfinished: ['item-c1', 'video-c1'] })]
  const view = render(<Reader post={handout} posts={[handout]} selectedItem="item-c1" quizzes={quizzes} onQuiz={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('c1.mp4 is still being prepared. Check back soon.')
  screen.getByText('c1.mp4 ยังเตรียมไม่เสร็จ กรุณากลับมาดูอีกครั้งในภายหลัง')
  expect(screen.queryByRole('button', { name: /^(Continue the chapter|Take the Chapter)/ })).toBeNull()
  expect(screen.queryByText('You’ve reached the end of the available content.')).toBeNull()
  view.unmount()
  render(<Reader post={handout} posts={[handout]} selectedItem="video-c1" quizzes={quizzes} onQuiz={() => {}} onOpen={() => {}} onBack={() => {}} />)
  screen.getByText('c1.mp4 is still being prepared. Check back soon.')
  expect(screen.queryByLabelText('c1.pdf, page 1')).toBeNull()
})

test('an item that failed to prepare says an admin needs to upload it again, not that it is being prepared', async () => {
  // Value: protects=a failed item is not promised as coming soon, and no button loops back to it; fails_when=any status but ready reads as "still being prepared"; why_new=a restore marks unreadable originals failed, and they never become ready without a new upload; seam=none
  const handout = chapter(1)
  handout.items[1].derive_status = 'failed'
  installApi(world({ posts: [handout] }))
  const view = render(<Reader post={handout} posts={[handout]} selectedItem="video-c1" quizzes={[standing(1, { unfinished: ['item-c1'] })]} onQuiz={() => {}} onOpen={() => {}} onBack={() => {}} />)
  screen.getByText('c1.mp4 is not available. An admin needs to upload it again.')
  screen.getByText('c1.mp4 เปิดดูไม่ได้ ผู้ดูแลระบบต้องอัปโหลดไฟล์นี้ใหม่')
  expect(screen.queryByText(/still being prepared/)).toBeNull()
  expect(screen.queryByLabelText('c1.pdf, page 1')).toBeNull()
  expect(screen.queryByRole('button', { name: /^(Next|Continue|Take|Start)/ })).toBeNull()
  view.unmount()
  // A post with nothing but failed items says so of the whole post.
  const lost = module('m1')
  lost.items[0].derive_status = 'failed'
  render(<Reader post={lost} posts={[lost]} onBack={() => {}} />)
  screen.getByText('This post is not available. An admin needs to upload it again.')
  screen.getByText('โพสต์นี้เปิดดูไม่ได้ ผู้ดูแลระบบต้องอัปโหลดใหม่')
  expect(screen.queryByText(/still being prepared/)).toBeNull()
  // The contents row says so too, rather than "Preparing".
  cleanup()
  render(<Contents posts={[handout]} quizzes={[standing(1)]} onOpen={() => {}} onQuiz={() => {}} />)
  expect(screen.getByRole('button', { name: /^c1\.mp4/ }).textContent).toMatch(/Unavailable$/)
})

test('the quiz checklist offers no way into an item still being prepared, and says so', async () => {
  // Value: protects=the checklist never sends staff to an item that cannot be opened yet; fails_when=every unfinished item gets an Open button; why_new=the home card, reader and lock card already say so, the checklist did not; seam=none
  const handout = chapter(1)
  handout.items[1].derive_status = 'queued'
  const state = world({ posts: [handout], quizzes: [standing(1, { unfinished: ['item-c1', 'video-c1'] })] })
  state.quizBank = { 1: { chapter: 1, category: category(1), version: 'v1', total: 5, pass_mark: 4, exempt: false,
    standing: standing(1, { unfinished: ['item-c1', 'video-c1'] }), ready: false, unfinished: ['item-c1', 'video-c1'], questions: [] } }
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByRole('heading', { name: /^Finish the chapter first/ })
  expect(screen.getAllByRole('button', { name: /^Open/ })).toHaveLength(1)
  screen.getByText('Still being prepared. Check back soon.')
  screen.getByText('ยังเตรียมไม่เสร็จ กรุณากลับมาดูอีกครั้งในภายหลัง')
})

// --- Videos ---

test('a skipped video says how much was watched instead of offering the quiz', async () => {
  const handout = chapter(1)
  handout.items = [handout.items[1]]
  handout.items[0].progress = { opened_pages: [], watched_seconds: 40, history_incomplete: false }
  installApi(world({ posts: [handout] }))
  vi.spyOn(api, 'video').mockResolvedValue({ path: '/api/video/ticket', url: '/api/video/ticket', email: 'reader@example.invalid', stamp: '2026-09-22 12:00 UTC' })
  vi.spyOn(api, 'videoProgress').mockResolvedValue({ saved: true })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  const onQuiz = vi.fn()
  const view = render(<Reader post={handout} posts={[handout]} quizzes={[standing(1, { unfinished: ['video-c1'] })]} onQuiz={onQuiz} onBack={() => {}} onOpen={() => {}} />)
  await waitFor(() => screen.getByLabelText('Personal watermark'))
  screen.getByText('Watched 0:40 of 1:40. Watch to the end without skipping; skipped parts do not count.')
  vi.useFakeTimers({ shouldAdvanceTime: true })
  fireEvent.ended(screen.getByLabelText('c1.mp4', { selector: 'video' }))
  // The server's figure may still complete it: wait for it before saying anything was skipped. One live
  // region, mounted before the video ends, announces both steps; the visible copies are hidden from it.
  const region = screen.getByRole('status')
  expect(region.textContent).toMatch(/^Checking what you have watched…/)
  act(() => { vi.advanceTimersByTime(SERVER_FIGURE_WAIT_MS + 100) })
  expect(region.textContent).toMatch(/^You skipped part of this video \(40% watched\)\. Watch the rest to open the quiz\./)
  expect(screen.getAllByText('You skipped part of this video (40% watched). Watch the rest to open the quiz.')).toHaveLength(2)
  vi.useRealTimers()
  expect(screen.queryByRole('button', { name: /Take the Chapter 1 quiz/ })).toBeNull()
  view.unmount()
  // Watched to within the end allowance: the quiz is offered as before.
  handout.items[0].progress = { opened_pages: [], watched_seconds: 99, history_incomplete: false }
  const finished = render(<Reader post={handout} posts={[handout]} quizzes={[standing(1, { unfinished: ['video-c1'] })]} onQuiz={onQuiz} onBack={() => {}} onOpen={() => {}} />)
  await waitFor(() => screen.getByLabelText('Personal watermark'))
  fireEvent.ended(screen.getByLabelText('c1.mp4', { selector: 'video' }))
  fireEvent.click(screen.getByRole('button', { name: /Take the Chapter 1 quiz/ }))
  expect(onQuiz).toHaveBeenCalledWith(1)
  finished.unmount()
  // Value: protects=an exempt role's partial watch still offers its optional quiz at once, with no check or skip warning; fails_when=requireFinish loses its !exempt guard; why_new=every video-end test is a staff reader; seam=none
  handout.items[0].progress = { opened_pages: [], watched_seconds: 40, history_incomplete: false }
  onQuiz.mockClear()
  render(<Reader post={handout} posts={[handout]} quizzes={[standing(1)]} exempt onQuiz={onQuiz} onBack={() => {}} onOpen={() => {}} />)
  await waitFor(() => screen.getByLabelText('Personal watermark'))
  fireEvent.ended(screen.getByLabelText('c1.mp4', { selector: 'video' }))
  expect(screen.queryByText(/Checking what you have watched|You skipped part of this video/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /^Take the Chapter 1 quiz \(optional\)/ }))
  expect(onQuiz).toHaveBeenCalledWith(1)
})

test('played ranges merge into the seconds they cover, and times read as minutes and seconds', () => {
  const ranges: number[][] = []
  expect(merge(ranges, [0, 10])).toBe(10)
  expect(merge(ranges, [5, 12])).toBe(12)
  expect(merge(ranges, [20, 25])).toBe(17)
  expect(merge(ranges, [12, 20])).toBe(25)
  expect(ranges).toEqual([[0, 25]])
  expect([clock(0), clock(75), clock(600.9)]).toEqual(['0:00', '1:15', '10:00'])
})

test('a full watch in this visit counts at once, before the server catches up', async () => {
  const handout = chapter(1)
  handout.items = [{ ...handout.items[1], duration_seconds: 10 }]
  installApi(world({ posts: [handout] }))
  vi.spyOn(api, 'video').mockResolvedValue({ path: '/api/video/ticket', url: '/api/video/ticket', email: 'reader@example.invalid', stamp: '2026-09-22 12:00 UTC' })
  vi.spyOn(api, 'videoProgress').mockResolvedValue({ saved: true })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  let wall = 0
  vi.spyOn(performance, 'now').mockImplementation(() => wall)
  const onQuiz = vi.fn()
  render(<Reader post={handout} posts={[handout]} quizzes={[standing(1, { unfinished: ['video-c1'] })]} onQuiz={onQuiz} onBack={() => {}} onOpen={() => {}} />)
  await waitFor(() => screen.getByLabelText('Personal watermark'))
  const player = screen.getByLabelText('c1.mp4', { selector: 'video' }) as HTMLVideoElement
  Object.defineProperty(player, 'paused', { configurable: true, get: () => false })
  fireEvent.play(player)
  for (let second = 1; second <= 10; second++) { wall = second * 1000; player.currentTime = second; fireEvent.timeUpdate(player) }
  screen.getByText('Watched 0:10 of 0:10. Watch to the end without skipping; skipped parts do not count.')
  fireEvent.ended(player)
  fireEvent.click(screen.getByRole('button', { name: /^Take the Chapter 1 quiz/ }))
  expect(onQuiz).toHaveBeenCalledWith(1)
})

test('a watch finished across two visits is confirmed by the server figure during the check, with no skip warning', async () => {
  // Value: protects=a split watch is not reported as skipped; fails_when=the warning latches at the end instead of following the refreshed progress; why_new=other tests set progress before the end; seam=none
  const handout = chapter(1)
  handout.items = [{ ...handout.items[1], progress: { opened_pages: [], watched_seconds: 40, history_incomplete: false } }]
  installApi(world({ posts: [handout] }))
  vi.spyOn(api, 'video').mockResolvedValue({ path: '/api/video/ticket', url: '/api/video/ticket', email: 'reader@example.invalid', stamp: '2026-09-22 12:00 UTC' })
  vi.spyOn(api, 'videoProgress').mockResolvedValue({ saved: true })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  const onQuiz = vi.fn()
  const quizzes = [standing(1, { unfinished: ['video-c1'] })]
  const view = render(<Reader post={handout} posts={[handout]} quizzes={quizzes} onQuiz={onQuiz} onBack={() => {}} onOpen={() => {}} />)
  await waitFor(() => screen.getByLabelText('Personal watermark'))
  vi.useFakeTimers({ shouldAdvanceTime: true })
  fireEvent.ended(screen.getByLabelText('c1.mp4', { selector: 'video' }))
  expect(screen.getByRole('status').textContent).toMatch(/^Checking what you have watched…/)
  // Value: protects=a slow successful split-watch refresh does not falsely report skipping; fails_when=the previous 2.5-second wait returns; why_new=the existing server refresh was immediate; seam=none
  act(() => { vi.advanceTimersByTime(4000) })
  expect(screen.getByRole('status').textContent).toMatch(/^Checking what you have watched…/)
  expect(screen.queryByText(/You skipped part of this video/)).toBeNull()
  const caughtUp = { ...handout, items: [{ ...handout.items[0], progress: { opened_pages: [], watched_seconds: 100, history_incomplete: false } }] }
  view.rerender(<Reader post={caughtUp} posts={[caughtUp]} quizzes={quizzes} onQuiz={onQuiz} onBack={() => {}} onOpen={() => {}} />)
  fireEvent.click(await screen.findByRole('button', { name: /^Take the Chapter 1 quiz/ }))
  expect(onQuiz).toHaveBeenCalledWith(1)
  expect(screen.queryByText(/You skipped part of this video/)).toBeNull()
})

// --- The quiz page ---

test('Check again checks in place, says what it found, and moves to the questions once the chapter is finished', async () => {
  const state = world({ posts: [chapter(1)], quizzes: [standing(1, { unfinished: ['item-c1'] })] })
  state.quizBank = { 1: { chapter: 1, category: category(1), version: 'v1', total: 5, pass_mark: 4, exempt: false,
    standing: standing(1, { unfinished: ['item-c1'] }), ready: false, unfinished: ['item-c1'], questions: [] } }
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByRole('heading', { name: /^Finish the chapter first/ })
  expect(screen.queryByText(/Checked just now/)).toBeNull()
  const check = screen.getByRole('button', { name: /^Check\ again/ })
  check.focus()
  fireEvent.click(check)
  await screen.findByText('Checked just now: one item is still not finished.')
  // Checked in place: the checklist and keyboard focus stay where they were.
  screen.getByRole('button', { name: /^Open/ })
  expect(document.activeElement).toBe(check)
  // Once the chapter is finished, the questions replace the checklist and focus moves to the title.
  state.quizBank[1] = { ...state.quizBank[1], ready: true, unfinished: [], questions: [{ id: 'q1', prompt: { en: 'Question one', th: 'ข้อหนึ่ง' },
    options: [{ id: 'a', en: 'A', th: 'ก' }, { id: 'b', en: 'B', th: 'ข' }] }] }
  fireEvent.click(check)
  await screen.findByText('Question one')
  await waitFor(() => expect(document.activeElement?.tagName).toBe('H1'))
})

test('each check is announced afresh, and a failed check says why and keeps the checklist', async () => {
  // Value: protects=screen readers hear every Check again result, and a failed check is explained without losing the list; fails_when=the status region mounts with its text, keeps identical text between checks, or the catch is dropped; why_new=only one successful check was tested; seam=none
  const state = world({ posts: [chapter(1)], quizzes: [standing(1, { unfinished: ['item-c1'] })] })
  state.quizBank = { 1: { chapter: 1, category: category(1), version: 'v1', total: 5, pass_mark: 4, exempt: false,
    standing: standing(1, { unfinished: ['item-c1'] }), ready: false, unfinished: ['item-c1'], questions: [] } }
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByRole('heading', { name: /^Finish the chapter first/ })
  const region = screen.getByRole('status')
  expect(region.textContent).toBe('')
  const check = screen.getByRole('button', { name: /^Check\ again/ })
  fireEvent.click(check)
  await within(region).findByText(/Checked just now/)
  fireEvent.click(check)
  expect(region.textContent).toBe('')
  await within(region).findByText(/Checked just now/)
  state.refuse = { status: 503, error: 'catalog_unavailable', message: 'Your training could not be loaded. Please try again shortly. / โหลดเนื้อหาการเรียนไม่สำเร็จ กรุณาลองใหม่อีกครั้งในอีกสักครู่', path: /\/api\/quizzes\/1$/ }
  fireEvent.click(check)
  await screen.findByText('Your training could not be loaded. Please try again shortly.')
  screen.getByRole('button', { name: /^Open/ })
  await waitFor(() => expect(check.getAttribute('aria-busy')).toBe('false'))
  expect(region.textContent).toBe('')
})

test('a reload after a check never claims it checked just now', async () => {
  // Value: protects=the checklist only says "Checked just now" after a check; fails_when=the reload reset forgets the checked flag; why_new=no test reloads after a check; seam=none
  const notReady = { chapter: 1, category: category(1), version: 'v1', total: 5, pass_mark: 4, exempt: false, standing: standing(1, { unfinished: ['item-c1'] }), ready: false, unfinished: ['item-c1'], questions: [] }
  const questions = [1, 2, 3, 4, 5].map(n => ({ id: `c1q${n}`, prompt: { en: `Prompt ${n}`, th: `คำถาม ${n}` }, options: ['a', 'b'].map(id => ({ id, en: `Choice ${n}${id}`, th: `ตัวเลือก ${n}${id}` })) }))
  const state = world({ posts: [chapter(1)], quizzes: [standing(1, { unfinished: ['item-c1'] })] })
  state.quizBank = { 1: notReady }
  state.grade = chapter => ({ chapter, score: 1, total: 5, pass_mark: 4, passed: false, results: [], quizzes: [standing(1, { attempts: 1 })], next_chapter: null })
  installApi(state)
  const page = (posts: Post[]) => <QuizPage chapter={1} posts={posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />
  const view = render(page(state.posts))
  fireEvent.click(await screen.findByRole('button', { name: /^Check again/ }))
  await screen.findByText(/Checked just now/)
  state.quizBank = { 1: { ...notReady, ready: true, unfinished: [], questions } }
  view.rerender(page([...state.posts]))
  await screen.findByText('Prompt 1')
  for (const n of [1, 2, 3, 4, 5]) fireEvent.click(screen.getByRole('radio', { name: new RegExp(`Choice ${n}a`) }))
  fireEvent.click(screen.getByRole('button', { name: /^Submit answers/ }))
  await screen.findByText('Not passed yet')
  state.quizBank = { 1: notReady }
  fireEvent.click(screen.getAllByRole('button', { name: /^Try again/ })[0])
  await screen.findByRole('heading', { name: /^Finish the chapter first/ })
  expect(screen.queryByText(/Checked just now/)).toBeNull()
})

test('after a fail, reviewing the chapter comes first, and trying again asks for a new hand', async () => {
  const state = world({ posts: [chapter(1)], quizzes: [standing(1)] })
  const questions = [1, 2, 3, 4, 5].map(n => ({ id: `c1q${n}`, prompt: { en: `Prompt ${n}`, th: `คำถาม ${n}` },
    options: ['a', 'b', 'c', 'd'].map(id => ({ id, en: `Choice ${n}${id}`, th: `ตัวเลือก ${n}${id}` })) }))
  state.quizBank = { 1: { chapter: 1, category: category(1), version: 'v1', total: 5, pass_mark: 4, pool: 12, exempt: false, standing: standing(1), ready: true, unfinished: [], questions } }
  state.grade = chapter => ({ chapter, score: 1, total: 5, pass_mark: 4, passed: false, results: [], quizzes: [standing(1, { attempts: 1, best_score: 1, last_score: 1 })], next_chapter: null })
  installApi(state)
  const onOpen = vi.fn()
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={onOpen} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4, 5]) fireEvent.click(screen.getByRole('radio', { name: new RegExp(`Choice ${n}a`) }))
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('Not passed yet')
  const actions = within(screen.getByText('Not passed yet').closest('section')!).getAllByRole('button').map(button => button.getAttribute('class')?.includes('border') ? 'secondary' : 'primary')
  expect(within(screen.getByText('Not passed yet').closest('section')!).getAllByRole('button').map(button => button.textContent?.split(/[ก-๙]/)[0].trim())).toEqual(['Review Chapter 1', 'Try again'])
  expect(actions).toEqual(['primary', 'secondary'])
  const loads = () => calls.filter(call => call.method === 'GET' && call.url.endsWith('/api/quizzes/1')).length
  const before = loads()
  fireEvent.click(screen.getByRole('button', { name: /^Try\ again/ }))
  await waitFor(() => expect(loads()).toBe(before + 1))
  await screen.findByRole('button', { name: /^Answer\ all\ 5\ questions\ \(0\ of\ 5\)/ })
})

test('a hand answered elsewhere asks for a fresh reading room, so a recorded pass shows', async () => {
  // Value: protects=a 409 quiz_changed refreshes standing so a pass recorded on another screen appears; fails_when=QuizPage stops calling onStale on quiz_changed; why_new=the refresh on quiz_changed is new; seam=none
  const state = world({ posts: [chapter(1)], quizzes: [standing(1)] })
  const questions = [1, 2, 3, 4, 5].map(n => ({ id: `c1q${n}`, prompt: { en: `Prompt ${n}`, th: `คำถาม ${n}` },
    options: ['a', 'b', 'c', 'd'].map(id => ({ id, en: `Choice ${n}${id}`, th: `ตัวเลือก ${n}${id}` })) }))
  state.quizBank = { 1: { chapter: 1, category: category(1), version: 'v1', total: 5, pass_mark: 4, pool: 12, exempt: false, standing: standing(1), ready: true, unfinished: [], questions } }
  state.refuse = { status: 409, error: 'quiz_changed', message: 'These questions were already answered or are no longer open. Please start the quiz again.', path: /attempts$/ }
  installApi(state)
  const onStale = vi.fn()
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} onStale={onStale} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4, 5]) fireEvent.click(screen.getByRole('radio', { name: new RegExp(`Choice ${n}a`) }))
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByRole('button', { name: /^Start\ again/ })
  expect(onStale).toHaveBeenCalled()
})

test('a passing retake after a pass shows its answers, and a passed quiz promises no new questions', async () => {
  // Value: protects=every pass shows its answers, and someone who passed is told a retake asks their pass's questions again instead of a new set; fails_when=passing retakes go score-only again or the new-questions line ignores the pass; why_new=owner decision P2-D1 A deals retakes after a pass from the pass's questions; seam=none
  const state = world({ posts: [chapter(1)], quizzes: [standing(1, { passed: true, attempts: 1, best_score: 5 })] })
  const questions = [1, 2, 3, 4, 5].map(n => ({ id: `c1q${n}`, prompt: { en: `Prompt ${n}`, th: `คำถาม ${n}` },
    options: ['a', 'b', 'c', 'd'].map(id => ({ id, en: `Choice ${n}${id}`, th: `ตัวเลือก ${n}${id}` })) }))
  state.quizBank = { 1: { chapter: 1, category: category(1), version: 'v1', total: 5, pass_mark: 4, pool: 12, exempt: false,
    standing: standing(1, { passed: true }), ready: true, unfinished: [], questions } }
  state.grade = chapter => ({ chapter, score: 5, total: 5, pass_mark: 4, passed: true,
    results: questions.map(question => ({ id: question.id, chosen: 'a', correct: true, answer: 'a', explanation: { en: `Why ${question.id}`, th: `เหตุผล ${question.id}` } })),
    quizzes: [standing(1, { passed: true, attempts: 2, best_score: 5, last_score: 5 })], next_chapter: null })
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('You passed this quiz before. Taking it again keeps your pass and asks the questions from your pass again.')
  screen.getByText('คุณเคยสอบผ่านแบบทดสอบนี้แล้ว ทำซ้ำได้ และผลที่ผ่านแล้วยังนับอยู่ โดยจะได้คำถามเดิมจากครั้งที่สอบผ่าน')
  // The pool is larger than an attempt, but once passed there is no new set to promise.
  screen.getByText('5 questions · Answer 4 correctly (80%) to pass and unlock the next chapter.')
  expect(screen.queryByText(/new set of questions|คำถามชุดใหม่/)).toBeNull()
  for (const n of [1, 2, 3, 4, 5]) fireEvent.click(screen.getByRole('radio', { name: new RegExp(`Choice ${n}a`) }))
  fireEvent.click(screen.getByRole('button', { name: /^Submit answers/ }))
  await screen.findByRole('heading', { name: 'Your answers' })
  expect(screen.getAllByText('Correct')).toHaveLength(5)
  screen.getByText('Why c1q5')
  expect(screen.queryByText(/Retakes show only the score|appear after you pass|new set of questions/)).toBeNull()
})

test('a Thai heading line takes the size it asks for, not the default under it', () => {
  // Value: protects=Thai lines under headings render at heading size; fails_when=Thai keeps its default text-sm beside another size utility, which wins by CSS order; why_new=class order in JSX never decided which size applied; seam=none
  render(<Thai size="text-base">x</Thai>)
  expect(screen.getByText('x').className.split(/\s+/).filter(name => /^text-(xs|sm|base|lg|xl)$/.test(name))).toEqual(['text-base'])
})

test('quiz status has a stable state, and bilingual messages show the Thai on its own line', () => {
  // Value: protects=callers branch on state not display text, and "English / ไทย" messages render as two lines; fails_when=state is dropped or the split misses Thai; why_new=both are new contracts; seam=none
  expect(quizStatus(standing(1, { unfinished: ['x'] }), false)).toMatchObject({ state: 'unfinished', label: 'Not ready' })
  expect(quizStatus(standing(1, { locked_by: 1 }), false).state).toBe('locked')
  expect(quizStatus(standing(1), true).state).toBe('optional')
  render(<Message text="Too many quiz attempts. Please wait a minute. / ทำแบบทดสอบถี่เกินไป" />)
  screen.getByText('Too many quiz attempts. Please wait a minute.')
  expect(screen.getByText('ทำแบบทดสอบถี่เกินไป').getAttribute('lang')).toBe('th')
  render(<Message text="Either / or, in English only" />)
  screen.getByText('Either / or, in English only')
})

// --- Navigation and structure ---

test('moving to another screen starts it at the top', () => {
  const root = document.scrollingElement ?? document.documentElement
  root.scrollTop = 900
  navigate('/quiz/1')
  expect(root.scrollTop).toBe(0)
  navigate('/')
})

test('the reader sidebar uses level-2 section headings under the page, the home list level 3', () => {
  // Value: protects=heading levels never skip a level in the reader sidebar; fails_when=Contents ignores headingLevel; why_new=heading structure was untested; seam=none
  const view = render(<Reader post={chapter(1)} posts={[module('m1'), chapter(1)]} quizzes={[standing(1)]} onQuiz={() => {}} onBack={() => {}} onOpen={() => {}} />)
  expect(screen.getByRole('heading', { name: /^Modules · optional, no quiz/ }).tagName).toBe('H2')
  view.unmount()
  render(<Grid posts={[module('m1'), chapter(1)]} quizzes={[standing(1)]} onOpen={() => {}} onQuiz={() => {}} />)
  expect(screen.getByRole('heading', { name: /^Modules · optional, no quiz/ }).tagName).toBe('H3')
})

test('the contents list the SOP chapters first, then the optional Modules, headed only when both are there', () => {
  // Value: protects=the required chapters come before the optional Modules in the contents, as in "Continue training"; fails_when=modules() sorts the Modules first again or a heading lands on the wrong kind; why_new=owner decision P2-D3 A; the headings were only checked for presence; seam=none
  const policies = { ...module('p1'), category: 'Policies' }
  const quizzes = [standing(1), standing(2, { locked_by: 1 })]
  const view = render(<Contents posts={[module('m1'), chapter(2, 1), policies, chapter(1)]} quizzes={quizzes} onOpen={() => {}} onQuiz={() => {}} />)
  const order = () => [...view.container.querySelectorAll('h3, button[aria-label]')].map(node => node.getAttribute('aria-label') ?? node.textContent!.split(/[ก-๙]/)[0])
  expect(order()).toEqual(['SOP chapters · each ends with a quiz', 'c1.pdf', 'c1.mp4', 'c2.pdf, locked until the Chapter 1 quiz is passed',
    'c2.mp4, locked until the Chapter 1 quiz is passed', 'Modules · optional, no quiz', 'm1 handout', 'p1 handout'])
  screen.getByText('They open one at a time, in order.')
  view.rerender(<Contents posts={[chapter(1), chapter(2, 1)]} quizzes={quizzes} onOpen={() => {}} onQuiz={() => {}} />)
  expect(screen.queryAllByRole('heading')).toHaveLength(0)
  view.rerender(<Contents posts={[module('m1'), policies]} quizzes={quizzes} onOpen={() => {}} onQuiz={() => {}} />)
  expect(screen.queryAllByRole('heading')).toHaveLength(0)
})

test('the final exam comes before optional modules until passed, including for exempt roles', () => {
  const optional = module('optional')
  const finalExam: QuizStanding = { chapter: 0, category: 'Final Exam', total: 20, pass_mark: 16, attempts: 0,
    best_score: null, last_score: null, passed: false, passed_at: null, last_attempt_at: null, locked_by: null }
  for (const exempt of [false, true]) {
    expect(nextStep([optional], [], exempt, undefined, finalExam)).toEqual({ kind: 'quiz', chapter: 0, required: true })
    expect(nextStep([optional], [], exempt, undefined, { ...finalExam, passed: true })?.kind).toBe('item')
    expect(nextStep([optional], [], exempt, undefined, { ...finalExam, locked_by: 9 })?.kind).toBe('item')
  }
})


test('an optional admin final exam never becomes required next work', () => {
  const exam = standing(0, { optional: true, locked_by: null, total: 20, pass_mark: 16 })
  expect(nextStep([module('extra')], [], true, undefined, exam)?.kind).toBe('item')
  expect(nextStep([], [], true, undefined, exam)).toBeNull()
  expect(nextStep([], [], true, undefined, { ...exam, attempts: 1, last_score: 15 })).toBeNull()
})
