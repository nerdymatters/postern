import { StrictMode } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { AccountsApp } from './accounts'
import { accountToken, setAccountToken, type Account } from './accounts-api'
import { currentToken, setToken } from './api'
import { SpacesClient, invitationCode, readInvitation, readReference, referenceCode, type SpaceRole } from './spaces-api'

vi.mock('./google', () => ({ loadIdentity: async () => ({ accounts: { id: { initialize() {}, renderButton() {} } } }) }))
const space = '11111111-1111-4111-8111-111111111111'
const inv = { space_id: space, invitation_id: '22222222-2222-4222-8222-222222222222', secret: 'pi_' + 's'.repeat(43) }
const self = '33333333-3333-4333-8333-333333333333'
const member = '44444444-4444-4444-8444-444444444444'
const ps = 'ps_' + 'r'.repeat(43)
const reference = { authority: 'fixture', account_id: 'recipient' }
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
const person = (name: string): Account => ({ id: name, email: name + '@example.invalid', identity: { issuer: 'fixture', subject: name }, profile: { handle: null, display_name: name, avatar_url: null, bio: '', website: '', published: false } })
type Call = { path: string; method: string; authorization: string | null; body: Record<string, unknown> }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
function fake() {
  const calls: Call[] = []
  const state = { transfer: null as import('./spaces-api').OwnershipStatus['transfer'], role: 'owner' as SpaceRole, revision: 7, canLeave: false, members: [
    { membership_id: self, role: 'owner' as SpaceRole, authority: 'fixture', account_id: 'owner', is_self: true, can_change_role: false, can_remove: false },
    { membership_id: member, role: 'member' as SpaceRole, authority: 'fixture', account_id: 'recipient', is_self: false, can_change_role: true, can_remove: true },
  ], creation: { allowed: true, limit: 2, used: 0 }, spaces: [] as object[], refusal: false,
    intercept: null as null | ((call: Call) => Promise<Response> | Response | undefined) }
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const call: Call = { path: String(url), method: init?.method ?? 'GET', authorization: new Headers(init?.headers).get('Authorization'), body: JSON.parse(String(init?.body ?? '{}')) }
    calls.push(call)
    const custom = state.intercept?.(call); if (custom) return custom
    if (call.path === '/accounts/v1/config') return json({ google_client_id: 'fixture', registration_open: true })
    if (call.path === '/accounts/v1/session') return json({ account: person(call.authorization === 'Bearer pa_b' ? 'Bob' : 'Alice') })
    if (call.path === '/accounts/v1/spaces') return call.method === 'POST' ? json({ space_id: space }) : json({ account_reference: reference, spaces: state.spaces, next_after: null, creation: state.creation })
    if (call.path === '/accounts/v1/invitations/preview') return json({ ...inv, title: 'Private workshop', role: 'member', state: 'pending', already_member: false, expires_at: 1800000000 })
    if (call.path === '/accounts/v1/invitations/accept') return state.refusal ? json({ message: 'Unavailable' }, 404) : json({ space_id: space })
    if (call.path === '/accounts/v1/space-authorizations') return json({ assertion: 'signed-handoff' })
    if (call.path.endsWith('/ownership')) return json({ title: 'Private workshop', role: state.role, membership_id: self, policy_revision: state.revision, transfer: state.transfer })
    if (call.path.endsWith('/ownership/propose')) {
      state.revision++; state.transfer = { transfer_id: inv.invitation_id, former_role: call.body.former_role as 'admin' | 'member', expires_at: 1800000000, is_recipient: false, can_cancel: true, proposer: { authority: 'fixture', account_id: 'owner' }, recipient: reference }
      return json({ transfer_id: inv.invitation_id, state: 'pending' })
    }
    if (call.path.includes('/ownership/') && /\/(accept|decline|cancel)$/.test(call.path)) {
      const action = call.path.split('/').at(-1); state.transfer = null; state.revision++
      if (action === 'accept') state.role = 'owner'
      return json({ transfer_id: inv.invitation_id, state: action === 'accept' ? 'accepted' : action === 'decline' ? 'declined' : 'cancelled' })
    }
    if (call.path.endsWith('/session/exchange')) return json({ token: ps })
    if (call.path.endsWith('/session/signout')) return json({ signed_out: true })
    if (call.path.endsWith('/session')) return json({ title: 'Private workshop', role: state.role, policy_revision: state.revision, membership_id: self, can_leave: state.canLeave })
    if (call.path.endsWith('/memberships')) return json({ members: state.members, next_cursor: null, role: state.role, membership_id: self, policy_revision: state.revision })
    if (call.path.endsWith('/role')) { state.members[1].role = call.body.role as SpaceRole; state.revision++; return json({ target: member, revision: state.revision }) }
    if (call.path.endsWith('/remove')) { state.members = state.members.filter(m => m.membership_id !== member); state.revision++; return json({ target: member, revision: state.revision }) }
    if (call.path.endsWith('/leave')) return json({ left: true })
    if (call.path.includes('/resources?')) return json({ resources: [], next_cursor: null })
    if (call.path.endsWith('/invitations')) return call.method === 'POST' ? json(inv) : json({ invitations: [] })
    throw new Error('Unexpected path: ' + call.path)
  }))
  return { calls, state }
}
beforeEach(() => { vi.stubEnv('VITE_POSTERN_ACCOUNTS_API', '/accounts'); vi.stubEnv('VITE_POSTERN_SPACES_API', '/resources'); setAccountToken('pa_a') })
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals() })
function open(path = '/home') { window.history.pushState(null, '', path); return render(<StrictMode><AccountsApp /></StrictMode>) }

