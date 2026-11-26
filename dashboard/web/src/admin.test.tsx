import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { Admin } from './admin'
import { Denials, Report } from './report'
import { calls, draft, installApi, post, world } from './testing'

afterEach(() => { vi.unstubAllGlobals() })

function clipboard(write: () => Promise<void>) {
  const writeText = vi.fn(write)
  vi.stubGlobal('navigator', Object.create(navigator, { clipboard: { value: { writeText } } }))
  return writeText
}

async function openPublished() {
  installApi(world({ posts: [post('p1', 'Tip pooling policy')] }))
  render(<Admin />)
  await waitFor(() => screen.getByText('Tip pooling policy'))
  fireEvent.click(screen.getByRole('button', { name: 'Manage' }))
  return await screen.findByRole('button', { name: 'Copy link' })
}

// Value: protects=an admin whose browser blocks copying can still take the post link;
//   fails_when=Copy link fails silently with no manual copy field;
//   why_new=no test reached the failure branch of Copy link; seam=navigator.clipboard stubbed; no_claim=text selection on focus
test('a blocked clipboard offers the post link for manual copy', async () => {
  clipboard(async () => { throw new Error('blocked') })
  fireEvent.click(await openPublished())
  expect(await screen.findByLabelText('Post link — select and copy')).toHaveProperty('value', `${window.location.origin}/post/p1`)
  screen.getByRole('button', { name: 'Copy link' })
})

// Value: protects=a copy that works on a retry says so and takes the manual field away;
//   fails_when=the success label is lost, or the fallback stays up after a successful copy;
//   why_new=the success branch had no test; seam=navigator.clipboard stubbed; no_claim=real clipboard access
test('after a blocked copy, a successful retry says the link was copied and hides the manual field', async () => {
  let blocked = true
  const writeText = clipboard(async () => { if (blocked) throw new Error('blocked') })
  fireEvent.click(await openPublished())
  await screen.findByLabelText('Post link — select and copy')
  blocked = false
  fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))
  await screen.findByRole('button', { name: 'Link copied' })
  expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/post/p1`)
  expect(screen.queryByLabelText('Post link — select and copy')).toBeNull()
})

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
