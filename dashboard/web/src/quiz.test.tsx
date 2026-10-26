import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { api, setToken, type Post, type Quiz, type QuizResult, type QuizStanding, type TrainingPerson } from './api'
import { App } from './app'
import { Grid } from './grid'
import { lockedBy, QuizPage } from './quiz'
import { Contents, modules } from './contents'
import { Reader } from './reader'
import { navigate, parse } from './routes'
import { calls, canvasOps, installApi, post, viewPosts, world, type World } from './testing'
import { TrainingDashboard } from './training-progress'

afterEach(() => vi.restoreAllMocks())

const category = (chapter: number) => `SOP Chapter ${chapter}: Topic ${chapter}`

function chapterPost(chapter: number, locked: number | null = null, pages = 2): Post {
  return { ...post(`c${chapter}`, `Chapter ${chapter} handout`, pages), category: category(chapter), chapter, locked_by: locked, published_at: 100 + chapter }
}

function standing(chapter: number, overrides: Partial<QuizStanding> = {}): QuizStanding {
  return { chapter, category: category(chapter), total: 5, pass_mark: 4, attempts: 0, best_score: null, last_score: null,
    passed: false, passed_at: null, last_attempt_at: null, locked_by: null, unfinished: [], ...overrides }
}

function quiz(chapter: number, exempt = false): Quiz {
  return {
    chapter, category: category(chapter), version: 'v1', total: 5, pass_mark: 4, exempt, standing: standing(chapter), ready: true, unfinished: [],
    questions: [1, 2, 3, 4, 5].map(n => ({
      id: `c${chapter}q${n}`, prompt: { en: `Prompt ${n}`, th: `คำถาม ${n}` },
      options: ['a', 'b', 'c', 'd'].map(id => ({ id, en: `Choice ${n}${id}`, th: `ตัวเลือก ${n}${id}` })),
    })),
  }
}

/** Option a is always right; two chapters, the second locked behind the first quiz. */
function journey(): World {
  const state = world({
    posts: [{ ...post('m1', 'Service basics', 2), category: 'Module 1', chapter: null, locked_by: null, published_at: 1 }, chapterPost(1), chapterPost(2, 1)],
    quizzes: [standing(1), standing(2, { locked_by: 1 })],
    quizBank: { 1: quiz(1), 2: quiz(2) },
  })
  let attempts = 0
  state.grade = (chapter, answers): QuizResult => {
    const score = Object.values(answers).filter(choice => choice === 'a').length
    const passed = score >= 4
    attempts += 1
    if (passed) state.posts = state.posts.map(candidate => candidate.chapter === 2 ? { ...candidate, locked_by: null } : candidate)
    return {
      chapter, score, total: 5, pass_mark: 4, passed,
      // Like the server: a failed attempt reports only its score.
      results: passed ? Object.entries(answers).map(([id, chosen]) => ({ id, chosen, correct: chosen === 'a', answer: 'a',
        explanation: { en: 'Guests come first.', th: 'แขกมาก่อนเสมอ' } })) : [],
      quizzes: [standing(1, { attempts, best_score: score, last_score: score, passed }), standing(2, { locked_by: passed ? null : 1 })],
      next_chapter: { chapter: 2, category: category(2), unlocked: passed },
    }
  }
  return state
}

function answer(question: number, option: string) {
  fireEvent.click(screen.getByRole('radio', { name: new RegExp(`Choice ${question}${option}`) }))
}

test('the earliest unpassed earlier quiz locks a chapter, never for exempt roles or modules', () => {
  const quizzes = [standing(1, { passed: true }), standing(2), standing(3)]
  expect(lockedBy(3, quizzes, false)).toBe(2)
  expect(lockedBy(2, quizzes, false)).toBeNull()
  expect(lockedBy(3, quizzes, true)).toBeNull()
  expect(lockedBy(null, quizzes, false)).toBeNull()
  expect(parse('/quiz/3')).toEqual({ name: 'quiz', chapter: 3 })
  expect(parse('/quiz/three')).toEqual({ name: 'grid' })
})

// Value: protects=only a pass earns a badge: a failed first attempt shows no new badge or burst, and the pass after it does;
//   fails_when=QuizPage's earned check stops requiring result.passed, celebrating a failed attempt;
//   why_new=failure tests never looked for a badge, and the badge tests always graded a pass;
//   seam=none
test('a reader fails, retries and passes a chapter quiz, then continues into the unlocked chapter', async () => {
  const state = journey()
  installApi(state)
  setToken('session-token')
  render(<App />)

  await screen.findByText('0 of 2 passed')
  expect(screen.getByRole('button', { name: 'Chapter 2 quiz: Locked' })).toHaveProperty('disabled', true)
  screen.getByText('Finish Chapter 1 and pass its quiz to unlock')
  const locked = screen.getByRole('button', { name: 'Chapter 2 handout, locked until the Chapter 1 quiz is passed' })
  within(locked).getByText('Locked')

  fireEvent.click(screen.getByRole('button', { name: 'Chapter 1 quiz: Not taken' }))
  await screen.findByRole('heading', { name: category(1) })
  screen.getByText('5 questions · Answer 4 correctly (80%) to pass and unlock the next chapter.')
  screen.getByText('คำถาม 1')
  const send = screen.getByRole('button', { name: /^Answer\ all\ 5\ questions\ \(0\ of\ 5\)/ })
  expect(send).toHaveProperty('disabled', true)

  for (const n of [1, 2, 3]) answer(n, 'a')
  for (const n of [4, 5]) answer(n, 'b')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('Not passed yet')
  screen.getByText('3 of 5 correct. You need 4 to pass. Review the chapter, then try again.')
  screen.getByText('Correct answers and explanations appear after you pass.')
  expect(screen.queryByText('Guests come first.')).toBeNull()
  // A failed attempt earns no badge and no burst; the first pass below does.
  expect(screen.queryByText(/New badge/)).toBeNull()
  expect(document.querySelector('.confetti')).toBeNull()
  const sent = JSON.parse(calls.filter(call => call.url.endsWith('/api/quizzes/1/attempts'))[0].body)
  expect(sent).toEqual({ version: 'v1', answers: { c1q1: 'a', c1q2: 'a', c1q3: 'a', c1q4: 'b', c1q5: 'b' } })

  fireEvent.click(screen.getByRole('button', { name: /^Try\ again/ }))
  await screen.findByRole('button', { name: /^Answer\ all\ 5\ questions\ \(0\ of\ 5\)/ })
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'a')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('You passed!')
  screen.getByText('Chapter 2 is unlocked. It works the same way: read every page, watch every video, then pass its quiz.')
  screen.getByText('New badge: Chapter 1')
  expect(screen.getAllByText('Guests come first.')).toHaveLength(5)

  fireEvent.click(screen.getByRole('button', { name: /Continue to Chapter 2/ }))
  await waitFor(() => expect(viewPosts().map(call => call.url)).toEqual([expect.stringContaining('/api/items/item-c2/views')]))
  expect(screen.queryByText('This chapter is locked')).toBeNull()
})

