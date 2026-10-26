import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { Admin } from './admin'
import { Denials, Report } from './report'
import { calls, draft, installApi, world } from './testing'

test('a draft is created, published through its status url, and offers a link to copy', async () => {
  const state = world({ posts: [draft('p1', 'Tip pooling policy')] })
  installApi(state)
  render(<Admin />)

  await waitFor(() => screen.getByText('Tip pooling policy'))
  fireEvent.change(screen.getByLabelText('Caption'), { target: { value: 'Effective Oct 6.' } })
  screen.getByText('16/280')

  fireEvent.click(screen.getByRole('button', { name: 'Manage' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Publish' }))

  await waitFor(() => screen.getByRole('button', { name: 'Copy link' }), { timeout: 4000 })
  expect(calls.filter(call => call.url.endsWith('/publish'))).toHaveLength(1)
  expect(calls.filter(call => call.url.includes('/api/admin/operations/')).length).toBeGreaterThan(1)
  screen.getByText(`${window.location.origin}/post/p1`)
})

test('the report reads like the wireframe: a count, who has not opened it, and who has', async () => {
  installApi(world())
  render(<Report postId="p1" onBack={() => {}} />)

  await waitFor(() => screen.getByText(/1 of 2/))
  screen.getByText('opened')
  screen.getByText('Not opened yet (1)')
  screen.getByText('Opened (1)')
  screen.getByText('Carlos Rivera')
  screen.getByText('Not opened yet')
  screen.getByText('Opened means content served to the signed-in reader, not proof it was read.')
})

test('the denied sign-in list names the account that was turned away', async () => {
  installApi(world())
  render(<Denials onBack={() => {}} />)
  await waitFor(() => screen.getByText('stranger@example.invalid'))
})

test('a cancelled publication shows its reason, and an archive that is not confirmed sends nothing', async () => {
  const state = world({ posts: [draft('p1', 'Tip pooling policy')] })
  installApi(state)
  const confirmAll = globalThis.fetch
  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/admin/operations/')) {
      calls.push({ method: 'GET', url, body: '' })
      return json({
        id: 'op-1', post_id: 'p1', kind: 'publish', status: 'cancelled', attempts: 1,
        error: 'The account that requested this publication no longer has permission. Publish it again.',
        post: { status: 'draft', error: null },
      })
    }
    if (url.endsWith('/archive')) {
      calls.push({ method: 'POST', url, body: '' })
      state.posts = state.posts.map(candidate => candidate.id === 'p1' ? { ...candidate, status: 'archived' } : candidate)
      return json({ status: 'archived' })
    }
    return confirmAll(input, init)
  }) as typeof fetch
  render(<Admin />)

  await waitFor(() => screen.getByText('Tip pooling policy'))
  fireEvent.click(screen.getByRole('button', { name: 'Manage' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Publish' }))

  await waitFor(() => screen.getByRole('alert'))
  expect(screen.getByRole('alert').textContent).toContain('no longer has permission')
  expect(screen.queryByRole('button', { name: 'Copy link' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Publish' })).toHaveProperty('disabled', false)
  expect(calls.filter(call => call.url.endsWith('/publish'))).toHaveLength(1)

  window.confirm = () => false
  fireEvent.click(screen.getByRole('button', { name: 'Archive' }))
  expect(calls.filter(call => call.url.endsWith('/archive'))).toHaveLength(0)
  screen.getByRole('button', { name: 'Archive' })

  window.confirm = () => true
  fireEvent.click(screen.getByRole('button', { name: 'Archive' }))
  await waitFor(() => expect(calls.filter(call => call.url.endsWith('/archive'))).toHaveLength(1))
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Archive' })).toBeNull())
  expect(screen.queryByRole('button', { name: 'Publish' })).toBeNull()
  expect(screen.getAllByText(/archived/).length).toBeGreaterThan(0)
})


test('post status filters and search keep archived posts in their own section', async () => {
  const state = world({ posts: [
    { ...draft('draft', 'Draft handbook'), category: 'Policies' },
    { ...draft('live', 'Published video'), status: 'published', category: 'Safety' },
    { ...draft('old', 'Old handbook'), status: 'archived', category: 'Policies' },
  ] })
  installApi(state)
  render(<Admin />)
  const active = (await screen.findByRole('heading', { name: 'Posts (2)' })).closest('section')!
  within(active).getByText('Draft'); within(active).getByText('Published')
  expect(within(active).queryByText('Old handbook')).toBeNull()
  const archives = screen.getByText('Archived (1)').closest('section')!
  within(archives).getByText('Old handbook'); within(archives).getByText('Archived')
  expect(archives.querySelector('details')?.open).toBe(false)
  fireEvent.change(screen.getByRole('combobox', { name: 'Post status' }), { target: { value: 'published' } })
  expect(within(active).queryByText('Draft handbook')).toBeNull()
  within(active).getByText('Published video')
  fireEvent.change(screen.getByRole('searchbox', { name: 'Find a post' }), { target: { value: 'Policies' } })
  within(active).getByText('No active posts match these filters.')
  within(archives).getByText('Old handbook')
  expect(archives.querySelector('details')?.open).toBe(true)
  within(archives).getByText('Archived (1 of 1)')
})
