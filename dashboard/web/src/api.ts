/** Shared API types and refusal handling. */

const BASE = (import.meta.env.VITE_POSTERN_API ?? '').replace(/\/+$/, '')
const TOKEN = 'postern.token'

export interface Status { process: string; permissions: string; audit: string; backup: string; storage: string }
export interface Config { mode: 'demo' | 'google'; google_client_id: string; notice: string; notice_version: string; ip_notice: string; ip_notice_version: string; status: Status; selfie_enabled?: boolean; selfie_notice_version?: string; selfie_retention_days?: number; preview?: boolean; branding?: import('./branding').Branding }
export interface SelfieState { enabled: boolean; required: boolean; id?: string; expires_at?: number; pending_id?: string; error?: string }
export interface SelfieRecord { id: string; email: string; captured_at: number; expires_at: number; retired_at: number | null; status: string }
/** Managers and C-suite read like staff and may skip chapter quizzes; only admins manage content. */
export type Role = 'reader' | 'admin' | 'manager' | 'c-suite'
export interface Identity { issuer: string; subject: string }
export interface Session { identity?: Identity; email: string; name: string; role: Role; consent_required: boolean; notice: string; notice_version: string; selfie?: SelfieState }
export interface SignIn { identity?: Identity; token: string; email: string; name: string; role: Role }
export interface Item {
  id: string; post_id: string; filename: string; kind: 'pdf' | 'photo' | 'video'; pages: number; duration_seconds?: number
  derive_status: 'queued' | 'converting' | 'ready' | 'failed'; derive_error: string | null; sort: number
  progress?: { opened_pages: number[]; watched_seconds?: number; history_incomplete: boolean }
}
export interface Post {
  id: string; title: string; body: string; category: string
  status: 'draft' | 'publishing' | 'published' | 'archived'
  title_sync_pending?: boolean
  category_sync_pending?: boolean
  /** Reading room only: the SOP chapter number, and the earlier chapter whose quiz still locks it. */
  chapter?: number | null; locked_by?: number | null
  revision: number; created_at: number; published_at: number | null; error: string | null; items: Item[]
}
/** English with its Thai translation. */
export interface Bilingual { en: string; th: string }
export interface QuizStanding {
  /** Final Exam is optional for Admins; absent on older APIs. */
  optional?: boolean
  chapter: number; category: string; total: number; pass_mark: number; attempts: number
  best_score: number | null; last_score: number | null; passed: boolean; passed_at: number | null
  last_attempt_at: number | null; locked_by: number | null
  /** Questions in the chapter's pool; absent before 0.8.0. */
  pool?: number
  /** Items in this chapter, other than failed ones, the reader still has to finish before the quiz opens;
   *  one still being prepared is listed and keeps the quiz closed.
   *  Optional: an API from before 0.7.1 does not send it during a staggered deploy. */
  unfinished?: string[]
}
/** The certificate for passing the Final Exam. The number and date come from the first pass; the name is the
 *  one its owner saved (`name_confirmed`), else the fuller of their staff-list and Google account names. */
