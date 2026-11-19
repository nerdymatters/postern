import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { setAccountToken, type Account } from './accounts-api'
import { SpaceContentPage } from './space-content'
import { nativeContentRoute, onboardingDestination, SpacesClient } from './spaces-api'
import { SpaceOnboarding } from './spaces'
import { objectUrls } from './testing'

const space = '11111111-1111-4111-8111-111111111111'
const otherSpace = '22222222-2222-4222-8222-222222222222'
const resource = '33333333-3333-4333-8333-333333333333'
const principal = '44444444-4444-4444-8444-444444444444'
const bearer = 'ps_'+'s'.repeat(43)
const props = { credential: 'pa_a', space, resource, kind: 'document' as const, revision: 7 }
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const jpeg = () => new Response(new Uint8Array([255,216,255,217]), { headers: { 'Content-Type': 'image/jpeg' } })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
type Call = { url: string; init: RequestInit; path: string }
function server(kind: 'document' | 'video' = 'document') {
  const calls: Call[] = []
  const state = { refuse: false, tickets: 0, viewer: principal, intercept: null as null | ((call: Call) => Response | Promise<Response> | undefined) }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input), path = new URL(url, 'https://fixture.invalid').pathname
    const call = { url, path, init }; calls.push(call)
    const intercepted = state.intercept?.(call); if (intercepted) return intercepted
    if (path === '/accounts/v1/space-authorizations') return json({ assertion: 'signed-fixture' })
    if (path.endsWith('/session/exchange')) return json({ token: bearer })
    if (path.endsWith('/session/signout')) return json({ signed_out: true })
    if (state.refuse) return json({ error: 'not_found', message: 'Private content unavailable' }, 404)
    const title = path.includes(otherSpace) ? 'Other space resource' : 'Native resource'
    const detail = { id: resource, title, kind, content_revision: 7, state: 'published' }
    if (path.endsWith('/session')) return json({ title: 'Workshop', role: 'member' })
    if (path.endsWith('/resources')) return json({ resources: [detail], next_cursor: null })
    if (path.endsWith(`/resources/${resource}`)) return json(detail)
    if (path.endsWith('/document')) return json({ resource_id: resource, revision: 7, page_count: 2 })
    if (/\/document\/pages\/\d+.jpg$/.test(path)) return jpeg()
    if (path.endsWith('/video/ticket')) return json({ ticket: 'pc_'+String.fromCharCode(97+state.tickets++).repeat(43), expires_in_seconds: 30 })
    if (path.endsWith('/video')) return json({ resource_id: resource, revision: 7, duration_ms: 90000, viewer_label: state.viewer })
    throw new Error('Unexpected fixture URL '+url)
  }))
  return { calls, state }
}
beforeEach(() => {
  vi.stubEnv('VITE_POSTERN_ACCOUNTS_API', '/accounts')
  vi.stubEnv('VITE_POSTERN_SPACES_API', '/resources')
  vi.stubEnv('VITE_POSTERN_NATIVE_CONTENT', 'true')
  setAccountToken('pa_a')
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllEnvs(); vi.unstubAllGlobals() })

test('reader routes require explicit opt-in and canonical immutable identity', () => {
  const path = `/s/${space}/resources/${resource}/document/7`
  expect(nativeContentRoute(path)).toEqual({ space, resource, kind: 'document', revision: 7 })
  expect(onboardingDestination(path)).toBe(true)
  for (const invalid of [path.replace('/7', '/0'), path.replace('/7', '/9007199254740992'), path.replace(resource, '..'), path.replace('/document/', '/original/')]) expect(nativeContentRoute(invalid)).toBeNull()
  vi.stubEnv('VITE_POSTERN_NATIVE_CONTENT', 'false')
  const f = server(); render(<SpaceContentPage {...props} />)
  expect(screen.getByText('Protected content is not enabled.')).toBeTruthy()
  expect(onboardingDestination(path)).toBe(false)
  expect(f.calls).toHaveLength(0)
})

