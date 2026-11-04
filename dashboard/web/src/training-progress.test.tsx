import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { api, ApiError, setToken, type CertificateHistory, type QuizStanding, type TrainingItem, type TrainingPerson, type TrainingProgress } from './api'
import { TrainingDashboard } from './training-progress'
import { csv } from './table-export'
import { App } from './app'
import { installApi, world } from './testing'

const item: TrainingItem = { id: 'video', post_id: 'post', title: 'Kitchen safety', category: 'Module 1', filename: 'safety.mp4', kind: 'video', pages: 0, duration_seconds: 100, opened_pages: 0, watched_seconds: 25, fraction: .25, opened: true, attempts: 1, last_activity_at: 100 }
const person: TrainingPerson = { email: 'asha@example.invalid', name: 'Asha', role: 'reader', percent: 25, status: 'in_progress', completed_items: 0, total_items: 1, opened_items: 1, last_activity_at: 100, items: [item] }
function report(): TrainingProgress { return { people: [{ ...person }, { ...person, name: 'Ben', email: 'ben@example.invalid', percent: 0, status: 'not_started', opened_items: 0, last_activity_at: null, items: [{ ...item, fraction: 0, opened: false, watched_seconds: 0, attempts: 2, last_activity_at: null }] }], total_items: 1, history_incomplete: false } }
afterEach(() => vi.restoreAllMocks())

const issued = { number: 'PT-2026-ABC01234', name: 'Asha Printed', name_confirmed: true, awarded_at: 1791456000 }
const namePage = (overrides: Partial<CertificateHistory> = {}): CertificateHistory => ({
  certificate: { ...issued, name: 'Asha Corrected' }, entries: [{ id: 'newest', name: 'Asha Corrected', saved_at: 1791456001 }],
  next_cursor: null, history_incomplete: false, ...overrides,
})

test('certificate search includes printed names and numbers across an explicit All locations tab', async () => {
  const data = report()
  data.groups = ['Team One', 'Team Two']
  data.people[0] = { ...data.people[0], groups: ['Team One'], certificate: issued }
  // A duplicate short number must produce two people, never silently pick one.
  data.people[1] = { ...data.people[1], groups: ['Team Two'], certificate: { ...issued, name: 'Ben Printed' } }
  vi.spyOn(api, 'trainingProgress').mockResolvedValue(data)
  render(<TrainingDashboard />)
  const first = await screen.findByRole('tab', { name: 'Team One' })
  expect(first.getAttribute('aria-selected')).toBe('true')
  const field = screen.getByRole('searchbox', { name: 'Find a person or certificate' })
  fireEvent.change(field, { target: { value: '  pt-2026-abc01234  ' } })
  screen.getByText('1 of 1 people · 1 training items')
  fireEvent.keyDown(first, { key: 'ArrowLeft' })
  expect(screen.getByRole('tab', { name: 'All locations' }).getAttribute('aria-selected')).toBe('true')
  fireEvent.change(field, { target: { value: issued.number } })
  screen.getByText('2 of 2 people · 1 training items')
  fireEvent.change(field, { target: { value: 'ben printed' } })
  expect(screen.queryByText('Asha')).toBeNull()
  screen.getByText('Ben')
})

test('saved name history is lazy, updates the displayed and printed certificate, and pages with retry', async () => {
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ ...report(), people: [{ ...person, certificate: issued }] })
  const history = vi.spyOn(api, 'certificateHistory').mockResolvedValueOnce(namePage({ next_cursor: 'newest' }))
    .mockRejectedValueOnce(new ApiError(503, 'offline', 'History temporarily unavailable.'))
    .mockResolvedValueOnce(namePage({ entries: [{ id: 'older', name: 'Asha Earlier', saved_at: 1791455900 }] }))
  render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('button', { name: 'View progress for Asha' }))
  expect(history).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Show name history' }))
  await screen.findByRole('heading', { name: 'Saved name history' })
  await screen.findByText('Asha Corrected', { selector: 'li p' })
  expect(history).toHaveBeenCalledWith(person.email, undefined)
  fireEvent.click(screen.getByRole('button', { name: 'View certificate' }))
  expect(document.querySelector('.print-root')?.textContent).toContain('Asha Corrected')
  expect(document.querySelector('.print-root')?.textContent).not.toContain('Asha Printed')
  fireEvent.click(screen.getByRole('button', { name: 'Load older names' }))
  await screen.findByText('History temporarily unavailable.')
  screen.getByText('Asha Corrected', { selector: 'li p' })
  fireEvent.click(screen.getByRole('button', { name: 'Load older names' }))
  await screen.findByText('Asha Earlier', { selector: 'li p' })
  expect(history).toHaveBeenLastCalledWith(person.email, 'newest')
  expect(screen.queryByRole('button', { name: 'Load older names' })).toBeNull()
})

test('empty restored name history describes missing evidence without inventing prior suggestions', async () => {
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ ...report(), people: [{ ...person, certificate: issued }] })
  vi.spyOn(api, 'certificateHistory').mockResolvedValue(namePage({ entries: [], history_incomplete: true, certificate: { ...issued, name_confirmed: false } }))
  render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('button', { name: 'View progress for Asha' }))
  fireEvent.click(screen.getByRole('button', { name: 'Show name history' }))
  await screen.findByText('No saved names in the available history.')
  screen.getByText(/Some restored history is missing/)
  screen.getByText(/Suggested names that were never saved are not recorded/)
  expect(screen.queryByRole('list', { name: 'Saved certificate names' })).toBeNull()
})

