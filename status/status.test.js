import { test, expect } from 'bun:test'
import { Database } from 'bun:sqlite'
import { readFileSync } from 'node:fs'
import { AGENTS, SERVICES, HEALTH, STALE_MS, healthChecks, publicStatus, parseHeartbeat, saveChecks } from './model.js'
import { handle, probe, scheduled } from './worker.js'
import { targets } from './config.js'

test('cloud targets come only from explicit deployment settings', () => {
  expect(targets({})).toEqual({ website: null, api: null, dedicated: null })
  expect(targets({ WEBSITE_URL: 'https://web.example.invalid', API_HEALTH_URL: 'https://api.example.invalid:8443/health',
    SECONDARY_HEALTH_URL: 'https://backup.example.invalid/health' })).toEqual({
    website: 'https://web.example.invalid/', api: 'https://api.example.invalid:8443/health', dedicated: 'https://backup.example.invalid/health',
  })
  for (const url of ['http://example.invalid', 'javascript:alert(1)', 'https://token@example.invalid',
    'https://example.invalid/#fragment', 'https://example.invalid:bad', 'https://example.invalid/\n', {}]) {
    expect(targets({ API_HEALTH_URL: url }).api).toBeNull()
  }
})

function database() {
  const sqlite = new Database(':memory:')
  sqlite.exec(readFileSync(new URL('./migrations/0001_status.sql', import.meta.url), 'utf8'))
  return {
    prepare(sql) {
      const statement = { args: [], bind(...args) { this.args = args; return this },
        async all() { return { results: sqlite.query(sql).all(...this.args) } },
        async run() { return sqlite.query(sql).run(...this.args) } }
      return statement
    },
    async batch(statements) { for (const s of statements) await s.run() },
  }
}
const now = 1791400000000
const heartbeat = () => ({ sent_at: now, agents: Object.fromEntries(AGENTS.map(id => [id, 'operational'])) })

test('never reports green before measurements or after their freshness window', () => {
  expect(publicStatus([], now).overall).toBe('unknown')
  const rows = SERVICES.map(s => ({ id: s.id, state: 'operational', checked_at: now }))
  expect(publicStatus(rows, now + STALE_MS - 1).overall).toBe('operational')
  expect(publicStatus(rows, now + STALE_MS).services.every(s => s.state === 'unknown')).toBe(true)
  expect(publicStatus(rows, now - 16000).overall).toBe('unknown')
})

test('previous connection failure is visible but does not mark active services down', () => {
  const rows = SERVICES.map(s => ({ id: s.id, state: s.required === false ? 'outage' : 'operational', checked_at: now }))
  const status = publicStatus(rows, now)
  expect(status.overall).toBe('operational')
  expect(status.services.find(s => s.id === 'dedicated').state).toBe('outage')
})

test('HTTP 200 with paused permissions or delayed backup is degraded', () => {
  const checks = healthChecks({ ...HEALTH, permissions: 'paused', backup: 'delayed' }, now)
  expect(checks.find(c => c.id === 'permissions').state).toBe('degraded')
  expect(checks.find(c => c.id === 'backup').state).toBe('degraded')
  expect(healthChecks({ process: 'up' }, now).filter(c => c.state === 'unknown')).toHaveLength(4)
})

test('probe rejects redirects, generic HTML and oversized health; invalidates dependent checks on failure', async () => {
  for (const response of [new Response('oops', { status: 502 }), new Response('', { status: 302 }),
    new Response('<html>Sign in</html>'), new Response('x'.repeat(5000))]) {
    const checks = await probe('api', 'https://example.invalid/health', now, async () => response)
    expect(checks.find(c => c.id === 'api').state).toBe('outage')
    expect(checks.filter(c => c.state === 'unknown')).toHaveLength(4)
  }
  const checks = await probe('website', 'https://example.invalid', now, async () => new Response('<html>Maintenance</html>'))
  expect(checks[0].state).toBe('outage')
})

test('probe preserves explicit port and does not follow redirects', async () => {
  const url = 'https://example.invalid:8443/health'
  const result = await probe('api', url, now, async (target, options) => {
    expect(target).toBe(url); expect(options.redirect).toBe('manual'); expect(options.signal).toBeDefined()
    return Response.json(HEALTH)
  })
  expect(result.every(r => r.state === 'operational')).toBe(true)
})

test('website is not green when its entry bundle is missing or is an HTML fallback', async () => {
  const html = '<div id="root"></div><script type="module" crossorigin src="/assets/app.js"></script>'
  for (const bundle of [new Response('', { status: 404 }), new Response('<html>fallback</html>', { headers: { 'Content-Type': 'text/html' } })]) {
    let calls = 0
    const checks = await probe('website', 'https://example.invalid/', now, async () => ++calls === 1 ? new Response(html) : bundle)
    expect(checks[0].state).toBe('outage')
  }
  const checks = await probe('website', 'https://example.invalid/', now, async url => url.endsWith('/app.js')
    ? new Response('console.log("app")', { headers: { 'Content-Type': 'application/javascript' } }) : new Response(html))
  expect(checks[0].state).toBe('operational')
  let calls = 0
  await probe('website', 'https://example.invalid/', now, async () => { calls++; return new Response(html.replace('/assets/app.js', 'https://other.invalid/assets/app.js')) })
  expect(calls).toBe(1)
})

