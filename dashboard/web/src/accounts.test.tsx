import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { AccountsApp } from './accounts'
import { App } from './app'
import { accountToken, setAccountToken, type Account } from './accounts-api'
import { currentToken, setToken } from './api'
import { calls, installApi, world, type World } from './testing'
import { hold, liveUrls } from './blobs'
import { navigate, parse } from './routes'

const google = vi.hoisted(() => ({ callback: null as null | ((response: { credential: string }) => void), credential: 'google-a' }))
vi.mock('./google', () => ({ loadIdentity: async () => ({ accounts: { id: {
  initialize: (options: { callback: (response: { credential: string }) => void }) => { google.callback = options.callback },
  renderButton: (element: HTMLElement) => { const button = document.createElement('button'); button.textContent = 'Google test sign-in'; button.onclick = () => google.callback?.({ credential: google.credential }); element.append(button) },
} } }) }))

const first = (): Account => ({ id: 'a', email: 'private-a@example.invalid', identity: { issuer: 'https://accounts.google.com', subject: 'google-a' }, profile: { handle: null, display_name: 'Account Alice', avatar_url: null, bio: '', website: '', published: false } })
const second = (): Account => ({ ...first(), id: 'b', email: 'private-b@example.invalid', identity: { issuer: 'https://accounts.google.com', subject: 'google-b' }, profile: { ...first().profile, display_name: 'Account Bob' } })
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

interface AccountWorld {
  a: Account
  b: Account
  private: World
  accountCalls: { path: string; method: string; authorization: string | null; body: unknown }[]
  spaceSubject: string
  spaceDenied: boolean
  saveError: boolean
}

function fake(): AccountWorld {
  const state: AccountWorld = { a: first(), b: second(), private: world(), accountCalls: [], spaceSubject: 'google-a', spaceDenied: false, saveError: false }
  state.private.config.mode = 'google'; state.private.config.google_client_id = 'space-client'
  state.private.session.identity = state.a.identity
  installApi(state.private)
  const legacy = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/auth/google') {
      calls.push({ url: path, method: 'POST', body: String(init?.body ?? '') })
      if (state.spaceDenied) return json({ error: 'access_denied', message: 'You have not been invited to this space.' }, 403)
      return json({ token: 'private-new', identity: { issuer: 'https://accounts.google.com', subject: state.spaceSubject }, email: 'private@example.invalid', role: 'reader', name: 'Private user' })
    }
    if (!path.startsWith('/accounts/')) return legacy(input, init)
    const authorization = new Headers(init?.headers).get('Authorization')
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : null
    state.accountCalls.push({ path, authorization, method: init?.method ?? 'GET', body })
    const account = authorization === 'Bearer account-b' ? state.b : state.a
    if (path.endsWith('/v1/config')) return json({ google_client_id: 'account-client', registration_open: true })
    if (path.endsWith('/v1/session')) return json({ account })
    if (path.endsWith('/v1/auth/google')) return json({ account: body.credential === 'google-b' ? state.b : state.a, token: body.credential === 'google-b' ? 'account-b' : 'account-a' })
    if (path.endsWith('/v1/auth/signout')) return json({ signed_out: true })
    if (path.endsWith('/v1/profile')) {
      if (state.saveError) return json({ error: 'handle_taken', message: 'That handle is already taken.' }, 409)
      account.profile = { ...account.profile, ...body }
      return json({ account })
    }
    if (path.endsWith('/v1/account')) return json({ deleted: true })
    if (path.includes('/v1/profiles/')) return json({ profile: { ...state.a.profile, handle: 'alice', email: state.a.email, spaces: ['Secret space'], certificate: 'PRIVATE-CERTIFICATE' } })
    return json({ error: 'not_found', message: 'Not found' }, 404)
  }) as typeof fetch
  return state
}

beforeEach(() => { vi.stubEnv('VITE_POSTERN_ACCOUNTS_API', '/accounts'); google.credential = 'google-a'; google.callback = null })
afterEach(() => { vi.unstubAllEnvs() })

