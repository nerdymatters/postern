import { afterEach, expect, test, vi } from 'vitest'
import { AccountError, accounts, accountToken, setAccountToken, type Account } from './accounts-api'
import { api, currentToken, onRefusal, setToken } from './api'

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllEnvs() })

test('a late refusal from an old space request cannot invalidate a newer space session', async () => {
  setToken('old-space')
  const announced = vi.fn()
  const unsubscribe = onRefusal(announced)
  let resolve!: (value: Response) => void
  globalThis.fetch = vi.fn(() => new Promise<Response>(done => { resolve = done }))
  const pending = api.posts()
  setToken('new-space')
  resolve(new Response(JSON.stringify({ error: 'sign_in_required', message: 'Expired' }), { status: 401 }))
  await expect(pending).rejects.toThrow('Expired')
  expect(announced).not.toHaveBeenCalled()
  unsubscribe()
})

test.each([
  { name: 'configuration', path: '/api/config', request: () => api.configForAccount() },
  { name: 'session check', path: '/api/session', request: () => api.sessionForAccount() },
  { name: 'Google sign-in', path: '/api/auth/google', request: () => api.signInGoogleForAccount('google-credential') },
])('account-mode $name times out without invalidating a replacement space session', async ({ path, request }) => {
  const controller = new AbortController()
  const timeout = vi.spyOn(AbortSignal, 'timeout').mockReturnValue(controller.signal)
  const announced = vi.fn()
  const unsubscribe = onRefusal(announced)
  setToken('existing-space')
  const transport = vi.fn<typeof fetch>((_url, init) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true })
  }))
  globalThis.fetch = transport

  const pending = request()
  const rejected = expect(pending).rejects.toMatchObject({ status: 0, code: 'unreachable' })
  setToken('replacement-space')
  controller.abort(new DOMException('Timed out', 'TimeoutError'))
  await rejected

  expect(timeout).toHaveBeenCalledWith(15000)
  expect(transport).toHaveBeenCalledWith(path, expect.objectContaining({ signal: controller.signal }))
  expect(currentToken()).toBe('replacement-space')
  expect(announced).not.toHaveBeenCalled()
  unsubscribe()
})

test('standalone configuration, session and sign-in keep their existing request lifetime', async () => {
  const timeout = vi.spyOn(AbortSignal, 'timeout')
  const transport = vi.fn<typeof fetch>(async () => new Response('{}'))
  globalThis.fetch = transport

  await api.config()
  await api.session()
  await api.signInGoogle('google-credential')

  expect(timeout).not.toHaveBeenCalled()
  expect(transport.mock.calls.every(([, init]) => init?.signal === undefined)).toBe(true)
})

test('a bounded session recheck can retain the private app refusal handler', async () => {
  const announced = vi.fn()
  const unsubscribe = onRefusal(announced)
  globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ error: 'access_denied', message: 'Unavailable' }), { status: 403 }))

  await expect(api.sessionForAccount()).rejects.toThrow('Unavailable')
  expect(announced).not.toHaveBeenCalled()
  await expect(api.sessionForAccount({ quietRefusal: false })).rejects.toThrow('Unavailable')
  expect(announced).toHaveBeenCalledWith(expect.objectContaining({ status: 403, code: 'access_denied' }))
  unsubscribe()
})

test('account expiration clears only the matching credential', async () => {
  vi.stubEnv('VITE_POSTERN_ACCOUNTS_API', '/accounts')
  setAccountToken('expired')
  globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ error: 'sign_in_required', message: 'Expired' }), { status: 401 }))
  await expect(accounts.session()).rejects.toThrow('Expired')
  expect(accountToken()).toBeNull()
})

test('a late account refusal leaves a replacement credential intact', async () => {
  vi.stubEnv('VITE_POSTERN_ACCOUNTS_API', '/accounts')
  setAccountToken('old-account')
  let resolve!: (value: Response) => void
  globalThis.fetch = vi.fn(() => new Promise<Response>(done => { resolve = done }))
  const pending = accounts.session()
  setAccountToken('new-account')
  resolve(new Response(JSON.stringify({ error: 'account_suspended', message: 'Suspended' }), { status: 403 }))
  await expect(pending).rejects.toThrow('Suspended')
  expect(accountToken()).toBe('new-account')
})

// Coverage audit path 24.
// Value: protects=retrying account reads without losing a valid session;
// fails_when=transport or malformed-success failures escape AccountError or clear the credential;
// why_new=existing cases cover HTTP auth refusals, not transport and response-body failures;
// seam=none;
// no_claim=live service recovery or browser behavior; fetch is mocked at the real API client's HTTP boundary.
test.each([
  { failure: 'rejected fetch', respond: async () => { throw new TypeError('Offline') }, status: 0, code: 'unreachable' },
  { failure: 'invalid JSON', respond: async () => new Response('not-json'), status: 502, code: 'invalid_response' },
  { failure: 'null success response', respond: async () => new Response('null'), status: 502, code: 'invalid_response' },
])('$failure is recoverable without replacing the account credential', async ({ respond, status, code }) => {
  vi.stubEnv('VITE_POSTERN_ACCOUNTS_API', '/accounts')
  setAccountToken('existing-account')
  const account: Account = {
    id: 'existing', email: 'reader@example.invalid',
    identity: { issuer: 'https://accounts.google.com', subject: 'existing-subject' },
    profile: { handle: null, display_name: 'Reader', avatar_url: null, bio: '', website: '', published: false },
  }
  const request = vi.fn<typeof fetch>().mockImplementationOnce(respond)
    .mockResolvedValueOnce(new Response(JSON.stringify({ account })))
  globalThis.fetch = request

  const pending = accounts.session()
  await expect(pending).rejects.toBeInstanceOf(AccountError)
  await expect(pending).rejects.toMatchObject({ status, code, message: expect.stringMatching(/try again/i) })
  expect(accountToken()).toBe('existing-account')

  await expect(accounts.session()).resolves.toEqual({ account })
  expect(accountToken()).toBe('existing-account')
  expect(request.mock.calls.map(([url, init]) => [url, new Headers(init?.headers).get('Authorization')]))
    .toEqual([['/accounts/v1/session', 'Bearer existing-account'], ['/accounts/v1/session', 'Bearer existing-account']])
})