test('native home shows live entitlement and never opens a leftover legacy session', async () => {
  const f = fake(); setToken('legacy-session'); open()
  expect(await screen.findByRole('heading', { name: 'My Posterns' })).toBeTruthy()
  expect(await screen.findByRole('link', { name: 'Create a Postern' })).toBeTruthy()
  expect(f.calls.every(c => c.path.startsWith('/accounts/'))).toBe(true)
  f.state.creation = { allowed: false, limit: 2, used: 2 }
  act(() => window.dispatchEvent(new Event('focus')))
  await screen.findByText('You have used your approved space creations.')
  expect(screen.queryByRole('link', { name: 'Create a Postern' })).toBeNull()
})

test('late My Posterns result cannot follow an account switch', async () => {
  const f = fake(), held = deferred<Response>()
  f.state.intercept = c => c.path === '/accounts/v1/spaces' && c.authorization === 'Bearer pa_a' ? held.promise : undefined
  open(); await waitFor(() => expect(f.calls.some(c => c.path === '/accounts/v1/spaces')).toBe(true))
  act(() => setAccountToken('pa_b'))
  await screen.findByText('Bob', { selector: 'strong' })
  await act(async () => held.resolve(json({ account_reference: reference, creation: f.state.creation, next_after: null, spaces: [{ space_id: space, title: 'Alice private title', role: 'owner' }] })))
  expect(screen.queryByText('Alice private title')).toBeNull()
  expect(accountToken()).toBe('pa_b')
})

test('create retries reuse the same operation after a lost response and double submit', async () => {
  const f = fake(), held = deferred<Response>(); let attempts = 0
  f.state.intercept = c => c.path === '/accounts/v1/spaces' && c.method === 'POST' ? ++attempts === 1 ? held.promise : undefined : undefined
  open('/spaces/new'); fireEvent.change(await screen.findByLabelText('Postern name'), { target: { value: 'Workshop' } })
  const button = screen.getByRole('button', { name: 'Create Postern' }); fireEvent.click(button); fireEvent.click(button)
  await waitFor(() => expect(attempts).toBe(1))
  await act(async () => held.resolve(json({ message: 'Response lost' }, 503)))
  expect(screen.getByLabelText('Postern name')).toHaveProperty('disabled', true)
  fireEvent.click(await screen.findByRole('button', { name: 'Try again' }))
  await screen.findByRole('heading', { name: 'Private workshop' })
  const requests = f.calls.filter(c => c.path === '/accounts/v1/spaces' && c.method === 'POST')
  expect(requests).toHaveLength(2); expect(requests[0].body).toEqual(requests[1].body)
})

