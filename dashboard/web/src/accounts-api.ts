import type { Identity } from './api'

export interface Profile { handle: string | null; display_name: string; avatar_url: string | null; bio: string; website: string; published: boolean }
export type PublicProfile = Omit<Profile, 'published' | 'handle'> & { handle: string }
export interface Terms { version: string; sha256: string; url: string }
export interface TermsAcceptance { accepted: true; version: string; sha256: string }
export interface Account { id: string; email: string; identity: Identity; profile: Profile; terms?: (Terms & { required: boolean; accepted_at: number | null }) | null; terms_unavailable?: boolean }
export interface AccountConfig { google_client_id: string; registration_open: boolean; terms?: Terms | null; terms_unavailable?: boolean }
export type ProfileEdit = Pick<Profile, 'display_name' | 'bio' | 'website' | 'published'> & { handle: string }

const KEY = 'postern.account.token'
const CHANGE = 'postern-account-change'
export const accountsEnabled = () => Boolean(import.meta.env.VITE_POSTERN_ACCOUNTS_API)
export function accountToken() { return localStorage.getItem(KEY) }
export function setAccountToken(value: string | null) {
  if (value) localStorage.setItem(KEY, value)
  else localStorage.removeItem(KEY)
  window.dispatchEvent(new Event(CHANGE))
}
export function subscribeAccount(handler: () => void) {
  const changed = (event: StorageEvent) => { if (event.key === KEY || event.key === null) handler() }
  window.addEventListener(CHANGE, handler)
  window.addEventListener('storage', changed)
  return () => { window.removeEventListener(CHANGE, handler); window.removeEventListener('storage', changed) }
}

export class AccountError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message); this.status = status; this.code = code
  }
}

async function request<T>(path: string, method = 'GET', body?: unknown, token: string | null = accountToken()): Promise<T> {
  const base = (import.meta.env.VITE_POSTERN_ACCOUNTS_API ?? '').replace(/\/+$/, '')
  const headers = new Headers({ Accept: 'application/json' })
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (body !== undefined) headers.set('Content-Type', 'application/json')
  let response: Response
  try {
    response = await fetch(base + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', credentials: 'omit', signal: AbortSignal.timeout(15000) })
  } catch {
    throw new AccountError(0, 'unreachable', 'Your Postern account could not be reached. Check your connection and try again.')
  }
  const value = await response.json().catch(() => null)
  if (!response.ok) {
    // Only the credential that made this request can expire the current account.
    // A late refusal for a signed-out account must not clear a newer sign-in.
    if (token && token === accountToken() && (response.status === 401 || value?.error === 'account_suspended')) setAccountToken(null)
    if (token && token === accountToken() && ['terms_required', 'terms_unavailable'].includes(value?.error)) window.dispatchEvent(new Event('postern-terms-change'))
    throw new AccountError(response.status, value?.error ?? '', value?.message ?? 'The account service could not complete that request.')
  }
  if (value === null) throw new AccountError(502, 'invalid_response', 'The account service returned an incomplete response. Please try again.')
  return value as T
}

export const accounts = {
  config: () => request<AccountConfig>('/v1/config', 'GET', undefined, null),
  signIn: (credential: string, terms?: TermsAcceptance) => request<{ token: string; account: Account }>('/v1/auth/google', 'POST', { credential, ...(terms ? { terms } : {}) }, null),
  acceptTerms: (terms: TermsAcceptance) => request<{ account: Account }>('/v1/terms/accept', 'POST', terms),
  session: () => request<{ account: Account }>('/v1/session'),
  save: (profile: ProfileEdit) => request<{ account: Account }>('/v1/profile', 'PATCH', profile),
  signOut: (token: string | null) => request<{ signed_out: boolean }>('/v1/auth/signout', 'POST', undefined, token),
  delete: () => request<{ deleted: boolean }>('/v1/account', 'DELETE', { confirm: true }),
  profile: (handle: string) => request<{ profile: PublicProfile }>(`/v1/profiles/${encodeURIComponent(handle)}`, 'GET', undefined, null),
}

export function sameIdentity(a: Identity | undefined, b: Identity | undefined) {
  return Boolean(a?.issuer && a.subject && b?.issuer && b.subject && a.issuer === b.issuer && a.subject === b.subject)
}

export function accountMessage(error: unknown) { return error instanceof Error ? error.message : 'Something went wrong. Please try again.' }

// Native onboarding reuses the account boundary with an explicitly captured credential.
export { request as accountRequest }