test('a stale quiz asks the reader to start again with the updated questions', async () => {
  const state = journey()
  state.refuse = { status: 409, error: 'quiz_changed', message: 'This quiz was updated. Please start it again.', path: /attempts$/ }
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'a')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('This quiz was updated. Please start it again.')
  fireEvent.click(screen.getByRole('button', { name: /^Start\ again/ }))
  await screen.findByRole('button', { name: /^Answer\ all\ 5\ questions\ \(0\ of\ 5\)/ })
  expect(calls.filter(call => call.url.endsWith('/api/quizzes/1'))).toHaveLength(2)
})

test('the end of a chapter leads to its quiz, and a locked chapter shows how to unlock it', async () => {
  const state = journey()
  installApi(state)
  const onQuiz = vi.fn()
  const first = chapterPost(1, null, 1)
  const view = render(<Reader post={first} posts={[first, chapterPost(2, 1)]} quizzes={state.quizzes} onQuiz={onQuiz} onBack={() => {}} onOpen={() => {}} />)
  fireEvent.click(await screen.findByRole('button', { name: /Take the Chapter 1 quiz/ }))
  expect(onQuiz).toHaveBeenCalledWith(1)
  view.unmount()

  const before = viewPosts().length
  render(<Reader post={chapterPost(2, 1)} posts={[first, chapterPost(2, 1)]} quizzes={state.quizzes} onQuiz={onQuiz} onBack={() => {}} />)
  screen.getByText('This chapter is locked')
  screen.getByText(`Finish Chapter 1 and pass its quiz to unlock ${category(2)}. Answer at least 4 of 5 questions correctly.`)
  fireEvent.click(screen.getAllByRole('button', { name: /^Take\ the\ Chapter\ 1\ quiz/ })[0])
  expect(onQuiz).toHaveBeenLastCalledWith(1)
  expect(viewPosts()).toHaveLength(before)
})

test('admins, managers and C-suite see quizzes as optional and can skip ahead', async () => {
  const state = journey()
  state.quizzes = [standing(1), standing(2)]
  state.posts = state.posts.map(candidate => ({ ...candidate, locked_by: null }))
  installApi(state)
  render(<Grid posts={state.posts} quizzes={state.quizzes} exempt onQuiz={() => {}} onOpen={() => {}} />)
  screen.getByText('Quizzes are optional for your role, so every chapter is open. You can still take them, and your results are recorded. Staff must read every page and watch every video in a chapter, then pass its quiz, before the next chapter opens.')
  screen.getByRole('button', { name: 'Chapter 2 quiz: Optional' })
  // Every chapter is open for exempt roles: no lock lines, and the heading says the quizzes are optional.
  expect(screen.queryByText(/pass its quiz to unlock|Opens after Chapter/)).toBeNull()
  screen.getByText('Optional for your role.')

  const onQuiz = vi.fn()
  const first = chapterPost(1, null, 1)
  render(<Reader post={first} posts={[first, chapterPost(2)]} quizzes={state.quizzes} exempt onQuiz={onQuiz} onBack={() => {}} onOpen={() => {}} />)
  await screen.findByRole('button', { name: /Take the Chapter 1 quiz \(optional\)/ })

  const onOpen = vi.fn()
  state.quizBank = { 1: quiz(1, true) }
  render(<QuizPage chapter={1} posts={state.posts} exempt onFinished={() => {}} onOpen={onOpen} onBack={() => {}} />)
  await screen.findByText('This quiz is optional for your role. Your result is still recorded.')
  fireEvent.click(screen.getByRole('button', { name: /Skip to Chapter 2/ }))
  expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'c2' }))
})

test('staff progress shows quizzes passed, roles and per-chapter attempts', async () => {
  const person: TrainingPerson = {
    email: 'asha@example.invalid', name: 'Asha', role: 'manager', percent: 50, status: 'in_progress', completed_items: 0,
    total_items: 1, opened_items: 1, last_activity_at: 100, items: [], quizzes_passed: 1, quiz_exempt: true,
    quizzes: [standing(1, { passed: true, attempts: 2, best_score: 5, last_score: 5, passed_at: 1_700_000_000, last_attempt_at: 1_700_000_000 }),
      standing(2, { attempts: 3, best_score: 3, last_score: 3, last_attempt_at: 1_700_000_000 })],
  }
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people: [person], total_items: 1, history_incomplete: false, quiz_total: 2 })
  render(<TrainingDashboard />)
  const row = (await screen.findByText('Asha')).closest('tr')!
  within(row).getByText('Manager')
  within(row).getByText('Optional')
  expect(row.textContent).toContain('1 / 2')
  screen.getByRole('option', { name: 'Fewest quizzes passed' })
  fireEvent.click(screen.getByRole('button', { name: 'View progress for Asha' }))
  screen.getByText('Chapter quizzes for Asha')
  screen.getByText('1 of 2 passed · Optional for this role')
  screen.getByText(/^Best 5\/5 · passed .* · 2 attempts$/)
  screen.getByText(/^Best 3\/5 · needs 4 · 3 attempts · last /)
})

test('a failed retake after a pass is practice, keeps the way forward, and moves focus to the result', async () => {
  const state = journey()
  state.quizBank = { 1: { ...quiz(1), standing: standing(1, { passed: true, attempts: 1, best_score: 5 }) } }
  const grade = state.grade!
  state.grade = (chapter, answers) => {
    const result = grade(chapter, answers)
    return { ...result, quizzes: [standing(1, { passed: true, attempts: 2, best_score: 5, last_score: result.score }), standing(2)], next_chapter: { chapter: 2, category: category(2), unlocked: true } }
  }
  installApi(state)
  const onOpen = vi.fn()
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={onOpen} onBack={() => {}} />)
  await screen.findByText('You passed this quiz before. Taking it again keeps your pass and asks the questions from your pass again.')
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'b')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  const heading = await screen.findByRole('heading', { name: /^Practice result\s*0 of 5 correct\./ })
  await waitFor(() => expect(document.activeElement).toBe(heading))
  screen.getByText('0 of 5 correct this time. Your earlier pass still counts.')
  fireEvent.click(screen.getByRole('button', { name: /Continue to Chapter 2/ }))
  expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'c2' }))

  fireEvent.click(screen.getByRole('button', { name: /^Try\ again/ }))
  screen.getByText('Loading…')
  expect(screen.queryByRole('radio')).toBeNull()
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { name: category(1) })))
})