test('review is a separate step; acceptance refusal removes private preview', async () => {
  const f = fake(); f.state.refusal = true; open('/invitations/join')
  fireEvent.change(await screen.findByLabelText('Invitation code'), { target: { value: invitationCode(inv) } })
  fireEvent.click(screen.getByRole('button', { name: 'Review invitation' }))
  await screen.findByRole('heading', { name: 'Private workshop' })
  expect(f.calls.some(c => c.path.endsWith('/accept'))).toBe(false)
  expect(location.href).not.toContain(inv.secret)
  expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain(inv.secret)
  fireEvent.click(screen.getByRole('button', { name: 'Join Postern' }))
  await screen.findByText(/This invitation is unavailable/)
  expect(screen.queryByRole('heading', { name: 'Private workshop' })).toBeNull()
  expect(screen.getByLabelText('Invitation code')).toHaveProperty('value', '')
  expect(accountToken()).toBe('pa_a')
})

test('account switch during preview hides its late response', async () => {
  const f = fake(), held = deferred<Response>(); f.state.intercept = c => c.path.endsWith('/preview') ? held.promise : undefined
  open('/invitations/join'); fireEvent.change(await screen.findByLabelText('Invitation code'), { target: { value: invitationCode(inv) } })
  fireEvent.click(screen.getByRole('button', { name: 'Review invitation' }))
  act(() => setAccountToken('pa_b'))
  await screen.findByLabelText('Invitation code')
  await act(async () => held.resolve(json({ ...inv, title: 'Old private title', role: 'member', already_member: false })))
  expect(screen.queryByText('Old private title')).toBeNull()
})

test('native handoff is single flight, keeps credential domains separate, and revokes on close', async () => {
  const f = fake(); setToken('legacy-session'); const client = new SpacesClient('pa_a')
  await Promise.all([client.resource(space, '/session'), client.resource(space, '/resources?')])
  expect(f.calls.filter(c => c.path.endsWith('/space-authorizations'))).toHaveLength(1)
  expect(f.calls.filter(c => c.path.endsWith('/session/exchange'))).toHaveLength(1)
  expect(f.calls.find(c => c.path.endsWith('/space-authorizations'))?.authorization).toBe('Bearer pa_a')
  expect(f.calls.find(c => c.path.endsWith('/session/exchange'))?.authorization).toBeNull()
  expect(f.calls.find(c => c.path.endsWith('/resources?'))?.authorization).toBe('Bearer ' + ps)
  expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain(ps)
  client.close(); await waitFor(() => expect(f.calls.some(c => c.path.endsWith('/session/signout'))).toBe(true))
  expect(accountToken()).toBe('pa_a'); expect(currentToken()).toBe('legacy-session')
})

test('a session issued after the old account leaves is revoked without a catalog read', async () => {
  const f = fake(), held = deferred<Response>(); f.state.intercept = c => c.path.endsWith('/session/exchange') ? held.promise : undefined
  const client = new SpacesClient('pa_a'); const request = client.resource(space, '/resources?'); const rejected = expect(request).rejects.toThrow('This account has changed.')
  await waitFor(() => expect(f.calls.some(c => c.path.endsWith('/session/exchange'))).toBe(true))
  client.close(); setAccountToken('pa_b'); held.resolve(json({ token: ps })); await rejected
  await waitFor(() => expect(f.calls.some(c => c.path.endsWith('/session/signout'))).toBe(true))
  expect(f.calls.some(c => c.path.endsWith('/resources?'))).toBe(false)
  expect(accountToken()).toBe('pa_b')
})

test('resource refusal keeps the account and legacy credential, then reacquires space access', async () => {
  const f = fake(); setToken('legacy-session'); const client = new SpacesClient('pa_a'); let refuse = true
  f.state.intercept = c => c.path.endsWith('/resources?') && refuse ? json({ message: 'denied' }, 401) : undefined
  await expect(client.resource(space, '/resources?')).rejects.toThrow('denied')
  expect(accountToken()).toBe('pa_a'); expect(currentToken()).toBe('legacy-session')
  refuse = false; await client.resource(space, '/resources?')
  expect(f.calls.filter(c => c.path.endsWith('/session/exchange'))).toHaveLength(2); client.close()
})