test('a late name-history response cannot cross an account change and duplicate requests are ignored', async () => {
  setToken('first-admin')
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ ...report(), people: [{ ...person, certificate: issued }] })
  let finish!: (value: CertificateHistory) => void
  const history = vi.spyOn(api, 'certificateHistory').mockImplementation(() => new Promise(resolve => { finish = resolve }))
  render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('button', { name: 'View progress for Asha' }))
  fireEvent.click(screen.getByRole('button', { name: 'Show name history' }))
  const refresh = screen.getByRole('button', { name: 'Refresh name history' })
  fireEvent.click(refresh); fireEvent.click(refresh)
  expect(history).toHaveBeenCalledTimes(1)
  screen.getByText('Loading saved names…')
  setToken('next-admin')
  await act(async () => finish(namePage({ certificate: { ...issued, name: 'Private Late Name' }, entries: [{ id: 'late', name: 'Private Late Name', saved_at: 1 }] })))
  expect(screen.queryByText(/Private Late Name/)).toBeNull()
})

test('switching people discards the pending history and an initial failure can be retried', async () => {
  const data = report()
  data.people = data.people.map(value => ({ ...value, certificate: issued }))
  vi.spyOn(api, 'trainingProgress').mockResolvedValue(data)
  let finish!: (value: CertificateHistory) => void
  vi.spyOn(api, 'certificateHistory').mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    .mockRejectedValueOnce(new ApiError(503, 'offline', 'Name history unavailable.'))
    .mockResolvedValueOnce(namePage({ certificate: { ...issued, name: 'Ben Corrected' }, entries: [{ id: 'ben-name', name: 'Ben Corrected', saved_at: 10 }] }))
  render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('button', { name: 'View progress for Asha' }))
  fireEvent.click(screen.getByRole('button', { name: 'Show name history' }))
  fireEvent.click(screen.getByRole('button', { name: 'View progress for Ben' }))
  fireEvent.click(screen.getByRole('button', { name: 'Show name history' }))
  await screen.findByText('Name history unavailable.')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh name history' }))
  await screen.findByText('Ben Corrected', { selector: 'li p' })
  await act(async () => finish(namePage()))
  expect(screen.queryByText('Asha Corrected', { selector: 'li p' })).toBeNull()
  screen.getByText('Ben Corrected', { selector: 'li p' })
})

test('required progress precedes overall in cards and rows, and averages only the active location', async () => {
  const data = report()
  data.groups = ['Pattana', 'Restaurant']
  data.people[0] = { ...data.people[0], groups: ['Pattana', 'Restaurant'], required_progress: { fraction: 1 / 3, total_steps: 3 } }
  data.people[1] = { ...data.people[1], groups: ['Restaurant'], required_progress: { fraction: 2 / 3, total_steps: 3 } }
  vi.spyOn(api, 'trainingProgress').mockResolvedValue(data)
  render(<TrainingDashboard />)
  await screen.findByText('Asha')
  const requiredCard = screen.getAllByText('Required progress')[0].parentElement!
  within(requiredCard).getByText('33%')
  expect(requiredCard.nextElementSibling?.textContent).toContain('Overall progress')
  expect(screen.getAllByRole('columnheader').slice(0, 3).map(header => header.textContent)).toEqual(['Person', 'Required progress', 'Overall progress'])
  const row = screen.getByText('Asha').closest('tr')!
  expect(within(row).getAllByRole('progressbar').map(bar => [bar.getAttribute('aria-label'), bar.getAttribute('aria-valuenow')])).toEqual([
    ['Asha: required progress', '33'], ['Asha: overall progress', '25'],
  ])
  fireEvent.click(screen.getByRole('button', { name: 'View progress for Asha' }))
  expect(screen.getByText('Content progress for Asha').closest('td')?.colSpan).toBe(screen.getAllByRole('columnheader').length)
  fireEvent.click(screen.getByRole('tab', { name: 'Restaurant' }))
  within(requiredCard).getByText('50%') // Average the fractions before rounding.
  fireEvent.change(screen.getByRole('searchbox', { name: 'Find a person or certificate' }), { target: { value: 'Asha' } })
  within(requiredCard).getByText('50%') // Search narrows rows, not the location's average.
})

test('required progress retains unknown history, unavailable coverage and no-content states', async () => {
  const data = report()
  data.history_incomplete = true
  data.people[0] = { ...data.people[0], required_progress: { fraction: .35, total_steps: 5 } }
  data.people[1] = { ...data.people[1], required_progress: { fraction: 0, total_steps: 5 } }
  const load = vi.spyOn(api, 'trainingProgress').mockResolvedValueOnce(data)
  render(<TrainingDashboard />)
  await screen.findByText('Asha')
  within(screen.getByText('Asha').closest('tr')!).getByText('At least 35%')
  expect(screen.queryByRole('progressbar', { name: 'Ben: required progress' })).toBeNull()
  within(within(screen.getByText('Ben').closest('tr')!).getAllByRole('cell')[0]).getByText('Unknown')
  const next = report()
  next.people[0] = { ...next.people[0], required_progress: { fraction: null, total_steps: 5 } }
  next.people[1] = { ...next.people[1], required_progress: { fraction: null, total_steps: 0 } }
  load.mockResolvedValueOnce(next)
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByText('No required content')
  within(screen.getByText('Asha').closest('tr')!).getByText('Unavailable')
  within(screen.getAllByText('Required progress')[0].parentElement!).getByText('Unavailable')
  expect(screen.queryByRole('progressbar', { name: 'Asha: required progress' })).toBeNull()
})

