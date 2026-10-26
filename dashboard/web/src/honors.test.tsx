import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { api, setToken, type Certificate, type Post, type Quiz, type QuizResult, type QuizStanding, type Session, type TrainingPerson } from './api'
import { App } from './app'
import { dated, day, EASTERN, moment } from './format'
import { Grid } from './grid'
import { badges, BadgesPage, CertificatePage, chapterTitle, NAME_LIMIT } from './honors'
import { QuizPage } from './quiz'
import { parse } from './routes'
import { calls, installApi, post, world } from './testing'
import { TrainingDashboard } from './training-progress'

afterEach(() => vi.restoreAllMocks())

// 4 pm UTC, noon Eastern: the same calendar day in every US time zone.
const OCT_5 = Date.UTC(2026, 9, 5, 16) / 1000
const DAY = 86400
const category = (chapter: number) => `SOP Chapter ${chapter}: Topic ${chapter}`

function standing(chapter: number, overrides: Partial<QuizStanding> = {}): QuizStanding {
  return { chapter, category: category(chapter), total: 5, pass_mark: 4, attempts: 0, best_score: null, last_score: null,
    passed: false, passed_at: null, last_attempt_at: null, locked_by: null, unfinished: [], ...overrides }
}
const passed = (chapter: number, at = OCT_5) => standing(chapter, { passed: true, passed_at: at, attempts: 1, best_score: 5, last_score: 5 })
const exam = (overrides: Partial<QuizStanding> = {}) => standing(0, { category: 'Final Exam', total: 20, pass_mark: 16, locked_by: 9, ...overrides })
const certificate = (overrides: Partial<Certificate> = {}): Certificate =>
  ({ number: 'PT-2026-0AF23CC6', name: 'Maria Santos', awarded_at: OCT_5 + 2 * DAY, name_confirmed: false, ...overrides })

function chapterPost(chapter: number): Post {
  return { ...post(`c${chapter}`, `Chapter ${chapter} handout`, 2), category: category(chapter), chapter, locked_by: null, published_at: 100 + chapter }
}

function quiz(chapter: number, before: QuizStanding): Quiz {
  const size = chapter === 0 ? 20 : 5
  return {
    chapter, category: chapter === 0 ? 'Final Exam' : category(chapter), version: 'v1', total: size, pass_mark: chapter === 0 ? 16 : 4,
    exempt: false, standing: before, ready: true, unfinished: [],
    questions: Array.from({ length: size }, (_, i) => ({ id: `q${i + 1}`, prompt: { en: `Prompt ${i + 1}`, th: `คำถาม ${i + 1}` },
      options: ['a', 'b', 'c', 'd'].map(id => ({ id, en: `Choice ${i + 1}${id}`, th: `ตัวเลือก ${i + 1}${id}` })) })),
  }
}

/** Option a of every question, found by its input rather than by accessible name, which is slow for 80 options.
 *  The title shows before the questions load, so wait for the first one. */
async function answerAll(count: number) {
  await screen.findByText('คำถาม 1')
  for (let n = 1; n <= count; n++) fireEvent.click(document.querySelector(`input[name="q${n}"][value="a"]`)!)
  fireEvent.click(screen.getByRole('button', { name: /^Submit answers/ }))
}

test('badges follow passes: one per chapter quiz, every chapter, and the Final Exam with its certificate', () => {
  const list = badges([standing(3), passed(1, OCT_5), passed(2, OCT_5 + DAY)], exam({ locked_by: 3 }), null)
  expect(list.map(badge => [badge.key, badge.earned])).toEqual([
    ['chapter-1', true], ['chapter-2', true], ['chapter-3', false], ['chapters', false], ['final', false]])
  expect(list[0]).toMatchObject({ title: 'Chapter 1', th: 'บทที่ 1', subtitle: 'Topic 1', earnedAt: OCT_5 })
  const all = badges([passed(1, OCT_5), passed(2, OCT_5 + 3 * DAY)], exam({ passed: true, passed_at: OCT_5 + 4 * DAY }))
  expect(all.find(badge => badge.kind === 'chapters')).toMatchObject({ earned: true, earnedAt: OCT_5 + 3 * DAY })
  expect(all.find(badge => badge.kind === 'final')).toMatchObject({ earned: true, earnedAt: OCT_5 + 4 * DAY })
  // The certificate's date wins; one chapter needs no "every chapter" badge; no exam, no exam badge.
  expect(badges([passed(1)], null, certificate()).map(badge => [badge.key, badge.earnedAt])).toEqual([['chapter-1', OCT_5], ['final', OCT_5 + 2 * DAY]])
  expect(badges([standing(1), standing(2)], null, null).map(badge => badge.key)).toEqual(['chapter-1', 'chapter-2', 'chapters'])
})

test('a badge names its chapter without the chapter number', () => {
  expect(chapterTitle('SOP Chapter 3: Professional Appearance and Grooming Standards')).toBe('Professional Appearance and Grooming Standards')
  expect(chapterTitle('Chapter 4 - Service Flow')).toBe('Service Flow')
  expect(chapterTitle('บทที่ ๕ การดูแลลูกค้า')).toBe('การดูแลลูกค้า')
  expect(chapterTitle('Safety appendix')).toBe('Safety appendix')
  // postern/quiz.py chapter_number() reads both as Chapter 3 (NFKC, format characters dropped).
  expect(chapterTitle('SOP Chapter \uff13: Grooming')).toBe('Grooming')
  expect(chapterTitle('SOP\u200b Chapter 3: Grooming')).toBe('Grooming')
})

/** A device outside Eastern, as the Thai-device test fakes a device language: the zone fills in only when the caller gave none. */
function deviceZone(zone: string) {
  const native = Date.prototype.toLocaleDateString
  vi.spyOn(Date.prototype, 'toLocaleDateString').mockImplementation(function (this: Date, locales?: Intl.LocalesArgument, options?: Intl.DateTimeFormatOptions) {
    return native.call(this, locales, { ...options, timeZone: options?.timeZone ?? zone })
  })
}

