import { page } from './page.js'
import { client } from './client.js'
import { SERVICES, HEALTH, healthChecks, publicStatus, parseHeartbeat, saveChecks } from './model.js'
import { targets } from './config.js'
const HEADERS = {
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Robots-Tag': 'noindex',
}
function json(body, status = 200) { return Response.json(body, { status, headers: HEADERS }) }

async function authorized(request, secret) {
  if (!secret || secret.length < 32) return false
  const presented = request.headers.get('Authorization') || ''
  const hashes = await Promise.all([presented, `Bearer ${secret}`].map(value =>
    crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))))
  const a = new Uint8Array(hashes[0]), b = new Uint8Array(hashes[1])
  let difference = 0
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i]
  return difference === 0
}

async function limitedText(response, limit) {
  const reader = response.body?.getReader()
  if (!reader) return ''
  const chunks = []; let bytes = 0
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      bytes += value.length
      if (bytes > limit) throw new Error('Response too large')
      chunks.push(value)
    }
  } finally { await reader.cancel().catch(() => {}) }
  const body = new Uint8Array(bytes); let offset = 0
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.length }
  return new TextDecoder().decode(body)
}

export async function probe(id, url, now, fetcher = fetch) {
  const failed = { id, state: 'outage', checked_at: now }
  try {
    const response = await fetcher(url, { redirect: 'manual', signal: AbortSignal.timeout(10_000),
      headers: { 'Cache-Control': 'no-cache', 'User-Agent': 'PosternStatus/1.0' } })
    if (response.status !== 200) { await response.body?.cancel(); throw new Error('HTTP failure') }
    const text = await limitedText(response, id === 'website' ? 100_000 : 4096)
    if (id === 'website') {
      if (!text.includes('<div id="root">') || !text.includes('/assets/')) throw new Error('Not the app')
      const script = text.match(/<script\b[^>]*\btype=["']module["'][^>]*\bsrc=["']([^"']+)["'][^>]*>/i)
      if (!script) throw new Error('Missing entry module')
      const asset = new URL(script[1], url)
      if (asset.origin !== new URL(url).origin || !asset.pathname.startsWith('/assets/') || asset.username || asset.password) {
        throw new Error('Unexpected asset origin')
      }
      const bundle = await fetcher(asset.href, { redirect: 'manual', signal: AbortSignal.timeout(10_000),
        headers: { 'Cache-Control': 'no-cache', 'User-Agent': 'PosternStatus/1.0' } })
      if (bundle.status !== 200 || !/javascript/.test(bundle.headers.get('Content-Type') || '')) {
        await bundle.body?.cancel(); throw new Error('Entry module unavailable')
      }
      const code = await limitedText(bundle, 1_000_000)
      if (!code.trim() || /^\s*</.test(code)) throw new Error('Invalid entry module')
      return [{ id, state: 'operational', checked_at: now }]
    }
    const checks = healthChecks(JSON.parse(text), now)
    return id === 'api' ? checks : [{ id, state: checks.every(c => c.state === 'operational') ? 'operational' : 'degraded', checked_at: now }]
  } catch {
    // API failure also invalidates its dependent checks, rather than retaining old green readings.
    return id === 'api' ? [failed, ...Object.keys(HEALTH).filter(k => k !== 'process')
      .map(key => ({ id: key, state: 'unknown', checked_at: now }))] : [failed]
  }
}

export async function scheduled(env, fetcher = fetch) {
  const now = Date.now()
  const checks = (await Promise.all(Object.entries(targets(env)).map(([id, url]) => url ? probe(id, url, now, fetcher)
    : (id === 'api' ? ['api', ...Object.keys(HEALTH).filter(key => key !== 'process')] : [id])
      .map(key => ({ id: key, state: 'unknown', checked_at: now }))))).flat()
  await saveChecks(env.DB, checks)
  await env.DB.prepare('DELETE FROM changes WHERE changed_at < ?').bind(now - 7 * 86400_000).run()
}

export async function handle(request, env) {
  const path = new URL(request.url).pathname
  if (request.method === 'GET' && path === '/favicon.ico') return new Response(null, { status: 204, headers: HEADERS })
  if (request.method === 'GET' && path === '/status.js') return new Response(client, {
    headers: { ...HEADERS, 'Content-Type': 'text/javascript; charset=utf-8' },
  })
  if (request.method === 'GET' && path === '/') return new Response(page(targets(env).website), { headers: {
    ...HEADERS, 'Content-Type': 'text/html; charset=utf-8',
    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
  } })
  if (request.method === 'GET' && path === '/api/status') {
    try {
      const { results } = await env.DB.prepare('SELECT id, state, checked_at FROM checks').all()
      const { results: changes } = await env.DB.prepare('SELECT service, state, changed_at FROM changes ORDER BY changed_at DESC LIMIT 30').all()
      return json({ ...publicStatus(results, Date.now()), changes: changes.filter(c => SERVICES.some(s => s.id === c.service)) })
    } catch { return json({ ...publicStatus([], Date.now()), changes: [] }, 503) }
  }
  if (request.method === 'POST' && path === '/api/heartbeat') {
    if (!await authorized(request, env.HEARTBEAT_TOKEN)) return json({ error: 'Unauthorized' }, 401)
    if (!request.headers.get('Content-Type')?.startsWith('application/json')) return json({ error: 'JSON required' }, 415)
    let checks
    try { checks = parseHeartbeat(JSON.parse(await limitedText(request, 2048)), Date.now()) }
    catch { return json({ error: 'Invalid heartbeat' }, 400) }
    try { await saveChecks(env.DB, checks); return json({ accepted: true }) }
    catch { return json({ error: 'Storage unavailable' }, 503) }
  }
  return json({ error: 'Not found' }, 404)
}

export default { fetch: handle, scheduled: (_event, env, ctx) => ctx.waitUntil(scheduled(env)) }