export interface Certificate { number: string; name: string; awarded_at: number; name_confirmed: boolean }
/** `certificate` is null before a Final Exam pass and absent from APIs before badges, during a staggered deploy. */
export interface Wall { posts: Post[]; quizzes?: QuizStanding[]; quiz_exempt?: boolean; final_exam?: QuizStanding | null; certificate?: Certificate | null }
export interface QuizQuestion { id: string; prompt: Bilingual; options: (Bilingual & { id: string })[] }
export interface Quiz {
  chapter: number; category: string; version: string; total: number; pass_mark: number
  exempt: boolean; standing: QuizStanding
  /** Questions in the chapter's pool; each attempt deals `total` of them (after a pass, only from the
   *  questions of the person's passing attempts and of any attempt since their first pass, topped up from
   *  the pool only when a bank edit removed some). Absent before 0.8.0. */
  pool?: number
  /** False until every page is opened and every video watched; questions are empty until then.
   *  Absent from APIs before 0.7.1, which always send the questions. */
  ready?: boolean; unfinished?: string[]; questions: QuizQuestion[]
}
export interface QuizResult {
  final_exam?: QuizStanding | null
  chapter: number; score: number; total: number; pass_mark: number; passed: boolean
  /** Each answer, whether it was right, and its explanation after every passing attempt; empty after a
   *  failed one, which reports only the score. Once a chapter is passed its retakes ask only questions
   *  from the person's passing attempts and any attempt since their first pass (topped up from the pool
   *  only when a bank edit removed some), so a passing retake reveals no answer a pass had not shown,
   *  other than a top-up's. */
  results: { id: string; chosen: string; correct: boolean; answer: string; explanation: Bilingual }[]
  quizzes: QuizStanding[]; next_chapter: { chapter: number; category: string; unlocked: boolean } | null
}
export interface View { id: string; item_id: string; post_id: string; attempt_id: string }
export interface Person {
  email: string; name: string; role: string; status: 'opened' | 'not_opened' | 'unknown'
  first_opened_at: number | null; attempts: number; seconds_to_open: number | null; selfie_id?: string | null
}
export interface Report { post: Post; people: Person[]; opened: number; total: number; history_incomplete: boolean; meaning: string }
export interface TrainingItem {
  id: string; post_id: string; title: string; category: string; filename: string
  kind: Item['kind']; pages: number; duration_seconds: number | null
  opened_pages: number; watched_seconds: number; fraction: number | null; opened: boolean
  attempts: number; last_activity_at: number | null
}
export interface TrainingPerson {
  groups?: string[]
  final_exam?: QuizStanding | null
  certificate?: Certificate | null
  email: string; name: string; role: string; percent: number | null
  status: 'no_content' | 'unavailable' | 'complete' | 'in_progress' | 'unknown' | 'not_started'
  completed_items: number; total_items: number; opened_items: number; last_activity_at: number | null
  items: TrainingItem[]; quizzes?: QuizStanding[]; quizzes_passed?: number; quiz_exempt?: boolean
}
export interface TrainingProgress {
  people: TrainingPerson[]; total_items: number; history_incomplete: boolean; quiz_total?: number
  groups?: string[]; roster_warnings?: { group: string; row: number; message: string }[]
}
export interface CertificateHistory {
  certificate: Certificate
  entries: { id: string; name: string; saved_at: number }[]
  next_cursor: string | null
  history_incomplete: boolean
}
export interface Denial { ts: number; email: string | null; ip: string | null; user_agent: string | null }
export interface Operation {
  id: string; post_id: string; kind: string; status: 'pending' | 'confirmed' | 'cancelled'
  attempts: number; error: string | null; post: { status: string; error: string | null } | null
}
export interface AdminHealth extends Status {
  mode: string; pending_events: number; oldest_unexported_at: number | null; last_export_at: number | null
  allowlist_error: string | null; export_error: string | null; recovery_mode: boolean
}
export interface Publication { operation_id: string; status: string; status_url: string }

export type RefusalCode =
  | 'sign_in_required'
  | 'access_denied'
  | 'consent_required'
  | 'selfie_required'
  | 'selfie_unavailable'
  | 'authorization_source_unavailable'
  | 'audit_unavailable'

/** A refusal handled by the shared application gate. */
export class Refusal extends Error {
  code: RefusalCode
  status: number
  constructor(code: RefusalCode, status: number, message: string) {
    super(message)
    this.code = code
    this.status = status
  }
}

/** Everything else: a rejected upload, a caption too long, a post still preparing.
 *  These are shown where they happen, never as a whole-screen state. */
export class ApiError extends Error {
  code: string
  status: number
  constructor(status: number, code: string, message: string) {
    super(message)
    this.code = code
    this.status = status
  }
}

let token = localStorage.getItem(TOKEN)
export function currentToken() { return token }
/** Revoke sessions held by this tab or opened in another tab, without adopting
 * another tab's bearer for private API requests before identity verification. */
export function tokensForSignOut() {
  return [...new Set([token, localStorage.getItem(TOKEN)])].filter((value): value is string => !!value)
}
export function setToken(value: string | null) {
  token = value
  if (value) localStorage.setItem(TOKEN, value)
  else localStorage.removeItem(TOKEN)
}

let announce: (refusal: Refusal) => void = () => {}
export function onRefusal(handler: (refusal: Refusal) => void) {
  announce = handler
  return () => { if (announce === handler) announce = () => {} }
}

function refusalCode(status: number, code: string): RefusalCode | null {
  if (status === 401) return 'sign_in_required'
  if (status === 403) return code === 'selfie_required' ? 'selfie_required' : code === 'consent_required' ? 'consent_required' : 'access_denied'
  if (status === 503 && (code === 'authorization_source_unavailable' || code === 'audit_unavailable' || code === 'selfie_unavailable')) return code
  return null
}