// Value: protects=dates stay American on a phone set to Thai, with no Buddhist era; fails_when=format.ts stops passing
//   en-US and the device language applies; why_new=the suite runs under an en-US default, so only a faked Thai device catches it; seam=none
test('a device set to Thai still shows American dates, with no Buddhist era', () => {
  const native = { date: Date.prototype.toLocaleDateString, both: Date.prototype.toLocaleString }
  vi.spyOn(Date.prototype, 'toLocaleDateString').mockImplementation(function (this: Date, locales?: Intl.LocalesArgument, options?: Intl.DateTimeFormatOptions) {
    return native.date.call(this, locales ?? 'th-TH', options)
  })
  vi.spyOn(Date.prototype, 'toLocaleString').mockImplementation(function (this: Date, locales?: Intl.LocalesArgument, options?: Intl.DateTimeFormatOptions) {
    return native.both.call(this, locales ?? 'th-TH', options)
  })
  expect(dated(OCT_5, 'short', EASTERN)).toBe('Oct 5, 2026')
  expect(dated(OCT_5, 'long', EASTERN)).toBe('October 5, 2026')
  // day() and moment() use the device's zone: check the American shape, not the day.
  expect(day(OCT_5)).toMatch(/^Oct \d{1,2}$/)
  expect(moment(OCT_5)).toMatch(/^Oct \d{1,2}(,| at) \d{1,2}:\d{2}\s?[AP]M$/)
})