test('chapters follow their numbers whatever the name style, before modules and other categories', () => {
  const named = (id: string, category: string, chapter: number | null): Post => ({ ...post(id, id), category, chapter })
  const groups = modules([named('t', 'บทที่ ๓ ครัว', 3), named('c2', 'Chapter 2 Grooming', 2), named('s4', 'SOP Chapter 4: Bar', 4),
    named('m1', 'Module 1', null), named('s1', 'SOP Chapter 1: Welcome', 1), named('g', 'Policies', null)])
  expect(groups.map(group => group.name)).toEqual(['SOP Chapter 1: Welcome', 'Chapter 2 Grooming', 'บทที่ ๓ ครัว', 'SOP Chapter 4: Bar', 'Module 1', 'Policies'])
})

test('the end of a passed chapter does not lead into a chapter locked by one still being prepared', async () => {
  const state = journey()
  installApi(state)
  const first = chapterPost(1, null, 1)
  const preparing = { ...chapterPost(2), items: chapterPost(2).items.map(item => ({ ...item, derive_status: 'converting' as const })) }
  const third = chapterPost(3, 2)
  // As the server lists it, an item still being prepared is not finished: it is the next step, but not one to open yet.
  render(<Reader post={first} posts={[first, preparing, third]} quizzes={[standing(1, { passed: true }), standing(2, { unfinished: ['item-c2'] }), standing(3, { locked_by: 2 })]} onQuiz={() => {}} onBack={() => {}} onOpen={() => {}} />)
  await screen.findByText('Chapter 2 handout is still being prepared. Check back soon.')
  expect(screen.queryByRole('button', { name: /^(Next:|Next (document|module)|Start|Continue|Take the Chapter)/ })).toBeNull()
})

test('a deep link to a quiz that is still locked says why and leads back to the training', async () => {
  const state = journey()
  state.refuse = { status: 423, error: 'chapter_locked', message: 'Finish Chapter 1 and pass its quiz to unlock this chapter. / เรียนบทที่ 1 ให้ครบและสอบผ่านก่อน จึงจะเปิดบทนี้ได้', path: /\/api\/quizzes\/2$/ }
  installApi(state)
  setToken('session-token')
  window.history.pushState(null, '', '/quiz/2')
  render(<App />)
  // The server's "English / ไทย" refusal shows as two lines, the Thai marked as Thai.
  await screen.findByText('Finish Chapter 1 and pass its quiz to unlock this chapter.')
  expect(screen.getByText('เรียนบทที่ 1 ให้ครบและสอบผ่านก่อน จึงจะเปิดบทนี้ได้').getAttribute('lang')).toBe('th')
  // The page is titled from the chapter's own post when the quiz itself is refused.
  screen.getByRole('heading', { name: category(2) })
  expect(screen.queryByRole('radio')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /^Back\ to\ your\ training/ }))
  await screen.findByText('0 of 2 passed')
  expect(window.location.pathname).toBe('/')
})

test('a quiz with no chapter post is titled by its number', async () => {
  installApi(journey())
  render(<QuizPage chapter={7} posts={journey().posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('No such route in the fake server.')
  screen.getByRole('heading', { name: 'Chapter 7' })
})

test('a rate-limited submission keeps the answers so the reader can send them again', async () => {
  const state = journey()
  state.refuse = { status: 429, error: 'rate_limited', message: 'Too many quiz attempts. Please wait a minute and try again.', path: /attempts$/ }
  installApi(state)
  const onFinished = vi.fn()
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={onFinished} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'a')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('Too many quiz attempts. Please wait a minute and try again.')
  expect(screen.queryByRole('button', { name: /^Start\ again/ })).toBeNull()
  expect(onFinished).not.toHaveBeenCalled()

  state.refuse = null
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('You passed!')
  expect(screen.queryByText('Too many quiz attempts. Please wait a minute and try again.')).toBeNull()
  expect(onFinished).toHaveBeenCalledTimes(1)
})

test('passing the last chapter quiz reveals the right answer for each miss and leads back to the training', async () => {
  const state = journey()
  const grade = state.grade!
  state.grade = (chapter, answers) => ({ ...grade(chapter, answers), next_chapter: null })
  installApi(state)
  const onBack = vi.fn()
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={onBack} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4]) answer(n, 'a')
  answer(5, 'c')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('You passed!')
  screen.getByText('Well done. You have reached the last chapter quiz.')
  screen.getByText('Your answer: Choice 5c')
  expect(screen.getAllByText(/^Correct answer:/).map(line => line.textContent)).toEqual(['Correct answer: Choice 5a คำตอบที่ถูก: ตัวเลือก 5a'])
  expect(screen.getAllByText('Correct')).toHaveLength(4)
  expect(screen.queryByText('Correct answers and explanations appear after you pass.')).toBeNull()
  expect(screen.queryByRole('button', { name: /Continue to Chapter/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /^Back\ to\ your\ training/ }))
  expect(onBack).toHaveBeenCalledTimes(1)
})

test('a failed attempt offers the chapter again, and an exempt role may skip ahead after any result', async () => {
  const state = journey()
  state.quizBank = { 1: quiz(1, true) }
  const grade = state.grade!
  state.grade = (chapter, answers) => ({ ...grade(chapter, answers), next_chapter: { chapter: 2, category: category(2), unlocked: true } })
  installApi(state)
  const onOpen = vi.fn()
  render(<QuizPage chapter={1} posts={state.posts} exempt onFinished={() => {}} onOpen={onOpen} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  screen.getByText('5 questions · Answer 4 correctly (80%) to pass.')
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'b')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('Not passed yet')
  fireEvent.click(screen.getByRole('button', { name: /^Review\ Chapter\ 1/ }))
  expect(onOpen).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'c1' }))
  fireEvent.click(screen.getByRole('button', { name: /^Skip\ to\ Chapter\ 2/ }))
  expect(onOpen).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'c2' }))

  fireEvent.click(screen.getByRole('button', { name: /^Try\ again/ }))
  await screen.findByRole('button', { name: /^Answer\ all\ 5\ questions\ \(0\ of\ 5\)/ })
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'a')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('You passed!')
  screen.getByText('Well done.')
  expect(screen.queryByText('Chapter 2 is unlocked.')).toBeNull()
  expect(screen.queryByRole('button', { name: /^Review\ Chapter\ 1/ })).toBeNull()
})

test('a staff member who fails sees no way to skip ahead', async () => {
  const state = journey()
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  expect(screen.queryByRole('button', { name: /Skip to Chapter/ })).toBeNull()
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'b')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('Not passed yet')
  expect(screen.queryByRole('button', { name: /Skip to Chapter|Continue to Chapter/ })).toBeNull()
})