test('a memberless Google user gets an account home without private config/content requests', async () => {
  const state = fake()
  render(<AccountsApp />)
  fireEvent.click(await screen.findByRole('button', { name: 'Google test sign-in' }))
  expect(await screen.findByRole('heading', { name: 'No Posterns yet' })).toBeTruthy()
  expect(accountToken()).toBe('account-a')
  expect(currentToken()).toBeNull()
  expect(calls).toEqual([])
  expect(state.accountCalls.find(call => call.path.endsWith('/v1/auth/google'))?.authorization).toBeNull()
})

test('an anonymous public profile projects only allowed fields and sends no account credential', async () => {
  const state = fake(); state.a.profile.bio = '<img src=x onerror=alert(1)>'; state.a.profile.website = 'javascript:alert(1)'
  setAccountToken('account-a'); setToken('private-old')
  window.history.replaceState(null, '', '/@alice')
  render(<AccountsApp />)
  expect(await screen.findByRole('heading', { name: 'Account Alice' })).toBeTruthy()
  expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeTruthy()
  expect(document.querySelector('img')).toBeNull()
  expect(document.body.textContent).not.toContain('private-a@example.invalid')
  expect(document.body.textContent).not.toContain('Secret space')
  expect(document.body.textContent).not.toContain('PRIVATE-CERTIFICATE')
  expect(document.querySelector('a[href^="javascript:"]')).toBeNull()
  expect(calls).toEqual([])
  expect(state.accountCalls).toHaveLength(1)
  expect(state.accountCalls[0].authorization).toBeNull()
})

test('profile editing requires an explicit publication choice and preserves the account context', async () => {
  const state = fake(); setAccountToken('account-a'); window.history.replaceState(null, '', '/settings/profile')
  render(<AccountsApp />)
  const handle = await screen.findByLabelText('Handle')
  expect((screen.getByLabelText(/Publish my profile/) as HTMLInputElement).checked).toBe(false)
  fireEvent.change(handle, { target: { value: 'alice' } })
  fireEvent.change(screen.getByLabelText('Bio'), { target: { value: 'Hello from Alice' } })
  fireEvent.click(screen.getByRole('button', { name: 'Preview public profile' }))
  expect(screen.getByRole('region', { name: 'Public profile preview' }).textContent).not.toContain(state.a.email)
  fireEvent.click(screen.getByLabelText(/Publish my profile/))
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  expect(await screen.findByText('Profile saved.')).toBeTruthy()
  const saved = state.accountCalls.find(call => call.path.endsWith('/v1/profile'))
  expect(saved?.method).toBe('PATCH')
  expect(saved?.body).toMatchObject({ handle: 'alice', published: true, bio: 'Hello from Alice' })
  expect(Object.keys(saved?.body as object).sort()).toEqual(['bio', 'display_name', 'handle', 'published', 'website'])
  expect(saved?.authorization).toBe('Bearer account-a')
  expect(calls).toEqual([])
})

