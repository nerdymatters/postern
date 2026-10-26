import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, test } from 'vitest'
import { App } from './app'
import { setToken } from './api'
import { liveUrls } from './blobs'
import { calls, canvasOps, installApi, objectUrls, viewPosts, world } from './testing'

test('opening a page refreshes module and overall progress without reopening its media', async () => {
  const state = world()
  state.posts.forEach((post, index) => {
    post.category = `Module ${index + 1}`
    post.items[0].pages = 2
    post.items[0].progress = { opened_pages: [], history_incomplete: false }
  })
  installApi(state)
  setToken('session-token')
  render(<App />)
  await waitFor(() => expect(screen.getByRole('progressbar', { name: 'Overall progress' }).getAttribute('aria-valuenow')).toBe('0'))

  fireEvent.click(screen.getByRole('button', { name: 'Tip pooling policy' }))
  await waitFor(() => expect(canvasOps.drawn).toBe(1))
  // The server records the delivered page; the next posts response carries it.
  state.posts[0].items[0].progress!.opened_pages = [0]
  await waitFor(() => expect(screen.getByRole('progressbar', { name: 'Module 1' }).getAttribute('aria-valuenow')).toBe('50'))
  expect(calls.filter(call => call.url.endsWith('/page/0.jpg'))).toHaveLength(1)
  expect(viewPosts()).toHaveLength(1)
  expect(canvasOps.drawn).toBe(1)

  fireEvent.click(screen.getByRole('button', { name: 'Back' }))
  await waitFor(() => expect(screen.getByRole('progressbar', { name: 'Overall progress' }).getAttribute('aria-valuenow')).toBe('25'))
  expect(screen.getByRole('progressbar', { name: 'Module 1' }).getAttribute('aria-valuenow')).toBe('50')
  expect(screen.getByRole('progressbar', { name: 'Module 2' }).getAttribute('aria-valuenow')).toBe('0')
})

test('a progress response from before sign-out cannot overwrite the next reader’s progress', async () => {
  const state = world()
  for (const post of state.posts) post.items[0].progress = { opened_pages: [], history_incomplete: false }
  installApi(state)
  const respond = globalThis.fetch
  let postRequests = 0
  let finishOldRefresh: ((response: Response) => void) | undefined
  globalThis.fetch = (async (input, init) => {
    if (String(input).endsWith('/api/posts') && ++postRequests === 2) {
      return new Promise<Response>(resolve => { finishOldRefresh = resolve })
    }
    return respond(input, init)
  }) as typeof fetch
  setToken('first-reader-token')
  render(<App />)
  await waitFor(() => screen.getByRole('button', { name: 'Tip pooling policy' }))
  fireEvent.click(screen.getByRole('button', { name: 'Tip pooling policy' }))
  await waitFor(() => expect(finishOldRefresh).toBeDefined())

  fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
  await waitFor(() => screen.getByRole('button', { name: 'Continue' }))
  state.session = { ...state.session, email: 'second@example.invalid', name: 'Second Reader' }
  fireEvent.change(screen.getByRole('textbox'), { target: { value: state.session.email } })
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await waitFor(() => screen.getByText(`Signed in as ${state.session.email}`))
  await waitFor(() => expect(screen.getByRole('progressbar', { name: 'Overall progress' }).getAttribute('aria-valuenow')).toBe('0'))

  const oldPosts = state.posts.map(post => ({ ...post, items: post.items.map(item => ({ ...item,
    progress: { opened_pages: Array.from({ length: item.pages }, (_, index) => index), history_incomplete: false },
  })) }))
  await act(async () => {
    finishOldRefresh!(new Response(JSON.stringify({ posts: oldPosts }), { headers: { 'Content-Type': 'application/json' } }))
  })
  expect(screen.getByRole('progressbar', { name: 'Overall progress' }).getAttribute('aria-valuenow')).toBe('0')
  expect(screen.getByText(`Signed in as ${state.session.email}`)).toBeTruthy()
})