test('the home page shelves every badge, earned or not, and links to the full list', () => {
  const quizzes = [passed(1), passed(2), standing(3)]
  const { unmount } = render(<Grid posts={[1, 2, 3].map(chapterPost)} quizzes={quizzes} finalExam={exam()} onOpen={() => {}} onQuiz={() => {}} />)
  const shelf = screen.getByRole('region', { name: /Your badges/ })
  within(shelf).getByText('2 of 5 earned')
  within(shelf).getByRole('img', { name: 'Chapter 1 badge: earned Oct 5, 2026' })
  within(shelf).getByRole('img', { name: 'Chapter 3 badge: not earned yet' })
  within(shelf).getByRole('img', { name: 'Final Exam badge: not earned yet' })
  expect(within(shelf).getByRole('link', { name: /See all badges/ }).getAttribute('href')).toBe('/badges')
  expect(screen.queryByRole('link', { name: /View certificate/ })).toBeNull()
  unmount()
  render(<Grid posts={[1, 2, 3].map(chapterPost)} quizzes={[standing(1), standing(2, { locked_by: 1 })]} onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByText('Pass a chapter quiz to earn your first badge.')
})

// Value: protects=a holder can reach the certificate while nothing is published; fails_when=the empty home page drops it;
//   why_new=red team: every other way to /certificate sits on the non-empty home page; seam=none
test('with nothing published, a certificate holder still finds the certificate', () => {
  render(<Grid posts={[]} quizzes={[]} certificate={certificate()} onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByText('Nothing has been posted yet.')
  expect(screen.getByRole('link', { name: /View certificate/ }).getAttribute('href')).toBe('/certificate')
})

test('a site without chapter quizzes or a Final Exam shows no badge shelf', () => {
  render(<Grid posts={[post('p1', 'Tip pooling policy')]} quizzes={[]} onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByRole('heading', { name: /Your training/ })
  expect(screen.queryByRole('region', { name: /Your badges/ })).toBeNull()
})

test('a finished reader sees the certificate first, ahead of the optional Modules', () => {
  const module: Post = { ...post('m1', 'Service basics', 2), category: 'Module 1', chapter: null, locked_by: null, published_at: 1 }
  render(<Grid posts={[chapterPost(1), module]} quizzes={[passed(1)]} finalExam={exam({ passed: true, passed_at: OCT_5, locked_by: null })}
    certificate={certificate()} onOpen={() => {}} onQuiz={() => {}} />)
  const view = screen.getByRole('link', { name: /View certificate/ })
  expect(view.getAttribute('href')).toBe('/certificate')
  const modules = screen.getByRole('button', { name: /Explore the optional Modules/ })
  expect(view.compareDocumentPosition(modules) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  screen.getByText('You have earned every badge. Well done!')
})

// Value: protects=the certificate leads optional reading (Continue training turns secondary) and waits behind required work;
//   fails_when=TrainingGuide places it by the Modules-only flag or after the step; why_new=only the Modules case was tested; seam=none
test('the certificate leads anything optional, waits behind required work, and closes a finished guide', () => {
  const exempt = render(<Grid posts={[chapterPost(1)]} quizzes={[standing(1)]} exempt finalExam={exam({ optional: true, passed: true, passed_at: OCT_5, locked_by: null })}
    certificate={certificate()} onOpen={() => {}} onQuiz={() => {}} />)
  const view = screen.getByRole('link', { name: /View certificate/ })
  const more = screen.getByRole('button', { name: /Continue training/ })
  expect(view.compareDocumentPosition(more) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(more.className).toContain('border')
  exempt.unmount()
  const finished = render(<Grid posts={[{ ...chapterPost(9), items: chapterPost(9).items.map(item => ({ ...item, progress: { opened_pages: [0, 1], history_incomplete: false } })) }]}
    quizzes={[passed(9)]} finalExam={exam({ passed: true, passed_at: OCT_5, locked_by: null })} certificate={certificate()} onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByText('You have finished every chapter and passed every quiz.')
  expect(screen.getByRole('link', { name: /View certificate/ }).getAttribute('href')).toBe('/certificate')
  finished.unmount()
  // A chapter published after the exam is required work again: its quiz leads and the certificate waits.
  render(<Grid posts={[chapterPost(9), chapterPost(10)]} quizzes={[passed(9), standing(10)]} finalExam={exam({ passed: true, passed_at: OCT_5, locked_by: null })}
    certificate={certificate()} onOpen={() => {}} onQuiz={() => {}} />)
  screen.getByRole('button', { name: /Take the Chapter 10 quiz/ })
  expect(screen.queryByRole('link', { name: /View certificate/ })).toBeNull()
})

test('the badges page offers one next badge, says what each badge needs, and how the certificate is earned', () => {
  const onQuiz = vi.fn()
  const quizzes = [passed(1), standing(2), standing(3, { locked_by: 2 })]
  const { unmount } = render(<BadgesPage quizzes={quizzes} finalExam={exam({ locked_by: 9 })} onQuiz={onQuiz} onRefresh={() => {}} onBack={() => {}} />)
  const next = screen.getByRole('region', { name: 'Next badge' })
  within(next).getByText('Chapter 2 · Topic 2')
  within(next).getByText('บทที่ 2')
  fireEvent.click(within(next).getByRole('button', { name: /Take the quiz/ }))
  expect(onQuiz).toHaveBeenCalledWith(2)
  const list = screen.getByRole('list', { name: 'Badges' })
  within(list).getByText('Oct 5, 2026')
  expect(within(list).getAllByText('Pass its quiz to earn it.')).toHaveLength(2)
  screen.getByText(/The Final Exam opens after you pass the Chapter 9 quiz\./)
  expect(screen.queryByRole('button', { name: /Take the Final Exam/ })).toBeNull()
  unmount()
  // An unfinished chapter's quiz page lists what is left; the open exam is one tap away.
  render(<BadgesPage quizzes={[passed(1), standing(2, { unfinished: ['item-c2'] })]} finalExam={exam({ locked_by: null })} onQuiz={onQuiz} onRefresh={() => {}} onBack={() => {}} />)
  screen.getByRole('button', { name: /See what is left/ })
  fireEvent.click(screen.getByRole('button', { name: /Take the Final Exam/ }))
  expect(onQuiz).toHaveBeenLastCalledWith(0)
})

// Value: protects=a passer who opens /badges directly is never told to pass the exam while the reading room loads;
//   fails_when=BadgesPage renders its empty list and NotEarned before the first wall; why_new=route tests only saw the loaded page; seam=none
test('a certificate holder opening /badges is not told to pass the exam while the reading room loads', async () => {
  const state = world({ posts: [chapterPost(9)], quizzes: [passed(9)], finalExam: exam({ passed: true, passed_at: OCT_5, locked_by: null }), certificate: certificate() })
  installApi(state)
  vi.spyOn(api, 'posts').mockImplementation(() => new Promise(() => {}))
  setToken('session-token')
  window.history.replaceState(null, '', '/badges')
  render(<App />)
  await screen.findByRole('heading', { name: /^Your badges/ })
  screen.getByText('Loading…')
  expect(screen.queryByText(/Pass the Final Exam to earn your certificate/)).toBeNull()
  expect(screen.queryByText('No chapter quizzes are published yet.')).toBeNull()
})

test('once every chapter badge is earned, the Final Exam is the next badge', () => {
  const onQuiz = vi.fn()
  render(<BadgesPage quizzes={[passed(1), passed(2)]} finalExam={exam({ locked_by: null })} onQuiz={onQuiz} onRefresh={() => {}} onBack={() => {}} />)
  const next = screen.getByRole('region', { name: 'Next badge' })
  within(next).getByText(/^Final Exam/)
  fireEvent.click(within(next).getByRole('button', { name: /Take the Final Exam/ }))
  expect(onQuiz).toHaveBeenCalledWith(0)
  within(screen.getByRole('list', { name: 'Badges' })).getByText(/^Next/)
})

test('an earned certificate shows on the badges page with the way to print it', () => {
  render(<BadgesPage quizzes={[passed(1), passed(2)]} finalExam={exam({ passed: true, passed_at: OCT_5, locked_by: null })} certificate={certificate()} onQuiz={() => {}} onRefresh={() => {}} onBack={() => {}} />)
  expect(screen.queryByRole('region', { name: 'Next badge' })).toBeNull()
  // Nested under the section's own heading, not a second one beside it.
  within(screen.getByRole('region', { name: /^Certificate of completion/ })).getByRole('heading', { name: 'Certificate of Completion', level: 3 })
  screen.getByText('You passed the Final Exam on Oct 7, 2026.')
  within(screen.getByRole('article', { name: 'Certificate of completion' })).getByText('Maria Santos')
  expect(screen.getByRole('link', { name: /View your certificate/ }).getAttribute('href')).toBe('/certificate')
})

test('a passed exam whose certificate has not arrived is never told to pass the exam again', () => {
  // null: recorded a moment ago; undefined: an API from before certificates, during a deployment.
  for (const missing of [null, undefined]) {
    const refresh = vi.fn()
    const { unmount } = render(<BadgesPage quizzes={[passed(9)]} finalExam={exam({ passed: true, passed_at: OCT_5, locked_by: null })} certificate={missing}
      onQuiz={() => {}} onRefresh={refresh} onBack={() => {}} />)
    screen.getByText('Your certificate is being prepared. It will appear here soon.')
    expect(screen.queryByText(/Pass the Final Exam to earn your certificate/)).toBeNull()
    expect(screen.queryByRole('button', { name: /Take the Final Exam/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Check again/ }))
    expect(refresh).toHaveBeenCalledTimes(1)
    unmount()
  }
})

test('certificate dates are Eastern, like the year in the certificate number', () => {
  // 8:30 pm on New Year's Eve in the restaurants' time zone is already January 1 in UTC, and in Bangkok.
  deviceZone('Asia/Bangkok')
  const eve = Date.UTC(2027, 0, 1, 1, 30) / 1000
  render(<CertificatePage certificate={certificate({ number: 'PT-2026-0AF23CC6', awarded_at: eve })} finalExam={null} loading={false}
    onSaved={() => {}} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  within(screen.getAllByRole('article', { name: 'Certificate of completion' })[0]).getByText('December 31, 2026')
})

test('the certificate page shows the certificate, confirms the printed name and prints it alone', async () => {
  const state = world({ certificate: certificate() })
  installApi(state)
  setToken('session-token')
  const print = vi.spyOn(window, 'print').mockImplementation(() => {})
  const saved = vi.fn()
  render(<CertificatePage certificate={state.certificate} finalExam={exam({ passed: true, passed_at: OCT_5 })} loading={false}
    onSaved={saved} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  const shown = screen.getAllByRole('article', { name: 'Certificate of completion' })[0]
  within(shown).getByText('Maria Santos')
  within(shown).getByText('PT-2026-0AF23CC6')
  within(shown).getByText('October 7, 2026')
  within(shown).getByText('ประกาศนียบัตร')
  screen.getByText('Check your name before you print. Keep it if it is right, or change it, for example to your full name.')
  // The printed copy lives outside the app, so the print stylesheet can hide everything else.
  const printed = document.body.querySelector(':scope > .print-root')!
  within(printed as HTMLElement).getByText('Maria Santos')
  // Landscape only while a certificate is printable: other screens print with the browser's settings.
  expect(printed.querySelector('style')?.textContent).toContain('@page { size: landscape')
  // No page margin, so Chrome leaves out its headers and footers (index.css insets the copy instead).
  expect(printed.querySelector('style')?.textContent).toMatch(/margin:\s*0(?![.\d])/)
  fireEvent.click(screen.getByRole('button', { name: /Print or save as PDF/ }))
  expect(print).toHaveBeenCalledTimes(1)

  fireEvent.click(screen.getByRole('button', { name: /Change name/ }))
  const field = screen.getByRole<HTMLInputElement>('textbox', { name: /Name on your certificate/ })
  expect(document.activeElement).toBe(field)
  expect(field.maxLength).toBe(NAME_LIMIT)
  fireEvent.change(field, { target: { value: '  Maria  Santos Rivera ' } })
  fireEvent.click(screen.getByRole('button', { name: /Save name/ }))
  await screen.findByText(/Saved\. Your certificate shows this name\./)
  expect(JSON.parse(calls.find(call => call.url.endsWith('/api/certificate/name'))!.body)).toEqual({ name: '  Maria  Santos Rivera ' })
  expect(saved).toHaveBeenCalledWith(expect.objectContaining({ name: 'Maria Santos Rivera', name_confirmed: true }))
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: /Change name/ })))
})

// Value: protects=a reader whose suggested name is right confirms it in one tap; fails_when=Keep this name sends anything
//   but the suggested name, or shows once the name is confirmed; why_new=the owner chose a one-tap confirmation; seam=none
test('a reader keeps a correct suggested name in one tap, and the note and button go', async () => {
  installApi(world({ posts: [chapterPost(9)], quizzes: [passed(9)], finalExam: exam({ passed: true, passed_at: OCT_5, locked_by: null }), certificate: certificate() }))
  setToken('session-token')
  window.history.replaceState(null, '', '/certificate')
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /Keep this name/ }))
  await screen.findByText(/Saved\. Your certificate shows this name\./)
  expect(JSON.parse(calls.find(call => call.url.endsWith('/api/certificate/name'))!.body)).toEqual({ name: 'Maria Santos' })
  expect(screen.queryByRole('button', { name: /Keep this name/ })).toBeNull()
  expect(screen.queryByText(/Check your name before you print/)).toBeNull()
  // Focus does not fall to the page when the button goes.
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: /Change name/ })))
})

