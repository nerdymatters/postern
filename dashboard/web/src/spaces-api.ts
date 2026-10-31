import { AccountError, accountRequest, accountToken } from './accounts-api'

export type SpaceRole = 'owner' | 'admin' | 'member'
export interface AccountReference { authority: string; account_id: string }
export interface SpaceSummary { space_id: string; title: string; membership_id: string; role: SpaceRole; policy_revision: number }
export interface MySpaces { account_reference: AccountReference; spaces: SpaceSummary[]; next_after: string | null; creation: { allowed: boolean; limit: number; used: number } }
export interface Invitation { space_id: string; invitation_id: string; secret: string }
export interface Preview { space_id: string; invitation_id: string; title: string; role: SpaceRole; state: 'pending' | 'accepted'; already_member: boolean; expires_at: number }
export interface PendingInvite { invitation_id: string; role: 'admin' | 'member'; expires_at: number; authority: string; account_id: string }
export interface ResourceMetadata { id: string; title: string; kind: string; state: string; content_revision: number }
export interface Member { membership_id: string; role: SpaceRole; authority: string; account_id: string; is_self: boolean; can_change_role: boolean; can_remove: boolean }
export interface MemberList { members: Member[]; next_cursor: string | null; role: SpaceRole; membership_id: string; policy_revision: number }
export interface SpaceContext { title: string; role: SpaceRole; membership_id: string; policy_revision: number; can_leave: boolean }
export interface OwnershipStatus { title: string; role: SpaceRole; membership_id: string; policy_revision: number; transfer: { transfer_id: string; former_role: 'admin' | 'member'; expires_at: number; is_recipient: boolean; can_cancel: boolean; proposer: AccountReference; recipient: AccountReference } | null }
export interface TransferResult { transfer_id: string; state: 'pending' | 'accepted' | 'cancelled' | 'declined' | 'invalidated' | 'expired' }
export interface OwnershipProposal { operation_id: string; recipient_membership_id: string; former_role: 'admin' | 'member'; expires_in_seconds: number; expected_revision: number }
export interface Catalog { resources: ResourceMetadata[]; next_cursor: string | null }
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
export const nativeSpacesEnabled = () => Boolean(import.meta.env.VITE_POSTERN_SPACES_API)
export const nativeSpaceId = (path: string) => { const id = /^\/s\/([^/]+)(?:\/(?:members|leave|ownership))?\/?$/.exec(path)?.[1]; return id && UUID.test(id) ? id : null }
export const onboardingDestination = (path: string) => path === '/spaces/new' || path === '/invitations/join' || Boolean(nativeSpaceId(path))

function encode(prefix: string, value: unknown) {
  return prefix + btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(value)))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}
function decode(prefix: string, value: string): Record<string, unknown> {
  const code = value.trim()
  if (code.length > 2048 || !code.startsWith(prefix) || !/^[A-Za-z0-9_-]+$/.test(code.slice(prefix.length))) throw new Error('Invalid code')
  const text = atob(code.slice(prefix.length).replace(/-/g, '+').replace(/_/g, '/'))
  const object: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Uint8Array.from(text, c => c.charCodeAt(0))))
  if (!object || typeof object !== 'object' || Array.isArray(object)) throw new Error('Invalid code')
  return object as Record<string, unknown>
}
export function referenceCode(reference: AccountReference) { return encode('pr1.', reference) }
export function readReference(code: string): AccountReference {
  try {
    const value = decode('pr1.', code)
    if (Object.keys(value).sort().join(',') !== 'account_id,authority' || Object.values(value).some(v => typeof v !== 'string' || !v || [...v].length > 255 || /[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(v))) throw new Error()
    return value as unknown as AccountReference
  } catch { throw new Error('Paste the invitation address shared by the person you want to invite.') }
}
export function invitationCode(invitation: Invitation) { return encode('pi1.', { space_id: invitation.space_id, invitation_id: invitation.invitation_id, secret: invitation.secret }) }
export function readInvitation(code: string): Invitation {
  try {
    const value = decode('pi1.', code)
    if (Object.keys(value).sort().join(',') !== 'invitation_id,secret,space_id' || typeof value.space_id !== 'string' || !UUID.test(value.space_id)
      || typeof value.invitation_id !== 'string' || !UUID.test(value.invitation_id) || typeof value.secret !== 'string' || !/^pi_[A-Za-z0-9_-]{43}$/.test(value.secret)) throw new Error()
    return value as unknown as Invitation
  } catch { throw new Error('Paste the complete invitation code you received from a Postern owner or administrator.') }
}

export class SpaceError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) { super(message); this.status = status; this.code = code }
}
export function spaceMessage(error: unknown) {
  if (error instanceof AccountError && error.status === 404) return 'This invitation is unavailable for this account. It may have expired or been revoked. Check which account was invited.'
  if (error instanceof SpaceError && [401, 403, 404].includes(error.status)) return 'Your access to this Postern has changed. Check access again or return to My Posterns.'
  return error instanceof Error ? error.message : 'The request could not be completed. Please try again.'
}

