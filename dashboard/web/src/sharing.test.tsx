import { StrictMode } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { setAccountToken } from './accounts-api'
import { nativeSpaceId } from './spaces-api'
import { SharingPage } from './sharing'

const space = '11111111-1111-4111-8111-111111111111'
const group = '22222222-2222-4222-8222-222222222222'
const member = '33333333-3333-4333-8333-333333333333'
const resource = '44444444-4444-4444-8444-444444444444'
const direct = '55555555-5555-4555-8555-555555555555'
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
type Call = { path: string; method: string; body: Record<string, unknown> }
function fake() {
  const calls: Call[] = []
  const state = { revision: 7, role: 'owner', groupLabel: 'Readers', stale: 1, direct: true,
    intercept: null as null | ((call: Call) => Promise<Response> | Response | undefined) }
  const page = (items: unknown[]) => ({ items, next_cursor: null, policy_revision: state.revision })
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const call = { path: String(url).split(`/v1/spaces/${space}`).at(-1)!, method: init?.method ?? 'GET', body: JSON.parse(String(init?.body ?? '{}')) }
    calls.push(call); const custom = state.intercept?.(call); if (custom) return custom
    if (call.path.endsWith('/space-authorizations')) return json({ assertion: 'synthetic' })
    if (call.path === '/session/exchange') return json({ token: 'ps_'+'s'.repeat(43) })
    if (call.path === '/session/signout') return json({ signed_out: true })
    if (call.method === 'POST') {
      state.revision++
      if (call.path.endsWith('/revoke')) state.direct = false
      if (call.path.endsWith('/cleanup')) state.stale = 0
      if (call.path.endsWith('/audiences')) state.groupLabel = String(call.body.label)
      const verb = /\/audiences\/[^/]+\/(rename|archive|add|remove)$/.exec(call.path)?.[1]
      const action = verb ? 'audience.'+verb : call.path.endsWith('/cleanup') ? 'grant.cleanup' : call.path.endsWith('/audiences') ? 'audience.create' : call.path.endsWith('/grants') ? 'grant.create' : 'grant.revoke'
      return json({ action, target: group, retired_count: 1 })
    }
    if (call.path === '/session') return json({ title: 'Workshop', role: state.role, membership_id: 'owner', policy_revision: state.revision, can_leave: false })
    if (call.path === '/audiences') return json(page([{ id: group, label: state.groupLabel, member_count: 1 }]))
    if (call.path === '/memberships') return json({ members: [{ membership_id: member, role: 'member', authority: 'fixture', account_id: 'Riley' }], next_cursor: null, policy_revision: state.revision })
    if (call.path === '/resources') return json({ resources: [{ id: resource, title: 'Handbook', state: 'published', kind: 'document' }], next_cursor: null })
    if (call.path === '/grants/cleanup') return json(page(state.stale ? [{ id: 'stale', reason: 'expired' }] : []))
    if (call.path === `/audiences/${group}/members`) return json(page([{ id: member, role: 'member', authority: 'fixture', account_id: 'Riley' }]))
    const grants = [{ id: group, audience_id: group, audience_label: state.groupLabel, all_members: false, actions: ['discover'], expires_at: null, stale_reason: null }, ...(state.direct ? [{ id: direct, membership_id: member, authority: 'fixture', account_id: 'Riley', all_members: false, actions: ['discover','view'], expires_at: null, stale_reason: null }] : [])]
    if (call.path === `/resources/${resource}/grants`) return json(page(grants))
    if (call.path === `/resources/${resource}/access/${member}`) return json({ role: 'member', actions: state.direct ? ['discover','view'] : ['discover'], sources: grants, more_sources: false, policy_revision: state.revision })
    throw new Error('Unexpected request: '+call.path)
  }))
  return { calls, state }
}
beforeEach(() => { vi.stubEnv('VITE_POSTERN_ACCOUNTS_API', '/accounts'); vi.stubEnv('VITE_POSTERN_SPACES_API', '/resources'); setAccountToken('pa_fixture') })
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals() })
function open() { return render(<StrictMode><SharingPage credential="pa_fixture" space={space} /></StrictMode>) }
async function select(label: string, value: string) {
  const field = await screen.findByLabelText(label)
  await waitFor(() => expect(field).toHaveProperty('disabled', false))
  fireEvent.change(field, { target: { value } })
  await waitFor(() => expect(screen.getByLabelText(label)).toHaveProperty('disabled', false))
}