test('location tabs scope counts, progress and filters while sharing a person’s history', async () => {
  const data = report()
  data.groups = ['Team One', 'Restaurant', 'Empty']
  data.people[0].groups = ['Team One', 'Restaurant']
  data.people[1].groups = ['Restaurant']
  data.roster_warnings = [{ group: 'Empty', row: 16, message: 'Missing email address' }]
  const load = vi.spyOn(api, 'trainingProgress').mockResolvedValue(data)
  render(<TrainingDashboard />)
  const teamOne = await screen.findByRole('tab', { name: 'Team One' })
  expect(teamOne.getAttribute('aria-selected')).toBe('true')
  screen.getByText('1 of 1 people · 1 training items')
  expect(screen.queryByText('Ben')).toBeNull()
  fireEvent.change(screen.getByRole('searchbox', { name: 'Find a person or certificate' }), { target: { value: 'no match' } })
  fireEvent.keyDown(teamOne, { key: 'ArrowRight' })
  const restaurant = screen.getByRole('tab', { name: 'Restaurant' })
  expect(document.activeElement).toBe(restaurant)
  expect(restaurant.getAttribute('aria-selected')).toBe('true')
  expect(screen.getByRole('tabpanel').getAttribute('aria-labelledby')).toBe(restaurant.id)
  screen.getByText('2 of 2 people · 1 training items')
  screen.getByText('12%') // Average includes only the selected tab, with one shared record.
  fireEvent.click(screen.getByRole('button', { name: 'View progress for Asha' }))
  screen.getByText('0:25 of 1:40 watched')
  expect(screen.queryByText(/Spreadsheet row 16/)).toBeNull()
  fireEvent.keyDown(restaurant, { key: 'End' })
  screen.getByText(/Spreadsheet row 16: Missing email address/)
  expect(screen.queryByText('Content progress for Asha')).toBeNull()
  expect(screen.queryByRole('table')).toBeNull()
  fireEvent.keyDown(screen.getByRole('tab', { name: 'Empty' }), { key: 'Home' })
  expect(screen.getByRole('tab', { name: 'All locations' }).getAttribute('aria-selected')).toBe('true')
  screen.getByText('2 of 2 people · 1 training items')
  expect(load).toHaveBeenCalledTimes(1)
})

test('refresh removes stale location membership and falls back when a tab disappears', async () => {
  const initial = { ...report(), groups: ['Team One', 'Restaurant'], people: [{ ...person, groups: ['Restaurant'] }] }
  const next = { ...report(), groups: ['Team One'], people: [{ ...person, groups: ['Team One'] }] }
  vi.spyOn(api, 'trainingProgress').mockResolvedValueOnce(initial).mockResolvedValueOnce(next)
  render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('tab', { name: 'Restaurant' }))
  screen.getByText('Asha')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(screen.queryByRole('tab', { name: 'Restaurant' })).toBeNull())
  expect(screen.getByRole('tab', { name: 'Team One' }).getAttribute('aria-selected')).toBe('true')
  screen.getByText('Asha')
})

test('staff table expands per-content progress, filters and refreshes activity', async () => {
  const load = vi.spyOn(api, 'trainingProgress').mockResolvedValue(report())
  render(<TrainingDashboard />)
  await screen.findByText('Asha')
  expect(screen.getByRole('progressbar', { name: 'Asha: overall progress' }).getAttribute('aria-valuenow')).toBe('25')
  fireEvent.click(screen.getByRole('button', { name: 'View progress for Asha' }))
  screen.getByText('0:25 of 1:40 watched')
  expect(screen.getByRole('link', { name: 'Kitchen safety' }).getAttribute('href')).toBe('/admin/posts/post')
  fireEvent.change(screen.getByRole('combobox', { name: 'Progress status' }), { target: { value: 'not_started' } })
  expect(screen.queryByText('Asha')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'View progress for Ben' }))
  screen.getByText('Tried to open · no playback recorded')
  fireEvent.change(screen.getByRole('searchbox', { name: 'Find a person or certificate' }), { target: { value: 'missing' } })
  screen.getByText('No people match these filters.')
  fireEvent.change(screen.getByRole('searchbox', { name: 'Find a person or certificate' }), { target: { value: 'BEN@EXAMPLE' } })
  screen.getByText('Ben')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(load).toHaveBeenCalledTimes(2))
})

test('gaps show lower bounds and unknown people without a false zero progress bar', async () => {
  const data = report(); data.history_incomplete = true; data.people[1].status = 'unknown'
  vi.spyOn(api, 'trainingProgress').mockResolvedValue(data)
  render(<TrainingDashboard />)
  await screen.findByText('Asha')
  screen.getByText('At least 25%')
  const row = screen.getByText('Ben').closest('tr')!
  within(row).getByText('Unknown')
  expect(within(row).queryByRole('progressbar')).toBeNull()
  expect(screen.queryByText('Tried to open · no playback recorded')).toBeNull()
})

test('error has retry, empty catalog does not suggest that everyone completed training', async () => {
  vi.spyOn(api, 'trainingProgress').mockRejectedValueOnce(new ApiError(409, 'history_restoring', 'Training history is being restored. Please try again shortly.'))
    .mockResolvedValue({ people: [], total_items: 0, history_incomplete: false })
  render(<TrainingDashboard />)
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: /^Try\ again/ }))
  await screen.findByText(/No published training content yet/)
  expect(screen.queryByRole('table')).toBeNull()
})

test('progress deep link keeps readers out of the admin report', async () => {
  const state = world(); installApi(state)
  const { setToken } = await import('./api'); setToken('reader-session')
  window.history.pushState(null, '', '/admin/progress')
  const load = vi.spyOn(api, 'trainingProgress')
  render(<App />)
  await screen.findByText('Administrator access is required.')
  expect(load).not.toHaveBeenCalled()
})

