/** Test doubles for the three browser things jsdom does not implement (object
 *  URLs we can count, canvas 2d, IntersectionObserver) plus a fake Postern. */
import { setToken, type Certificate, type Config, type Post, type Quiz, type QuizResult, type QuizStanding, type Report, type Session } from './api'
import { forgetViews } from './views'

export const objectUrls = { created: [] as string[], revoked: [] as string[] }
export const canvasOps = { drawn: 0, cleared: 0 }
export const calls: { method: string; url: string; body: string }[] = []
const observers: FakeObserver[] = []

class FakeObserver {
  callback: IntersectionObserverCallback
  targets = new Set<Element>()
  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback
    observers.push(this)
  }
  observe(target: Element) { this.targets.add(target) }
  unobserve(target: Element) { this.targets.delete(target) }
  disconnect() { this.targets.clear() }
  show() {
    const entries = [...this.targets].map(target => ({ target, isIntersecting: true } as IntersectionObserverEntry))
    if (entries.length) this.callback(entries, this as unknown as IntersectionObserver)
  }
}

class FakeImage {
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  naturalWidth = 800
  naturalHeight = 1000
  set src(_value: string) { setTimeout(() => this.onload?.(), 0) }
}

/** Scroll everything below the fold into view. */
export function showEverything() {
  for (const observer of [...observers]) observer.show()
}

export function installStubs() {
  let counter = 0
  URL.createObjectURL = ((): string => {
    const url = `blob:postern/${++counter}`
    objectUrls.created.push(url)
    return url
  }) as typeof URL.createObjectURL
  URL.revokeObjectURL = ((url: string) => { objectUrls.revoked.push(url) }) as typeof URL.revokeObjectURL
  HTMLCanvasElement.prototype.getContext = (() => ({
    drawImage: () => { canvasOps.drawn++ },
    clearRect: () => { canvasOps.cleared++ },
  })) as unknown as typeof HTMLCanvasElement.prototype.getContext
  globalThis.IntersectionObserver = FakeObserver as unknown as typeof IntersectionObserver
  globalThis.Image = FakeImage as unknown as typeof Image
}

export function resetStubs() {
  objectUrls.created.length = 0
  objectUrls.revoked.length = 0
  calls.length = 0
  observers.length = 0
  canvasOps.drawn = 0
  canvasOps.cleared = 0
  setToken(null)
  forgetViews()
  localStorage.clear()
  sessionStorage.clear()
  window.history.pushState(null, '', '/')
}

export interface World {
  config: Config
  session: Session
  posts: Post[]
  /** Make the server answer one of its refusal shapes. */
  refuse: { status: number; error: string; message: string; path?: RegExp } | null
  /** How many times a publication reports itself still pending before it confirms. */
  pendingPolls: number
  publishing?: string
  quizzes?: QuizStanding[]
  quizExempt?: boolean
  finalExam?: QuizStanding | null
  certificate?: Certificate | null
  /** Chapter quizzes the fake server can hand out, and how it grades a submission. */
  quizBank?: Record<number, Quiz>
  grade?: (chapter: number, answers: Record<string, string>) => QuizResult
}

export function post(id: string, title: string, pages = 8): Post {
  return {
    id, title, body: 'Effective from Oct 6.', category: 'Policies', status: 'published', revision: 1,
    created_at: Date.now() / 1000 - 3600, published_at: Date.now() / 1000 - 3600, error: null,
    items: [{
      id: `item-${id}`, post_id: id, filename: `${id}.pdf`, kind: 'pdf', pages,
      derive_status: 'ready', derive_error: null, sort: 0,
    }],
  }
}

export function draft(id: string, title: string): Post {
  return { ...post(id, title), status: 'draft', published_at: null }
}

export function report(subject: Post): Report {
  const now = Date.now() / 1000
  return {
    post: subject,
    people: [
      { email: 'maria.santos@example.invalid', name: 'Maria Santos', role: 'reader', status: 'opened', first_opened_at: now - 600, attempts: 1, seconds_to_open: 3000 },
      { email: 'carlos.rivera@example.invalid', name: 'Carlos Rivera', role: 'reader', status: 'not_opened', first_opened_at: null, attempts: 0, seconds_to_open: null },
    ],
    opened: 1, total: 2, history_incomplete: false,
    meaning: 'Opened means content served to the signed-in reader, not proof it was read.',
  }
}

export function world(overrides: Partial<World> = {}): World {
  return {
    config: {
      mode: 'demo', google_client_id: '', notice: 'Postern records your sign-ins and the items you open.',
      notice_version: '1', ip_notice_version: 'ip-1',
      ip_notice: 'Training materials belong to their respective rights holders. You may not copy, revise, update, or reproduce these materials without prior permission from the rights holder.',
      status: { process: 'up', permissions: 'ready', audit: 'ready', backup: 'current', storage: 'ready' },
    },
    session: { email: 'maria.santos@example.invalid', name: 'Maria Santos', role: 'reader', consent_required: false, notice: '', notice_version: '1' },
    posts: [post('p1', 'Tip pooling policy'), post('p2', 'Fall menu rollout')],
    refuse: null,
    pendingPolls: 1,
    ...overrides,
  }
}