test('sharing route and explicit review before creating a group', async () => {
  const f = fake(); open()
  expect(nativeSpaceId(`/s/${space}/sharing`)).toBe(space)
  fireEvent.change(await screen.findByLabelText('New group name'), { target: { value: 'Reviewers' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create group' }))
  await screen.findByRole('heading', { name: 'Create audience group?' })
  expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Create audience group?' }))
  expect(f.calls.some(c => c.method === 'POST' && c.path === '/audiences')).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm change' }))
  await screen.findByRole('heading', { name: 'Group members' })
  expect(f.calls.find(c => c.method === 'POST' && c.path === '/audiences')?.body).toMatchObject({ label: 'Reviewers', expected_revision: 7 })
})

test('lost-response retry preserves full grant including expiry and blocks a double submit', async () => {
  const f = fake(); open(); await select('Audience group', group); await select('Resource', resource)
  fireEvent.change(screen.getByLabelText('Expires at (optional)'), { target: { value: '2099-01-01T10:00' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review grant' }))
  await screen.findByText(/Share “Handbook” with Readers/)
  const held = deferred<Response>(); let attempts = 0
  f.state.intercept = c => c.method === 'POST' && c.path.endsWith('/grants') && ++attempts === 1 ? held.promise : undefined
  fireEvent.click(screen.getByRole('button', { name: 'Confirm change' })); fireEvent.click(screen.getByRole('button', { name: 'Working…' }))
  await waitFor(() => expect(attempts).toBe(1))
  await act(async () => held.resolve(json({ message: 'Lost response' }, 503)))
  expect(screen.getByLabelText('Share with')).toHaveProperty('disabled', true)
  const before = f.calls.length; act(() => window.dispatchEvent(new Event('focus')))
  expect(f.calls.length).toBe(before)
  fireEvent.click(screen.getByRole('button', { name: 'Retry change' }))
  await screen.findByText('Sharing updated. Current access is shown below.')
  const changes = f.calls.filter(c => c.method === 'POST' && c.path.endsWith('/grants'))
  expect(changes).toHaveLength(2); expect(changes[0].body).toEqual(changes[1].body)
  expect(changes[0].body).toMatchObject({ audience_id: group, membership_id: null, all_members: false, actions: ['discover','view'], expected_revision: 7 })
  // Value: protects=the local expiry reaches the grant as absolute seconds and the retry resends it; fails_when=the
  // expiry input is dropped, made relative or recomputed on retry; why_new=equal bodies also pass when both omit it;
  // seam=none; no_claim=server storage of the expiry (fetch mocked)
  expect(changes[0].body.expires_at).toBe(Math.floor(new Date('2099-01-01T10:00').getTime() / 1000))
})

test('grant review refuses a past expiry or missing recipient and sends the chosen member or all-member terms', async () => {
  // Value: protects=no grant is reviewed without a recipient or with a past expiry, and member/all/discover-only choices
  // send exactly those terms; fails_when=a recipient or access mapping is swapped or a guard removed; why_new=other tests
  // share only with a group for discover and view; seam=none; no_claim=server validation (fetch mocked)
  const f = fake(); open(); await select('Resource', resource)
  const review = () => fireEvent.click(screen.getByRole('button', { name: 'Review grant' }))
  const posts = () => f.calls.filter(c => c.method === 'POST' && c.path === `/resources/${resource}/grants`)
  review(); await screen.findByText('Choose the recipient group or member first.')
  fireEvent.change(screen.getByLabelText('Share with'), { target: { value: 'all' } })
  fireEvent.change(screen.getByLabelText('Expires at (optional)'), { target: { value: '2000-01-01T10:00' } })
  review(); await screen.findByText('Choose a future expiry, or leave it empty.')
  expect(screen.queryByRole('heading', { name: 'Share this resource?' })).toBeNull()
  fireEvent.change(screen.getByLabelText('Expires at (optional)'), { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('Access'), { target: { value: 'discover' } })
  review(); await screen.findByText(/Share “Handbook” with all current and future members\. Discover only\. No expiry\./)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm change' }))
  await screen.findByText('Sharing updated. Current access is shown below.')
  await select('Member', member)
  fireEvent.change(screen.getByLabelText('Share with'), { target: { value: 'member' } })
  fireEvent.change(screen.getByLabelText('Access'), { target: { value: 'view' } })
  review(); await screen.findByText(/Share “Handbook” with Riley\. Discover and view\./)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm change' }))
  await waitFor(() => expect(posts()).toHaveLength(2))
  expect(posts()[0].body).toMatchObject({ membership_id: null, audience_id: null, all_members: true, actions: ['discover'], expires_at: null })
  expect(posts()[1].body).toMatchObject({ membership_id: member, audience_id: null, all_members: false, actions: ['discover', 'view'], expires_at: null })
})

test('revoking direct access leaves and explains the group path', async () => {
  const f = fake(); open(); await select('Member', member); await select('Resource', resource)
  await screen.findByText('discover and view')
  fireEvent.click(screen.getByRole('button', { name: 'Revoke grant '+direct }))
  await screen.findByText(/Other grants and owner or administrator access still apply/)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm change' }))
  await waitFor(() => expect(screen.queryByText('discover and view')).toBeNull())
  await screen.findByText('Group: Readers · discover')
  expect(f.state.direct).toBe(false)
})

test('cleanup requires confirmation and reports retained history', async () => {
  const f = fake(); open()
  fireEvent.click(await screen.findByRole('button', { name: 'Review cleanup' }))
  await screen.findByText(/Retire up to 500 stale grants across this Postern/)
  expect(f.state.stale).toBe(1)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm change' }))
  await screen.findByText('1 stale grants retired. Grant records and audit history are retained.')
  await screen.findByText('No stale grants found.')
  expect(screen.getByRole('button', { name: 'Review cleanup' })).toHaveProperty('disabled', true)
})

test('stale revision and lost authority clear private data and pending controls', async () => {
  const f = fake(); open(); await screen.findByLabelText('New group name')
  fireEvent.click(screen.getByRole('button', { name: 'Review cleanup' }))
  f.state.intercept = c => c.method === 'POST' && c.path.endsWith('/cleanup') ? json({ message: 'Sharing changed.' }, 409) : undefined
  fireEvent.click(screen.getByRole('button', { name: 'Confirm change' }))
  await screen.findByText('Sharing changed.')
  expect(screen.queryByLabelText('Audience group')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Confirm change' })).toBeNull()
  f.state.intercept = c => c.path === '/audiences' ? json({ message: 'Denied' }, 403) : undefined
  fireEvent.click(screen.getByRole('button', { name: 'Reload sharing' }))
  await screen.findByText(/Your access to this Postern has changed/)
  expect(screen.queryByText(/Workshop/)).toBeNull()
})

test('late list response cannot restore old account data', async () => {
  const f = fake(), held = deferred<Response>()
  f.state.intercept = c => c.path === '/audiences' ? held.promise : undefined
  const view = open()
  await waitFor(() => expect(f.calls.some(c => c.path === '/audiences')).toBe(true))
  act(() => setAccountToken('pa_other')); view.unmount()
  await act(async () => held.resolve(json({ items: [{ id: group, label: 'Private group', member_count: 1 }], next_cursor: null, policy_revision: 7 })))
  expect(screen.queryByText('Private group')).toBeNull()
})

test('mixed revisions never enable sharing controls', async () => {
  const f = fake(); f.state.intercept = c => c.path === '/audiences' ? json({ items: [], next_cursor: null, policy_revision: 8 }) : undefined
  open(); await screen.findByText('Sharing changed. Reload to see current access.')
  expect(screen.queryByLabelText('New group name')).toBeNull()
})

test('choosing a resource retains a group loaded from another page', async () => {
  const f = fake(); f.state.intercept = c => c.path === '/audiences' ? json({ items: [], next_cursor: 'page-two', policy_revision: 7 })
    : c.path === '/audiences?cursor=page-two' ? json({ items: [{ id: group, label: 'Readers', member_count: 1 }], next_cursor: null, policy_revision: 7 }) : undefined
  open(); fireEvent.click(await screen.findByRole('button', { name: 'Load more groups' }))
  await screen.findByRole('option', { name: 'Readers (1 members)' })
  await select('Audience group', group); await select('Resource', resource)
  expect(screen.getByRole('option', { name: 'Readers (1 members)' })).toHaveProperty('selected', true)
  fireEvent.click(screen.getByRole('button', { name: 'Review grant' }))
  await screen.findByText(/Share “Handbook” with Readers/)
})

test('group actions name the member and group, send their own paths and bodies, and Cancel sends nothing', async () => {
  // Value: protects=add, remove, rename and archive each post to their own path with their own body after a
  // confirmation that names the member and group, Cancel posts nothing, and archive clears the group selection;
  // fails_when=a button posts to the wrong path or body, a confirmation drops the names, or archive keeps loading
  // the archived group's members; why_new=the fake answered every POST as grant.revoke, so none of these ran;
  // seam=none; no_claim=server behavior (fetch mocked)
  const f = fake(); open(); await select('Audience group', group); await select('Member', member)
  const posts = (path: string) => f.calls.filter(c => c.method === 'POST' && c.path === `/audiences/${group}/${path}`)
  fireEvent.click(screen.getByRole('button', { name: 'Add selected member to group' }))
  await screen.findByText(/Add account Riley to “Readers”/)
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(screen.queryByRole('heading', { name: 'Add to this group?' })).toBeNull()
  expect(posts('add')).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: 'Add selected member to group' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm change' }))
  await screen.findByText('Sharing updated. Current access is shown below.')
  expect(posts('add').map(c => c.body.membership_id)).toEqual([member])
  fireEvent.click(await screen.findByRole('button', { name: /^Remove/ }))
  await screen.findByText(/Account Riley will lose access through “Readers”/)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm change' }))
  await waitFor(() => expect(posts('remove').map(c => c.body.membership_id)).toEqual([member]))
  await waitFor(() => expect(screen.getByLabelText('Rename group')).toHaveProperty('disabled', false))
  fireEvent.change(screen.getByLabelText('Rename group'), { target: { value: 'Editors' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review rename' }))
  await screen.findByText(/Rename “Readers” to “Editors”/)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm change' }))
  await waitFor(() => expect(posts('rename').map(c => c.body.label)).toEqual(['Editors']))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Archive group' })).toHaveProperty('disabled', false))
  fireEvent.click(screen.getByRole('button', { name: 'Archive group' }))
  await screen.findByText(/Remove everyone from “Readers” and revoke every grant to it/)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm change' }))
  await waitFor(() => expect(posts('archive')).toHaveLength(1))
  await waitFor(() => expect(screen.getByLabelText('Audience group')).toHaveProperty('value', ''))
  const archived = f.calls.findIndex(c => c.method === 'POST' && c.path.endsWith('/archive'))
  expect(f.calls.slice(archived).some(c => c.path === `/audiences/${group}/members`)).toBe(false)
  expect(screen.queryByRole('heading', { name: 'Group members' })).toBeNull()
})

test('an unpublished resource offers no grant form and a not-shareable refusal keeps the page', async () => {
  // Value: protects=drafts and archives show why they cannot be shared instead of a form whose confirmation
  // fails as a stale page, and a not_shareable refusal keeps the loaded page; fails_when=the form shows for
  // drafts or the refusal clears the page; why_new=red-team#2 (QA capture 005 showed a 409 'reload' loop);
  // seam=none; no_claim=server refusal (fetch mocked)
  const draft = '66666666-6666-4666-8666-666666666666'
  const f = fake(); let refuse = false
  f.state.intercept = c => c.path === '/resources' ? json({ resources: [{ id: resource, title: 'Handbook', state: 'published', kind: 'document' }, { id: draft, title: 'Draft notes', state: 'draft', kind: 'document' }], next_cursor: null })
    : c.path === `/resources/${draft}/grants` ? json({ items: [], next_cursor: null, policy_revision: f.state.revision })
    : refuse && c.method === 'POST' && c.path.endsWith('/grants') ? json({ error: 'not_shareable', message: 'Only published resources can be shared, with actions they support.' }, 409) : undefined
  open(); await select('Resource', draft)
  await screen.findByText('Only published resources can receive new grants. This resource is draft.')
  expect(screen.queryByRole('button', { name: 'Review grant' })).toBeNull()
  await select('Resource', resource); await select('Audience group', group)
  refuse = true
  fireEvent.click(screen.getByRole('button', { name: 'Review grant' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm change' }))
  await screen.findByText('Only published resources can be shared, with actions they support.')
  expect(screen.queryByRole('heading', { name: 'Share this resource?' })).toBeNull()
  expect(screen.getByLabelText('Resource')).toHaveProperty('value', resource)
})

test('changing a selection keeps the page in place while the new lists load', async () => {
  // Value: protects=a selection change keeps every section mounted (disabled while busy) instead of collapsing
  // the page and dropping focus; fails_when=load clears the snapshot for a selection change; why_new=design
  // review (Codex) found the page unmounted on every dropdown change; seam=none; no_claim=focus handling of
  // real browsers when a control is disabled
  const f = fake(); open(); await screen.findByLabelText('New group name')
  const held = deferred<Response>()
  f.state.intercept = c => c.path === `/audiences/${group}/members` ? held.promise : undefined
  fireEvent.change(screen.getByLabelText('Audience group'), { target: { value: group } })
  await waitFor(() => expect(screen.getByLabelText('Audience group')).toHaveProperty('disabled', true))
  expect(screen.getByLabelText('New group name')).toBeTruthy()
  expect(screen.getByRole('heading', { name: 'Stale grants' })).toBeTruthy()
  await act(async () => held.resolve(json({ items: [], next_cursor: null, policy_revision: f.state.revision })))
  await screen.findByText('This group has no members.')
  expect(screen.getByLabelText('Audience group')).toHaveProperty('disabled', false)
})