test('module details use equal item weighting and show document, photo and unavailable video progress', async () => {
  const items: TrainingItem[] = [
    { ...item, id: 'pdf', kind: 'pdf', title: 'Handbook', filename: 'handbook.pdf', pages: 4, opened_pages: 2, fraction: .5, category: 'Module 2' },
    { ...item, id: 'photo', kind: 'photo', title: 'Uniform', filename: 'uniform.jpg', pages: 1, opened_pages: 1, fraction: 1, category: 'module 2' },
    { ...item, id: 'missing', title: 'Legacy video', filename: 'legacy.mp4', duration_seconds: null, fraction: null, category: 'Module 10' },
  ]
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people: [{ ...person, percent: null, status: 'unavailable', items, total_items: 3, completed_items: 1 }], total_items: 3, history_incomplete: false })
  render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('button', { name: 'View progress for Asha' }))
  screen.getByText('2 of 4 pages opened')
  screen.getByText('Photo opened')
  expect(screen.getByRole('progressbar', { name: 'Asha: Module 2' }).getAttribute('aria-valuenow')).toBe('75')
  expect(screen.queryByRole('progressbar', { name: 'Asha: Module 10' })).toBeNull()
  expect(screen.queryByRole('progressbar', { name: 'Asha: overall progress' })).toBeNull()
  const modules = screen.getAllByRole('heading', { level: 3 }).map(heading => heading.textContent)
  expect(modules).toEqual(['Module 2', 'Module 10'])
  fireEvent.click(screen.getByRole('button', { name: 'Hide progress for Asha' }))
  expect(screen.queryByText('Content progress for Asha')).toBeNull()
})

test('sorts staff by coverage and last viewing, and keeps the previous report when refresh fails', async () => {
  const data = report()
  data.people[1] = { ...data.people[1], last_activity_at: 200 }
  vi.spyOn(api, 'trainingProgress').mockResolvedValueOnce(data).mockRejectedValueOnce(new ApiError(503, 'offline', 'Network unavailable'))
  render(<TrainingDashboard />)
  await screen.findByText('Asha')
  const names = () => screen.getAllByRole('rowheader').map(row => row.textContent)
  fireEvent.change(screen.getByRole('combobox', { name: 'Sort by' }), { target: { value: 'overall:ascending' } })
  expect(names()[0]).toContain('Ben')
  fireEvent.change(screen.getByRole('combobox', { name: 'Sort by' }), { target: { value: 'overall:descending' } })
  expect(names()[0]).toContain('Asha')
  fireEvent.change(screen.getByRole('combobox', { name: 'Sort by' }), { target: { value: 'viewed:descending' } })
  expect(names()[0]).toContain('Ben')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByRole('alert')
  screen.getByText('Asha'); screen.getByText('Ben')
  expect(screen.getByRole('button', { name: /^Try\ again/ })).toHaveProperty('disabled', false)
})

test('published catalog with an empty staff list does not render misleading completion rows', async () => {
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people: [], total_items: 1, history_incomplete: false })
  render(<TrainingDashboard />)
  await screen.findByText('No staff are on the current list.')
  expect(screen.queryByRole('table')).toBeNull()
  expect(screen.queryByRole('progressbar')).toBeNull()
})

const names = () => screen.getAllByRole('rowheader').map(header => header.firstChild!.textContent)
const sortBy = (title: string) => fireEvent.click(within(screen.getByRole('columnheader', { name: title })).getByRole('button'))
const status = () => screen.getByRole('combobox', { name: 'Progress status' })
const standing = (overrides: Partial<QuizStanding> = {}): QuizStanding => ({ chapter: 0, category: 'Final Exam', total: 20, pass_mark: 16, attempts: 0, best_score: null, last_score: null, passed: false, passed_at: null, last_attempt_at: null, locked_by: null, ...overrides })
const exam = (name: string, final_exam: TrainingPerson['final_exam'], extra: Partial<TrainingPerson> = {}): TrainingPerson => ({ ...person, name, email: `${name}@example.invalid`, final_exam, ...extra })

// Value: protects=the to-chase list never hides someone who may still owe required work; fails_when=the filter treats unknown or
//   unavailable as finished, or counts people with nothing required; why_new=no test covers this filter; seam=none
test('Incomplete required progress lists everyone short of a confirmed 100%, including unknown coverage', async () => {
  const required = (name: string, value: TrainingPerson['required_progress']): TrainingPerson => ({ ...person, name, email: `${name}@example.invalid`, required_progress: value })
  const people = [
    required('Done', { fraction: 1, total_steps: 11 }), required('Halfway', { fraction: .5, total_steps: 11 }), required('Unstarted', { fraction: 0, total_steps: 11 }),
    required('Uncovered', { fraction: null, total_steps: 11 }), required('Nothing', { fraction: null, total_steps: 0 }), required('Older', undefined),
  ]
  const load = vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people, total_items: 1, history_incomplete: false })
  render(<TrainingDashboard />)
  await screen.findByText('Done')
  within(status()).getByRole('option', { name: 'Incomplete required progress' })
  fireEvent.change(status(), { target: { value: 'required_incomplete' } })
  expect(names()).toEqual(['Halfway', 'Uncovered', 'Unstarted'])
  screen.getByText('3 of 6 people · 1 training items')
  within(screen.getByText('Uncovered').closest('tr')!).getByText('Unavailable')

  // A lower bound under 100% is not proof of finishing, and a confirmed 100% stays out even with gaps elsewhere.
  load.mockResolvedValue({ people, total_items: 1, history_incomplete: true })
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await screen.findByText('At least 50%')
  expect(names()).toEqual(['Halfway', 'Uncovered', 'Unstarted'])
})