test('a taken handle stays an inline error without logging out the account', async () => {
  const state = fake(); state.saveError = true; setAccountToken('account-a'); window.history.replaceState(null, '', '/settings/profile')
  render(<AccountsApp />)
  fireEvent.change(await screen.findByLabelText('Handle'), { target: { value: 'taken' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  expect(await screen.findByText('That handle is already taken.')).toBeTruthy()
  expect(accountToken()).toBe('account-a')
  expect((screen.getByLabelText('Handle') as HTMLInputElement).value).toBe('taken')
})

test('a private-space refusal keeps the public account signed in and never opens content', async () => {
  const state = fake(); state.spaceDenied = true; setAccountToken('account-a'); window.history.replaceState(null, '', '/s/pilot')
  render(<AccountsApp />)
  fireEvent.click(await screen.findByRole('button', { name: 'Google test sign-in' }))
  expect(await screen.findByText('You have not been invited to this space.')).toBeTruthy()
  expect(accountToken()).toBe('account-a')
  expect(currentToken()).toBeNull()
  expect(calls.some(call => call.url === '/api/posts')).toBe(false)
  fireEvent.click(screen.getByRole('link', { name: 'Back to My Posterns' }))
  expect(await screen.findByRole('heading', { name: 'No Posterns yet' })).toBeTruthy()
})

test('a Google account mismatch cannot attach a private-space session', async () => {
  const state = fake(); state.spaceSubject = 'google-b'; setAccountToken('account-a'); window.history.replaceState(null, '', '/s/pilot')
  render(<AccountsApp />)
  fireEvent.click(await screen.findByRole('button', { name: 'Google test sign-in' }))
  expect(await screen.findByText(/Choose the same Google account/)).toBeTruthy()
  expect(currentToken()).toBeNull()
  expect(accountToken()).toBe('account-a')
  expect(calls.some(call => call.url === '/api/posts')).toBe(false)
  expect(calls.some(call => call.url === '/api/auth/signout')).toBe(true)
})

test('reload rejects a mismatched stored private session before fetching private config or content', async () => {
  const state = fake(); state.private.session.identity = state.b.identity
  setAccountToken('account-a'); setToken('private-old'); window.history.replaceState(null, '', '/home')
  render(<AccountsApp />)
  expect(await screen.findByText(/Sign in to the private space with the same Google account/)).toBeTruthy()
  expect(currentToken()).toBeNull()
  expect(accountToken()).toBe('account-a')
  expect(calls.map(call => call.url)).toEqual(['/api/session'])
})

test('a matching space session opens the existing protected app and namespaces navigation', async () => {
  fake(); setAccountToken('account-a'); setToken('private-old'); window.history.replaceState(null, '', '/s/pilot')
  render(<AccountsApp />)
  expect(await screen.findByText('Tip pooling policy')).toBeTruthy()
  expect(calls.some(call => call.url === '/api/posts')).toBe(true)
  act(() => navigate('/certificate'))
  expect(window.location.pathname).toBe('/s/pilot/certificate')
  expect(parse(window.location.pathname)).toEqual({ name: 'certificate' })
})

test.each(['/post/p1/item/item-p1', '/quiz/1', '/final-exam'])('returning from legacy link %s stays in private training', async path => {
  fake(); setAccountToken('account-a'); setToken('private-old')
  window.history.replaceState(null, '', `${path}?from=legacy#section`)
  const historyLength = window.history.length
  render(<AccountsApp />)
  const back = await screen.findByRole('button', { name: 'Back' })
  const destination = window.location.pathname + window.location.search + window.location.hash
  expect(window.history.length).toBe(historyLength)
  fireEvent.click(back)
  expect(window.location.pathname).toBe('/s/pilot')
  expect(await screen.findByText('Tip pooling policy')).toBeTruthy()
  expect(screen.queryByRole('heading', { name: 'My Posterns' })).toBeNull()
  expect(destination).toBe(`/s/pilot${path}?from=legacy#section`)
  expect(accountToken()).toBe('account-a'); expect(currentToken()).toBe('private-old')
})

test.each(['/post/p1', '/quiz/1'])('standalone navigation from %s keeps ordinary training routes', async path => {
  const state = fake(); vi.stubEnv('VITE_POSTERN_ACCOUNTS_API', '')
  setToken('private-old'); window.history.replaceState(null, '', path)
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Back' }))
  expect(window.location.pathname).toBe('/')
  expect(await screen.findByText('Tip pooling policy')).toBeTruthy()
  expect(state.accountCalls).toEqual([])
})

test('a revoked private session affects only that space and clears private media', async () => {
  const state = fake(); state.private.refuse = { status: 403, error: 'access_denied', message: 'Membership removed', path: /\/api\/session$/ }
  setAccountToken('account-a'); setToken('private-old'); hold(new Blob(['private']))
  render(<AccountsApp />)
  expect(await screen.findByText(/Private access needs to be checked again/)).toBeTruthy()
  expect(accountToken()).toBe('account-a')
  expect(currentToken()).toBeNull()
  expect(liveUrls()).toBe(0)
})

test('a stale account request cannot overwrite a newer Google account', async () => {
  fake(); setAccountToken('account-a')
  const normal = fetch
  let resolve!: (response: Response) => void
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith('/v1/session') && new Headers(init?.headers).get('Authorization') === 'Bearer account-a' ? new Promise<Response>(done => { resolve = done }) : normal(input, init)) as typeof fetch
  render(<AccountsApp />)
  await waitFor(() => expect(resolve).toBeTypeOf('function'))
  act(() => setAccountToken('account-b'))
  expect(await screen.findByText('Account Bob')).toBeTruthy()
  await act(async () => { resolve(json({ account: first() })) })
  expect(screen.queryByText('Account Alice')).toBeNull()
  expect(screen.getByText('Account Bob')).toBeTruthy()
})

test('cross-tab account changes clear private media and the previous space session', async () => {
  fake(); setAccountToken('account-a'); setToken('private-old'); hold(new Blob(['private']))
  render(<AccountsApp />)
  expect(await screen.findByText('Account Alice')).toBeTruthy()
  act(() => { localStorage.setItem('postern.account.token', 'account-b'); window.dispatchEvent(new StorageEvent('storage', { key: 'postern.account.token', newValue: 'account-b' })) })
  expect(await screen.findByText('Account Bob')).toBeTruthy()
  expect(screen.queryByText('Account Alice')).toBeNull()
  expect(currentToken()).toBeNull()
  expect(liveUrls()).toBe(0)
})

test.each([
  { cached: null, shared: 'private-other-tab', expected: ['Bearer private-other-tab'] },
  { cached: 'private-old', shared: 'private-other-tab', expected: ['Bearer private-old', 'Bearer private-other-tab'] },
  { cached: 'private-old', shared: 'private-old', expected: ['Bearer private-old'] },
])('account sign-out revokes cached $cached and shared $shared sessions once', async ({ cached, shared, expected }) => {
  fake(); setAccountToken('account-a'); setToken(cached)
  const normal = fetch
  const revoked: (string | null)[] = []
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/auth/signout') revoked.push(new Headers(init?.headers).get('Authorization'))
    return normal(input, init)
  }) as typeof fetch
  render(<AccountsApp />)
  await screen.findByRole('heading', { name: cached ? 'Private Postern' : 'No Posterns yet' })
  // Another tab signs into the space after this tab loaded. Browser storage is
  // shared, but this tab must not adopt that bearer for private API requests.
  act(() => {
    localStorage.setItem('postern.token', shared)
    window.dispatchEvent(new StorageEvent('storage', { key: 'postern.token', newValue: shared }))
  })
  expect(currentToken()).toBe(cached)
  fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
  await screen.findByRole('heading', { name: 'Your place on Postern' })
  await waitFor(() => expect(revoked.sort()).toEqual(expected.sort()))
  expect(accountToken()).toBeNull(); expect(currentToken()).toBeNull()
  expect(localStorage.getItem('postern.token')).toBeNull()
})