test('catalog links preserve resource identity and revision only when enabled', async () => {
  server()
  const account = { profile: { display_name: 'Viewer' } } as Account
  const shown = render(<SpaceOnboarding account={account} credential="pa_a" path={`/s/${space}`} />)
  expect((await screen.findByRole('link', { name: 'Read document: Native resource' })).getAttribute('href')).toBe(`/s/${space}/resources/${resource}/document/7`)
  shown.unmount(); vi.stubEnv('VITE_POSTERN_NATIVE_CONTENT', 'false')
  render(<SpaceOnboarding account={account} credential="pa_a" path={`/s/${space}`} />)
  await screen.findByText('Resource previews and playback are not available in this version.')
  expect(screen.queryByRole('link', { name: /Read document/ })).toBeNull()
})

test('document pages use authenticated no-store blobs and release URLs on navigation and unmount', async () => {
  const f = server(), shown = render(<SpaceContentPage {...props} />)
  const first = await screen.findByRole('img', { name: 'Native resource, page 1' })
  const firstUrl = first.getAttribute('src')!
  const pageRequest = f.calls.find(c => c.path.endsWith('/pages/0.jpg'))!
  expect(new Headers(pageRequest.init.headers).get('Authorization')).toBe('Bearer '+bearer)
  expect(pageRequest.init.cache).toBe('no-store'); expect(pageRequest.init.credentials).toBe('omit')
  expect(pageRequest.url).not.toContain(bearer)
  fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
  await screen.findByRole('img', { name: 'Native resource, page 2' })
  expect(objectUrls.revoked).toContain(firstUrl)
  expect(screen.getByRole('button', { name: 'Next page' })).toHaveProperty('disabled', true)
  shown.unmount()
  expect([...objectUrls.revoked].sort()).toEqual([...objectUrls.created].sort())
})

test('late page fetch is aborted and cannot follow a change of space', async () => {
  const f = server(), pending = deferred<Response>()
  f.state.intercept = call => call.path.includes(space) && call.path.endsWith('/pages/0.jpg') ? pending.promise : undefined
  const shown = render(<SpaceContentPage {...props} />)
  await waitFor(() => expect(f.calls.some(c => c.path.endsWith('/pages/0.jpg'))).toBe(true))
  const prior = f.calls.find(c => c.path.endsWith('/pages/0.jpg'))!
  shown.rerender(<SpaceContentPage {...props} space={otherSpace} />)
  await screen.findByRole('img', { name: 'Other space resource, page 1' })
  expect(prior.init.signal?.aborted).toBe(true)
  await act(async () => { pending.resolve(jpeg()) })
  expect(screen.queryByRole('img', { name: 'Native resource, page 1' })).toBeNull()
  expect(objectUrls.created).toHaveLength(1)
})

test('oversized page streams are cancelled before creating a displayed object URL', async () => {
  const f = server(), cancel = vi.fn()
  f.state.intercept = call => call.path.endsWith('/pages/0.jpg') ? new Response(new ReadableStream({
    start(controller) { controller.enqueue(new Uint8Array(4*1024*1024+1)) }, cancel,
  }), { headers: { 'Content-Type': 'image/jpeg' } }) : undefined
  render(<SpaceContentPage {...props} />)
  await screen.findByText('This document exceeded its page limit.')
  expect(cancel).toHaveBeenCalled(); expect(objectUrls.created).toHaveLength(0)
  expect(screen.queryByRole('img')).toBeNull()
})

test('account change clears an existing page and ignores a late page result', async () => {
  const f = server(), pending = deferred<Response>()
  const shown = render(<SpaceContentPage {...props} />)
  await screen.findByRole('img')
  f.state.intercept = call => call.path.endsWith('/pages/1.jpg') ? pending.promise : undefined
  fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
  await waitFor(() => expect(f.calls.some(c => c.path.endsWith('/pages/1.jpg'))).toBe(true))
  act(() => setAccountToken('pa_b'))
  expect(screen.queryByRole('img')).toBeNull()
  expect(objectUrls.revoked).toEqual(objectUrls.created)
  await act(async () => pending.resolve(jpeg()))
  expect(objectUrls.created).toHaveLength(1)
  shown.rerender(<SpaceContentPage {...props} credential="pa_b" />)
  await screen.findByRole('img', { name: 'Native resource, page 1' })
})

