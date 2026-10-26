import { act, render, screen, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import type { Post } from './api'
import { Reader } from './reader'
import { calls, canvasOps, installApi, post, viewPosts, world } from './testing'

test('blur, focus, a re-render and a remount still produce exactly one view POST', async () => {
  const state = world()
  installApi(state)
  const chosen = state.posts[0]

  const first = render(<Reader post={chosen} onBack={() => {}} />)
  await waitFor(() => expect(viewPosts()).toHaveLength(1))
  await waitFor(() => expect(canvasOps.drawn).toBe(1))

  act(() => {
    window.dispatchEvent(new Event('blur'))
    window.dispatchEvent(new Event('focus'))
  })
  first.rerender(<Reader post={chosen} onBack={() => {}} />)
  first.unmount()

  render(<Reader post={chosen} onBack={() => {}} />)
  await waitFor(() => expect(screen.getByText('Page 1 of 8')).toBeTruthy())
  expect(viewPosts()).toHaveLength(1)
})

test('paging fetches each page with the bearer token and never opens a second view', async () => {
  const state = world()
  installApi(state)
  render(<Reader post={state.posts[0]} onBack={() => {}} />)
  await waitFor(() => expect(canvasOps.drawn).toBe(1))

  act(() => { screen.getByRole('button', { name: 'Next' }).click() })
  await waitFor(() => expect(screen.getByText('Page 2 of 8')).toBeTruthy())
  await waitFor(() => expect(canvasOps.drawn).toBe(2))

  expect(calls.filter(call => call.url.endsWith('/page/1.jpg'))).toHaveLength(1)
  expect(viewPosts()).toHaveLength(1)
  expect(screen.queryByText(/download/i)).toBeNull()
})

test('opening a document keeps its title in view, and turning a page brings that page into view', async () => {
  // Value: protects=only a page turn scrolls the viewer, so opening an item shows its chapter and title first; fails_when=Document scrolls on every page display again or Next stops marking the turn; why_new=no test observes viewer scrolling; seam=none
  const scrolled = vi.fn()
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scrolled })
  try {
    const state = world()
    installApi(state)
    render(<Reader post={state.posts[0]} onBack={() => {}} />)
    await waitFor(() => expect(canvasOps.drawn).toBe(1))
    expect(scrolled).not.toHaveBeenCalled()
    act(() => { screen.getByRole('button', { name: 'Next' }).click() })
    await waitFor(() => expect(canvasOps.drawn).toBe(2))
    expect(scrolled).toHaveBeenCalledTimes(1)
    expect(scrolled.mock.contexts[0]).toBeInstanceOf(HTMLCanvasElement)
  } finally {
    delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
  }
})

test('leaving the viewer clears the canvas', async () => {
  const state = world()
  installApi(state)
  const view = render(<Reader post={state.posts[0]} onBack={() => {}} />)
  await waitFor(() => expect(canvasOps.drawn).toBe(1))
  view.unmount()
  expect(canvasOps.cleared).toBeGreaterThan(0)
})

test('the final page offers the next attachment, then the next module in numeric order', async () => {
  const first = { ...post('first', 'First lesson', 2), category: 'Module1' }
  first.items.push({ ...first.items[0], id: 'second-attachment', filename: 'second.pdf', pages: 1, sort: 1 })
  const next = { ...post('next', 'Second module', 1), category: 'Module 2' }
  const last = { ...post('last', 'Tenth module', 1), category: 'Module 10' }
  const posts = [last, next, first]
  installApi(world({ posts }))
  const onOpen = vi.fn()
  render(<Reader post={first} posts={posts} onBack={() => {}} onOpen={onOpen} />)
  await waitFor(() => expect(canvasOps.drawn).toBe(1))
  expect(screen.queryByRole('button', { name: /Next document:/ })).toBeNull()
  act(() => screen.getByRole('button', { name: 'Next' }).click())
  await waitFor(() => screen.getByRole('button', { name: /Next document: second.pdf/ }))
  act(() => screen.getByRole('button', { name: /Next document: second.pdf/ }).click())
  await waitFor(() => screen.getByRole('button', { name: /Next module: Second module/ }))
  act(() => screen.getByRole('button', { name: /Next module: Second module/ }).click())
  expect(onOpen).toHaveBeenCalledWith(next)
})

test('a failed final page cannot expose the next-document action', async () => {
  const first = post('first', 'First lesson', 1)
  const next = post('next', 'Next lesson', 1)
  installApi(world({ posts: [first, next], refuse: { status: 409, error: 'preparing', message: 'Still preparing', path: /page\/0.jpg$/ } }))
  render(<Reader post={first} posts={[first, next]} onBack={() => {}} onOpen={() => {}} />)
  await waitFor(() => screen.getByText('Still preparing'))
  expect(screen.queryByRole('button', { name: /Next document:/ })).toBeNull()
})