test('a fail with no chapter post on the page still offers Try again, which deals a new hand', async () => {
  // Value: protects=a failed attempt always has a retry when Review Chapter cannot be offered; fails_when=the review-first reorder drops the primary Try again fallback for a chapter with no loaded post; why_new=every fail test has a chapter post; seam=none
  installApi(journey())
  render(<QuizPage chapter={1} posts={[]} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'b')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('Not passed yet')
  expect(screen.queryByRole('button', { name: /Review Chapter/ })).toBeNull()
  const loads = () => calls.filter(call => call.method === 'GET' && call.url.endsWith('/api/quizzes/1')).length
  const before = loads()
  fireEvent.click(screen.getByRole('button', { name: /^Try\ again/ }))
  await screen.findByRole('button', { name: /^Answer\ all\ 5\ questions\ \(0\ of\ 5\)/ })
  expect(loads()).toBe(before + 1)
})

test('each chapter in the contents ends with its quiz, disabled while locked', () => {
  const posts = [{ ...post('m1', 'Service basics', 2), category: 'Module 1', chapter: null, locked_by: null }, chapterPost(1), chapterPost(2, 1)]
  const onQuiz = vi.fn()
  const quizzes = [standing(1, { attempts: 2, best_score: 3, last_score: 3 }), standing(2, { locked_by: 1 })]
  const view = render(<Contents posts={posts} onOpen={() => {}} quizzes={quizzes} onQuiz={onQuiz} />)
  expect(screen.getAllByRole('button', { name: /^Chapter [0-9] quiz/ })).toHaveLength(2)
  const first = screen.getByRole('button', { name: /^Chapter 1 quiz/ })
  within(first).getByText('Best 3/5')
  within(first).getByText('5 questions · Pass with 4')
  fireEvent.click(first)
  expect(onQuiz).toHaveBeenCalledWith(1)
  const second = screen.getByRole('button', { name: /^Chapter 2 quiz/ })
  expect(second).toHaveProperty('disabled', true)
  within(second).getByText('Locked')
  view.rerender(<Contents posts={posts} onOpen={() => {}} quizzes={quizzes} />)
  expect(screen.queryByRole('button', { name: /^Chapter [0-9] quiz/ })).toBeNull()
})

test('a chapter the server still locks shows its reason, and a locked chapter hides its documents', async () => {
  const state = journey()
  state.refuse = { status: 423, error: 'chapter_locked', message: 'Finish Chapter 1 and pass its quiz to unlock this chapter. / เรียนบทที่ 1 ให้ครบและสอบผ่านก่อน จึงจะเปิดบทนี้ได้', path: /\/views$/ }
  installApi(state)
  // The wall said open, but the server checks again on every content request.
  const stale = render(<Reader post={chapterPost(2)} posts={state.posts} quizzes={state.quizzes} onQuiz={() => {}} onBack={() => {}} />)
  await screen.findByText('Finish Chapter 1 and pass its quiz to unlock this chapter.')
  within(screen.getByRole('alert')).getByText('เรียนบทที่ 1 ให้ครบและสอบผ่านก่อน จึงจะเปิดบทนี้ได้')
  expect(canvasOps.drawn).toBe(0)
  stale.unmount()

  state.refuse = null
  const before = viewPosts().length
  const second = chapterPost(2, 1)
  const two = { ...second, items: [...second.items, { ...second.items[0], id: 'item-c2b', filename: 'c2b.pdf', sort: 1 }] }
  render(<Reader post={two} posts={[chapterPost(1), two]} quizzes={state.quizzes} onBack={() => {}} />)
  screen.getByText('This chapter is locked')
  expect(screen.queryByRole('button', { name: /Take the Chapter/ })).toBeNull()
  expect(screen.queryByRole('button', { name: 'c2b.pdf' })).toBeNull()
  expect(viewPosts()).toHaveLength(before)
})

test('staff progress explains quizzes without attempts and sorts by fewest passed', async () => {
  const base: TrainingPerson = {
    email: '', name: '', role: 'reader', percent: 0, status: 'not_started', completed_items: 0, total_items: 1, opened_items: 0,
    last_activity_at: null, items: [], quizzes_passed: 0, quiz_exempt: false, quizzes: [],
  }
  const passedAll = [standing(1, { passed: true, attempts: 1, best_score: 5, last_score: 5, passed_at: 1_700_000_000, last_attempt_at: 1_700_000_000 }),
    standing(2, { passed: true, attempts: 1, best_score: 4, last_score: 4, passed_at: 1_700_000_000, last_attempt_at: 1_700_000_000 })]
  const people: TrainingPerson[] = [
    { ...base, email: 'amy@example.invalid', name: 'Amy', role: 'c-suite', quiz_exempt: true, quizzes_passed: 2, quizzes: passedAll },
    { ...base, email: 'bo@example.invalid', name: 'Bo', quizzes_passed: 1, quizzes: [passedAll[0], standing(2)] },
    { ...base, email: 'zed@example.invalid', name: 'Zed', quizzes: [standing(1), standing(2, { locked_by: 1 })] },
  ]
  const load = vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people, total_items: 1, history_incomplete: false, quiz_total: 2 })
  const view = render(<TrainingDashboard />)
  await screen.findByText('Zed')
  const order = () => screen.getAllByRole('rowheader').map(header => header.firstChild!.textContent)
  expect(order()).toEqual(['Amy', 'Bo', 'Zed'])
  screen.getByRole('columnheader', { name: 'Quizzes passed' })
  within(screen.getByText('Amy').closest('tr')!).getByText('C-suite')
  fireEvent.change(screen.getByRole('combobox', { name: 'Sort by' }), { target: { value: 'quizzes' } })
  expect(order()).toEqual(['Zed', 'Bo', 'Amy'])
  fireEvent.click(screen.getByRole('button', { name: 'View progress for Zed' }))
  screen.getByText('No attempts yet')
  screen.getByText('Opens after the Chapter 1 quiz')
  view.unmount()

  // Missing history: an absent attempt is unknown, not a confirmed miss.
  load.mockResolvedValue({ people, total_items: 1, history_incomplete: true, quiz_total: 2 })
  const gaps = render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('button', { name: 'View progress for Zed' }))
  expect(screen.getAllByText('No attempt in available history')).toHaveLength(2)
  screen.getByText(/Quiz passes in the missing history are not shown/)
  gaps.unmount()

  // Without published chapter quizzes the column and sort disappear.
  load.mockResolvedValue({ people: [{ ...base, email: 'zed@example.invalid', name: 'Zed' }], total_items: 1, history_incomplete: false, quiz_total: 0 })
  render(<TrainingDashboard />)
  await screen.findByText('Zed')
  expect(screen.queryByRole('columnheader', { name: 'Quizzes passed' })).toBeNull()
  expect(screen.queryByRole('option', { name: 'Fewest quizzes passed' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'View progress for Zed' }))
  expect(screen.queryByText(/Chapter quizzes for/)).toBeNull()
})

