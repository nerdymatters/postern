import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { api, ApiError, type TrainingItem, type TrainingPerson, type TrainingProgress } from './api'
import { TrainingDashboard } from './training-progress'
import { App } from './app'
import { installApi, world } from './testing'

const item: TrainingItem = { id: 'video', post_id: 'post', title: 'Kitchen safety', category: 'Module 1', filename: 'safety.mp4', kind: 'video', pages: 0, duration_seconds: 100, opened_pages: 0, watched_seconds: 25, fraction: .25, opened: true, attempts: 1, last_activity_at: 100 }
const person: TrainingPerson = { email: 'asha@example.invalid', name: 'Asha', role: 'reader', percent: 25, status: 'in_progress', completed_items: 0, total_items: 1, opened_items: 1, last_activity_at: 100, items: [item] }
function report(): TrainingProgress { return { people: [{ ...person }, { ...person, name: 'Ben', email: 'ben@example.invalid', percent: 0, status: 'not_started', opened_items: 0, last_activity_at: null, items: [{ ...item, fraction: 0, opened: false, watched_seconds: 0, attempts: 2, last_activity_at: null }] }], total_items: 1, history_incomplete: false } }
afterEach(() => vi.restoreAllMocks())

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
  fireEvent.change(screen.getByRole('searchbox', { name: 'Find a person' }), { target: { value: 'no match' } })
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
  screen.getByText('1 of 1 people · 1 training items')
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
  fireEvent.change(screen.getByRole('searchbox', { name: 'Find a person' }), { target: { value: 'missing' } })
  screen.getByText('No people match these filters.')
  fireEvent.change(screen.getByRole('searchbox', { name: 'Find a person' }), { target: { value: 'BEN@EXAMPLE' } })
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
  fireEvent.change(screen.getByRole('combobox', { name: 'Sort by' }), { target: { value: 'low' } })
  expect(names()[0]).toContain('Ben')
  fireEvent.change(screen.getByRole('combobox', { name: 'Sort by' }), { target: { value: 'high' } })
  expect(names()[0]).toContain('Asha')
  fireEvent.change(screen.getByRole('combobox', { name: 'Sort by' }), { target: { value: 'recent' } })
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