test('a name that cannot be kept says why, outside the form', async () => {
  installApi(world({ certificate: certificate(), refuse: { status: 429, error: 'rate_limited', message: 'You have changed this name many times today. Please try again tomorrow. / วันนี้แก้ไขชื่อนี้หลายครั้งแล้ว กรุณาลองใหม่พรุ่งนี้', path: /certificate\/name/ } }))
  setToken('session-token')
  const saved = vi.fn()
  render(<CertificatePage certificate={certificate()} finalExam={null} loading={false} onSaved={saved} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: /Keep this name/ }))
  within(await screen.findByRole('alert')).getByText('You have changed this name many times today. Please try again tomorrow.')
  expect(saved).not.toHaveBeenCalled()
  screen.getByRole('button', { name: /Keep this name/ })
  // Opening the form starts it afresh.
  fireEvent.click(screen.getByRole('button', { name: /Change name/ }))
  expect(screen.queryByRole('alert')).toBeNull()
})

// Value: protects=pressing Save again while a name is saving sends nothing more; fails_when=save() stops ignoring presses
//   while busy (Save stays enabled to keep focus); why_new=no test pressed Save twice; seam=none
test('a second press while the name is saving sends nothing more', async () => {
  installApi(world({ certificate: certificate() }))
  setToken('session-token')
  let answer: (saved: Certificate) => void = () => {}
  const save = vi.spyOn(api, 'nameCertificate').mockImplementation(() => new Promise(resolve => { answer = resolve }))
  render(<CertificatePage certificate={certificate()} finalExam={null} loading={false} onSaved={() => {}} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: /Change name/ }))
  fireEvent.change(screen.getByRole('textbox', { name: /Name on your certificate/ }), { target: { value: 'Maria Santos Rivera' } })
  fireEvent.click(screen.getByRole('button', { name: /Save name/ }))
  fireEvent.click(screen.getByRole('button', { name: /Saving/ }))
  expect(save).toHaveBeenCalledTimes(1)
  await act(async () => answer(certificate({ name: 'Maria Santos Rivera', name_confirmed: true })))
})

// Value: protects=a saved certificate is "Training Certificate.pdf" and the page title comes back after; fails_when=
//   PrintableCertificate stops setting or restoring document.title; why_new=the owner named the file; seam=none
test('a certificate saved as a PDF is named Training Certificate, with no personal details', () => {
  const before = document.title
  const { unmount } = render(<CertificatePage certificate={certificate()} finalExam={null} loading={false} onSaved={() => {}} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  expect(document.title).toBe('Training Certificate')
  unmount()
  expect(document.title).toBe(before)
})

test('the certificate page repeats the date and number at a readable size outside the picture', () => {
  render(<CertificatePage certificate={certificate()} finalExam={null} loading={false} onSaved={() => {}} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  const main = screen.getByRole('main')
  const outside = (text: RegExp) => within(main).getAllByText(text).some(node => !node.closest('article'))
  expect(outside(/^PT-2026-0AF23CC6$/)).toBe(true)
  expect(outside(/Awarded October 7, 2026/)).toBe(true)
})

// Value: protects=no one prints the old name while a corrected one sits unsaved in the form; fails_when=Print stays while
//   editing; why_new=found by the Codex design review; seam=none
test('Print waits until a name change is saved or cancelled', () => {
  render(<CertificatePage certificate={certificate()} finalExam={null} loading={false} onSaved={() => {}} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: /Change name/ }))
  expect(screen.queryByRole('button', { name: /Print or save as PDF/ })).toBeNull()
  screen.getByText('Save or cancel your name change before you print.')
  fireEvent.click(screen.getByRole('button', { name: /Cancel/ }))
  screen.getByRole('button', { name: /Print or save as PDF/ })
})