test('the filter is scoped to the tab and clears with it, like the other progress statuses', async () => {
  const data = report()
  data.groups = ['Team One', 'Team Two']
  data.people[0] = { ...data.people[0], groups: ['Team One'], required_progress: { fraction: .5, total_steps: 2 } }
  data.people[1] = { ...data.people[1], groups: ['Team Two'], required_progress: { fraction: .5, total_steps: 2 } }
  vi.spyOn(api, 'trainingProgress').mockResolvedValue(data)
  render(<TrainingDashboard />)
  await screen.findByText('Asha')
  fireEvent.change(status(), { target: { value: 'required_incomplete' } })
  expect(names()).toEqual(['Asha'])
  fireEvent.click(screen.getByRole('tab', { name: 'Team Two' }))
  expect((status() as HTMLSelectElement).value).toBe('all')
  expect(names()).toEqual(['Ben'])
})

// Value: protects=each column heading sorts by what its cells show; fails_when=a heading sorts the wrong field or its reverse
//   breaks ties differently; why_new=only the dropdown's four sorts were tested; seam=none
test('every data column sorts from its heading, reverses on a second press, and breaks ties by name', async () => {
  const people: TrainingPerson[] = [
    { ...person, name: 'Cara', email: 'cara@example.invalid', percent: 50, completed_items: 2, quizzes_passed: 1, last_activity_at: 300, required_progress: { fraction: .5, total_steps: 4 }, final_exam: standing({ passed: true }) },
    { ...person, name: 'Abe', email: 'abe@example.invalid', percent: 10, completed_items: 1, quizzes_passed: 2, last_activity_at: null, required_progress: { fraction: null, total_steps: 4 }, final_exam: standing({ locked_by: 9 }) },
    { ...person, name: 'Bo', email: 'bo@example.invalid', percent: null, completed_items: 2, quizzes_passed: 0, last_activity_at: 100, required_progress: { fraction: 1, total_steps: 4 }, final_exam: null },
    { ...person, name: 'Dee', email: 'dee@example.invalid', percent: 50, completed_items: 0, quizzes_passed: 2, last_activity_at: 200, required_progress: { fraction: .254, total_steps: 4 }, final_exam: standing({ attempts: 2, best_score: 12 }) },
  ]
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people, total_items: 3, history_incomplete: false, quiz_total: 2 })
  render(<TrainingDashboard />)
  await screen.findByText('Abe')
  const pressed = (title: string) => screen.getByRole('columnheader', { name: title }).getAttribute('aria-sort')
  expect(names()).toEqual(['Abe', 'Bo', 'Cara', 'Dee'])
  expect(pressed('Person')).toBe('ascending')
  expect(screen.getAllByRole('columnheader').slice(0, -1).map(header => header.textContent)).toEqual(['Person', 'Required progress', 'Overall progress', 'Items completed', 'Chapter quizzes passed', 'Final Exam', 'Last viewed'])
  // Details is a control, not data.
  expect(within(screen.getAllByRole('columnheader').at(-1)!).queryByRole('button')).toBeNull()

  sortBy('Person')
  expect(names()).toEqual(['Dee', 'Cara', 'Bo', 'Abe'])
  expect(pressed('Person')).toBe('descending')

  for (const [title, lowest, highest] of [
    // Unavailable sits below 0%, in the order the table reads.
    ['Required progress', ['Abe', 'Dee', 'Cara', 'Bo'], ['Bo', 'Cara', 'Dee', 'Abe']],
    // Cara and Dee show the same 50%, so they read by name both ways.
    ['Overall progress', ['Bo', 'Abe', 'Cara', 'Dee'], ['Cara', 'Dee', 'Abe', 'Bo']],
    ['Items completed', ['Dee', 'Abe', 'Bo', 'Cara'], ['Bo', 'Cara', 'Abe', 'Dee']],
    ['Chapter quizzes passed', ['Bo', 'Cara', 'Abe', 'Dee'], ['Abe', 'Dee', 'Cara', 'Bo']],
    ['Final Exam', ['Bo', 'Abe', 'Dee', 'Cara'], ['Cara', 'Dee', 'Abe', 'Bo']],
  ] as const) {
    sortBy(title)
    expect(names()).toEqual(lowest)
    expect(pressed(title)).toBe('ascending')
    expect(pressed('Person')).toBeNull()
    sortBy(title)
    expect(names()).toEqual(highest)
    expect(pressed(title)).toBe('descending')
  }

  // Last viewed starts with the most recent; no record reads as the oldest.
  sortBy('Last viewed')
  expect(names()).toEqual(['Cara', 'Dee', 'Bo', 'Abe'])
  expect(pressed('Last viewed')).toBe('descending')
  sortBy('Last viewed')
  expect(names()).toEqual(['Abe', 'Bo', 'Dee', 'Cara'])
})

test('Final Exam sorts by how far each person has got', async () => {
  const people = [
    exam('Passed', standing({ passed: true, attempts: 1, best_score: 18 })), exam('Best fifteen', standing({ attempts: 3, best_score: 15 })), exam('Best two', standing({ attempts: 1, best_score: 2 })),
    exam('Optional', standing({ optional: true })), exam('Not taken', standing()), exam('Not ready', standing({ unfinished: ['one'] })), exam('Locked', standing({ locked_by: 9 })), exam('None', null),
  ]
  vi.spyOn(api, 'trainingProgress').mockResolvedValue({ people, total_items: 1, history_incomplete: false })
  render(<TrainingDashboard />)
  await screen.findAllByRole('rowheader')
  sortBy('Final Exam')
  expect(names()).toEqual(['None', 'Locked', 'Not ready', 'Not taken', 'Optional', 'Best two', 'Best fifteen', 'Passed'])
})

