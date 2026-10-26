import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { Admin } from './admin'
import { api, ApiError } from './api'
import { Categories } from './categories'
import { draft, installApi, world } from './testing'

afterEach(() => vi.restoreAllMocks())

test('rename updates the grouped category, preserves posts and refreshes the reading room', async () => {
  const first = { ...draft('p1', 'First document'), category: 'module1', status: 'published' as const }
  const second = { ...draft('p2', 'Older document'), category: 'Module 1', status: 'archived' as const }
  const state = world({ posts: [first, second] })
  installApi(state)
  const renamed = vi.spyOn(api, 'renameCategory').mockImplementation(async (category, name) => {
    expect(category).toBe('Module 1')
    state.posts = state.posts.map(post => ({ ...post, category: name }))
    return { name, updated: 2 }
  })
  const changed = vi.fn()
  render(<Admin onMetadataChanged={changed} />)
  fireEvent.click(await screen.findByText('Categories (1)'))
  fireEvent.click(screen.getByRole('button', { name: 'Rename Module 1' }))
  expect(screen.getByText(/This updates all 2 posts/)).toBeTruthy()
  fireEvent.change(screen.getByRole('textbox', { name: 'New category name' }), { target: { value: '  Orientation  ' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save category name' }))
  await screen.findByRole('button', { name: 'Rename Orientation' })
  expect(renamed).toHaveBeenCalledWith('Module 1', 'Orientation')
  expect(changed).toHaveBeenCalledOnce()
  expect(state.posts.map(post => [post.id, post.status])).toEqual([['p1', 'published'], ['p2', 'archived']])
  expect(screen.getByRole('status').textContent).toContain('2 posts updated')
})

test('cancel and blank names send nothing; a conflicting name can be corrected', async () => {
  const renamed = vi.spyOn(api, 'renameCategory').mockRejectedValueOnce(new ApiError(409, 'category_exists', 'That category already exists. Choose a different name.')).mockResolvedValue({ name: 'Welcome', updated: 1 })
  const changed = vi.fn().mockResolvedValue(undefined)
  render(<Categories posts={[{ ...draft('p1', 'Document'), category: '' }]} onChanged={changed} />)
  fireEvent.click(screen.getByText(/^Categories \(/))
  fireEvent.click(screen.getByRole('button', { name: 'Rename General' }))
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(renamed).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Rename General' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'New category name' }), { target: { value: ' ' } })
  expect(screen.getByRole('button', { name: 'Save category name' })).toHaveProperty('disabled', true)
  fireEvent.change(screen.getByRole('textbox', { name: 'New category name' }), { target: { value: 'Existing' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save category name' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('alert').textContent).toContain('already exists')
  fireEvent.change(screen.getByRole('textbox', { name: 'New category name' }), { target: { value: 'Welcome' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save category name' }))
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Welcome'))
})

test('pending catalog changes are visible and prevent another rename', () => {
  render(<Categories posts={[{ ...draft('p1', 'Document'), category: 'Policies', category_sync_pending: true }]} onChanged={async () => {}} />)
  fireEvent.click(screen.getByText(/^Categories \(/))
  expect(screen.getByRole('button', { name: 'Rename Policies' })).toHaveProperty('disabled', true)
  screen.getByText(/Saving category changes/)
})

test('slow category saves prevent duplicate submissions and survive leaving the panel', async () => {
  let finish!: (result: { name: string; updated: number }) => void
  const renamed = vi.spyOn(api, 'renameCategory').mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const changed = vi.fn().mockResolvedValue(undefined)
  const panel = render(<Categories posts={[draft('p1', 'Document')]} onChanged={changed} />)
  fireEvent.click(screen.getByText(/^Categories \(/))
  fireEvent.click(screen.getByRole('button', { name: 'Rename Policies' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'New category name' }), { target: { value: 'Welcome' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save category name' }))
  const saving = screen.getByRole('button', { name: 'Saving…' })
  expect(saving).toHaveProperty('disabled', true)
  expect(screen.getByRole('button', { name: 'Cancel' })).toHaveProperty('disabled', true)
  fireEvent.submit(saving.closest('form')!)
  expect(renamed).toHaveBeenCalledOnce()
  panel.unmount()
  finish({ name: 'Welcome', updated: 1 })
  await waitFor(() => expect(changed).toHaveBeenCalledOnce())
})

test('empty categories and delayed backups explain their state', () => {
  const panel = render(<Categories posts={[]} onChanged={async () => {}} />)
  fireEvent.click(screen.getByText(/^Categories \(/))
  screen.getByText('Categories appear after you create a post.')
  expect(screen.queryByRole('button')).toBeNull()
  panel.rerender(<Categories posts={[{ ...draft('p1', 'Document'), category_sync_pending: true, error: 'Offline' }]} onChanged={async () => {}} />)
  screen.getByText(/backup is delayed and will retry automatically/)
})

test.each(['category_sync_pending', 'title_sync_pending'] as const)('admin polls %s until confirmed, and clears its timer', async pending => {
  const state = world({ posts: [{ ...draft('p1', 'Document'), [pending]: true }] })
  installApi(state)
  const listed = vi.spyOn(api, 'adminPosts')
  const scheduled = vi.spyOn(window, 'setInterval')
  const cleared = vi.spyOn(window, 'clearInterval')
  const panel = render(<Admin />)
  fireEvent.click(await screen.findByText('Categories (1)'))
  await screen.findByRole('button', { name: 'Rename Policies' })
  await waitFor(() => expect(scheduled).toHaveBeenCalled())
  const timerIndex = scheduled.mock.calls.findIndex(call => call[1] === 3000)
  const timer = scheduled.mock.results[timerIndex].value as number
  state.posts = [{ ...state.posts[0], [pending]: false }]
  const before = listed.mock.calls.length
  await act(async () => { (scheduled.mock.calls[timerIndex][0] as () => void)() })
  expect(listed.mock.calls.length).toBe(before + 1)
  expect(screen.getByRole('button', { name: 'Rename Policies' })).toHaveProperty('disabled', false)
  expect(cleared).toHaveBeenCalledWith(timer)
  panel.unmount()
})


test.each([{ title_sync_pending: true }, { status: 'publishing' as const }])('explains why another content operation blocks category renaming: %j', pending => {
  render(<Categories posts={[{ ...draft('p1', 'Document'), category: 'Policies', ...pending }]} onChanged={async () => {}} />)
  fireEvent.click(screen.getByText(/^Categories \(/))
  expect(screen.getByRole('button', { name: 'Rename Policies' })).toHaveProperty('disabled', true)
  screen.getByText(/Waiting for content changes to finish/)
})