test('a refused name stays in the form with the server’s bilingual reason', async () => {
  const state = world({ certificate: certificate(), refuse: { status: 422, error: 'invalid_name', message: 'Enter your name in 1 to 80 characters. / กรุณาใส่ชื่อความยาว 1 ถึง 80 ตัวอักษร', path: /certificate\/name/ } })
  installApi(state)
  setToken('session-token')
  render(<CertificatePage certificate={certificate()} finalExam={null} loading={false} onSaved={() => {}} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: /Change name/ }))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'x' } })
  // A tap focuses the button first; fireEvent.click alone would leave focus on the field.
  const save = screen.getByRole('button', { name: /Save name/ })
  save.focus()
  fireEvent.click(save)
  const alert = await screen.findByRole('alert')
  within(alert).getByText('กรุณาใส่ชื่อความยาว 1 ถึง 80 ตัวอักษร')
  // Keyboard users land back on the name to correct it.
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('textbox')))
})

// Value: protects=a holder whose reading room failed to load, or whose certificate is hidden during a restore, is never told
//   to pass the exam; fails_when=either page shows NotEarned without a Final Exam standing; why_new=red team: a failed
//   load leaves certificate and final_exam empty; seam=none
test('without a Final Exam standing, neither page says the certificate is still to earn, and both can check again', () => {
  const refresh = vi.fn()
  for (const missing of [undefined, null]) {
    const page = render(<CertificatePage certificate={missing} finalExam={null} loading={false} onSaved={() => {}} onRefresh={refresh} onQuiz={() => {}} onBack={() => {}} />)
    screen.getByText('Certificate details are not available right now. Please check again in a moment.')
    expect(screen.queryByText(/Pass the Final Exam to earn your certificate/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Check again/ }))
    page.unmount()
    const badgesPage = render(<BadgesPage quizzes={[passed(1)]} finalExam={null} certificate={missing} onQuiz={() => {}} onRefresh={refresh} onBack={() => {}} />)
    expect(screen.queryByText(/Pass the Final Exam to earn your certificate/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Check again/ }))
    badgesPage.unmount()
  }
  expect(refresh).toHaveBeenCalledTimes(4)
})

// Value: protects=a certificate that goes away and comes back opens on its name, not a half-made edit; fails_when=the
//   page keeps an old editing state across a missing certificate; why_new=the editing state moved up to the page; seam=none
test('a certificate that disappears during an edit comes back closed', () => {
  const props = { finalExam: exam({ passed: true, passed_at: OCT_5, locked_by: null }), loading: false, onSaved: () => {}, onRefresh: () => {}, onQuiz: () => {}, onBack: () => {} }
  const { rerender } = render(<CertificatePage certificate={certificate()} {...props} />)
  fireEvent.click(screen.getByRole('button', { name: /Change name/ }))
  screen.getByRole('textbox', { name: /Name on your certificate/ })
  rerender(<CertificatePage certificate={null} {...props} />)
  rerender(<CertificatePage certificate={certificate()} {...props} />)
  expect(screen.queryByRole('textbox')).toBeNull()
  screen.getByRole('button', { name: /Print or save as PDF/ })
})

// Value: protects=Print never returns next to the old name while a new one is still saving; fails_when=Cancel or Change
//   name work while a save is in flight; why_new=design specialist and the outside voice both found it; seam=none
test('Cancel cannot drop a name that is still saving, so Print waits for the answer', async () => {
  installApi(world({ certificate: certificate() }))
  setToken('session-token')
  let answer: (saved: Certificate) => void = () => {}
  vi.spyOn(api, 'nameCertificate').mockImplementation(() => new Promise(resolve => { answer = resolve }))
  render(<CertificatePage certificate={certificate()} finalExam={null} loading={false} onSaved={() => {}} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: /Change name/ }))
  fireEvent.change(screen.getByRole('textbox', { name: /Name on your certificate/ }), { target: { value: 'Maria Santos Rivera' } })
  fireEvent.click(screen.getByRole('button', { name: /Save name/ }))
  fireEvent.click(screen.getByRole('button', { name: /Cancel/ }))
  expect(screen.queryByRole('button', { name: /Print or save as PDF/ })).toBeNull()
  await act(async () => answer(certificate({ name: 'Maria Santos Rivera', name_confirmed: true })))
  screen.getByRole('button', { name: /Print or save as PDF/ })
})

// Value: protects=Cancel keeps keyboard focus and discards the edit, and reopening clears an old Saved status; fails_when=Cancel
//   skips close(), Change name stops resetting the field, or the status survives reopening; why_new=only the save path checked these; seam=none
test('Cancel returns focus to Change name, and reopening the form starts from the saved name with no old status', async () => {
  installApi(world({ certificate: certificate() }))
  setToken('session-token')
  render(<CertificatePage certificate={certificate()} finalExam={null} loading={false} onSaved={() => {}} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: /Keep this name/ }))
  await screen.findByText(/Saved\. Your certificate shows this name\./)
  fireEvent.click(screen.getByRole('button', { name: /Change name/ }))
  expect(screen.queryByText(/Saved\. Your certificate shows this name\./)).toBeNull()
  fireEvent.change(screen.getByRole('textbox', { name: /Name on your certificate/ }), { target: { value: 'Typo Nmae' } })
  fireEvent.click(screen.getByRole('button', { name: /Cancel/ }))
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: /Change name/ })))
  fireEvent.click(screen.getByRole('button', { name: /Change name/ }))
  expect(screen.getByRole<HTMLInputElement>('textbox', { name: /Name on your certificate/ }).value).toBe('Maria Santos')
})