// Value: protects=phones, which hide the headings, reach every sort, and the two controls never disagree; fails_when=the dropdown
//   keeps its own state, or sorts by a column no longer shown; why_new=the headings are new; seam=none
test('Sort by offers every column both ways, follows the headings, and falls back when a column disappears', async () => {
  const data: TrainingProgress = { ...report(), quiz_total: 2, people: [exam('Asha', standing()), { ...exam('Ben', null), quizzes_passed: 1 }] }
  const load = vi.spyOn(api, 'trainingProgress').mockResolvedValue(data)
  render(<TrainingDashboard />)
  await screen.findByText('Asha')
  const control = screen.getByRole('combobox', { name: 'Sort by' }) as HTMLSelectElement
  expect(within(control).getAllByRole('option').map(option => option.textContent)).toEqual([
    'Name, A to Z', 'Name, Z to A', 'Lowest required progress', 'Highest required progress', 'Lowest overall progress', 'Highest overall progress',
    'Fewest items completed', 'Most items completed', 'Fewest quizzes passed', 'Most quizzes passed', 'Final Exam, least progress', 'Final Exam, most progress', 'Oldest activity', 'Recent activity',
  ])
  sortBy('Chapter quizzes passed')
  expect(control.selectedOptions[0].textContent).toBe('Fewest quizzes passed')
  sortBy('Chapter quizzes passed')
  expect(control.selectedOptions[0].textContent).toBe('Most quizzes passed')
  expect(names()).toEqual(['Ben', 'Asha'])
  fireEvent.change(control, { target: { value: 'quizzes:ascending' } })
  expect(names()).toEqual(['Asha', 'Ben'])
  expect(screen.getByRole('columnheader', { name: 'Chapter quizzes passed' }).getAttribute('aria-sort')).toBe('ascending')

  load.mockResolvedValue({ ...data, quiz_total: 0, people: [{ ...data.people[1], final_exam: null }, data.people[0]] })
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await waitFor(() => expect(screen.queryByRole('columnheader', { name: /quizzes passed/i })).toBeNull())
  expect(control.selectedOptions[0].textContent).toBe('Name, A to Z')
  expect(screen.getByRole('columnheader', { name: 'Person' }).getAttribute('aria-sort')).toBe('ascending')
})

test('sorting keeps the open details with their person', async () => {
  vi.spyOn(api, 'trainingProgress').mockResolvedValue(report())
  render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('button', { name: 'View progress for Asha' }))
  sortBy('Person')
  const details = screen.getByText('Content progress for Asha').closest('tr')!
  expect(details.previousElementSibling).toBe(screen.getByText('Asha').closest('tr'))
})

test('a CSV keeps every value as text a spreadsheet will not run, across any separator', () => {
  const text = csv({ headers: ['Name', 'Note'], rows: [
    ['=HYPERLINK("http://x","y")', '+1'], ['-2', '@sum(a1)'], ['\t=1+1', ' =1+1'], ['＝1', 'a,b;c'], ['line\nbreak', 'ปาล์ม'], ['Asha "A"', '—'], ['25%', 'At least 25%'],
  ] })
  expect(text.charCodeAt(0)).toBe(0xFEFF)
  expect(text.slice(1).split('\r\n')).toEqual([
    `"Name","Note"`,
    `"'=HYPERLINK(""http://x"",""y"")","'+1"`,
    `"'-2","'@sum(a1)"`,
    `"'\t=1+1","' =1+1"`,
    `"'＝1","a,b;c"`,
    `"line\nbreak","ปาล์ม"`,
    `"Asha ""A""","—"`,
    `"25%","At least 25%"`,
    '',
  ])
})

/** Everything an admin gets from Export report → CSV: the file name and its lines, without the byte-order mark. */
async function exportCsv(data: TrainingProgress, before: () => void = () => {}) {
  vi.spyOn(api, 'trainingProgress').mockResolvedValue(data)
  const created = vi.spyOn(URL, 'createObjectURL')
  let name = ''
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { name = this.download })
  render(<TrainingDashboard />)
  await screen.findAllByRole('rowheader')
  before()
  fireEvent.click(screen.getByRole('button', { name: 'Export report' }))
  fireEvent.click(screen.getByRole('button', { name: /^CSV file/ }))
  const blob = created.mock.calls.at(-1)![0] as Blob
  expect(blob.type).toBe('text/csv;charset=utf-8')
  const text = await new Promise<string>(resolve => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.readAsText(blob)
  })
  return { name, lines: text.replace(/^\uFEFF/, '').trimEnd().split('\r\n') }
}