test('a second tap while the answers are being checked sends nothing more', async () => {
  const state = journey()
  installApi(state)
  const respond = globalThis.fetch
  let release: (() => void) | undefined
  let sent = 0
  globalThis.fetch = (async (input, init) => {
    if (String(input).endsWith('/attempts')) {
      sent += 1
      await new Promise<void>(resolve => { release = resolve })
    }
    return respond(input, init)
  }) as typeof fetch
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'a')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  const checking = await screen.findByRole('button', { name: 'Checking…' })
  expect(checking).toHaveProperty('disabled', true)
  expect(screen.getAllByRole('group').every(group => (group as HTMLFieldSetElement).disabled)).toBe(true)
  fireEvent.submit(checking.closest('form')!)
  await waitFor(() => expect(release).toBeDefined())
  release!()
  await screen.findByText('You passed!')
  expect(sent).toBe(1)
})

test('passing unlocks the next chapter at once, before the reading room refreshes', async () => {
  const state = journey()
  const grade = state.grade!
  // The server's refreshed wall would still say locked; only the submit response may unlock it.
  state.grade = (chapter, answers) => { const before = state.posts; const result = grade(chapter, answers); state.posts = before; return result }
  installApi(state)
  const respond = globalThis.fetch
  let walls = 0
  globalThis.fetch = (async (input, init) => {
    if (String(input).endsWith('/api/posts') && ++walls > 1) return new Promise<Response>(() => {})
    return respond(input, init)
  }) as typeof fetch
  setToken('session-token')
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Chapter 1 quiz: Not taken' }))
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'a')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  fireEvent.click(await screen.findByRole('button', { name: /Continue to Chapter 2/ }))
  await waitFor(() => expect(viewPosts().map(call => call.url)).toEqual([expect.stringContaining('/api/items/item-c2/views')]))
  expect(screen.queryByText('This chapter is locked')).toBeNull()
})

test('an expired session clears the previous reader’s quiz results before the next person signs in', async () => {
  const state = journey()
  state.quizzes = [standing(1, { passed: true, attempts: 1, best_score: 5 }), standing(2)]
  installApi(state)
  const respond = globalThis.fetch
  let hang = false
  globalThis.fetch = (async (input, init) => {
    if (hang && String(input).endsWith('/api/posts')) return new Promise<Response>(() => {})
    return respond(input, init)
  }) as typeof fetch
  setToken('session-token')
  render(<App />)
  await screen.findByText('1 of 2 passed')
  state.refuse = { status: 401, error: 'sign_in_required', message: 'Please sign in to continue.', path: /\/api\/quizzes\/1$/ }
  fireEvent.click(screen.getByRole('button', { name: 'Chapter 1 quiz: Passed' }))
  const email = await screen.findByRole('textbox')
  state.refuse = null
  hang = true
  act(() => navigate('/'))
  fireEvent.change(email, { target: { value: 'next.person@example.invalid' } })
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByText('Nothing has been posted yet.')
  expect(screen.queryByText('1 of 2 passed')).toBeNull()
  expect(screen.queryByRole('button', { name: /Chapter 1 handout/ })).toBeNull()
})

test('a chapter split across two category names lists its quiz once, after its last part', () => {
  const thai = { ...chapterPost(2), id: 'c2th', category: `${category(2)} (Thai)` }
  thai.items = thai.items.map(item => ({ ...item, id: 'item-c2th' }))
  render(<Contents posts={[chapterPost(1), chapterPost(2), thai]} onOpen={() => {}} quizzes={[standing(1), standing(2)]} onQuiz={() => {}} />)
  expect(screen.getAllByRole('button', { name: /^Chapter 2 quiz/ })).toHaveLength(1)
  const order = screen.getAllByRole('button').map(button => button.getAttribute('aria-label') ?? button.textContent)
  expect(order.findIndex(name => /^Chapter 2 quiz/.test(name ?? ''))).toBeGreaterThan(order.findIndex(name => name === 'Chapter 2 handout'))
})

test('a quiz result that arrives after sign-out or leaving the page is ignored', async () => {
  const state = journey()
  installApi(state)
  const respond = globalThis.fetch
  let release: (() => void) | undefined
  globalThis.fetch = (async (input, init) => {
    if (String(input).endsWith('/attempts')) {
      await new Promise<void>(resolve => { release = resolve })
    }
    return respond(input, init)
  }) as typeof fetch
  const onFinished = vi.fn()
  setToken('reader-a')
  const page = render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={onFinished} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'a')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await waitFor(() => expect(release).toBeDefined())
  setToken('reader-b')
  release!()
  await waitFor(() => expect(calls.some(call => call.url.endsWith('/attempts'))).toBe(true))
  await new Promise(resolve => setTimeout(resolve, 20))
  expect(onFinished).not.toHaveBeenCalled()
  expect(screen.queryByText('You passed!')).toBeNull()

  release = undefined
  setToken('reader-b')
  page.unmount()
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={onFinished} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'a')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await waitFor(() => expect(release).toBeDefined())
  cleanup()
  release!()
  await new Promise(resolve => setTimeout(resolve, 20))
  expect(onFinished).not.toHaveBeenCalled()
})

test('until the chapter is finished the quiz page lists what is left to open', async () => {
  const state = journey()
  const half = chapterPost(1)
  half.items[0].progress = { opened_pages: [0], history_incomplete: false }
  state.posts = [half, chapterPost(2, 1)]
  state.quizBank = { 1: { ...quiz(1), ready: false, unfinished: ['item-c1'], questions: [] } }
  installApi(state)
  const onOpen = vi.fn()
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={onOpen} onBack={() => {}} />)
  await screen.findByRole('heading', { name: /^Finish the chapter first/ })
  screen.getByText('PDF · 2 pages · 50% done')
  expect(screen.queryByRole('radio')).toBeNull()
  expect(screen.queryByRole('button', { name: /Submit|Answer all/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /^Open/ }))
  expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'c1' }), expect.objectContaining({ id: 'item-c1' }))
})

test('a failed attempt shows only the score, never which answers were wrong', async () => {
  const state = journey()
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3]) answer(n, 'a')
  for (const n of [4, 5]) answer(n, 'b')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('3 of 5 correct. You need 4 to pass. Review the chapter, then try again.')
  expect(screen.queryByRole('heading', { name: 'Your answers' })).toBeNull()
  expect(screen.queryByText('Incorrect')).toBeNull()
  expect(screen.queryByText('Correct')).toBeNull()
  screen.getByText('Correct answers and explanations appear after you pass.')
})