// Value: protects=the empty status line adds no space under the name card; fails_when=the card goes back to space-y with the
//   status last (Tailwind 4 gives the previous child a margin the hidden status no longer follows); why_new=design found 12 px; seam=none
test('the empty name status adds no space under the name card', () => {
  render(<CertificatePage certificate={certificate({ name_confirmed: true })} finalExam={null} loading={false} onSaved={() => {}} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  const status = within(screen.getByRole('region', { name: 'Name on your certificate' })).getByRole('status')
  expect(status.textContent).toBe('')
  const stack = status.parentElement!
  expect(/(^|\s)space-y-/.test(stack.className) && stack.lastElementChild === status).toBe(false)
})

test('the certificate page says Loading… until the reading room arrives, never "pass the exam"', () => {
  render(<CertificatePage certificate={undefined} finalExam={null} loading onSaved={() => {}} onRefresh={() => {}} onQuiz={() => {}} onBack={() => {}} />)
  screen.getByText('Loading…')
  expect(screen.queryByText(/Pass the Final Exam to earn your certificate/)).toBeNull()
})

test('the certificate page waits for a pass the reading room has not caught up with, and explains how to earn one', () => {
  const refresh = vi.fn()
  const { unmount } = render(<CertificatePage certificate={null} finalExam={exam({ passed: true, passed_at: OCT_5, locked_by: null })} loading={false}
    onSaved={() => {}} onRefresh={refresh} onQuiz={() => {}} onBack={() => {}} />)
  screen.getByText('Your certificate is being prepared. It will appear here soon.')
  expect(refresh).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: /Check again/ }))
  expect(refresh).toHaveBeenCalledTimes(2)
  unmount()
  const onQuiz = vi.fn()
  render(<CertificatePage certificate={null} finalExam={exam({ locked_by: null })} loading={false} onSaved={() => {}} onRefresh={refresh} onQuiz={onQuiz} onBack={() => {}} />)
  screen.getByText(/Pass the Final Exam to earn your certificate\./)
  fireEvent.click(screen.getByRole('button', { name: /Take the Final Exam/ }))
  expect(onQuiz).toHaveBeenCalledWith(0)
  expect(refresh).toHaveBeenCalledTimes(2)
})

test('a first chapter pass awards its badge with a burst; a retake after a pass awards nothing new', async () => {
  const state = world({ quizBank: { 1: quiz(1, standing(1)) } })
  state.grade = (chapter, answers): QuizResult => ({ chapter, score: 5, total: 5, pass_mark: 4, passed: true,
    results: Object.entries(answers).map(([id, chosen]) => ({ id, chosen, correct: true, answer: 'a', explanation: { en: 'Why.', th: 'เพราะ' } })),
    quizzes: [passed(1), standing(2)], next_chapter: { chapter: 2, category: category(2), unlocked: true } })
  installApi(state)
  setToken('session-token')
  const { container, unmount } = render(<QuizPage chapter={1} posts={[chapterPost(1), chapterPost(2)]} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByRole('heading', { name: category(1) })
  await answerAll(5)
  await screen.findByText('New badge: Chapter 1')
  screen.getByText('ได้รับเหรียญตราใหม่: บทที่ 1')
  screen.getByRole('img', { name: 'Chapter 1 badge: earned Oct 5, 2026' })
  expect(screen.getByRole('link', { name: /See your badges/ }).getAttribute('href')).toBe('/badges')
  expect(container.querySelector('.confetti')?.getAttribute('aria-hidden')).toBe('true')
  expect(screen.queryByText(/All chapters badge/)).toBeNull()
  unmount()
  state.quizBank = { 1: quiz(1, passed(1)) }
  render(<QuizPage chapter={1} posts={[chapterPost(1), chapterPost(2)]} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByText(/You passed this quiz before/)
  await answerAll(5)
  await screen.findByText('You passed!')
  expect(screen.queryByText(/New badge/)).toBeNull()
  expect(document.querySelector('.confetti')).toBeNull()
})

test('passing the last chapter quiz also earns the every-chapter badge', async () => {
  const state = world({ quizBank: { 2: quiz(2, standing(2)) } })
  state.grade = (chapter): QuizResult => ({ chapter, score: 4, total: 5, pass_mark: 4, passed: true, results: [],
    quizzes: [passed(1), passed(2)], next_chapter: null })
  installApi(state)
  setToken('session-token')
  render(<QuizPage chapter={2} posts={[chapterPost(1), chapterPost(2)]} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByRole('heading', { name: category(2) })
  await answerAll(5)
  await screen.findByText('New badge: Chapter 2')
  screen.getByText('You also earned the All chapters badge.')
})

// Value: protects=a site with one chapter quiz never announces an All chapters badge that badges() does not make;
//   fails_when=QuizPage drops its more-than-one-quiz guard; why_new=every other QuizPage badge test publishes two chapters; seam=none
test('passing the only published chapter quiz earns its badge but no All chapters badge', async () => {
  const state = world({ quizBank: { 1: quiz(1, standing(1)) } })
  state.grade = (chapter): QuizResult => ({ chapter, score: 5, total: 5, pass_mark: 4, passed: true, results: [],
    quizzes: [passed(1)], next_chapter: null })
  installApi(state)
  setToken('session-token')
  render(<QuizPage chapter={1} posts={[chapterPost(1)]} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByRole('heading', { name: category(1) })
  await answerAll(5)
  await screen.findByText('New badge: Chapter 1')
  expect(screen.queryByText(/All chapters badge/)).toBeNull()
})

// Value: protects=a Final Exam pass never claims the All chapters badge, which the last chapter pass earned; fails_when=QuizPage
//   drops `!final` from everyChapter; why_new=the other Final Exam test grades one chapter, so length > 1 hid it; seam=none
test('a first Final Exam pass does not announce the All chapters badge, even with every chapter passed', async () => {
  const state = world({ quizBank: { 0: quiz(0, exam({ locked_by: null })) } })
  // Like service.submit_quiz, a Final Exam result carries every chapter's standing.
  state.grade = (): QuizResult => ({ chapter: 0, score: 20, total: 20, pass_mark: 16, passed: true, results: [],
    quizzes: [passed(1), passed(2)], final_exam: exam({ passed: true, passed_at: OCT_5, locked_by: null }), next_chapter: null })
  installApi(state)
  setToken('session-token')
  render(<QuizPage chapter={0} posts={[]} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await answerAll(20)
  await screen.findByText('You earned your certificate!')
  expect(screen.queryByText(/You also earned the All chapters badge/)).toBeNull()
})

test('a first Final Exam pass leads to the certificate', async () => {
  const state = world({ quizBank: { 0: quiz(0, exam({ locked_by: null })) } })
  state.grade = (): QuizResult => ({ chapter: 0, score: 20, total: 20, pass_mark: 16, passed: true, results: [],
    quizzes: [passed(9)], final_exam: exam({ passed: true, passed_at: OCT_5, locked_by: null }), next_chapter: null })
  installApi(state)
  setToken('session-token')
  render(<QuizPage chapter={0} posts={[chapterPost(9)]} exempt={false} onFinished={() => {}} onOpen={() => {}} onBack={() => {}} />)
  await screen.findByRole('heading', { name: 'Final Exam' })
  await answerAll(20)
  await screen.findByText('You earned your certificate!')
  screen.getByText('คุณได้รับประกาศนียบัตรแล้ว!')
  expect(screen.getByRole('link', { name: /View your certificate/ }).getAttribute('href')).toBe('/certificate')
  expect(screen.getByRole('button', { name: /Back to your training/ }).className).toContain('border')
})

test('the app routes to the badges and certificate pages from the reading room data', async () => {
  expect(parse('/badges')).toEqual({ name: 'badges' })
  expect(parse('/certificate/')).toEqual({ name: 'certificate' })
  const state = world({ posts: [chapterPost(9)], quizzes: [passed(9)], finalExam: exam({ passed: true, passed_at: OCT_5, locked_by: null }),
    certificate: certificate({ name: 'Maria Santos', name_confirmed: true }) })
  installApi(state)
  setToken('session-token')
  window.history.replaceState(null, '', '/certificate')
  const { unmount } = render(<App />)
  await screen.findByRole('heading', { name: /Your certificate/ })
  expect(await screen.findAllByText('PT-2026-0AF23CC6', { selector: 'main *' })).not.toHaveLength(0)
  expect(screen.queryByText(/Check your name before you print/)).toBeNull()
  expect(screen.queryByRole('button', { name: /Keep this name/ })).toBeNull()
  unmount()
  window.history.replaceState(null, '', '/badges')
  render(<App />)
  await screen.findByRole('heading', { name: /^Your badges/ })
  screen.getByText('2 of 2 earned')
})

// Value: protects=App passes a saved name to the certificate on screen and its print copy; fails_when=App's onSaved is not
//   wired to its certificate state; why_new=CertificatePage tests pass their own spies; seam=none
test('a saved name reaches the certificate on screen and its print copy', async () => {
  installApi(world({ posts: [chapterPost(9)], quizzes: [passed(9)], finalExam: exam({ passed: true, passed_at: OCT_5, locked_by: null }), certificate: certificate() }))
  setToken('session-token')
  window.history.replaceState(null, '', '/certificate')
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /Change name/ }))
  fireEvent.change(screen.getByRole('textbox', { name: /Name on your certificate/ }), { target: { value: 'Maria Santos Rivera' } })
  fireEvent.click(screen.getByRole('button', { name: /Save name/ }))
  await screen.findByText(/Saved\. Your certificate shows this name\./)
  within(screen.getAllByRole('article', { name: 'Certificate of completion' })[0]).getByText('Maria Santos Rivera')
  within(document.querySelector('.print-root') as HTMLElement).getByText('Maria Santos Rivera')
})

// Value: protects=a passer who opens /certificate before the certificate arrives gets it without a tap; fails_when=App's
//   onRefresh is not wired to a fresh reading room; why_new=CertificatePage tests pass their own spies; seam=none
test('a certificate that arrives after /certificate opened replaces "being prepared" without a tap', async () => {
  const state = world({ posts: [chapterPost(9)], quizzes: [passed(9)], finalExam: exam({ passed: true, passed_at: OCT_5, locked_by: null }), certificate: null })
  installApi(state)
  setToken('session-token')
  window.history.replaceState(null, '', '/certificate')
  render(<App />)
  await screen.findByText('Your certificate is being prepared. It will appear here soon.')
  state.certificate = certificate()
  expect(await screen.findAllByRole('article', { name: 'Certificate of completion' })).not.toHaveLength(0)
})

// Value: protects=a refresh already on its way cannot put the old name back after a save; fails_when=App's onSaved stops
//   reading the reading room again; why_new=red team showed a stale snapshot overwriting a saved name; seam=none
test('saving a name reads the reading room again, so an older answer cannot undo it', async () => {
  installApi(world({ posts: [chapterPost(9)], quizzes: [passed(9)], finalExam: exam({ passed: true, passed_at: OCT_5, locked_by: null }), certificate: certificate() }))
  setToken('session-token')
  window.history.replaceState(null, '', '/certificate')
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /Keep this name/ }))
  await screen.findByText(/Saved\. Your certificate shows this name\./)
  const reads = () => calls.filter(call => call.url.endsWith('/api/posts')).length
  const before = reads()
  await waitFor(() => expect(reads()).toBe(before + 1))
})