test('manual copy fallback is usable and invitation issuing retries preserve the request', async () => {
  const f = fake(); let first = true
  const write = vi.fn(async () => { throw new Error('blocked') })
  vi.stubGlobal('navigator', Object.create(navigator, { clipboard: { value: { writeText: write } } }))
  f.state.intercept = c => c.path.endsWith('/invitations') && c.method === 'POST' && first ? (first = false, json({ message: 'Lost response' }, 503)) : undefined
  open('/s/' + space)
  fireEvent.change(await screen.findByLabelText('Recipient’s invitation address'), { target: { value: referenceCode(reference) } })
  fireEvent.click(screen.getByRole('button', { name: 'Create invitation' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Try invitation again' }))
  const copyButton = await screen.findByRole('button', { name: 'Copy invitation code' })
  await act(async () => { fireEvent.click(copyButton) })
  expect(write).toHaveBeenCalledWith(invitationCode(inv))
  const field = await screen.findByLabelText('Invitation code — select and copy')
  expect(field).toHaveProperty('value', invitationCode(inv))
  const issued = f.calls.filter(c => c.path.endsWith('/invitations') && c.method === 'POST')
  expect(issued).toHaveLength(2); expect(issued[0].body).toEqual(issued[1].body)
})

test('account-bound invitation formats reject arbitrary URLs, extra fields and malformed secrets', () => {
  expect(readReference(referenceCode(reference))).toEqual(reference)
  expect(readInvitation(invitationCode(inv))).toEqual(inv)
  for (const value of ['https://foreign.invalid', invitationCode({ ...inv, secret: 'bad' }), 'pi1.e30']) expect(() => readInvitation(value)).toThrow()
  expect(() => readReference(referenceCode({ ...reference, email: 'extra' } as typeof reference))).toThrow()
})

test('lost membership on handoff shows space recovery without invitation wording', async () => {
  const f = fake(); f.state.intercept = c => c.path.endsWith('/space-authorizations') ? json({ message: 'Unavailable' }, 404) : undefined
  open('/s/' + space)
  await screen.findByText('Your access to this Postern has changed. Check access again or return to My Posterns.')
  expect(screen.queryByText(/This invitation is unavailable/)).toBeNull()
  expect(screen.queryByRole('heading', { name: 'Private workshop' })).toBeNull()
  expect(accountToken()).toBe('pa_a')
})

test('owner confirms a named member role change, then sees the refreshed role', async () => {
  const f = fake(); open('/s/' + space + '/members')
  fireEvent.click(await screen.findByRole('button', { name: 'Make administrator' }))
  await screen.findByRole('heading', { name: 'Change role to Administrator?' })
  expect(f.calls.some(c => c.path.endsWith('/role'))).toBe(false)
  expect(screen.getByText('Account recipient')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm role change' }))
  await screen.findByRole('button', { name: 'Make member' })
  const change = f.calls.find(c => c.path.endsWith('/role'))!
  expect(change.body).toMatchObject({ role: 'admin', expected_revision: 7 })
  expect(change.authorization).toBe('Bearer ' + ps)
})

test('member removal requires confirmation and retries the original operation after a lost response', async () => {
  const f = fake(), held = deferred<Response>(); let attempts = 0
  f.state.intercept = c => c.path.endsWith('/remove') && ++attempts === 1 ? held.promise : undefined
  open('/s/' + space + '/members')
  fireEvent.click(await screen.findByRole('button', { name: 'Remove from Postern' }))
  expect(f.calls.some(c => c.path.endsWith('/remove'))).toBe(false)
  const button = screen.getByRole('button', { name: 'Confirm removal' }); fireEvent.click(button); fireEvent.click(button)
  await waitFor(() => expect(attempts).toBe(1))
  await act(async () => held.resolve(json({ message: 'Lost response' }, 503)))
  const reads = f.calls.filter(c => c.path.endsWith('/memberships')).length
  act(() => window.dispatchEvent(new Event('focus')))
  expect(f.calls.filter(c => c.path.endsWith('/memberships'))).toHaveLength(reads)
  fireEvent.click(await screen.findByRole('button', { name: 'Retry change' }))
  await screen.findByText('Member removed from this Postern.')
  await screen.findByText('Your membership')
  const changed = f.calls.filter(c => c.path.endsWith('/remove'))
  expect(changed).toHaveLength(2); expect(changed[0].body).toEqual(changed[1].body)
  expect(screen.queryByText('Account recipient')).toBeNull()
})

test('stale membership revision drops the confirmation and requires a fresh list', async () => {
  const f = fake(); f.state.intercept = c => c.path.endsWith('/role') ? json({ message: 'Memberships changed. Reload the member list before trying again.' }, 409) : undefined
  open('/s/' + space + '/members'); fireEvent.click(await screen.findByRole('button', { name: 'Make administrator' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm role change' }))
  await screen.findByRole('button', { name: 'Reload members' })
  expect(screen.queryByText('Account recipient')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Retry change' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Reload members' }))
  await screen.findByRole('button', { name: 'Make administrator' })
})

test('administrator sees removal of ordinary members but no promotion controls', async () => {
  const f = fake(); f.state.role = 'admin'; f.state.members[0].role = 'admin'; f.state.members[1].can_change_role = false
  open('/s/' + space + '/members'); await screen.findByText('Account recipient')
  expect(screen.queryByRole('button', { name: 'Make administrator' })).toBeNull()
  expect(screen.getAllByRole('button', { name: 'Remove from Postern' })).toHaveLength(1)
  const own = screen.getByText('Your membership').closest('section')!
  expect(within(own).queryByRole('button', { name: 'Remove from Postern' })).toBeNull()
})

test('member roster denial and late account-switch responses never show a directory', async () => {
  const f = fake(), held = deferred<Response>()
  f.state.intercept = c => c.path.endsWith('/memberships') ? c.authorization === 'Bearer ' + ps && accountToken() === 'pa_a' ? held.promise : json({ message: 'forbidden' }, 403) : undefined
  open('/s/' + space + '/members')
  await waitFor(() => expect(f.calls.some(c => c.path.endsWith('/memberships'))).toBe(true))
  act(() => setAccountToken('pa_b'))
  await screen.findByText(/Your access to this Postern has changed/)
  await act(async () => held.resolve(json({ members: f.state.members, role: 'owner', membership_id: self, policy_revision: 7, next_cursor: null })))
  expect(screen.queryByText('Account recipient')).toBeNull()
  expect(accountToken()).toBe('pa_b')
})

test('member address matching uses the immutable reference and does not change permissions', async () => {
  fake(); open('/s/' + space + '/members')
  fireEvent.change(await screen.findByLabelText('Invitation address'), { target: { value: referenceCode(reference) } })
  fireEvent.click(screen.getByRole('button', { name: 'Find member' }))
  expect(screen.getByText('Account recipient')).toBeTruthy()
  expect(screen.queryByText('Your membership')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Show everyone' }))
  expect(screen.getByText('Your membership')).toBeTruthy()
})

test('last owner gets an explanation and cannot submit self-departure', async () => {
  const f = fake(); open('/s/' + space + '/leave')
  await screen.findByText('You are the last owner. Another active owner is required before you can leave.')
  expect(screen.queryByRole('button', { name: 'Confirm leave' })).toBeNull()
  expect(f.calls.some(c => c.path.endsWith('/leave'))).toBe(false)
})

test('leaving requires confirmation and retries through the account after space access ends', async () => {
  const f = fake(); f.state.canLeave = true; f.state.role = 'member'; let first = true
  f.state.intercept = c => c.path.endsWith('/leave') && first ? (first = false, json({ message: 'Response lost' }, 503)) : undefined
  open('/s/' + space + '/leave')
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm leave' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Retry leaving' }))
  await screen.findByRole('heading', { name: 'My Posterns' })
  const leaves = f.calls.filter(c => c.path.endsWith('/leave'))
  expect(leaves).toHaveLength(2); expect(leaves[0].body).toEqual(leaves[1].body)
  expect(leaves[0].body.membership_id).toBe(self)
  expect(leaves.every(c => c.authorization === 'Bearer pa_a' && c.path.startsWith('/accounts/'))).toBe(true)
  expect(accountToken()).toBe('pa_a')
})

test('leaving cannot navigate or mutate a new account after switching', async () => {
  const f = fake(), held = deferred<Response>(); f.state.canLeave = true
  f.state.intercept = c => c.path.endsWith('/leave') ? held.promise : undefined
  open('/s/' + space + '/leave'); fireEvent.click(await screen.findByRole('button', { name: 'Confirm leave' }))
  act(() => setAccountToken('pa_b'))
  await act(async () => held.resolve(json({ left: true })))
  expect(location.pathname).toBe('/s/' + space + '/leave')
  expect(accountToken()).toBe('pa_b')
  expect(f.calls.filter(c => c.path.endsWith('/leave'))).toHaveLength(1)
})

test('definitive leave denial removes the private confirmation details', async () => {
  const f = fake(); f.state.canLeave = true
  f.state.intercept = c => c.path.endsWith('/leave') ? json({ message: 'Membership unavailable' }, 404) : undefined
  open('/s/' + space + '/leave'); fireEvent.click(await screen.findByRole('button', { name: 'Confirm leave' }))
  await screen.findByText('Your membership has changed. Return to My Posterns to check your access.')
  expect(screen.queryByRole('heading', { name: 'Private workshop' })).toBeNull()
  expect(accountToken()).toBe('pa_a')
})

function incoming(f: ReturnType<typeof fake>) {
  f.state.role = 'member'
  f.state.transfer = { transfer_id: inv.invitation_id, former_role: 'admin', expires_at: 1800000000, is_recipient: true, can_cancel: false, proposer: { authority: 'fixture', account_id: 'old-owner' }, recipient: reference }
}

test('ownership proposal requires confirmation and retries the exact account-bound request', async () => {
  const f = fake(); let attempts = 0
  f.state.intercept = c => c.path.endsWith('/ownership/propose') && ++attempts === 1 ? json({ message: 'Response lost' }, 503) : undefined
  open(`/s/${space}/ownership`)
  fireEvent.change(await screen.findByLabelText('Recipient'), { target: { value: member } })
  fireEvent.change(screen.getByLabelText('Your role after acceptance'), { target: { value: 'member' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review transfer' }))
  await screen.findByRole('heading', { name: 'Propose transfer?' })
  expect(f.calls.some(c => c.path.endsWith('/ownership/propose'))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm proposal' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Retry request' }))
  await screen.findByRole('heading', { name: 'Waiting for recipient acceptance' })
  const requests = f.calls.filter(c => c.path.endsWith('/ownership/propose'))
  expect(requests).toHaveLength(2); expect(requests[0].body).toEqual(requests[1].body)
  expect(requests[0].body).toMatchObject({ recipient_membership_id: member, former_role: 'member', expected_revision: 7, expires_in_seconds: 604800 })
  expect(requests.every(c => c.authorization === 'Bearer pa_a')).toBe(true)
  expect(f.calls.some(c => c.path.endsWith('/accept'))).toBe(false)
})

test('ownership acceptance requires explicit consent and survives a lost response without a space session', async () => {
  const f = fake(); incoming(f); let attempts = 0
  f.state.intercept = c => c.path.endsWith('/accept') && ++attempts === 1 ? json({ message: 'Response lost' }, 503) : undefined
  open(`/s/${space}/ownership`)
  fireEvent.click(await screen.findByRole('button', { name: 'Review acceptance' }))
  const confirm = screen.getByRole('button', { name: 'Confirm acceptance' })
  expect(confirm).toHaveProperty('disabled', true)
  expect(f.calls.some(c => c.path.endsWith('/accept'))).toBe(false)
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(confirm); fireEvent.click(confirm)
  const retry = await screen.findByRole('button', { name: 'Retry request' })
  expect(f.calls.some(c => c.path.endsWith('/space-authorizations'))).toBe(false)
  fireEvent.click(retry)
  await screen.findByText('Ownership accepted. The owner and roles have been updated.')
  await screen.findByLabelText('Recipient')
  const requests = f.calls.filter(c => c.path.endsWith('/accept'))
  expect(requests).toHaveLength(2); expect(requests[0].body).toEqual(requests[1].body)
  expect(requests.every(c => c.authorization === 'Bearer pa_a')).toBe(true)
})

test.each(['decline', 'cancel'] as const)('ownership %s has its own confirmation and preserves membership', async action => {
  const f = fake(); incoming(f)
  if (action === 'cancel') { f.state.role = 'owner'; f.state.transfer!.is_recipient = false; f.state.transfer!.can_cancel = true }
  open(`/s/${space}/ownership`)
  const label = action === 'decline' ? 'Decline transfer' : 'Cancel transfer'
  fireEvent.click(await screen.findByRole('button', { name: label }))
  expect(f.calls.some(c => c.path.endsWith('/' + action))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: action === 'decline' ? 'Confirm decline' : 'Confirm cancellation' }))
  await screen.findByText(action === 'decline' ? 'Transfer declined.' : 'Transfer cancelled.')
  expect(f.calls.some(c => c.path.endsWith('/leave'))).toBe(false)
})

test('recipient refusal clears the private proposal and a refresh cannot auto-accept', async () => {
  const f = fake(); incoming(f)
  f.state.intercept = c => c.path.endsWith('/accept') ? json({ error: 'transfer_unavailable', message: 'This ownership transfer is unavailable.' }, 404) : undefined
  open(`/s/${space}/ownership`)
  fireEvent.click(await screen.findByRole('button', { name: 'Review acceptance' }))
  fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(screen.getByRole('button', { name: 'Confirm acceptance' }))
  await screen.findByText('This ownership transfer is unavailable.')
  expect(screen.queryByText('old-owner')).toBeNull()
  expect(screen.queryByText(/Responsibility for Private workshop/)).toBeNull()
  f.state.transfer = null
  fireEvent.click(screen.getByRole('button', { name: 'Reload ownership' }))
  await screen.findByText(/No ownership transfer is waiting for you/)
  expect(f.calls.filter(c => c.path.endsWith('/accept'))).toHaveLength(1)
})

test('late ownership acceptance cannot cross an account switch', async () => {
  const f = fake(), held = deferred<Response>(); incoming(f)
  f.state.intercept = c => c.path.endsWith('/accept') ? held.promise : undefined
  open(`/s/${space}/ownership`)
  fireEvent.click(await screen.findByRole('button', { name: 'Review acceptance' }))
  fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(screen.getByRole('button', { name: 'Confirm acceptance' }))
  await waitFor(() => expect(f.calls.some(c => c.path.endsWith('/accept'))).toBe(true))
  f.state.transfer = null; act(() => setAccountToken('pa_b'))
  await screen.findByText(/No ownership transfer is waiting for you/)
  expect(f.calls.some(c => c.path.endsWith('/ownership') && c.authorization === 'Bearer pa_b')).toBe(true)
  await act(async () => held.resolve(json({ transfer_id: inv.invitation_id, state: 'accepted' })))
  expect(screen.queryByText(/Ownership accepted/)).toBeNull()
  expect(accountToken()).toBe('pa_b')
})

test('an existing owner receiving a transfer refreshes a space session invalidated by acceptance', async () => {
  const f = fake(); open(`/s/${space}/ownership`)
  await screen.findByLabelText('Recipient')
  expect(f.calls.filter(c => c.path.endsWith('/session/exchange'))).toHaveLength(1)
  incoming(f); f.state.role = 'owner'; f.state.transfer!.can_cancel = true
  act(() => window.dispatchEvent(new Event('focus')))
  fireEvent.click(await screen.findByRole('button', { name: 'Review acceptance' }))
  fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(screen.getByRole('button', { name: 'Confirm acceptance' }))
  await screen.findByLabelText('Recipient')
  expect(f.calls.filter(c => c.path.endsWith('/session/exchange'))).toHaveLength(2)
  expect(f.calls.some(c => c.path.endsWith('/session/signout'))).toBe(true)
})
