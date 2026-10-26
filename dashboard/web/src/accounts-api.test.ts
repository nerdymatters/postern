import { afterEach, expect, test, vi } from 'vitest'
import { accounts, accountToken, setAccountToken } from './accounts-api'
import { api, onRefusal, setToken } from './api'

afterEach(() => { vi.unstubAllEnvs() })

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