// Value: protects=the file matches the screen: same tab, filters and order, in the words the table uses; fails_when=the export reads
//   the whole report, loses the Unknown and lower-bound wording, or leaves a filtered file looking like the whole tab; why_new=new; seam=URL.createObjectURL
test('a CSV exports the current tab as shown, in order, keeping missing history honest', async () => {
  const data: TrainingProgress = {
    people: [
      { ...person, name: 'Asha', role: 'admin', groups: ['Team One'], percent: 25, completed_items: 2, quizzes_passed: 1, quiz_exempt: true, last_activity_at: 1_700_000_000, required_progress: { fraction: .5, total_steps: 4 }, final_exam: standing({ optional: true, attempts: 2, best_score: 12 }) },
      { ...person, name: 'Ben', email: 'ben@example.invalid', groups: ['Team One'], percent: 0, status: 'unknown', completed_items: 0, quizzes_passed: 0, last_activity_at: null, required_progress: { fraction: 0, total_steps: 4 }, final_exam: standing({ locked_by: 9 }) },
      { ...person, name: 'Cara', email: 'cara@example.invalid', groups: ['Team Two'], required_progress: { fraction: 1, total_steps: 4 } },
    ],
    total_items: 4, quiz_total: 9, history_incomplete: true, groups: ['Team One', 'Team Two'],
  }
  const { name, lines } = await exportCsv(data, () => sortBy('Required progress'))
  expect(name).toBe(`Staff progress - Team One - ${new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'America/New_York' })}.csv`)
  expect(lines).toEqual([
    '"Name","Email","Role","Required progress","Overall progress","Status","Items completed, recorded (of 4)","Chapter quizzes passed, recorded (of 9)","Final Exam, recorded","Last viewed (Eastern)"',
    '"Ben","ben@example.invalid","Staff","Unknown","Unknown","History incomplete","0","0","Locked","No record"',
    '"Asha","asha@example.invalid","Admin","At least 50%","At least 25%","In progress","2","1 (optional)","Best 12/20 (optional)","Nov 14, 2023 5:13 PM"',
  ])
})

test('a filtered export says so in its name, and All locations adds each person’s locations', async () => {
  const data: TrainingProgress = { ...report(), groups: ['Team One', 'Team Two'], people: [
    { ...person, groups: ['Team One', 'Team Two'], last_activity_at: 1_700_000_000, required_progress: { fraction: .5, total_steps: 4 } },
    { ...person, name: 'Ben', email: 'ben@example.invalid', groups: ['Team Two'], required_progress: { fraction: 1, total_steps: 4 } },
    { ...person, name: 'No steps', email: 'none@example.invalid', groups: ['Team Two'], required_progress: { fraction: null, total_steps: 0 } },
  ] }
  const { name, lines } = await exportCsv(data, () => {
    fireEvent.click(screen.getByRole('tab', { name: 'All locations' }))
    fireEvent.change(status(), { target: { value: 'required_incomplete' } })
  })
  expect(name).toMatch(/^Staff progress - All locations - .* \(1 of 3 people\)\.csv$/)
  expect(lines[0]).toBe('"Name","Email","Role","Locations","Required progress","Overall progress","Status","Items completed (of 1)","Last viewed (Eastern)"')
  expect(lines.slice(1)).toEqual(['"Asha","asha@example.invalid","Staff","Team One, Team Two","50%","25%","In progress","0","Nov 14, 2023 5:13 PM"'])
})

// Value: protects=a file reader sees the same missing-data words as the table; fails_when=reportTable, which restates the cells' rules,
//   drops the no-required-content case or a missing Final Exam placeholder; why_new=only on-screen wording and filtered-out rows were
//   checked; seam=none; no_claim=opening the file in a real spreadsheet (Blob read in jsdom)
test('a CSV separates nothing required, unavailable progress and no Final Exam, as the table does', async () => {
  const { lines } = await exportCsv({ ...report(), people: [
    exam('Asha', standing(), { required_progress: { fraction: null, total_steps: 0 } }),
    exam('Ben', null, { required_progress: { fraction: null, total_steps: 4 } }),
  ] })
  const column = (title: string) => lines[0].split('","').findIndex(header => header.replaceAll('"', '') === title)
  const cell = (row: string, title: string) => row.split('","')[column(title)].replaceAll('"', '')
  expect(lines.slice(1).map(row => [cell(row, 'Required progress'), cell(row, 'Final Exam')])).toEqual([['No required content', 'Not taken'], ['Unavailable', '—']])
})

test('characters a file name cannot hold become hyphens, and the download is released afterward', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  try {
    const { name } = await exportCsv({ ...report(), groups: ['Bar/Grill: "North"'], people: [{ ...person, groups: ['Bar/Grill: "North"'] }] })
    expect(name).toMatch(/^Staff progress - Bar-Grill- -North- - .*\.csv$/)
    const revoked = vi.spyOn(URL, 'revokeObjectURL')
    vi.advanceTimersByTime(60_000)
    expect(revoked).toHaveBeenCalledTimes(1)
  } finally {
    vi.useRealTimers()
  }
})