test('a photo is one picture with no pager, an unprepared post says so, and a refused view can be retried', async () => {
  const state = world()
  const snapshot: Post = {
    ...post('p9', 'Kitchen photo'),
    items: [{ id: 'item-p9', post_id: 'p9', filename: 'kitchen.heic', kind: 'photo', pages: 0, derive_status: 'ready', derive_error: null, sort: 0 }],
  }
  const fresh = post('p1', 'Tip pooling policy')
  state.posts = [snapshot, fresh]
  installApi(state)
  const photo = render(<Reader post={snapshot} onBack={() => {}} />)
  await waitFor(() => expect(canvasOps.drawn).toBe(1))
  expect(calls.filter(call => call.url.endsWith('/api/views/view-item-p9/photo.jpg'))).toHaveLength(1)
  expect(calls.filter(call => call.url.includes('/page/'))).toHaveLength(0)
  expect(screen.queryByText(/Page 1 of/)).toBeNull()
  expect(viewPosts()).toHaveLength(1)
  photo.unmount()

  const converting: Post = { ...post('p8', 'Not yet'), items: [{ ...post('p8', 'Not yet').items[0], derive_status: 'converting' }] }
  const preparing = render(<Reader post={converting} onBack={() => {}} />)
  screen.getByText('This post is still being prepared. Please try again in a moment.')
  expect(viewPosts()).toHaveLength(1)  // nothing was opened for the unprepared post
  preparing.unmount()

  // The server refuses a view of another attachment (its derivative was evicted): the reason is shown
  // where it happened, the pending request is forgotten, and a remount asks again under the same attempt.
  state.refuse = { status: 409, error: 'preparing', message: 'This attachment is being prepared again. Please try in a moment.', path: /\/views$/ }
  const refusedOnce = render(<Reader post={fresh} onBack={() => {}} />)
  await waitFor(() => screen.getByRole('alert'))
  expect(screen.getByRole('alert').textContent).toBe('This attachment is being prepared again. Please try in a moment.')
  expect(screen.queryByText('Loading…')).toBeNull()
  expect(viewPosts()).toHaveLength(2)
  refusedOnce.unmount()

  state.refuse = null
  render(<Reader post={fresh} onBack={() => {}} />)
  await waitFor(() => expect(canvasOps.drawn).toBe(2))
  expect(viewPosts()).toHaveLength(3)
  const attempts = viewPosts().slice(1).map(call => (JSON.parse(call.body) as { attempt_id: string }).attempt_id)
  expect(attempts[0]).toBe(attempts[1])
})

test('switching attachments from the contents or tabs publishes the selected attachment for routing', async () => {
  const lesson = post('lesson', 'Lesson', 2)
  lesson.items.push({ ...lesson.items[0], id: 'second', filename: 'Second.pdf' })
  installApi(world({ posts: [lesson] }))
  const onOpen = vi.fn()
  render(<Reader post={lesson} selectedItem={lesson.items[0].id} onBack={() => {}} onOpen={onOpen} />)
  act(() => screen.getAllByRole('button', { name: 'Second.pdf' })[0].click())
  expect(onOpen).toHaveBeenCalledWith(lesson, lesson.items[1])
  await waitFor(() => expect(canvasOps.drawn).toBeGreaterThan(0))
  expect(screen.getByLabelText('Second.pdf, page 1')).toBeTruthy()
})

test('a deep selection scrolls only the contents panel and focuses its title once', async () => {
  const posts = Array.from({ length: 12 }, (_, i) => ({ ...post(`m${i}`, `Material ${i}`, 2), category: `Module ${i + 1}` }))
  installApi(world({ posts }))
  const chosen = posts[11]
  const rectangle = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 200, width: 200, height: bottom - top, x: 0, y: top, toJSON: () => ({}) })
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return this.id === 'reader-contents' ? rectangle(80, 600) : this.getAttribute('aria-current') === 'page' ? rectangle(900, 950) : rectangle(0, 0)
  })
  const rendered = render(<Reader post={chosen} posts={posts} onBack={() => {}} onOpen={() => {}} />)
  const selected = screen.getByRole('button', { name: 'Material 11' })
  expect(document.activeElement).toBe(selected)
  expect(document.getElementById('reader-contents')!.scrollTop).toBe(350)
  const back = screen.getByRole('button', { name: 'Back' })
  back.focus()
  rendered.rerender(<Reader post={{ ...chosen }} posts={[...posts]} onBack={() => {}} onOpen={() => {}} />)
  expect(document.activeElement).toBe(back)
  expect(document.getElementById('reader-contents')!.scrollTop).toBe(350)
})
