import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { Admin } from './admin'
import { api, ApiError } from './api'
import { PostTitle } from './post-title'
import { draft, installApi, world } from './testing'

afterEach(() => vi.restoreAllMocks())

test('admin renames a published title and refreshes the reading room', async () => {
  const original = { ...draft('p1', 'Original title'), status: 'published' as const }
  const state = world({ posts: [original] })
  installApi(state)
  const renamed = vi.spyOn(api, 'renamePost').mockImplementation(async (id, title) => {
    state.posts = state.posts.map(post => post.id === id ? { ...post, title } : post)
    return state.posts[0]
  })
  const changed = vi.fn()
  render(<Admin onMetadataChanged={changed} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Manage' }))
  fireEvent.click(screen.getByRole('button', { name: 'Rename title' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'New content title' }), { target: { value: '  Better title  ' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save title' }))
  await screen.findByRole('heading', { name: 'Better title' })
  expect(renamed).toHaveBeenCalledWith('p1', 'Better title')
  expect(changed).toHaveBeenCalledOnce()
  expect(state.posts[0]).toEqual({ ...original, title: 'Better title' })
})

test('blank and cancelled names send nothing; failures keep the form editable and reconcile', async () => {
  const post = draft('p1', 'Original title')
  const renamed = vi.spyOn(api, 'renamePost').mockRejectedValueOnce(new ApiError(409, 'post_busy', 'Try again shortly.')).mockResolvedValue({ ...post, title: 'Updated' })
  const changed = vi.fn().mockResolvedValue(undefined)
  render(<PostTitle post={post} onChanged={changed} />)
  fireEvent.click(screen.getByRole('button', { name: 'Rename title' }))
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(renamed).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Rename title' }))
  const input = screen.getByRole('textbox', { name: 'New content title' })
  expect(input).toHaveProperty('maxLength', 120)
  fireEvent.change(input, { target: { value: '  ' } })
  expect(screen.getByRole('button', { name: 'Save title' })).toHaveProperty('disabled', true)
  fireEvent.change(input, { target: { value: 'Updated' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save title' }))
  await screen.findByText('Try again shortly.')
  await waitFor(() => expect(changed).toHaveBeenCalledOnce())
  fireEvent.click(screen.getByRole('button', { name: 'Save title' }))
  await screen.findByText('Content title saved.')
})

test.each([
  { status: 'publishing' as const },
  { category_sync_pending: true },
  { title_sync_pending: true, error: 'Storage offline' },
])('pending operations prevent another title change: %j', pending => {
  render(<PostTitle post={{ ...draft('p1', 'Original'), ...pending }} onChanged={async () => {}} />)
  expect(screen.getByRole('button', { name: 'Rename title' })).toHaveProperty('disabled', true)
  if ('title_sync_pending' in pending) {
    screen.getByText(/backup is delayed/)
    expect(screen.queryByText(/Storage offline/)).toBeNull()
  } else screen.getByText(/Waiting for content changes to finish/)
})

test('slow title saves prevent duplicate submissions and show backup status', async () => {
  const post = draft('p1', 'Original')
  let finish!: (result: typeof post) => void
  const renamed = vi.spyOn(api, 'renamePost').mockImplementation(() => new Promise(resolve => { finish = resolve }))
  const changed = vi.fn().mockResolvedValue(undefined)
  const panel = render(<PostTitle post={post} onChanged={changed} />)
  fireEvent.click(screen.getByRole('button', { name: 'Rename title' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'New content title' }), { target: { value: 'Updated' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save title' }))
  const saving = screen.getByRole('button', { name: 'Saving…' })
  expect(saving).toHaveProperty('disabled', true)
  expect(screen.getByRole('button', { name: 'Cancel' })).toHaveProperty('disabled', true)
  fireEvent.submit(saving.closest('form')!)
  expect(renamed).toHaveBeenCalledOnce()
  finish({ ...post, title: 'Updated', title_sync_pending: true })
  await waitFor(() => expect(changed).toHaveBeenCalledOnce())
  panel.rerender(<PostTitle post={{ ...post, title: 'Updated', title_sync_pending: true }} onChanged={changed} />)
  screen.getByText('Saving title backup…')
  expect(screen.getByRole('heading').textContent).toBe('Updated')
})
