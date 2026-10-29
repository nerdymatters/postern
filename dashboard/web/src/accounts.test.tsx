import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { AccountsApp } from './accounts'
import { App } from './app'
import { accounts, AccountError, accountToken, setAccountToken, type Account, type Terms } from './accounts-api'
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
  terms?: Terms
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
    if (path.endsWith('/v1/config')) return json({ google_client_id: 'account-client', registration_open: true, terms: state.terms })
    if (path.endsWith('/v1/session')) return json({ account })
    if (path.endsWith('/v1/auth/google')) {
      if (state.terms && body.terms?.accepted) state.a.terms = { ...state.terms, required: false, accepted_at: 123 }
      return json({ account: body.credential === 'google-b' ? state.b : state.a, token: body.credential === 'google-b' ? 'account-b' : 'account-a' })
    }
    if (path.endsWith('/v1/terms/accept')) {
      if (account.terms) account.terms = { ...account.terms, required: false, accepted_at: 123 }
      return json({ account })
    }
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
  // Config and the mocked GIS loader resolve asynchronously. Flush their React
  // effects before polling the injected button, including under full-suite load.
  await act(async () => { render(<AccountsApp />) })
  fireEvent.click(await screen.findByRole('button', { name: 'Google test sign-in' }))
  expect(await screen.findByRole('heading', { name: 'No Posterns yet' })).toBeTruthy()
  expect(accountToken()).toBe('account-a')
  expect(currentToken()).toBeNull()
  expect(calls).toEqual([])
  expect(state.accountCalls.find(call => call.path.endsWith('/v1/auth/google'))?.authorization).toBeNull()
})

const publishedTerms: Terms = { version: '2026-10-29', sha256: 'a'.repeat(64), url: 'https://example.invalid/terms/2026-10-29.html' }

// Value: protects=failed Terms-gate deletion preserves identity and can be retried;
// fails_when=a failed delete signs out or leaves the action disabled;
// why_new=existing gate deletion tests cover success only; seam=none;
// no_claim=live deletion enforcement (mocked account API).
test('a failed Terms-gate deletion preserves the account and permits a confirmed retry', async () => {
  const state = fake(); state.a.terms = { ...publishedTerms, required: true, accepted_at: null }
  setAccountToken('account-a')
  const remove = vi.spyOn(accounts, 'delete').mockRejectedValueOnce(new AccountError(503, 'unavailable', 'Deletion is temporarily unavailable.'))
  render(<AccountsApp />)
  fireEvent.click(await screen.findByRole('button', { name: 'Delete account' }))
  fireEvent.change(screen.getByLabelText('Type DELETE to confirm'), { target: { value: 'DELETE' } })
  fireEvent.click(screen.getByRole('button', { name: 'Permanently delete account' }))
  await screen.findByText('Deletion is temporarily unavailable.')
  expect(accountToken()).toBe('account-a')
  expect(calls).toEqual([])
  expect(screen.getByRole('button', { name: 'Permanently delete account' })).toHaveProperty('disabled', false)
  fireEvent.click(screen.getByRole('button', { name: 'Permanently delete account' }))
  await waitFor(() => expect(accountToken()).toBeNull())
  expect(remove).toHaveBeenCalledTimes(2)
  expect(state.accountCalls.some(call => call.path.endsWith('/v1/terms/accept'))).toBe(false)
})

// Value: protects=pending Terms deletion sends once and cannot sign out a replacement account after navigation;
// fails_when=repeat clicks duplicate deletion or a late completion clears the new identity;
// why_new=late acceptance coverage does not exercise deletion callbacks; seam=none;
// no_claim=server deletion idempotency (mocked pending API).
test('a pending Terms deletion ignores repeat clicks and cannot clear the next account after navigation', async () => {
  const state = fake(); state.a.terms = { ...publishedTerms, required: true, accepted_at: null }
  setAccountToken('account-a')
  let finish!: (value: { deleted: boolean }) => void
  const remove = vi.spyOn(accounts, 'delete').mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  render(<AccountsApp />)
  fireEvent.click(await screen.findByRole('button', { name: 'Delete account' }))
  fireEvent.change(screen.getByLabelText('Type DELETE to confirm'), { target: { value: 'DELETE' } })
  const button = screen.getByRole('button', { name: 'Permanently delete account' })
  fireEvent.click(button); fireEvent.click(button)
  expect(remove).toHaveBeenCalledTimes(1)
  expect(button).toHaveProperty('disabled', true)
  act(() => navigate('/account/terms'))
  await screen.findByRole('heading', { name: 'Terms of Service' })
  act(() => { setAccountToken('account-b'); navigate('/home') })
  await screen.findByText('Account Bob')
  await act(async () => finish({ deleted: true }))
  expect(accountToken()).toBe('account-b')
  screen.getByText('Account Bob')
  expect(screen.queryByText('Account Alice')).toBeNull()
  expect(state.accountCalls.some(call => call.path.endsWith('/v1/auth/signout') && call.authorization === 'Bearer account-b')).toBe(false)
})