test('a late private sign-in after leaving the page is revoked without storing its token', async () => {
  fake(); setAccountToken('account-a'); window.history.replaceState(null, '', '/s/pilot')
  const normal = fetch
  let resolve!: (response: Response) => void
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/auth/google' ? new Promise<Response>(done => { resolve = done }) : normal(input, init)) as typeof fetch
  render(<AccountsApp />)
  fireEvent.click(await screen.findByRole('button', { name: 'Google test sign-in' }))
  act(() => navigate('/home'))
  await act(async () => { resolve(json({ token: 'late-private', identity: first().identity, email: first().email, role: 'reader', name: 'Alice' })) })
  expect(currentToken()).toBeNull()
  expect(await screen.findByRole('heading', { name: 'No Posterns yet' })).toBeTruthy()
  expect(calls.some(call => call.url === '/api/posts')).toBe(false)
})

test('deletion requires typed confirmation and clears both browser sessions', async () => {
  const state = fake(); setAccountToken('account-a'); setToken('private-old'); window.history.replaceState(null, '', '/settings/profile')
  render(<AccountsApp />)
  fireEvent.click(await screen.findByRole('button', { name: 'Delete account' }))
  expect((screen.getByRole('button', { name: 'Permanently delete account' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('Type DELETE to confirm'), { target: { value: 'DELETE' } })
  fireEvent.click(screen.getByRole('button', { name: 'Permanently delete account' }))
  expect(await screen.findByRole('heading', { name: 'Your place on Postern' })).toBeTruthy()
  expect(accountToken()).toBeNull(); expect(currentToken()).toBeNull()
  expect(state.accountCalls.find(call => call.path.endsWith('/v1/account'))?.body).toEqual({ confirm: true })
})

test('navigation away and back cannot revive an abandoned private sign-in', async () => {
  fake(); setAccountToken('account-a'); window.history.replaceState(null, '', '/s/pilot')
  const normal = fetch
  let resolve!: (response: Response) => void
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/auth/google' ? new Promise<Response>(done => { resolve = done }) : normal(input, init)) as typeof fetch
  render(<AccountsApp />)
  fireEvent.click(await screen.findByRole('button', { name: 'Google test sign-in' }))
  act(() => { navigate('/home'); navigate('/s/pilot') })
  await act(async () => { resolve(json({ token: 'abandoned-private', identity: first().identity, email: first().email, role: 'reader', name: 'Alice' })) })
  expect(currentToken()).toBeNull()
  expect(calls.some(call => call.url === '/api/posts')).toBe(false)
  expect(calls.some(call => call.url === '/api/auth/signout')).toBe(true)
})

test('a new sign-in after navigation wins over the abandoned Google account request', async () => {
  const state = fake()
  const normal = fetch
  let resolve!: (response: Response) => void
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith('/v1/auth/google') && JSON.parse(String(init?.body)).credential === 'google-a' ? new Promise<Response>(done => { resolve = done }) : normal(input, init)) as typeof fetch
  render(<AccountsApp />)
  fireEvent.click(await screen.findByRole('button', { name: 'Google test sign-in' }))
  act(() => navigate('/account/privacy'))
  expect(await screen.findByRole('heading', { name: 'Account privacy' })).toBeTruthy()
  google.credential = 'google-b'
  act(() => navigate('/'))
  fireEvent.click(await screen.findByRole('button', { name: 'Google test sign-in' }))
  expect(await screen.findByText('Account Bob')).toBeTruthy()
  await act(async () => { resolve(json({ token: 'abandoned-account', account: first() })) })
  expect(accountToken()).toBe('account-b')
  expect(screen.queryByText('Account Alice')).toBeNull()
  expect(state.accountCalls.some(call => call.path.endsWith('/v1/auth/signout') && call.authorization === 'Bearer abandoned-account')).toBe(true)
})

test('a profile save finishing after an account switch cannot restore the old profile', async () => {
  fake(); setAccountToken('account-a'); window.history.replaceState(null, '', '/settings/profile')
  const normal = fetch
  let resolve!: (response: Response) => void
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith('/v1/profile') ? new Promise<Response>(done => { resolve = done }) : normal(input, init)) as typeof fetch
  render(<AccountsApp />)
  fireEvent.change(await screen.findByLabelText('Handle'), { target: { value: 'alice' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  act(() => setAccountToken('account-b'))
  await waitFor(() => expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('Account Bob'))
  await act(async () => { resolve(json({ account: first() })) })
  expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('Account Bob')
  expect(accountToken()).toBe('account-b')
})

test('Sign out in a bound private space ends both account and space sessions', async () => {
  const state = fake(); setAccountToken('account-a'); setToken('private-old'); window.history.replaceState(null, '', '/s/pilot')
  render(<AccountsApp />)
  await screen.findByText('Tip pooling policy')
  fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
  expect(await screen.findByRole('heading', { name: 'Your place on Postern' })).toBeTruthy()
  expect(window.location.pathname).toBe('/home')
  expect(accountToken()).toBeNull(); expect(currentToken()).toBeNull()
  expect(state.accountCalls.some(call => call.path.endsWith('/v1/auth/signout') && call.authorization === 'Bearer account-a')).toBe(true)
  expect(calls.some(call => call.url === '/api/auth/signout')).toBe(true)
})

test('a private bootstrap network failure offers retry after the wrapper admitted the session', async () => {
  fake(); setAccountToken('account-a'); setToken('private-old'); window.history.replaceState(null, '', '/s/pilot')
  const normal = fetch
  let sessions = 0
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/session' && ++sessions === 2) return Promise.reject(new TypeError('Network offline'))
    return normal(input, init)
  }) as typeof fetch
  render(<AccountsApp />)
  expect(await screen.findByRole('heading', { name: 'Private space is unavailable' })).toBeTruthy()
  expect(calls.some(call => call.url === '/api/posts')).toBe(false)
  expect(accountToken()).toBe('account-a')
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  expect(await screen.findByText('Tip pooling policy')).toBeTruthy()
})


test.each([null, 'account-b'])('a receiving tab revokes its own bearer on account change to %s', async replacement => {
  fake(); setAccountToken('account-a'); setToken('private-middle-tab')
  const normal = fetch
  const revoked: (string | null)[] = []
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/auth/signout') revoked.push(new Headers(init?.headers).get('Authorization'))
    return normal(input, init)
  }) as typeof fetch
  render(<AccountsApp />)
  await screen.findByRole('heading', { name: 'Private Postern' })
  // A third tab has overwritten shared storage. This tab alone still knows its bearer.
  localStorage.setItem('postern.token', 'private-newest-tab')
  act(() => {
    if (replacement) localStorage.setItem('postern.account.token', replacement)
    else localStorage.removeItem('postern.account.token')
    window.dispatchEvent(new StorageEvent('storage', { key: 'postern.account.token', newValue: replacement }))
  })
  await waitFor(() => expect(revoked).toEqual(['Bearer private-middle-tab']))
  expect(currentToken()).toBeNull()
  expect(accountToken()).toBe(replacement)
})