test('a stale page that submits an unfinished chapter reloads to show what is left', async () => {
  const state = journey()
  state.refuse = { status: 409, error: 'chapter_unfinished', message: 'Open every page and watch every video in this chapter before taking its quiz.', path: /attempts$/ }
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'a')
  state.quizBank = { 1: { ...quiz(1), ready: false, unfinished: ['item-c1'], questions: [] } }
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByRole('heading', { name: /^Finish the chapter first/ })
  // English and Thai arrive in one message; the Thai is shown on its own line.
  screen.getByText('Your answers were not submitted. Finish the items below first, then take the quiz again.')
  screen.getByText('ยังไม่ได้ส่งคำตอบ กรุณาเรียนรายการด้านล่างให้ครบก่อน แล้วทำแบบทดสอบอีกครั้ง')
})

test('the end of an item continues the chapter before offering its quiz', async () => {
  const state = journey()
  installApi(state)
  const onOpen = vi.fn()
  const first = { ...chapterPost(1, null, 1), id: 'c1a' }
  first.items = first.items.map(item => ({ ...item, id: 'item-c1a', post_id: 'c1a' }))
  const second = { ...chapterPost(1), id: 'c1b', title: 'Chapter 1 video', published_at: 50 }
  second.items = second.items.map(item => ({ ...item, id: 'item-c1b', post_id: 'c1b' }))
  // Reading order puts the video first, so the handout is the chapter's last item.
  const quizzes = [standing(1, { unfinished: ['item-c1a', 'item-c1b'] })]
  render(<Reader post={first} posts={[second, first]} quizzes={quizzes} onQuiz={() => {}} onBack={() => {}} onOpen={onOpen} />)
  fireEvent.click(await screen.findByRole('button', { name: /Continue the chapter: Chapter 1 video/ }))
  expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'c1b' }), expect.objectContaining({ id: 'item-c1b' }))
})

test('contents and badges say a quiz opens after the chapter is finished', () => {
  render(<Contents posts={[chapterPost(1)]} onOpen={() => {}} quizzes={[standing(1, { unfinished: ['item-c1'] })]} onQuiz={() => {}} />)
  screen.getByText('Finish 1 more item first')
  screen.getByText('Not ready')
})

test('from the training, an unfinished chapter quiz lists what is left and opens it in the reader', async () => {
  const state = journey()
  state.quizzes = [standing(1, { unfinished: ['item-c1'] }), standing(2, { locked_by: 1 })]
  state.quizBank = { 1: { ...quiz(1), ready: false, unfinished: ['item-c1'], questions: [] }, 2: quiz(2) }
  installApi(state)
  setToken('session-token')
  render(<App />)
  await screen.findByText('0 of 2 passed')
  screen.getByText('Tap a number to open that chapter’s quiz. A lock means an earlier quiz comes first.')
  fireEvent.click(screen.getByRole('button', { name: 'Chapter 1 quiz: Not ready' }))
  await screen.findByRole('heading', { name: /^Finish the chapter first/ })
  fireEvent.click(screen.getByRole('button', { name: /^Open/ }))
  await waitFor(() => expect(window.location.pathname).toBe('/post/c1/item/item-c1'))
  await waitFor(() => expect(viewPosts().map(call => call.url)).toEqual([expect.stringContaining('/api/items/item-c1/views')]))
})

test('a locked quiz says so before what is left, and a retry waits for the chapter to be finished', () => {
  const quizzes = [standing(1, { attempts: 1, best_score: 3, last_score: 3, unfinished: ['item-c1', 'item-c1b'] }),
    standing(2, { locked_by: 1, unfinished: ['item-c2'] })]
  render(<Contents posts={[chapterPost(1), chapterPost(2, 1)]} onOpen={() => {}} quizzes={quizzes} onQuiz={() => {}} />)
  const first = screen.getByRole('button', { name: /^Chapter 1 quiz/ })
  within(first).getByText('Finish 2 more items first')
  within(first).getByText('Not ready')
  expect(within(first).queryByText('Best 3/5')).toBeNull()
  const second = screen.getByRole('button', { name: /^Chapter 2 quiz/ })
  expect(second).toHaveProperty('disabled', true)
  within(second).getByText('5 questions · Pass with 4')
  within(second).getByText('Locked')
})

test('the checklist names each unfinished video, photo or attachment and skips any it cannot find', async () => {
  const state = journey()
  const mixed = chapterPost(1)
  const base = mixed.items[0]
  mixed.items = [
    { ...base, id: 'item-v', filename: 'welcome.mp4', kind: 'video', pages: 0, duration_seconds: 120, sort: 0,
      progress: { opened_pages: [], watched_seconds: 30, history_incomplete: false } },
    { ...base, id: 'item-p', filename: 'uniform.jpg', kind: 'photo', pages: 1, sort: 1, progress: { opened_pages: [], history_incomplete: false } },
  ]
  state.posts = [mixed, chapterPost(2, 1)]
  // An item archived since the reading room loaded has nothing to open, so it is left out.
  state.quizBank = { 1: { ...quiz(1), ready: false, unfinished: ['item-v', 'item-p', 'item-gone'], questions: [] } }
  installApi(state)
  const onOpen = vi.fn()
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={onOpen} onBack={() => {}} />)
  await screen.findByRole('heading', { name: /^Finish the chapter first/ })
  screen.getByText('welcome.mp4')
  screen.getByText('Video · 25% done')
  screen.getByText('uniform.jpg')
  screen.getByText('Photo · 0% done')
  const open = screen.getAllByRole('button', { name: /^Open/ })
  expect(open).toHaveLength(2)
  fireEvent.click(open[1])
  expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'c1' }), expect.objectContaining({ id: 'item-p' }))
})

test('the end of a chapter offers its quiz once only the item just read is left, and names other attachments', async () => {
  const state = journey()
  installApi(state)
  const onQuiz = vi.fn()
  const onOpen = vi.fn()
  const last = chapterPost(1, null, 1)
  // The server's list may still name the item on screen, and ids the reading room no longer lists.
  const view = render(<Reader post={last} posts={[last, chapterPost(2, 1)]} quizzes={[standing(1, { unfinished: ['item-c1', 'item-gone'] })]} onQuiz={onQuiz} onBack={() => {}} onOpen={onOpen} />)
  fireEvent.click(await screen.findByRole('button', { name: /Take the Chapter 1 quiz/ }))
  expect(onQuiz).toHaveBeenCalledWith(1)
  view.unmount()

  const earlier = { ...chapterPost(1), id: 'c1a', title: 'Chapter 1 pack', published_at: 50 }
  earlier.items = [{ ...earlier.items[0], id: 'item-a1', post_id: 'c1a', filename: 'rules.pdf' },
    { ...earlier.items[0], id: 'item-a2', post_id: 'c1a', filename: 'checklist.pdf', sort: 1 }]
  render(<Reader post={last} posts={[earlier, last]} quizzes={[standing(1, { unfinished: ['item-a2'] })]} onQuiz={onQuiz} onBack={() => {}} onOpen={onOpen} />)
  fireEvent.click(await screen.findByRole('button', { name: /Continue the chapter: checklist\.pdf/ }))
  expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: 'c1a' }), expect.objectContaining({ id: 'item-a2' }))
  expect(onQuiz).toHaveBeenCalledTimes(1)
})