function failure(status: number, code: string, message: string, notify = true): Refusal | ApiError {
  const refused = refusalCode(status, code)
  if (!refused) return new ApiError(status, code, message)
  const refusal = new Refusal(refused, status, message)
  if (notify) announce(refusal)
  return refusal
}

function described(body: unknown, status: number) {
  const shape = (body ?? {}) as { error?: string; message?: string }
  return { code: shape.error ?? '', message: shape.message ?? `The server answered ${status}.` }
}

// inlineSelfieOutage: a private-storage outage on this call is the caller's to show in place, not a whole-screen refusal.
async function send(path: string, init?: RequestInit, inlineSelfieOutage = false, quietRefusal = false): Promise<Response> {
  const identity = token
  const headers = new Headers(init?.headers)
  headers.set('X-Postern-Client', 'web')
  if (path.startsWith('/api/auth/')) headers.set('X-Postern-Device', browserId())
  if (token) headers.set('Authorization', `Bearer ${token}`)
  let response: Response
  try {
    response = await fetch(BASE + path, { ...init, headers, cache: 'no-store' })
  } catch {
    throw new ApiError(0, 'unreachable', 'Postern could not be reached. Check your connection.')
  }
  if (!response.ok) {
    const { code, message } = described(await response.json().catch(() => null), response.status)
    if (inlineSelfieOutage && response.status === 503 && code === 'selfie_unavailable') throw new ApiError(response.status, code, message)
    throw failure(response.status, code, message, !quietRefusal && identity === token)
  }
  return response
}

async function read<T>(path: string): Promise<T> { return await (await send(path)).json() as T }

async function write<T>(path: string, body?: unknown, options?: RequestInit): Promise<T> {
  const init: RequestInit = { ...options, method: 'POST' }
  if (body !== undefined) {
    init.body = JSON.stringify(body)
    init.headers = { 'Content-Type': 'application/json' }
  }
  return await (await send(path, init)).json() as T
}

/** Protected pixels: fetched with the bearer token like every other route (ENG-D1). */
async function bytes(path: string): Promise<Blob> { return (await send(path)).blob() }

