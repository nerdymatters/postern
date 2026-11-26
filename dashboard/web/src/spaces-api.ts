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
export interface ContentRoute { space: string; resource: string; kind: 'document' | 'video'; revision: number }
export interface DocumentPresentation { resource_id: string; revision: number; page_count: number }
export interface VideoPresentation { resource_id: string; revision: number; duration_ms: number; viewer_label: string }
export interface VideoTicket { ticket: string; expires_in_seconds: number }
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
export const nativeSpacesEnabled = () => Boolean(import.meta.env.VITE_POSTERN_SPACES_API)
export const nativeContentEnabled = () => nativeSpacesEnabled() && ['1', 'true'].includes(import.meta.env.VITE_POSTERN_NATIVE_CONTENT ?? '')
export function nativeContentRoute(path: string): ContentRoute | null {
  const match = /^\/s\/([^/]+)\/resources\/([^/]+)\/(document|video)\/([1-9][0-9]*)\/?$/.exec(path)
  if (!match || !UUID.test(match[1]) || !UUID.test(match[2]) || !Number.isSafeInteger(Number(match[4]))) return null
  return { space: match[1], resource: match[2], kind: match[3] as ContentRoute['kind'], revision: Number(match[4]) }
}
export const nativeSpaceId = (path: string) => { const id = /^\/s\/([^/]+)(?:\/(?:members|leave|ownership|sharing))?\/?$/.exec(path)?.[1]; return id && UUID.test(id) ? id : nativeContentEnabled() ? nativeContentRoute(path)?.space ?? null : null }
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
/**
 * The file viewer's message. The service answers the same 404 on purpose whether
 * the file is not available, file reading is off for this Postern, or the member's
 * rights changed (do not probe which), so "access has changed" would often be false.
 * A 401 means the space session ended or was refused: Check access again opens a
 * new one, and the wording below stays true if the access did change.
 */