test('quizzes of different sizes still say they open once the chapter is finished', () => {
  const quizzes = [standing(1), { ...standing(2, { locked_by: 1 }), total: 10, pass_mark: 8 }]
  render(<Grid posts={[chapterPost(1), chapterPost(2, 1)]} quizzes={quizzes} exempt={false} onQuiz={() => {}} onOpen={() => {}} />)
  screen.getByText('In each chapter, read every page of the handout and watch every video to the end. Skipped parts of a video do not count.', { exact: false })
  screen.getByText('Then pass the chapter quiz to unlock the next chapter. If you do not pass, review and try again.')
})

test('a passed quiz with new material asks for it before a retake, and keeps the pass', async () => {
  const state = journey()
  const passed = standing(1, { passed: true, attempts: 1, best_score: 5, last_score: 5, unfinished: ['item-c1'] })
  state.quizBank = { 1: { ...quiz(1), standing: passed, ready: false, unfinished: ['item-c1'], questions: [] } }
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByRole('heading', { name: /^Finish the chapter first/ })
  screen.getByText('You passed this quiz before. Taking it again keeps your pass and asks the questions from your pass again.')
  expect(screen.queryByRole('radio')).toBeNull()
  expect(screen.queryByRole('button', { name: /Submit|Answer all/ })).toBeNull()
})

test('a passed quiz keeps its usual line, and unknown progress reads as not finished', async () => {
  render(<Contents posts={[chapterPost(1)]} onOpen={() => {}} quizzes={[standing(1, { passed: true, unfinished: ['item-c1'] })]} onQuiz={() => {}} />)
  screen.getByText('5 questions · Pass with 4')
  expect(screen.queryByText(/Opens after/)).toBeNull()
  cleanup()
  const state = journey()
  const unknown = chapterPost(1)
  unknown.items[0].progress = { opened_pages: [], history_incomplete: true }
  state.posts = [unknown]
  state.quizBank = { 1: { ...quiz(1), ready: false, unfinished: ['item-c1'], questions: [] } }
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('PDF · 2 pages · not finished')
})

test('during a staggered deploy the new app works with an API that predates 0.7.1', async () => {
  const older = ({ unfinished: _unused, ...rest }: QuizStanding) => rest as QuizStanding
  const state = journey()
  state.quizzes = [older(standing(1)), older(standing(2, { locked_by: 1 }))]
  const { ready: _ready, unfinished: _left, ...oldQuiz } = quiz(1)
  state.quizBank = { 1: oldQuiz as Quiz }
  installApi(state)
  setToken('session-token')
  render(<App />)
  await screen.findByText('0 of 2 passed')
  screen.getByRole('button', { name: 'Chapter 1 quiz: Not taken' })
  fireEvent.click(screen.getByRole('button', { name: 'Chapter 1 quiz: Not taken' }))
  await screen.findByText('คำถาม 1')
  expect(screen.queryByText('Finish the chapter first')).toBeNull()
})

test('a checklist whose items are not on the page yet says where to find them', async () => {
  const state = journey()
  state.quizBank = { 1: { ...quiz(1), ready: false, unfinished: ['item-new'], questions: [] } }
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('One more item was added to this chapter. Go back to your training to see it.')
})

test('a stale checklist asks for a fresh reading room, load errors can be retried, and practice copy is honest', async () => {
  const state = journey()
  state.quizBank = { 1: { ...quiz(1), ready: false, unfinished: ['item-new'], questions: [] } }
  installApi(state)
  const onStale = vi.fn()
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} onStale={onStale} />)
  await waitFor(() => expect(onStale).toHaveBeenCalled())
  cleanup()

  state.refuse = { status: 503, error: 'catalog_unavailable', message: 'Your reading progress could not be checked. Please try again shortly. / ตรวจความคืบหน้าการเรียนไม่สำเร็จ กรุณาลองใหม่อีกครั้งในอีกสักครู่', path: /\/api\/quizzes\/1$/ }
  state.quizBank = { 1: quiz(1) }
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('Your reading progress could not be checked. Please try again shortly.')
  state.refuse = null
  fireEvent.click(screen.getByRole('button', { name: /^Try\ again/ }))
  await screen.findByText('คำถาม 1')
  cleanup()

  const practice = journey()
  practice.quizBank = { 1: { ...quiz(1), standing: standing(1, { passed: true, attempts: 1, best_score: 5 }) } }
  const grade = practice.grade!
  practice.grade = (chapter, answers) => ({ ...grade(chapter, answers), quizzes: [standing(1, { passed: true, attempts: 2, best_score: 5 }), standing(2)] })
  installApi(practice)
  render(<QuizPage chapter={1} posts={practice.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  for (const n of [1, 2, 3, 4, 5]) answer(n, 'b')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  // A failed retake after a pass shows no answers either: they come with a pass.
  await screen.findByText('Practice attempts show only the score. Pass again to see the answers.')
  screen.getByText('การฝึกทำซ้ำจะแสดงเฉพาะคะแนน สอบผ่านอีกครั้งเพื่อดูเฉลย')
})