test('heartbeat rejects future, stale, incomplete and arbitrary service updates', () => {
  for (const body of [null, { ...heartbeat(), sent_at: now - 91000 }, { ...heartbeat(), sent_at: now + 16000 },
    { ...heartbeat(), agents: { api: 'operational' } }, { ...heartbeat(), agents: { ...heartbeat().agents, website: 'operational' } }]) {
    expect(() => parseHeartbeat(body, now)).toThrow()
  }
  expect(parseHeartbeat(heartbeat(), now)).toHaveLength(4)
})

test('heartbeats require a configured secret and cannot inject arbitrary state into storage', async () => {
  const DB = database(), token = 'test-only-'.repeat(5)
  const request = (auth, body) => new Request('https://status.example/api/heartbeat', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: auth }, body: JSON.stringify(body),
  })
  expect((await handle(request('Bearer '+token, heartbeat()), { DB })).status).toBe(401)
  expect((await handle(request('Bearer wrong', heartbeat()), { DB, HEARTBEAT_TOKEN: token })).status).toBe(401)
  expect((await handle(request('Bearer '+token, { sent_at: Date.now(), agents: { website: 'operational' } }), { DB, HEARTBEAT_TOKEN: token })).status).toBe(400)
  expect((await DB.prepare('SELECT * FROM checks').all()).results).toHaveLength(0)
  const response = await handle(request('Bearer '+token, { ...heartbeat(), sent_at: Date.now() }), { DB, HEARTBEAT_TOKEN: token })
  expect(response.status).toBe(200)
  expect((await DB.prepare('SELECT * FROM checks').all()).results).toHaveLength(4)
})

test('delayed or duplicate measurements cannot overwrite a newer outage or extend freshness', async () => {
  const db = database()
  await saveChecks(db, [{ id: 'watchdog', state: 'operational', checked_at: now }])
  await saveChecks(db, [{ id: 'watchdog', state: 'outage', checked_at: now + 60000 }])
  await saveChecks(db, [{ id: 'watchdog', state: 'operational', checked_at: now }])
  await saveChecks(db, [{ id: 'watchdog', state: 'operational', checked_at: now + 60000 }])
  expect((await db.prepare('SELECT * FROM checks').all()).results[0]).toEqual({ id: 'watchdog', state: 'outage', checked_at: now + 60000 })
  expect((await db.prepare('SELECT * FROM changes').all()).results).toHaveLength(2)
})

test('status is read-only and fails visibly when storage is unavailable', async () => {
  const response = await handle(new Request('https://status.example/api/status'), { DB: { prepare() { throw Error('private internal detail') } } })
  expect(response.status).toBe(503)
  const data = await response.json()
  expect(data.overall).toBe('unknown')
  expect(JSON.stringify(data)).not.toContain('private internal detail')
  expect(response.headers.get('Cache-Control')).toBe('no-store')
})


test('the active dedicated tunnel is required for overall health', () => {
  const rows = SERVICES.map(s => ({ id: s.id, state: s.id === 'tunnel' ? 'outage' : 'operational', checked_at: now }))
  expect(publicStatus(rows, now).overall).toBe('outage')
})


test('missing probe settings replace old green readings with unknown without network requests', async () => {
  const DB = database()
  await saveChecks(DB, SERVICES.map(service => ({ id: service.id, state: 'operational', checked_at: Date.now() - 1000 })))
  await scheduled({ DB }, async () => { throw Error('unconfigured target must not be fetched') })
  const rows = (await DB.prepare('SELECT * FROM checks').all()).results
  for (const id of ['website', 'api', 'permissions', 'audit', 'backup', 'storage', 'dedicated']) {
    expect(rows.find(row => row.id === id).state).toBe('unknown')
  }
  expect(publicStatus(rows, Date.now()).overall).toBe('unknown')
})


test('configured probe settings are used and page links reject scripts and escape HTML attributes', async () => {
  const DB = database(), seen = []
  await scheduled({ DB, API_HEALTH_URL: 'https://api.example.invalid/health' }, async url => {
    seen.push(url)
    return Response.json(HEALTH)
  })
  expect(seen).toEqual(['https://api.example.invalid/health'])
  const rows = (await DB.prepare('SELECT * FROM checks').all()).results
  expect(rows.find(row => row.id === 'api').state).toBe('operational')
  for (const url of [undefined, 'javascript:alert(1)', 'https://user:secret@example.invalid']) {
    const response = await handle(new Request('https://status.example.invalid/'), { WEBSITE_URL: url })
    expect(await response.text()).not.toContain('Open Postern')
  }
  const response = await handle(new Request('https://status.example.invalid/'), { WEBSITE_URL: "https://example.invalid/?a='&b=2" })
  const html = await response.text()
  expect(html).toContain('&amp;b=2')
  expect(html).not.toContain('href="https://example.invalid/?a=\'')
})