export function installApi(state: World) {
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ method, url, body: typeof init?.body === 'string' ? init.body : '' })
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

    if (url.endsWith('/api/config')) return json(state.config)
    const refuse = state.refuse
    if (refuse && (!refuse.path || refuse.path.test(url))) {
      return json({ error: refuse.error, message: refuse.message }, refuse.status)
    }
    if (url.endsWith('/api/session')) return json(state.session)
    if (url.endsWith('/api/auth/demo')) return json({ token: 'session-token', email: state.session.email, name: state.session.name, role: state.session.role })
    if (url.endsWith('/api/consent')) { state.session = { ...state.session, consent_required: false }; return json({ accepted: true }) }
    if (url.endsWith('/api/auth/signout')) return json({ signed_out: true })
    if (url.endsWith('/api/posts')) return json({ posts: state.posts, quizzes: state.quizzes ?? [], quiz_exempt: state.quizExempt ?? false, final_exam: state.finalExam ?? null, certificate: state.certificate ?? null })
    if (url.endsWith('/api/certificate/name') && state.certificate) {
      const body = JSON.parse(init?.body as string ?? '{}') as { name: string }
      state.certificate = { ...state.certificate, name: body.name.trim().replace(/\s+/g, ' '), name_confirmed: true }
      return json(state.certificate)
    }
    const attempt = /\/api\/quizzes\/([0-9]+)\/attempts$/.exec(url)
    if (attempt && state.grade) {
      const body = JSON.parse(init?.body as string ?? '{}') as { answers: Record<string, string> }
      const result = state.grade(Number(attempt[1]), body.answers)
      state.quizzes = result.quizzes
      if (result.final_exam !== undefined) state.finalExam = result.final_exam
      return json(result)
    }
    const quiz = /\/api\/quizzes\/([0-9]+)$/.exec(url)
    if (quiz && state.quizBank?.[Number(quiz[1])]) return json(state.quizBank[Number(quiz[1])])
    // Bytes, not a Blob: under Node the Blob here is jsdom's, which the native Response cannot read.
    if (url.endsWith('.jpg')) return new Response(new TextEncoder().encode('jpeg-bytes'), { headers: { 'Content-Type': 'image/jpeg' } })
    if (url.endsWith('/api/admin/posts') && method === 'GET') return json({ posts: state.posts })
    if (url.endsWith('/api/admin/posts')) {
      const body = JSON.parse(init?.body as string ?? '{}') as { title: string }
      const created = draft(`p${state.posts.length + 1}`, body.title)
      state.posts = [created, ...state.posts]
      return json(created)
    }
    if (url.endsWith('/api/admin/health')) {
      return json({
        ...state.config.status, mode: 'demo', pending_events: 0, oldest_unexported_at: null,
        last_export_at: Date.now() / 1000, allowlist_error: null, export_error: null, recovery_mode: false,
      })
    }
    if (url.endsWith('/api/admin/signin-denials')) {
      return json({ denied: [{ ts: Date.now() / 1000, email: 'stranger@example.invalid', ip: '203.0.113.7', user_agent: 'Safari' }] })
    }
    const reported = /\/api\/admin\/posts\/([^/]+)\/report$/.exec(url)
    if (reported) return json(report(state.posts.find(candidate => candidate.id === reported[1]) ?? state.posts[0]))
    const published = /\/api\/admin\/posts\/([^/]+)\/publish$/.exec(url)
    if (published) {
      state.publishing = published[1]
      return json({ operation_id: 'op-1', status: 'publishing', status_url: '/api/admin/operations/op-1' }, 202)
    }
    if (url.includes('/api/admin/operations/')) {
      if (state.pendingPolls > 0) {
        state.pendingPolls -= 1
        return json({ id: 'op-1', post_id: state.publishing, kind: 'publish', status: 'pending', attempts: 1, error: null, post: { status: 'publishing', error: null } })
      }
      state.posts = state.posts.map(candidate => candidate.id === state.publishing ? { ...candidate, status: 'published' } : candidate)
      return json({ id: 'op-1', post_id: state.publishing, kind: 'publish', status: 'confirmed', attempts: 1, error: null, post: { status: 'published', error: null } })
    }
    const view = /\/api\/items\/([^/]+)\/views$/.exec(url)
    if (view) {
      const attempt = JSON.parse(init?.body as string ?? '{}') as { attempt_id: string }
      const owner = state.posts.find(candidate => candidate.items.some(item => item.id === view[1]))
      return json({ id: `view-${view[1]}`, item_id: view[1], post_id: owner?.id ?? '', attempt_id: attempt.attempt_id })
    }
    return json({ error: 'not_found', message: 'No such route in the fake server.' }, 404)
  }) as typeof fetch
}

export const viewPosts = () => calls.filter(call => call.method === 'POST' && call.url.endsWith('/views'))
export const thumbnailGets = () => calls.filter(call => call.url.endsWith('/thumb.jpg'))