/** An account-scoped browser convenience, not a device fingerprint or credential. */
function randomBrowserId(): string {
  // getRandomValues also works on HTTP development origins where randomUUID is absent.
  if (crypto.randomUUID) return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
let transientBrowser = ''
export function browserId(): string {
  try {
    let value = localStorage.getItem('postern.browser')
    if (!value) { value = randomBrowserId(); localStorage.setItem('postern.browser', value) }
    return value
  } catch {
    return transientBrowser ||= randomBrowserId()
  }
}

/** Uploads report progress, so they go through XHR rather than fetch. */
function attach(postId: string, file: File, progress: (fraction: number) => void): Promise<Item> {
  return new Promise((resolve, reject) => {
    const identity = token
    const request = new XMLHttpRequest()
    request.open('POST', `${BASE}/api/admin/posts/${postId}/${file.name.toLowerCase().endsWith('.mp4') ? 'videos' : 'items'}`)
    request.setRequestHeader('X-Postern-Client', 'web')
    if (token) request.setRequestHeader('Authorization', `Bearer ${token}`)
    request.upload.onprogress = event => { if (event.lengthComputable) progress(event.loaded / event.total) }
    request.onerror = () => reject(new ApiError(0, 'unreachable', 'The upload could not reach the server.'))
    request.onload = () => {
      let body: unknown = null
      try { body = JSON.parse(request.responseText) } catch { body = null }
      if (request.status >= 200 && request.status < 300) resolve(body as Item)
      else {
        const { code, message } = described(body, request.status)
        reject(failure(request.status, code, message, identity === token))
      }
    }
    const form = new FormData()
    form.append('file', file)
    request.send(form)
  })
}

export const api = {
  selfieState: async (inlineOutage = false) => await (await send('/api/selfie', undefined, inlineOutage)).json() as SelfieState,
  selfieImage: async (id: string) => (await send(`/api/selfies/${encodeURIComponent(id)}.jpg`, undefined, true)).blob(),
  selfies: () => read<{ selfies: SelfieRecord[]; cleanup_error?: string }>('/api/admin/selfies'),
  captureSelfie: async (photo: Blob, version: string) => {
    const body = new FormData()
    body.append('file', photo, 'selfie.jpg')
    body.append('consent_version', version)
    // A storage or disk fault while saving stays on the capture screen with the photo kept, like a detector outage.
    return await (await send('/api/selfie', { method: 'POST', body }, true)).json() as { pending_id: string }
  },
  config: () => read<Config>('/api/config'),
  session: () => read<Session>('/api/session'),
  configForAccount: async () => await (await send('/api/config', { signal: AbortSignal.timeout(15000) }, false, true)).json() as Config,
  revokeForAccount: async (credential: string) => {
    await fetch(BASE + '/api/auth/signout', { method: 'POST', headers: { Authorization: `Bearer ${credential}` }, cache: 'no-store', signal: AbortSignal.timeout(10000) })
  },
  sessionForAccount: async ({ quietRefusal = true }: { quietRefusal?: boolean } = {}) => await (await send('/api/session', { signal: AbortSignal.timeout(15000) }, false, quietRefusal)).json() as Session,
  signInGoogleForAccount: async (credential: string) => await (await send('/api/auth/google', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ credential }), signal: AbortSignal.timeout(15000) }, false, true)).json() as SignIn,
  signInDemo: (email: string) => write<SignIn>('/api/auth/demo', { email, name: '' }),
  signInGoogle: (credential: string) => write<SignIn>('/api/auth/google', { credential }),
  consent: (version: string, ipVersion: string, monitoringAccepted: boolean, ipAccepted: boolean) => write<{ accepted: boolean }>('/api/consent', { version, ip_version: ipVersion, monitoring_accepted: monitoringAccepted, ip_accepted: ipAccepted }),
  signOut: () => write<{ signed_out: boolean }>('/api/auth/signout'),
  posts: () => read<Wall>('/api/posts'),
  quiz: (chapter: number) => read<Quiz>(`/api/quizzes/${chapter}`),
  submitQuiz: (chapter: number, version: string, answers: Record<string, string>) => write<QuizResult>(`/api/quizzes/${chapter}/attempts`, { version, answers }),
  nameCertificate: (name: string) => write<Certificate>('/api/certificate/name', { name }),
  createView: (itemId: string, attempt: string) => write<View>(`/api/items/${itemId}/views`, { attempt_id: attempt }),
  page: (viewId: string, page: number) => bytes(`/api/views/${viewId}/page/${page}.jpg`),
  video: async (viewId: string) => {
    const result = await write<{ path: string; email: string; stamp: string; selfie_id?: string; subtitles?: string }>(`/api/views/${viewId}/video`)
    return { ...result, url: BASE + result.path }
  },
  videoProgress: (viewId: string, ranges: number[][]) => write<{ saved: boolean }>(`/api/views/${viewId}/video-progress`, { ranges }, { signal: AbortSignal.timeout(10000) }),
  photo: (viewId: string) => bytes(`/api/views/${viewId}/photo.jpg`),
  thumb: (postId: string) => bytes(`/api/posts/${postId}/thumb.jpg`),
  adminPosts: () => read<{ posts: Post[] }>('/api/admin/posts'),
  renamePost: (id: string, title: string) => write<Post>(`/api/admin/posts/${id}/rename`, { title }),
  renameCategory: (category: string, name: string) => write<{ name: string; updated: number }>('/api/admin/categories/rename', { category, name }),
  createPost: (draft: { title: string; body: string; category: string }) => write<Post>('/api/admin/posts', draft),
  attach,
  publish: (postId: string) => write<Publication>(`/api/admin/posts/${postId}/publish`),
  operation: (statusUrl: string) => read<Operation>(statusUrl),
  archive: (postId: string) => write<{ status: string }>(`/api/admin/posts/${postId}/archive`),
  report: (postId: string) => read<Report>(`/api/admin/posts/${postId}/report`),
  trainingProgress: () => read<TrainingProgress>('/api/admin/progress'),
  certificateHistory: (email: string, before?: string) => read<CertificateHistory>(`/api/admin/certificate-history?${new URLSearchParams({ email, ...(before ? { before } : {}) })}`),
  denials: () => read<{ denied: Denial[] }>('/api/admin/signin-denials'),
  health: () => read<AdminHealth>('/api/admin/health'),
}

/** A refusal already owns the whole screen; anything else is said where it happened. */
export function inline(error: unknown): string {
  return error instanceof ApiError ? error.message : ''
}