test('signing out revokes every object URL and clears the canvas', async () => {
  const state = world()
  installApi(state)
  setToken('session-token')
  window.history.pushState(null, '', '/')
  render(<App />)

  await waitFor(() => screen.getByRole('button', { name: 'Tip pooling policy' }))
  expect(objectUrls.created).toHaveLength(0)
  expect(liveUrls()).toBe(0)
  fireEvent.click(screen.getByRole('button', { name: 'Tip pooling policy' }))
  await waitFor(() => expect(canvasOps.drawn).toBe(1))
  expect(objectUrls.created).toHaveLength(1)

  fireEvent.click(screen.getByText('Sign out'))
  await waitFor(() => screen.getByText('Staff training: finish each chapter, then pass its quiz.'))
  expect(new Set(objectUrls.revoked)).toEqual(new Set(objectUrls.created))
  expect(liveUrls()).toBe(0)
  expect(canvasOps.cleared).toBeGreaterThan(0)
})

test('a deep link to a post opens that post after sign-in', async () => {
  const state = world()
  installApi(state)
  window.history.pushState(null, '', '/post/p2')
  render(<App />)

  await waitFor(() => screen.getByRole('button', { name: 'Continue' }))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'maria.santos@example.invalid' } })
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

  await waitFor(() => screen.getByRole('heading', { name: 'Fall menu rollout' }))
  screen.getByText('Page 1 of 8')
})

test('an unreachable server is said plainly, and Try again reaches it once it is back', async () => {
  globalThis.fetch = (() => Promise.reject(new TypeError('Failed to fetch'))) as typeof fetch
  render(<App />)

  await waitFor(() => screen.getByText('Postern is not answering'))
  screen.getByText('Postern could not be reached. Check your connection.')

  installApi(world())
  fireEvent.click(screen.getByRole('button', { name: /^Try\ again/ }))
  await waitFor(() => screen.getByText('Staff training: finish each chapter, then pass its quiz.'))
})

test('a reader is kept out of the admin screens, and a link to a post they cannot see says so', async () => {
  installApi(world())
  setToken('session-token')
  window.history.pushState(null, '', '/admin')
  const blocked = render(<App />)

  await waitFor(() => screen.getByText('Administrator access is required.'))
  expect(screen.queryByText('New post')).toBeNull()
  expect(screen.queryByText('Admin')).toBeNull()  // no link to a screen the reader cannot use
  expect(calls.filter(call => call.url.endsWith('/api/admin/posts'))).toHaveLength(0)
  blocked.unmount()

  window.history.pushState(null, '', '/post/does-not-exist')
  render(<App />)
  await waitFor(() => screen.getByText('That post is not available to you.'))
  expect(viewPosts()).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: /^Back\ to\ your\ training/ }))
  await waitFor(() => screen.getByRole('button', { name: 'Tip pooling policy' }))
})

test('a sign-in the staff list refuses is told so, and a rate-limited one is told to wait', async () => {
  installApi(world({ refuse: { status: 403, error: 'access_denied', message: 'Your Google account is not on the staff list.', path: /\/api\/auth\/demo$/ } }))
  const denied = render(<App />)
  await waitFor(() => screen.getByRole('button', { name: 'Continue' }))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'stranger@example.invalid' } })
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

  await waitFor(() => screen.getByText('Your access is no longer available'))
  screen.getByText('Your Google account is not on the staff list.')
  expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull()
  denied.unmount()

  installApi(world({ refuse: { status: 429, error: 'rate_limited', message: 'Too many sign-in attempts. Please wait a minute.', path: /\/api\/auth\/demo$/ } }))
  render(<App />)
  await waitFor(() => screen.getByRole('button', { name: 'Continue' }))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'maria.santos@example.invalid' } })
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

  await waitFor(() => screen.getByText('Too many sign-in attempts. Please wait a minute.'))
  // Not one of the four refusals: the landing page keeps the screen and the button comes back.
  expect(screen.getByRole('button', { name: 'Continue' })).toHaveProperty('disabled', false)
  expect(screen.queryByText('Your access is no longer available')).toBeNull()
})