test('signing out removes the certificate and its print copy before the next person signs in', async () => {
  const state = world({ posts: [chapterPost(9)], quizzes: [passed(9)], finalExam: exam({ passed: true, passed_at: OCT_5, locked_by: null }),
    certificate: certificate() })
  installApi(state)
  setToken('session-token')
  window.history.replaceState(null, '', '/certificate')
  render(<App />)
  await screen.findByRole('heading', { name: /Your certificate/ })
  expect(document.querySelector('.print-root')).not.toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /Sign out/ }))
  await screen.findByText('You are signed out.')
  expect(document.querySelector('.print-root')).toBeNull()
  expect(screen.queryByText('Maria Santos')).toBeNull()
  expect(document.title).not.toBe('Training Certificate')
})

// Value: protects=a session check that answers after sign-out does not bring the last reader back on a shared tablet;
//   fails_when=the identity check after api.session() in App's load effect is dropped; why_new=the late-arrival test defers api.posts only; seam=none
test('a session check that answers after sign-out does not bring the last reader back', async () => {
  const state = world({ refuse: { status: 503, error: 'audit_unavailable', message: 'Postern could not record this. Try again.', path: /\/api\/posts$/ } })
  installApi(state)
  setToken('session-token')
  render(<App />)
  const retry = await screen.findByRole('button', { name: /Try again/ })
  state.refuse = null
  let answer: (session: Session) => void = () => {}
  vi.spyOn(api, 'session').mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))
  fireEvent.click(retry)
  fireEvent.click(await screen.findByRole('button', { name: /Sign out/ }))
  await screen.findByText('You are signed out.')
  await act(async () => answer(state.session))
  screen.getByText('You are signed out.')
  expect(screen.queryByRole('button', { name: /Sign out/ })).toBeNull()
})