export function fileMessage(error: unknown) {
  if (error instanceof SpaceError && [401, 403, 404].includes(error.status)) return "This file can't be opened right now. It may not be available in this Postern, or your access may have changed. Check access again or return to My Posterns."
  return spaceMessage(error)
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
  private async transport<T>(space: string, path: string, token: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
    if (!UUID.test(space)) throw new SpaceError(422, 'invalid_space', 'Choose a valid Postern.')
    const base = (import.meta.env.VITE_POSTERN_SPACES_API ?? '').replace(/\/+$/, '')
    if (!base) throw new SpaceError(503, 'unconfigured', 'Private Posterns are not configured.')
    const headers = new Headers({ Accept: 'application/json' })
    if (token) headers.set('Authorization', `Bearer ${token}`)
    if (body !== undefined) headers.set('Content-Type', 'application/json')
    let response: Response
    try { response = await fetch(`${base}/v1/spaces/${space}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'omit', cache: 'no-store', signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000) }) }
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
  async resource<T>(space: string, path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
    const check = this.guard(); const token = await this.session(space); this.requireCurrent(check)
    try { signal?.throwIfAborted(); const value = await this.transport<T>(space, path, token, method, body, signal); this.requireCurrent(check); signal?.throwIfAborted(); return value }
    catch (error) {
      if (check() && error instanceof SpaceError && [401, 403, 404].includes(error.status)) {
        this.sessions.delete(space)
        void this.transport(space, '/session/signout', token, 'POST').catch(() => {})
      }
      throw error
    }
  }
  private contentPath(space: string, resource: string, revision: number) {
    if (!nativeContentEnabled()) throw new SpaceError(404, 'content_disabled', 'Protected content is not enabled.')
    if (!UUID.test(space) || !UUID.test(resource) || !Number.isSafeInteger(revision) || revision < 1) throw new SpaceError(422, 'invalid_content', 'Choose a valid resource revision.')
    return `/resources/${resource}`
  }
  async document(space: string, resource: string, revision: number, signal?: AbortSignal) {
    const path = this.contentPath(space, resource, revision)
    const value = await this.resource<DocumentPresentation>(space, `${path}/document?revision=${revision}`, 'GET', undefined, signal)
    if (value.resource_id !== resource || value.revision !== revision || !Number.isInteger(value.page_count) || value.page_count < 1 || value.page_count > 10000) throw new SpaceError(502, 'invalid_response', 'This document returned an incomplete response.')
    return value
  }
  async video(space: string, resource: string, revision: number, signal?: AbortSignal) {
    const path = this.contentPath(space, resource, revision)
    const value = await this.resource<VideoPresentation>(space, `${path}/video?revision=${revision}`, 'GET', undefined, signal)
    if (value.resource_id !== resource || value.revision !== revision || !Number.isInteger(value.duration_ms) || value.duration_ms < 1 || value.duration_ms > 86400000 || typeof value.viewer_label !== 'string' || !UUID.test(value.viewer_label)) throw new SpaceError(502, 'invalid_response', 'This video returned an incomplete response.')
    return value
  }
  async videoTicket(space: string, resource: string, revision: number, signal?: AbortSignal) {
    const path = this.contentPath(space, resource, revision)
    const value = await this.resource<VideoTicket>(space, `${path}/video/ticket?revision=${revision}`, 'POST', undefined, signal)
    if (typeof value.ticket !== 'string' || !/^pc_[A-Za-z0-9_-]{43}$/.test(value.ticket) || !Number.isInteger(value.expires_in_seconds) || value.expires_in_seconds < 1 || value.expires_in_seconds > 30) throw new SpaceError(502, 'invalid_response', 'This video returned an incomplete response.')
    return value
  }
  videoSource(space: string, resource: string, revision: number, ticket: string) {
    this.requireCurrent()
    const path = this.contentPath(space, resource, revision)
    if (!/^pc_[A-Za-z0-9_-]{43}$/.test(ticket)) throw new SpaceError(502, 'invalid_response', 'This video returned an incomplete response.')
    const base = (import.meta.env.VITE_POSTERN_SPACES_API ?? '').replace(/\/+$/, '')
    return `${base}/v1/spaces/${space}${path}/video/content.mp4?revision=${revision}&ticket=${encodeURIComponent(ticket)}`
  }
  async documentPage(space: string, resource: string, revision: number, page: number, signal: AbortSignal): Promise<Blob> {
    const path = this.contentPath(space, resource, revision)
    if (!Number.isInteger(page) || page < 0 || page >= 10000) throw new SpaceError(422, 'invalid_page', 'Choose a valid document page.')
    const check = this.guard(), token = await this.session(space)
    this.requireCurrent(check); signal.throwIfAborted()
    const base = (import.meta.env.VITE_POSTERN_SPACES_API ?? '').replace(/\/+$/, '')
    try {
      const response = await fetch(`${base}/v1/spaces/${space}${path}/document/pages/${page}.jpg?revision=${revision}`, {
        headers: { Authorization: `Bearer ${token}`, Accept: 'image/jpeg' }, cache: 'no-store', credentials: 'omit',
        signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]),
      })
      this.requireCurrent(check); signal.throwIfAborted()
      if (!response.ok) {
        const value = await response.json().catch(() => null)
        throw new SpaceError(response.status, value?.error ?? '', 'This document is unavailable. Check access again.')
      }
      if (response.headers.get('content-type')?.split(';')[0] !== 'image/jpeg' || !response.body) {
        await response.body?.cancel().catch(() => {})
        throw new SpaceError(502, 'invalid_response', 'This document returned an incomplete response.')
      }
      const reader = response.body.getReader(), chunks: ArrayBuffer[] = []
      let size = 0, complete = false
      try {
        while (true) {
          const result = await reader.read()
          this.requireCurrent(check); signal.throwIfAborted()
          if (result.done) { complete = true; break }
          size += result.value.byteLength
          if (size > 4 * 1024 * 1024) throw new SpaceError(502, 'invalid_response', 'This document exceeded its page limit.')
          chunks.push(new Uint8Array(result.value).buffer)
        }
      } finally { if (!complete) await reader.cancel().catch(() => {}); reader.releaseLock() }
      if (!size) throw new SpaceError(502, 'invalid_response', 'This document returned an incomplete response.')
      return new Blob(chunks, { type: 'image/jpeg' })
    } catch (error) {
      if (check() && error instanceof SpaceError && [401, 403, 404].includes(error.status)) this.forgetResourceSession(space)
      if (!(error instanceof SpaceError) && !signal.aborted) throw new SpaceError(0, 'unreachable', 'This document could not be reached. Check your connection and try again.')
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