test('the checklist opens the quiz as soon as refreshed progress finishes the chapter', async () => {
  const state = journey()
  state.quizBank = { 1: { ...quiz(1), ready: false, unfinished: ['item-c1'], questions: [] } }
  installApi(state)
  const page = render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByRole('heading', { name: /^Finish the chapter first/ })
  // The video's last report lands after the page loaded; the reading room refreshes its posts.
  state.quizBank = { 1: quiz(1) }
  page.rerender(<QuizPage chapter={1} posts={[...state.posts]} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('คำถาม 1')
  expect(screen.queryByText('Finish the chapter first')).toBeNull()
})

test('a quiz drawn from a larger pool says every attempt brings new questions', async () => {
  const state = journey()
  state.quizBank = { 1: { ...quiz(1), pool: 12 } }
  installApi(state)
  render(<QuizPage chapter={1} posts={state.posts} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText('5 questions · Answer 4 correctly (80%) to pass and unlock the next chapter. Every attempt has a new set of questions.')
  for (const n of [1, 2, 3]) answer(n, 'a')
  for (const n of [4, 5]) answer(n, 'b')
  fireEvent.click(screen.getByRole('button', { name: /^Submit\ answers/ }))
  await screen.findByText('3 of 5 correct. You need 4 to pass. Review the chapter, then try again with a new set of questions.')
})

test('Final Exam sits between Chapter 9 and Module 1 and respects the separate Final Exam prerequisite', () => {
  const posts = [chapterPost(9), chapterPost(10), { ...post('m1', 'Module one'), category: 'Module 1' }, { ...post('m3', 'Module three'), category: 'Module 3' },
    { ...post('ebook', 'The ASEAN Service Frontier 2026–2028'), category: 'Supplemental material' }]
  const onQuiz = vi.fn()
  const finalExam = standing(0, { category: 'Final Exam', total: 20, pass_mark: 16, locked_by: 9 })
  const mounted = render(<Contents posts={posts} quizzes={[standing(9)]} finalExam={finalExam} exempt onQuiz={onQuiz} onOpen={() => {}} />)
  const entry = screen.getByRole('region', { name: 'Final Exam' })
  const button = within(entry).getByRole('button')
  expect(button).toHaveProperty('disabled', true)
  expect(screen.getByRole('button', { name: 'Chapter 9 handout' }).compareDocumentPosition(entry) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(entry.compareDocumentPosition(screen.getByRole('button', { name: 'Chapter 10 handout' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(entry.compareDocumentPosition(screen.getByRole('button', { name: 'Module one' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Module three' }).compareDocumentPosition(screen.getByRole('button', { name: 'The ASEAN Service Frontier 2026–2028' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  mounted.rerender(<Contents posts={posts} quizzes={[standing(9, { passed: true })]} finalExam={{ ...finalExam, locked_by: null }} exempt onQuiz={onQuiz} onOpen={() => {}} />)
  fireEvent.click(within(entry).getByRole('button'))
  expect(onQuiz).toHaveBeenCalledWith(0)
  expect(parse('/final-exam')).toEqual({ name: 'quiz', chapter: 0 })
  mounted.rerender(<Contents posts={posts} quizzes={[standing(9)]} finalExam={{ ...finalExam, optional: true, locked_by: null }} exempt onQuiz={onQuiz} onOpen={() => {}} />)
  expect(within(entry).getByRole('button')).toHaveProperty('disabled', false)
  within(entry).getByText('Optional')
  within(entry).getByText('Optional for Admins. You can take it without passing Chapter 9.')
})

// Twenty sequential selections across 80 bilingual controls can exceed 5s on a shared CI runner.
test('the final exam shows twenty bilingual questions, grades at 16, and preserves a pass on a failed retake', async () => {
  const state = world()
  const exam = { ...quiz(0), category: 'Final Exam', total: 20, pass_mark: 16,
    questions: Array.from({ length: 20 }, (_, i) => ({ id: `final${i + 1}`, prompt: { en: `Prompt ${i + 1}`, th: `คำถาม ${i + 1}` },
      options: ['a', 'b', 'c', 'd'].map(id => ({ id, en: `Choice ${i + 1}${id}`, th: `ตัวเลือก ${i + 1}${id}` })) })) }
  state.quizBank = { 0: exam }
  state.grade = (_chapter, answers) => ({ chapter: 0, total: 20, pass_mark: 16, score: Object.values(answers).filter(v => v === 'a').length,
    passed: false, results: [], quizzes: [], next_chapter: null,
    final_exam: standing(0, { passed: true, best_score: 16, total: 20, pass_mark: 16 }) })
  installApi(state)
  render(<QuizPage chapter={0} posts={[]} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByRole('heading', { name: 'Final Exam' })
  await screen.findByText('คำถาม 20')
  screen.getByText('20 questions · Answer 16 correctly (80%) to pass.')
  for (let n = 1; n <= 20; n++) answer(n, n <= 15 ? 'a' : 'b')
  fireEvent.click(screen.getByRole('button', { name: /^Submit answers/ }))
  await screen.findByText(/Your earlier pass still counts/)
  screen.getByText(/15 of 20 correct this time/)
  expect(screen.queryByText(/next chapter is unlocked/i)).toBeNull()
  expect(screen.getByText(/Your earlier pass still counts/)).toBeTruthy()
}, 15_000)

test('admin progress shows the final score separately from the nine chapter passes', async () => {
  const person: TrainingPerson = { email: 'learner@example.invalid', name: 'Learner', role: 'reader', percent: 100,
    status: 'complete', completed_items: 9, total_items: 9, opened_items: 9, last_activity_at: 1_700_000_000, items: [],
    quizzes_passed: 9, quiz_exempt: false, quizzes: Array.from({ length: 9 }, (_, i) => standing(i + 1, { passed: true })),
    final_exam: standing(0, { total: 20, pass_mark: 16, attempts: 2, best_score: 16, last_score: 16, passed: true, passed_at: 1_700_000_000 }) }
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people: [person], total_items: 9, history_incomplete: false, quiz_total: 9 })
  render(<TrainingDashboard />)
  await screen.findByRole('columnheader', { name: 'Final Exam' })
  const row = screen.getByRole('rowheader', { name: /Learner/ }).closest('tr')!
  within(row).getByText('Passed')
  screen.getByRole('columnheader', { name: 'Chapter quizzes passed' })
  fireEvent.click(screen.getByRole('button', { name: 'View progress for Learner' }))
  screen.getByRole('heading', { name: /Final Exam/ })
  screen.getByText(/Best 16\/20 · passed/)
  expect(screen.queryByText('10/10')).toBeNull()
})


test('the app carries optional Admin final status from the wall to the exam page', async () => {
  const state = world({ quizExempt: true, quizBank: { 0: { ...quiz(0, true), category: 'Final Exam', total: 20, pass_mark: 16 } } })
  installApi(state)
  vi.spyOn(api, 'posts').mockResolvedValue({ posts: [chapterPost(9)], quizzes: [standing(9)], quiz_exempt: true,
    final_exam: standing(0, { optional: true, total: 20, pass_mark: 16 }) })
  setToken('session-token')
  window.history.replaceState(null, '', '/final-exam')
  render(<App />)
  await screen.findByRole('heading', { name: 'Final Exam' })
  await screen.findByText('This quiz is optional for your role. Your result is still recorded.')
  screen.getByText('20 questions · Answer 16 correctly (80%) to pass.')
})

test('staff reporting retains the optional label after an Admin final attempt', async () => {
  const person: TrainingPerson = { email: 'admin@example.invalid', name: 'Administrator', role: 'admin', percent: 0,
    status: 'not_started', completed_items: 0, total_items: 1, opened_items: 0, last_activity_at: null, items: [],
    final_exam: standing(0, { optional: true, total: 20, pass_mark: 16, attempts: 1, best_score: 15, last_score: 15 }) }
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people: [person], total_items: 1, history_incomplete: false, quiz_total: 0 })
  render(<TrainingDashboard />)
  await screen.findByRole('columnheader', { name: 'Final Exam' })
  screen.getByText('Optional')
})