test('a reading room that arrives after sign-out is not shown to the next person', async () => {
  const state = world({ posts: [chapterPost(9)], quizzes: [passed(9)], finalExam: exam({ passed: true, passed_at: OCT_5, locked_by: null }),
    certificate: certificate() })
  installApi(state)
  let arrive: (wall: Awaited<ReturnType<typeof api.posts>>) => void = () => {}
  vi.spyOn(api, 'posts')
    .mockImplementationOnce(() => new Promise(resolve => { arrive = resolve }))
    .mockImplementation(() => new Promise(() => {}))
  setToken('session-token')
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /Sign out/ }))
  await screen.findByText('You are signed out.')
  // The first reader's training lands only now, then the next person signs in on the same tablet.
  arrive({ posts: state.posts, quizzes: state.quizzes, final_exam: state.finalExam, certificate: state.certificate })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'next@example.invalid' } })
  fireEvent.click(screen.getByRole('button', { name: /Continue/ }))
  await screen.findByText('Nothing has been posted yet.')
  expect(screen.queryByRole('region', { name: /Your badges/ })).toBeNull()
  expect(screen.queryByText(/Your certificate is ready/)).toBeNull()
})

// Value: protects=a name save answered after sign-out never puts the previous reader's certificate on the next reader's screen;
//   fails_when=CertificateName's save() calls onSaved without checking that the saving session is still signed in;
//   why_new=the late-arrival test covers only the App's api.posts, not api.nameCertificate;
//   seam=none
test('a name saved just before sign-out never reaches the next person on the tablet', async () => {
  const state = world({ posts: [chapterPost(9)], quizzes: [passed(9)], finalExam: exam({ passed: true, passed_at: OCT_5, locked_by: null }),
    certificate: certificate() })
  installApi(state)
  let answer: (saved: Certificate) => void = () => {}
  vi.spyOn(api, 'nameCertificate').mockImplementation(() => new Promise(resolve => { answer = resolve }))
  setToken('session-token')
  window.history.replaceState(null, '', '/certificate')
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: /Change name/ }))
  fireEvent.change(screen.getByRole('textbox', { name: /Name on your certificate/ }), { target: { value: 'Maria Santos Rivera' } })
  fireEvent.click(screen.getByRole('button', { name: /Save name/ }))
  fireEvent.click(screen.getByRole('button', { name: /Sign out/ }))
  await screen.findByText('You are signed out.')
  // The next person has passed nothing yet and signs in with a session of their own.
  Object.assign(state, { quizzes: [standing(9)], finalExam: exam(), certificate: null })
  vi.spyOn(api, 'signInDemo').mockResolvedValue({ token: 'next-token', email: 'next@example.invalid', name: '', role: 'reader' })
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'next@example.invalid' } })
  fireEvent.click(screen.getByRole('button', { name: /Continue/ }))
  await screen.findByText('0 of 2 earned')
  // Only now does the first reader's save come back.
  await act(async () => answer(certificate({ name: 'Maria Santos Rivera', name_confirmed: true })))
  screen.getByRole('img', { name: 'Final Exam badge: not earned yet' })
  expect(screen.queryByRole('img', { name: /Final Exam badge: earned/ })).toBeNull()
  screen.getByText('0 of 2 earned')
})

test('Staff progress keeps a certificate when the Final Exam is unavailable, for example Chapter 9 archived', async () => {
  const person: TrainingPerson = { email: 'maria.santos@example.invalid', name: 'Maria', role: 'reader', percent: 100,
    status: 'complete', completed_items: 1, total_items: 1, opened_items: 1, last_activity_at: OCT_5, items: [],
    final_exam: null, certificate: certificate({ name_confirmed: true }) }
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people: [person], total_items: 1, history_incomplete: false, quiz_total: 0 })
  render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('button', { name: 'View progress for Maria' }))
  screen.getByText('Certificate PT-2026-0AF23CC6 · awarded Oct 7, 2026 · printed name “Maria Santos”')
  fireEvent.click(screen.getByRole('button', { name: 'View certificate' }))
  within(screen.getAllByRole('article', { name: 'Certificate of completion' })[0]).getByText('Maria Santos')
})

// Value: protects=Staff progress dates a certificate like its printed copy and number; fails_when=training-progress drops
//   EASTERN; why_new=both Staff progress tests award at noon Eastern, the same day everywhere; seam=none
test('Staff progress dates a certificate in Eastern, like its number and print', async () => {
  deviceZone('Asia/Bangkok')
  const eve = Date.UTC(2027, 0, 1, 1, 30) / 1000 // 8:30 pm on December 31, Eastern
  const person: TrainingPerson = { email: 'maria.santos@example.invalid', name: 'Maria', role: 'reader', percent: 100,
    status: 'complete', completed_items: 1, total_items: 1, opened_items: 1, last_activity_at: eve, items: [],
    final_exam: null, certificate: certificate({ awarded_at: eve, name_confirmed: true }) }
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people: [person], total_items: 1, history_incomplete: false, quiz_total: 0 })
  render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('button', { name: 'View progress for Maria' }))
  screen.getByText(/awarded Dec 31, 2026/)
})

test('Staff progress shows each certificate’s number and printed name, and prints it for an admin', async () => {
  const person: TrainingPerson = { email: 'maria.santos@example.invalid', name: 'Maria', role: 'reader', percent: 100,
    status: 'complete', completed_items: 9, total_items: 9, opened_items: 9, last_activity_at: OCT_5, items: [],
    quizzes_passed: 9, quiz_exempt: false, quizzes: Array.from({ length: 9 }, (_, i) => passed(i + 1)),
    final_exam: exam({ attempts: 1, best_score: 18, last_score: 18, passed: true, passed_at: OCT_5 + 2 * DAY, locked_by: null }),
    certificate: certificate() }
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people: [person], total_items: 9, history_incomplete: false, quiz_total: 9 })
  const print = vi.spyOn(window, 'print').mockImplementation(() => {})
  render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('button', { name: 'View progress for Maria' }))
  screen.getByText('Certificate PT-2026-0AF23CC6 · awarded Oct 7, 2026 · printed name “Maria Santos” (suggested, not yet confirmed)')
  expect(document.querySelector('.print-root')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'View certificate' }))
  within(screen.getAllByRole('article', { name: 'Certificate of completion' })[0]).getByText('Maria Santos')
  expect(document.title).toBe('Training Certificate')
  fireEvent.click(screen.getByRole('button', { name: 'Print certificate' }))
  expect(print).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Hide certificate' }))
  expect(document.querySelector('.print-root')).toBeNull()
  expect(document.title).not.toBe('Training Certificate')
})