/** Per-screen, memory-only resource sessions. Account tokens go only to the account service. */
export class SpacesClient {
  private generation = 0
  private closed = false
  private sessions = new Map<string, string>()
  private pending = new Map<string, Promise<string>>()
  private account: string
  constructor(account: string) { this.account = account }
  activate() { this.closed = false; this.generation++ }
  guard() { const generation = this.generation; return () => !this.closed && generation === this.generation && accountToken() === this.account }
  private requireCurrent(check = this.guard()) { if (!check()) throw new SpaceError(0, 'abandoned', 'This account has changed.') }
  private async accountRequest<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
    const check = this.guard(); this.requireCurrent(check)
    const result = await accountRequest<T>(path, method, body, this.account)
    this.requireCurrent(check); return result
  }
  mySpaces(after?: string | null) { return this.accountRequest<MySpaces>('/v1/spaces' + (after ? '?after=' + encodeURIComponent(after) : '')) }
  create(operation: string, title: string) { return this.accountRequest<{ space_id: string }>('/v1/spaces', 'POST', { operation_id: operation, title }) }
  preview(invitation: Invitation) { return this.accountRequest<Preview>('/v1/invitations/preview', 'POST', invitation) }
  accept(invitation: Invitation) { return this.accountRequest<{ space_id: string; membership_id: string; role: SpaceRole }>('/v1/invitations/accept', 'POST', invitation) }
  async ownership<T = OwnershipStatus>(space: string, path = '', body?: unknown): Promise<T> {
    try { return await this.accountRequest<T>(`/v1/spaces/${space}/ownership${path}`, body === undefined ? 'GET' : 'POST', body) }
    catch (error) {
      if (error instanceof AccountError) throw new SpaceError(error.status, error.code, error.message)
      throw error
    }
  }
  async leave(space: string, member: string, operation: string) {
    try { return await this.accountRequest<{ left: boolean }>(`/v1/spaces/${space}/leave`, 'POST', { membership_id: member, operation_id: operation }) }
    catch (error) {
      if (error instanceof AccountError && error.status === 404) throw new SpaceError(404, 'membership_unavailable', 'Your membership has changed. Return to My Posterns to check your access.')
      throw error
    }
  }
  private async transport<T>(space: string, path: string, token: string, method = 'GET', body?: unknown): Promise<T> {
    if (!UUID.test(space)) throw new SpaceError(422, 'invalid_space', 'Choose a valid Postern.')
    const base = (import.meta.env.VITE_POSTERN_SPACES_API ?? '').replace(/\/+$/, '')
    if (!base) throw new SpaceError(503, 'unconfigured', 'Private Posterns are not configured.')
    const headers = new Headers({ Accept: 'application/json' })
    if (token) headers.set('Authorization', `Bearer ${token}`)
    if (body !== undefined) headers.set('Content-Type', 'application/json')
    let response: Response
    try { response = await fetch(`${base}/v1/spaces/${space}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'omit', cache: 'no-store', signal: AbortSignal.timeout(15000) }) }
    catch { throw new SpaceError(0, 'unreachable', 'This Postern could not be reached. Check your connection and try again.') }
    const value = await response.json().catch(() => null)
    if (!response.ok) throw new SpaceError(response.status, value?.error ?? '', value?.message ?? 'This Postern could not complete the request.')
    if (value === null) throw new SpaceError(502, 'invalid_response', 'This Postern returned an incomplete response.')
    return value as T
  }
  private session(space: string): Promise<string> {
    this.requireCurrent()
    const saved = this.sessions.get(space); if (saved) return Promise.resolve(saved)
    const running = this.pending.get(space); if (running) return running
    const check = this.guard()
    const operation = (async () => {
      const handoff = await this.accountRequest<{ assertion: string }>('/v1/space-authorizations', 'POST', { space_id: space }).catch(error => {
        if (error instanceof AccountError && error.status === 404) throw new SpaceError(404, 'space_unavailable', 'This Postern is unavailable to this account.')
        throw error
      })
      this.requireCurrent(check)
      const result = await this.transport<{ token: string }>(space, '/session/exchange', '', 'POST', { assertion: handoff.assertion })
      if (typeof result.token !== 'string' || !/^ps_[A-Za-z0-9_-]{43}$/.test(result.token)) throw new SpaceError(502, 'invalid_response', 'The space session could not be opened.')
      if (!check()) { void this.transport(space, '/session/signout', result.token, 'POST').catch(() => {}); this.requireCurrent(check) }
      this.sessions.set(space, result.token); return result.token
    })()
    this.pending.set(space, operation)
    void operation.finally(() => { if (this.pending.get(space) === operation) this.pending.delete(space) }).catch(() => {})
    return operation
  }
  async resource<T>(space: string, path: string, method = 'GET', body?: unknown): Promise<T> {
    const check = this.guard(); const token = await this.session(space); this.requireCurrent(check)
    try { const value = await this.transport<T>(space, path, token, method, body); this.requireCurrent(check); return value }
    catch (error) {
      if (check() && error instanceof SpaceError && [401, 403, 404].includes(error.status)) {
        this.sessions.delete(space)
        void this.transport(space, '/session/signout', token, 'POST').catch(() => {})
      }
      throw error
    }
  }
  forgetResourceSession(space: string) {
    const token = this.sessions.get(space)
    this.sessions.delete(space)
    if (token) void this.transport(space, '/session/signout', token, 'POST').catch(() => {})
  }
  close() {
    this.closed = true; this.generation++; this.pending.clear()
    for (const space of this.sessions.keys()) this.forgetResourceSession(space)
  }
}