test('signup requires an unchecked Terms choice and sends the exact displayed version', async () => {
  const state = fake(); state.terms = publishedTerms
  render(<AccountsApp />)
  const agree = await screen.findByRole('checkbox', { name: /I agree to the Terms/ })
  expect(agree).toHaveProperty('checked', false)
  const signIn = vi.spyOn(accounts, 'signIn').mockRejectedValueOnce(new AccountError(428, 'terms_required', 'Review and accept the Terms before creating your account.'))
  fireEvent.click(await screen.findByRole('button', { name: 'Google test sign-in' }))
  await screen.findByText('Review and accept the Terms before creating your account.')
  expect(accountToken()).toBeNull()
  expect(signIn).toHaveBeenCalledWith('google-a', undefined)
  expect(screen.getByRole('link', { name: /Terms of Service \(version/ }).getAttribute('href')).toBe(publishedTerms.url)
  fireEvent.click(agree)
  fireEvent.click(await screen.findByRole('button', { name: 'Google test sign-in' }))
  await screen.findByRole('heading', { name: 'My Posterns' })
  expect(state.accountCalls.find(call => call.path.endsWith('/v1/auth/google'))?.body).toEqual({ credential: 'google-a', terms: { accepted: true, version: publishedTerms.version, sha256: publishedTerms.sha256 } })
  signIn.mockRestore()
})

test('open registration lets existing users sign in and delete without accepting revised Terms', async () => {
  const state = fake(); state.terms = publishedTerms
  state.a.terms = { ...publishedTerms, required: true, accepted_at: null }
  await act(async () => { render(<AccountsApp />) })
  expect(screen.getByRole('checkbox', { name: /I agree to the Terms/ })).toHaveProperty('checked', false)
  fireEvent.click(screen.getByRole('button', { name: 'Google test sign-in' }))
  await screen.findByRole('heading', { name: 'Review the Terms of Service' })
  expect(state.accountCalls.find(call => call.path.endsWith('/v1/auth/google'))?.body).toEqual({ credential: 'google-a' })
  fireEvent.click(screen.getByRole('button', { name: 'Delete account' }))
  fireEvent.change(screen.getByLabelText('Type DELETE to confirm'), { target: { value: 'DELETE' } })
  fireEvent.click(screen.getByRole('button', { name: 'Permanently delete account' }))
  await waitFor(() => expect(accountToken()).toBeNull())
  expect(state.accountCalls.some(call => call.path.endsWith('/v1/terms/accept'))).toBe(false)
})

test('a Terms revision during signup explains the reset and requires a fresh choice', async () => {
  const state = fake(); state.terms = publishedTerms
  const signIn = vi.spyOn(accounts, 'signIn').mockImplementationOnce(async () => {
    state.terms = { ...publishedTerms, version: 'next', sha256: 'b'.repeat(64) }
    throw new AccountError(409, 'terms_changed', 'Terms changed.')
  })
  await act(async () => { render(<AccountsApp />) })
  fireEvent.click(screen.getByRole('checkbox', { name: /I agree to the Terms/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Google test sign-in' }))
  await screen.findByRole('link', { name: 'Terms of Service (version next)' })
  screen.getByText('The Terms have changed. Please review the updated version before continuing.')
  expect(screen.getByRole('checkbox', { name: /I agree to the Terms/ })).toHaveProperty('checked', false)
  expect(accountToken()).toBeNull()
  signIn.mockRestore()
})

test('unavailable Terms preserve existing sign-in and a reachable deletion flow without private access', async () => {
  const state = fake(); state.a.terms_unavailable = true
  const config = vi.spyOn(accounts, 'config').mockResolvedValue({ google_client_id: 'account-client', registration_open: false, terms: null, terms_unavailable: true })
  window.history.replaceState(null, '', '/s/pilot')
  await act(async () => { render(<AccountsApp />) })
  fireEvent.click(screen.getByRole('button', { name: 'Google test sign-in' }))
  await screen.findByRole('heading', { name: 'Terms are temporarily unavailable' })
  expect(screen.queryByRole('button', { name: 'Accept and continue' })).toBeNull()
  expect(calls).toEqual([])
  fireEvent.click(screen.getByRole('button', { name: 'Delete account' }))
  fireEvent.change(screen.getByLabelText('Type DELETE to confirm'), { target: { value: 'DELETE' } })
  fireEvent.click(screen.getByRole('button', { name: 'Permanently delete account' }))
  await waitFor(() => expect(accountToken()).toBeNull())
  expect(state.accountCalls.some(call => call.path.endsWith('/v1/terms/accept'))).toBe(false)
  config.mockRestore()
})

test('existing accounts must accept before a private deep link loads, and a revision resets the choice', async () => {
  const state = fake(); state.a.terms = { ...publishedTerms, required: true, accepted_at: null }
  setAccountToken('account-a'); setToken('private-old')
  window.history.replaceState(null, '', '/s/pilot')
  render(<AccountsApp />)
  await screen.findByRole('heading', { name: 'Review the Terms of Service' })
  expect(screen.getByRole('button', { name: 'Accept and continue' })).toHaveProperty('disabled', true)
  expect(calls).toEqual([])
  const accept = vi.spyOn(accounts, 'acceptTerms').mockRejectedValueOnce(new AccountError(409, 'terms_changed', 'The Terms have changed.'))
  fireEvent.click(screen.getByRole('checkbox', { name: /I have read and agree/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Accept and continue' }))
  await screen.findByText('The Terms have changed.')
  state.a.terms = { ...publishedTerms, version: 'next', sha256: 'b'.repeat(64), required: true, accepted_at: null }
  fireEvent.click(screen.getByRole('button', { name: 'Reload current Terms' }))
  await screen.findByRole('link', { name: 'Terms of Service (version next)' })
  expect(screen.getByRole('checkbox', { name: /I have read and agree/ })).toHaveProperty('checked', false)
  expect(calls).toEqual([])
  accept.mockRestore()
  fireEvent.click(screen.getByRole('checkbox', { name: /I have read and agree/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Accept and continue' }))
  await waitFor(() => expect(calls.some(call => call.url.includes('/api/session'))).toBe(true))
})

test('declining current Terms still allows confirmed account deletion', async () => {
  const state = fake(); state.a.terms = { ...publishedTerms, required: true, accepted_at: null }
  setAccountToken('account-a')
  render(<AccountsApp />)
  fireEvent.click(await screen.findByRole('button', { name: 'Delete account' }))
  expect(screen.getByRole('button', { name: 'Permanently delete account' })).toHaveProperty('disabled', true)
  fireEvent.change(screen.getByLabelText('Type DELETE to confirm'), { target: { value: 'DELETE' } })
  fireEvent.click(screen.getByRole('button', { name: 'Permanently delete account' }))
  await waitFor(() => expect(accountToken()).toBeNull())
  expect(state.accountCalls.some(call => call.path.endsWith('/v1/terms/accept'))).toBe(false)
})

test('a late acceptance response cannot replace a different signed-in account', async () => {
  const state = fake(); state.a.terms = { ...publishedTerms, required: true, accepted_at: null }
  setAccountToken('account-a')
  let finish!: (result: { account: Account }) => void
  const accept = vi.spyOn(accounts, 'acceptTerms').mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  render(<AccountsApp />)
  fireEvent.click(await screen.findByRole('checkbox', { name: /I have read and agree/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Accept and continue' }))
  act(() => setAccountToken('account-b'))
  await screen.findByText('Account Bob')
  await act(async () => finish({ account: { ...state.a, terms: { ...publishedTerms, required: false, accepted_at: 123 } } }))
  screen.getByText('Account Bob')
  expect(screen.queryByText('Account Alice')).toBeNull()
  accept.mockRestore()
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
  fireEvent.click(screen.getByRole('button', { name: /^Try again/ }))
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
  fireEvent.click(screen.getByRole('button', { name: /^Try again/ }))
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