test('hiding immediately clears pixels and foregrounding requires fresh authorization', async () => {
  const f = server()
  let visibility: DocumentVisibilityState = 'visible'
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
  render(<SpaceContentPage {...props} />)
  await screen.findByRole('img')
  act(() => { visibility = 'hidden'; document.dispatchEvent(new Event('visibilitychange')) })
  expect(screen.queryByRole('img')).toBeNull(); expect(objectUrls.revoked).toEqual(objectUrls.created)
  f.state.refuse = true
  act(() => { visibility = 'visible'; window.dispatchEvent(new Event('focus')) })
  await screen.findByText(/Your access to this Postern has changed/)
  expect(screen.queryByRole('img')).toBeNull()
})

test('periodic authority refusal clears displayed pixels without waiting for navigation', async () => {
  vi.useFakeTimers()
  const f = server(); render(<SpaceContentPage {...props} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  expect(screen.getByRole('img')).toBeTruthy()
  f.state.refuse = true
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(screen.queryByRole('img')).toBeNull()
  expect(objectUrls.revoked).toEqual(objectUrls.created)
  expect(screen.getByText(/Your access to this Postern has changed/)).toBeTruthy()
})

test('a stalled authority loses displayed pixels at the ten-second deadline and cannot restore them late', async () => {
  vi.useFakeTimers()
  const f = server(), pending = deferred<Response>()
  render(<SpaceContentPage {...props} />)
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  expect(screen.getByRole('img')).toBeTruthy()
  f.state.intercept = call => call.path.endsWith('/document') ? pending.promise : undefined
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(screen.queryByRole('img')).toBeNull()
  expect(screen.getByText(/Current access could not be verified/)).toBeTruthy()
  await act(async () => pending.resolve(json({ resource_id: resource, revision: 7, page_count: 2 })))
  expect(screen.queryByRole('img')).toBeNull(); expect(objectUrls.revoked).toEqual(objectUrls.created)
})

test('video uses scoped short tickets, renews with current authority and preserves playback position', async () => {
  vi.useFakeTimers()
  const f = server('video'), shown = render(<SpaceContentPage {...props} kind="video" />)
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  const player = screen.getByLabelText('Native resource') as HTMLVideoElement
  const first = player.getAttribute('src')!
  expect(first).toContain('ticket=pc_'); expect(first).not.toContain(bearer); expect(first).not.toContain('pa_a')
  expect(player.getAttribute('controlslist')).toContain('nodownload')
  expect(player.hasAttribute('disablepictureinpicture')).toBe(true)
  expect(screen.getByLabelText('Personal watermark').textContent).toContain(principal)
  expect(screen.getByLabelText('Personal watermark').textContent).toContain('UTC')
  player.currentTime = 12
  await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
  expect(f.state.tickets).toBe(2)
  expect(player.getAttribute('src')).not.toBe(first)
  fireEvent.loadedMetadata(player)
  expect(player.currentTime).toBe(12)
  expect(f.calls.filter(c => c.path.endsWith('/video/ticket')).every(c => new Headers(c.init.headers).get('Authorization') === 'Bearer '+bearer)).toBe(true)
  shown.unmount()
  expect(player.hasAttribute('src')).toBe(false)
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled()
  expect(HTMLMediaElement.prototype.load).toHaveBeenCalled()
})

test.each(['refusal', 'account change'])('video %s removes the media source and watermark', async reason => {
  vi.useFakeTimers()
  const f = server('video'); render(<SpaceContentPage {...props} kind="video" />)
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  const player = screen.getByLabelText('Native resource') as HTMLVideoElement
  if (reason === 'account change') act(() => setAccountToken('pa_b'))
  else {
    f.state.refuse = true
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  }
  expect(screen.queryByLabelText('Native resource')).toBeNull()
  expect(player.hasAttribute('src')).toBe(false)
  expect(screen.queryByLabelText('Personal watermark')).toBeNull()
})

test('malformed video identity or bearer-as-ticket never reaches a media URL', async () => {
  const f = server('video'); f.state.viewer = 'caller supplied name'
  render(<SpaceContentPage {...props} kind="video" />)
  await screen.findByText('This video returned an incomplete response.')
  expect(screen.queryByLabelText('Personal watermark')).toBeNull()
  expect(f.state.tickets).toBe(0)
  const client = new SpacesClient('pa_a')
  expect(() => client.videoSource(space, resource, 7, bearer)).toThrow()
  client.close()
})
