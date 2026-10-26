export const SERVICES = [
  { id: 'website', name: 'Postern website', group: 'Staff services' },
  { id: 'api', name: 'Training service', group: 'Staff services' },
  { id: 'permissions', name: 'Staff access list', group: 'Staff services' },
  { id: 'audit', name: 'Activity recording', group: 'Staff services' },
  { id: 'backup', name: 'Activity backup', group: 'Staff services' },
  { id: 'storage', name: 'File storage', group: 'Staff services' },
  { id: 'api-process', name: 'Application agent', group: 'Office agents' },
  { id: 'proxy', name: 'Connection agent', group: 'Office agents' },
  { id: 'watchdog', name: 'Health watchdog', group: 'Office agents' },
  { id: 'tunnel', name: 'Dedicated tunnel agent', group: 'Office agents' },
  { id: 'dedicated', name: 'Secondary connection', group: 'Connection recovery', required: false },
]
export const AGENTS = SERVICES.filter(s => s.group === 'Office agents').map(s => s.id)
export const STATES = ['operational', 'degraded', 'outage', 'unknown']
export const STALE_MS = 180_000
export const HEALTH = { process: 'up', permissions: 'ready', audit: 'ready', backup: 'current', storage: 'ready' }

export function healthChecks(value, now) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || value.process !== 'up') {
    throw new Error('Invalid health response')
  }
  return Object.entries(HEALTH).map(([key, expected]) => ({
    id: key === 'process' ? 'api' : key,
    state: !(key in value) ? 'unknown' : value[key] === expected ? 'operational' : 'degraded',
    checked_at: now,
  }))
}

export function publicStatus(rows, now) {
  const byId = new Map(rows.map(r => [r.id, r]))
  const services = SERVICES.map(service => {
    const row = byId.get(service.id)
    const fresh = row && row.checked_at <= now + 15_000 && now - row.checked_at < STALE_MS
    return { ...service, state: fresh && STATES.includes(row.state) ? row.state : 'unknown',
      checked_at: row?.checked_at ?? null }
  })
  const required = services.filter(s => s.required !== false)
  const overall = ['outage', 'degraded', 'unknown'].find(state => required.some(s => s.state === state)) || 'operational'
  return { overall, generated_at: now, stale_after_ms: STALE_MS, services }
}

export function parseHeartbeat(body, now) {
  if (!body || !Number.isSafeInteger(body.sent_at) || body.sent_at > now + 15_000 || now - body.sent_at > 90_000) {
    throw new Error('Heartbeat timestamp is missing or stale')
  }
  if (!body.agents || typeof body.agents !== 'object' || Array.isArray(body.agents)
      || Object.keys(body.agents).length !== AGENTS.length
      || AGENTS.some(id => !['operational', 'outage', 'unknown'].includes(body.agents[id]))) {
    throw new Error('Invalid agent states')
  }
  return AGENTS.map(id => ({ id, state: body.agents[id], checked_at: Math.min(now, body.sent_at) }))
}

export async function saveChecks(db, checks) {
  const statements = checks.flatMap(check => [
    db.prepare(`INSERT INTO changes(service, state, changed_at)
      SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM checks WHERE id = ? AND (state = ? OR checked_at >= ?))`)
      .bind(check.id, check.state, check.checked_at, check.id, check.state, check.checked_at),
    db.prepare(`INSERT INTO checks(id, state, checked_at) VALUES (?, ?, ?)
      ON CONFLICT(id) DO UPDATE SET state = excluded.state, checked_at = excluded.checked_at
      WHERE excluded.checked_at > checks.checked_at`).bind(check.id, check.state, check.checked_at),
  ])
  await db.batch(statements)
}
