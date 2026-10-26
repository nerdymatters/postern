import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { api, type Person } from './api'
import { copyText } from './clipboard'
import { Report } from './report'
import { post, report } from './testing'

vi.mock('./clipboard', () => ({ copyText: vi.fn(async () => true) }))
afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks() })

const unknown: Person[] = [0, 2].map(attempts => ({
  email: `unknown-${attempts}@example.invalid`, name: `Unknown reader ${attempts}`, role: 'reader',
  status: 'unknown', first_opened_at: null, attempts, seconds_to_open: null,
}))

test('missing history keeps unknown readers separate and out of unopened reminder recipients', async () => {
  const loaded = report(post('p1', 'Tip pooling policy'))
  loaded.people.push(...unknown)
  loaded.total = loaded.people.length
  loaded.history_incomplete = true
  vi.spyOn(api, 'report').mockResolvedValue(loaded)
  render(<Report postId="p1" onBack={() => {}} />)

  const heading = await screen.findByRole('heading', { name: 'Unknown (2)' })
  const section = within(heading.parentElement!)
  for (const person of unknown) section.getByText(person.name)
  expect(section.queryByText('no attempt yet')).toBeNull()
  expect(section.queryByText(/nothing served/)).toBeNull()
  section.getAllByText('Opening history is incomplete')
  screen.getByText(/1 have not opened it · 2 unknown/)
  screen.getByRole('heading', { name: 'Not opened yet (1)' })

  fireEvent.click(screen.getByRole('button', { name: 'Copy emails' }))
  await waitFor(() => expect(copyText).toHaveBeenCalledWith('carlos.rivera@example.invalid'))
})

test('an entirely unknown report does not claim everyone or nobody has opened it', async () => {
  vi.spyOn(api, 'report').mockResolvedValue({
    ...report(post('p1', 'Tip pooling policy')),
    people: unknown, opened: 0, total: unknown.length, history_incomplete: true,
  })
  render(<Report postId="p1" onBack={() => {}} />)

  await screen.findByRole('heading', { name: 'Unknown (2)' })
  expect(screen.queryByText('Everyone has opened it.')).toBeNull()
  expect(screen.queryByText('Nobody has opened it yet.')).toBeNull()
  expect(screen.queryByText('no attempt yet')).toBeNull()
  expect(screen.queryByText(/nothing served/)).toBeNull()
  const copy = screen.getByRole('button', { name: 'Copy emails' })
  expect(copy).toHaveProperty('disabled', true)
  fireEvent.click(copy)
  expect(copyText).not.toHaveBeenCalled()
})


test.each(['video', 'pdf'] as const)('attempts use clear %s viewing wording', async kind => {
  const loaded = report(post('p1', 'Training')); loaded.post.items[0].kind = kind
  loaded.people[1].attempts = 2
  vi.spyOn(api, 'report').mockResolvedValue(loaded)
  render(<Report postId="p1" onBack={() => {}} />)
  await screen.findByText(kind === 'video' ? 'Tried to open · no playback recorded' : 'Tried to open · no pages loaded')
  expect(screen.queryByText(/nothing served/)).toBeNull()
})

test('mixed document and video posts use neutral viewing wording for attempts', async () => {
  const loaded = report(post('p1', 'Training'))
  loaded.post.items.push({ ...loaded.post.items[0], id: 'video-item', kind: 'video' })
  loaded.people[1].attempts = 1
  vi.spyOn(api, 'report').mockResolvedValue(loaded)
  render(<Report postId="p1" onBack={() => {}} />)
  await screen.findByText('Tried to open · no viewing recorded')
  expect(screen.queryByText(/nothing served/)).toBeNull()
})
