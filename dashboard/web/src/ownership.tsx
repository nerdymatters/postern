import { useEffect, useRef, useState } from 'react'
import { Link } from './routes'
import { Button, Field, Message, Panel, input } from './ui'
import { useClient, Heading, muted, roleLabel, expiry } from './spaces-ui'
import { SpaceError, spaceMessage, type Member, type MemberList, type OwnershipProposal, type OwnershipStatus, type TransferResult } from './spaces-api'

type Attempt = { path: string; body: OwnershipProposal | { operation_id: string }; label: string; confirm: string; attempted: boolean }
export function OwnershipPage({ credential, space }: { credential: string; space: string }) {
  const client = useClient(credential)
  const [data, setData] = useState<OwnershipStatus | null>(null)
  const [members, setMembers] = useState<MemberList | null>(null)
  const [recipient, setRecipient] = useState('')
  const [formerRole, setFormerRole] = useState<'admin' | 'member'>('admin')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [attempt, setAttempt] = useState<Attempt | null>(null)
  const [consent, setConsent] = useState(false)
  const flight = useRef(false), uncertain = useRef(false), sequence = useRef(0)
  function clear() { setData(null); setMembers(null); setRecipient(''); setAttempt(null); setConsent(false) }
  async function load(cursor?: string | null) {
    if (flight.current || uncertain.current) return
    const check = client.guard(), seq = ++sequence.current
    setBusy(true); setError(''); setAttempt(null); setConsent(false)
    if (!cursor) { setData(null); setMembers(null); setRecipient('') }
    try {
      const status = await client.ownership(space)
      let list: MemberList | null = null
      if (status.role === 'owner' && !status.transfer) {
        list = await client.resource<MemberList>(space, '/memberships' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''))
        if (list.membership_id !== status.membership_id || list.policy_revision !== status.policy_revision || list.role !== status.role) throw new Error('Memberships changed. Reload ownership.')
      }
      if (!check() || seq !== sequence.current) return
      setData(status)
      setMembers(previous => list && cursor && previous ? { ...list, members: [...previous.members, ...list.members] } : list)
    } catch (failure) { if (check() && seq === sequence.current) { clear(); setError(spaceMessage(failure)) } }
    finally { if (check() && seq === sequence.current) setBusy(false) }
  }
  useEffect(() => {
    void load(); const refresh = () => { if (!flight.current && !uncertain.current) void load() }
    window.addEventListener('focus', refresh)
    return () => { sequence.current++; window.removeEventListener('focus', refresh) }
  }, [client, space])
  function propose() {
    if (!data || !members?.members.some(m => m.membership_id === recipient && !m.is_self)) return
    setError(''); setNotice(''); setAttempt({ path: '/propose', label: 'Propose transfer', confirm: 'Confirm proposal', attempted: false,
      body: { operation_id: crypto.randomUUID(), recipient_membership_id: recipient, former_role: formerRole, expires_in_seconds: 604800, expected_revision: data.policy_revision } })
  }
  function choose(action: 'accept' | 'decline' | 'cancel') {
    if (!data?.transfer) return
    setError(''); setNotice(''); setConsent(false)
    setAttempt({ path: `/${data.transfer.transfer_id}/${action}`, label: action === 'accept' ? 'Accept ownership' : action === 'decline' ? 'Decline transfer' : 'Cancel transfer', confirm: action === 'accept' ? 'Confirm acceptance' : action === 'decline' ? 'Confirm decline' : 'Confirm cancellation', body: { operation_id: crypto.randomUUID() }, attempted: false })
  }
  async function confirm() {
    if (!attempt || flight.current || (attempt.path.endsWith('/accept') && !consent)) return
    const selected = attempt, check = client.guard()
    flight.current = true; uncertain.current = true; sequence.current++; setBusy(true); setError(''); setAttempt({ ...selected, attempted: true })
    try {
      const result = await client.ownership<TransferResult>(space, selected.path, selected.body)
      if (!check()) return
      if (result.state === 'accepted') client.forgetResourceSession(space)
      uncertain.current = false; setAttempt(null)
      setNotice(result.state === 'accepted' ? 'Ownership accepted. The owner and roles have been updated.' : result.state === 'pending' ? 'Transfer proposed. The recipient must open Ownership transfer in this Postern and accept within seven days.' : `Transfer ${result.state}.`)
      flight.current = false; await load()
    } catch (failure) {
      if (!check()) return
      if (failure instanceof SpaceError && [401, 403, 404, 409, 422, 428].includes(failure.status)) { uncertain.current = false; clear() }
      setError(failure instanceof SpaceError && failure.code === 'transfer_unavailable' ? failure.message : spaceMessage(failure))
    } finally { flight.current = false; if (check()) setBusy(false) }
  }
  function reload() { uncertain.current = false; setAttempt(null); void load() }
  const selected: Member | undefined = members?.members.find(m => m.membership_id === recipient)
  const transfer = data?.transfer
  return <>
    <Link to={`/s/${space}`} className="text-sm underline">Back to Postern</Link>
    <Heading title="Ownership transfer" description={data ? `Responsibility for ${data.title}.` : 'Review and confirm a change of owner.'} />
    <Message text={error} />{notice ? <p role="status">{notice}</p> : null}
    {busy && !attempt ? <p role="status">Checking ownership…</p> : null}
    {!data && !busy ? <Button onClick={reload}>Reload ownership</Button> : null}
    {transfer ? <Panel className="space-y-4">
      <h2 className="text-xl font-semibold">{transfer.is_recipient ? 'You have been offered ownership' : 'Waiting for recipient acceptance'}</h2>
      <p className="break-all"><strong>From account:</strong> {transfer.proposer.account_id}<span className={`mt-1 block text-sm ${muted}`}>{transfer.proposer.authority}</span></p>
      <p className="break-all"><strong>To account:</strong> {transfer.recipient.account_id}<span className={`mt-1 block text-sm ${muted}`}>{transfer.recipient.authority}</span></p>
      <p className={muted}>On acceptance, the recipient becomes the responsible owner of this Postern and gains the owner role. The proposing owner’s role becomes {roleLabel(transfer.former_role)}. Other memberships and Posterns stay as they are.</p>
      <p className={`text-sm ${muted}`}>Expires {expiry(transfer.expires_at)}. No ownership changes until acceptance.</p>
      {!attempt ? <div className="flex flex-wrap gap-3">{transfer.is_recipient ? <><Button onClick={() => choose('accept')} disabled={busy}>Review acceptance</Button><Button variant="quiet" onClick={() => choose('decline')} disabled={busy}>Decline transfer</Button></> : null}{transfer.can_cancel ? <Button variant="secondary" onClick={() => choose('cancel')} disabled={busy}>Cancel transfer</Button> : null}</div> : null}
    </Panel> : null}
    {data && !transfer && data.role !== 'owner' ? <Panel><p>No ownership transfer is waiting for you. A current owner can propose a transfer to an existing member.</p></Panel> : null}
    {data && !transfer && members && !attempt ? <Panel className="space-y-5">
      <h2 className="text-xl font-semibold">Choose the next owner</h2>
      <p className={muted}>Choose an existing member. They will have seven days to accept responsibility for this Postern.</p>
      <form className="space-y-4" onSubmit={event => { event.preventDefault(); propose() }}>
        <Field label="Recipient" hint="Use the immutable account ID from Manage members to identify the person."><select className={`${input} max-w-full`} value={recipient} onChange={e => setRecipient(e.target.value)} required disabled={busy}><option value="">Choose a member</option>{members.members.filter(m => !m.is_self).map(m => <option key={m.membership_id} value={m.membership_id}>Account {m.account_id} · {roleLabel(m.role)}</option>)}</select></Field>
        {selected ? <p className={`break-all text-sm ${muted}`}>Account authority: {selected.authority}</p> : null}
        <Field label="Your role after acceptance"><select className={input} value={formerRole} onChange={e => setFormerRole(e.target.value as 'admin' | 'member')} disabled={busy}><option value="admin">Administrator</option><option value="member">Member</option></select></Field>
        <p className={`text-sm ${muted}`}>{formerRole === 'admin' ? 'You will retain administrator access, but cannot change ownership or administrators.' : 'You will lose administration and see only resources shared with your membership.'}</p>
        <Button type="submit" disabled={busy || !recipient}>Review transfer</Button>
      </form>
      {members.next_cursor ? <Button variant="secondary" disabled={busy} onClick={() => void load(members.next_cursor)}>Load more members</Button> : null}
      <Link to={`/s/${space}/members`} className="block text-sm underline">Manage members</Link>
    </Panel> : null}
    {attempt ? <Panel className="space-y-4">
      <h2 className="text-xl font-semibold">{attempt.label}?</h2>
      {attempt.path === '/propose' ? <><p className="break-all">Recipient: Account {selected?.account_id}</p><p className={muted}>Your role will become {roleLabel(formerRole)} when this person accepts. This changes only this Postern. The proposal expires in seven days.</p></> : attempt.path.endsWith('/accept') ? <label className="flex items-start gap-3"><input type="checkbox" className="mt-1" checked={consent} disabled={busy || attempt.attempted} onChange={e => setConsent(e.target.checked)} /><span>I accept responsibility for this Postern and the role changes shown above.</span></label> : <p className={muted}>This ends the proposal without changing ownership or roles.</p>}
      {attempt.path === '/propose' || attempt.path.endsWith('/accept') ? <p className={`text-sm ${muted}`}>Acceptance ends both participants’ current space sessions and revokes pending invitations they issued. Their accounts, resource grants and history remain.</p> : null}
      {attempt.attempted && error ? <p>The response may have been lost. Retry this same request, or reload ownership to check the outcome.</p> : null}
      <div className="flex flex-wrap gap-3"><Button disabled={busy || (attempt.path.endsWith('/accept') && !consent)} onClick={() => void confirm()}>{busy ? 'Working…' : attempt.attempted ? 'Retry request' : attempt.confirm}</Button><Button variant="secondary" disabled={busy} onClick={() => attempt.attempted ? reload() : setAttempt(null)}>{attempt.attempted ? 'Reload ownership' : 'Go back'}</Button></div>
    </Panel> : null}
    {data && !attempt ? <Button variant="quiet" disabled={busy} onClick={reload}>Refresh ownership</Button> : null}
  </>
}