// Value: protects=the PDF shows only this report, including when a certificate is open, whose own print rules would blank it;
//   fails_when=print runs before the report is on the page, or both print roots are mounted; why_new=new; seam=window.print
test('PDF prints the current view alone, closing an open certificate first', async () => {
  const [asha, ben] = report().people
  const data: TrainingProgress = { ...report(), quiz_total: 2, people: [{ ...asha, certificate: issued, required_progress: { fraction: .5, total_steps: 4 } }, { ...ben, required_progress: { fraction: 1, total_steps: 4 } }] }
  vi.spyOn(api, 'trainingProgress').mockResolvedValue(data)
  document.title = 'Postern'
  let printed = { title: '', report: '', certificate: true, rows: 0 }
  const print = vi.spyOn(window, 'print').mockImplementation(() => {
    const root = document.body.querySelector(':scope > .print-report')!
    printed = { title: document.title, report: root.textContent!, certificate: !!document.querySelector('.print-root'), rows: root.querySelectorAll('tbody tr').length }
  })
  const view = render(<TrainingDashboard />)
  fireEvent.click(await screen.findByRole('button', { name: 'View progress for Asha' }))
  fireEvent.click(screen.getByRole('button', { name: 'View certificate' }))
  expect(document.querySelector('.print-root')).not.toBeNull()
  fireEvent.change(status(), { target: { value: 'required_incomplete' } })
  fireEvent.click(screen.getByRole('button', { name: 'Export report' }))
  fireEvent.click(screen.getByRole('button', { name: /^PDF file/ }))

  expect(print).toHaveBeenCalledTimes(1)
  expect(printed.certificate).toBe(false)
  expect(printed.rows).toBe(1)
  expect(printed.title).toMatch(/^Staff progress - Staff - .* \(1 of 2 people\)$/)
  for (const text of ['Staff progress: Staff', '1 of 2 people', 'Showing: Incomplete required progress · Sorted by: Name, A to Z',
    'Staff: Staff 2 · Required progress 75% · Overall progress 12% · All items completed 0 · Not started 1',
    'Required progress covers Chapters 1–9', 'Activity does not confirm understanding.', 'Asha']) expect(printed.report).toContain(text)
  expect(printed.report).not.toContain('Ben')
  // Landscape only while the report is printable: other screens print with the browser's settings.
  expect(document.querySelector('.print-report style')?.textContent).toContain('@page { size: landscape')
  expect(screen.queryByText('Content progress for Asha')).toBeNull()

  // Details lead to certificates, which print alone, so the report leaves the page while they are open.
  fireEvent.click(screen.getByRole('button', { name: 'View progress for Asha' }))
  fireEvent.click(screen.getByRole('button', { name: 'View certificate' }))
  expect(document.querySelector('.print-report')).toBeNull()
  expect(document.querySelector('.print-root')).not.toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Hide progress for Asha' }))
  expect(document.querySelector('.print-root')).toBeNull()
  // Leaving the page restores its title and removes the report.
  view.unmount()
  expect(document.title).toBe('Postern')
  expect(document.querySelector('.print-report')).toBeNull()
})

test('the printed report keeps missing-history and missing-email warnings, and the next export follows the tab', async () => {
  const data: TrainingProgress = { ...report(), history_incomplete: true, groups: ['Team One', 'Team Two'], roster_warnings: [{ group: 'Team One', row: 16, message: 'Missing email address' }], people: [{ ...person, groups: ['Team One'] }, { ...person, name: 'Ben', email: 'ben@example.invalid', groups: ['Team Two'] }] }
  vi.spyOn(api, 'trainingProgress').mockResolvedValue(data)
  const print = vi.spyOn(window, 'print').mockImplementation(() => {})
  render(<TrainingDashboard />)
  await screen.findByText('Asha')
  const exportPdf = () => { fireEvent.click(screen.getByRole('button', { name: 'Export report' })); fireEvent.click(screen.getByRole('button', { name: /^PDF file/ })) }
  exportPdf()
  const printed = () => document.querySelector('.print-report')!.textContent!
  for (const text of ['Some viewing history is missing.', 'Spreadsheet row 16: Missing email address.', 'Items completed, recorded (of 1)', 'Asha']) expect(printed()).toContain(text)
  // On paper a person's details stack in one column, as on screen; a spreadsheet keeps them apart.
  expect([...document.querySelectorAll('.print-report th')].map(header => header.textContent)).toEqual(['Person', 'Required progress', 'Overall progress', 'Status', 'Items completed, recorded (of 1)', 'Last viewed (Eastern)'])
  expect(document.querySelector('.print-report td')!.textContent).toBe('Asha\nasha@example.invalid')
  fireEvent.click(screen.getByRole('tab', { name: 'Team Two' }))
  exportPdf()
  expect(print).toHaveBeenCalledTimes(2)
  expect(printed()).toContain('Staff progress: Team Two')
  expect(printed()).not.toContain('Spreadsheet row 16') // Another tab's rows stay out of this tab's report.
  expect(printed()).toContain('Ben')
  expect(printed()).not.toContain('Asha')
})

test('Export report opens a small menu that closes on Escape, an outside press and leaving by keyboard, and needs someone to export', async () => {
  vi.spyOn(api, 'trainingProgress').mockResolvedValue(report())
  render(<TrainingDashboard />)
  await screen.findByText('Asha')
  const toggle = screen.getByRole('button', { name: 'Export report' })
  const menu = () => screen.queryByRole('group', { name: 'Export format' })
  expect(menu()).toBeNull()
  fireEvent.click(toggle)
  expect(toggle.getAttribute('aria-expanded')).toBe('true')
  within(menu()!).getByText('Exports the 2 of 2 people shown, in this order.')
  within(menu()!).getByText(/Opens the print window/)
  fireEvent.keyDown(within(menu()!).getByRole('button', { name: /^CSV file/ }), { key: 'Escape' })
  expect(menu()).toBeNull()
  expect(document.activeElement).toBe(toggle)
  fireEvent.click(toggle)
  fireEvent.pointerDown(within(menu()!).getByRole('button', { name: /^PDF file/ }))
  expect(menu()).not.toBeNull()
  fireEvent.pointerDown(document.body)
  expect(menu()).toBeNull()
  fireEvent.click(toggle)
  fireEvent.focusIn(screen.getByRole('searchbox', { name: 'Find a person or certificate' }))
  expect(menu()).toBeNull()
  fireEvent.change(screen.getByRole('searchbox', { name: 'Find a person or certificate' }), { target: { value: 'nobody here' } })
  screen.getByText('No people match these filters.')
  expect(screen.getByRole('button', { name: 'Export report' })).toHaveProperty('disabled', true)
})