test.each(['network', 'roster outage'])('a %s during private binding preserves the bearer for a successful retry', async kind => {
  fake(); setAccountToken('account-a'); setToken('private-old'); window.history.replaceState(null, '', '/s/pilot')
  const normal = fetch
  let unavailable = true
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/session' && unavailable) {
      return kind === 'network' ? Promise.reject(new TypeError('Offline')) : Promise.resolve(json({ error: 'authorization_source_unavailable', message: 'Permissions temporarily unavailable.' }, 503))
    }
    return normal(input, init)
  }) as typeof fetch
  render(<AccountsApp />)
  await screen.findByRole('heading', { name: 'Private space is unavailable' })
  expect(accountToken()).toBe('account-a'); expect(currentToken()).toBe('private-old')
  expect(calls.some(call => call.url === '/api/posts')).toBe(false)
  expect(screen.queryByRole('button', { name: 'Google test sign-in' })).toBeNull()
  unavailable = false
  fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
  expect(await screen.findByText('Tip pooling policy')).toBeTruthy()
  expect(currentToken()).toBe('private-old')
})

test('a resource-level 403 keeps the bound private session and retries without Google sign-in', async () => {
  const state = fake(); state.private.refuse = { status: 403, error: 'access_denied', message: 'This post is not available.', path: /\/api\/posts$/ }
  setAccountToken('account-a'); setToken('private-old'); window.history.replaceState(null, '', '/s/pilot')
  render(<AccountsApp />)
  await screen.findByText('This post is not available.')
  expect(currentToken()).toBe('private-old'); expect(accountToken()).toBe('account-a')
  state.private.refuse = null
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  expect(await screen.findByText('Tip pooling policy')).toBeTruthy()
})

test('an already loaded owner keeps account navigation on a public profile without authenticated public reads', async () => {
  const state = fake(); setAccountToken('account-a')
  render(<AccountsApp />)
  await screen.findByText('Account Alice')
  const reads = state.accountCalls.length
  act(() => navigate('/@alice'))
  await screen.findByRole('heading', { name: 'Account Alice' })
  expect(screen.getByRole('button', { name: 'Sign out' })).toBeTruthy()
  expect(screen.getByRole('link', { name: 'Posterns' })).toBeTruthy()
  expect(state.accountCalls.slice(reads).map(call => [call.path, call.authorization])).toEqual([['/accounts/v1/profiles/alice', null]])
})

test('bio validation counts Unicode characters rather than UTF16 units', async () => {
  const state = fake(); setAccountToken('account-a'); window.history.replaceState(null, '', '/settings/profile')
  render(<AccountsApp />)
  fireEvent.change(await screen.findByLabelText('Handle'), { target: { value: 'alice' } })
  fireEvent.change(screen.getByLabelText('Bio'), { target: { value: 'a'.repeat(281) } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByText('Use no more than 280 characters for your bio.')
  expect(state.accountCalls.some(call => call.path.endsWith('/v1/profile'))).toBe(false)
  fireEvent.change(screen.getByLabelText('Bio'), { target: { value: '😀'.repeat(280) } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByText('Profile saved.')
  expect(state.a.profile.bio).toBe('😀'.repeat(280))
})
