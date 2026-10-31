import { useEffect, useRef, useState } from 'react'
import { Link, navigate } from './routes'
import { Button, Field, Message, Panel, buttonClass, input } from './ui'
import { useClient, Heading, CopyCode, muted, roleLabel } from './spaces-ui'
import { readReference, referenceCode, spaceMessage, SpaceError, type Member, type MemberList, type SpaceContext } from './spaces-api'

type Change = { member: Member; role: 'member' | 'admin' | null; operation: string; revision: number; attempted: boolean }
const accountLabel = (member: Member) => member.is_self ? 'Your membership' : `Account ${member.account_id}`
const failedAccess = (error: unknown) => error instanceof SpaceError && [401, 403, 404].includes(error.status)

export function MembersPage({ credential, space }: { credential: string; space: string }) {
  const client = useClient(credential)
  const [data, setData] = useState<MemberList | null>(null)
  const [context, setContext] = useState<SpaceContext | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [selection, setSelection] = useState<Change | null>(null)
  const [address, setAddress] = useState('')
  const [filter, setFilter] = useState<{ authority: string; account_id: string } | null>(null)
  const sequence = useRef(0)
  const flight = useRef(false)
  const uncertain = useRef(false)

  function clearPrivate() { setData(null); setContext(null); setSelection(null); setAddress(''); setFilter(null) }
  async function load(cursor?: string | null) {
    if (flight.current || uncertain.current) return
    const check = client.guard(), number = ++sequence.current
    setBusy(true); setError(''); setSelection(null); if (!cursor) { setData(null); setContext(null) }
    try {
      const [ctx, result] = await Promise.all([client.resource<SpaceContext>(space, '/session'), client.resource<MemberList>(space, '/memberships' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''))])
      if (!check() || sequence.current !== number) return
      // The two reads may straddle a role change. Never mount controls from a
      // stale context next to a newer list. Each mutation also checks revision.
      if (ctx.role !== result.role || ctx.membership_id !== result.membership_id || ctx.policy_revision !== result.policy_revision) throw new Error('Memberships changed. Reload the member list.')
      setContext(ctx)
      setData(previous => cursor && previous ? { ...result, members: [...previous.members, ...result.members] } : result)
    } catch (failure) {
      if (check() && sequence.current === number) { clearPrivate(); setError(spaceMessage(failure)) }
    } finally { if (check() && sequence.current === number) setBusy(false) }
  }
  useEffect(() => {
    void load(); const refresh = () => { if (!flight.current && !uncertain.current) void load() }
    window.addEventListener('focus', refresh)
    return () => { sequence.current++; window.removeEventListener('focus', refresh) }
  }, [client, space])
  function choose(member: Member, role: Change['role']) {
    setError(''); setNotice(''); setSelection({ member, role, operation: crypto.randomUUID(), revision: data!.policy_revision, attempted: false })
  }
  async function confirm() {
    if (!selection || flight.current) return
    const check = client.guard(), selected = selection
    flight.current = true; uncertain.current = true; sequence.current++; setBusy(true); setError('')
    setSelection({ ...selected, attempted: true })
    const body = { operation_id: selected.operation, expected_revision: selected.revision, ...(selected.role ? { role: selected.role } : {}) }
    try {
      await client.resource(space, `/memberships/${selected.member.membership_id}/${selected.role ? 'role' : 'remove'}`, 'POST', body)
      if (!check()) return
      uncertain.current = false; setSelection(null); setNotice(selected.role ? 'Role updated.' : 'Member removed from this Postern.')
      flight.current = false; await load()
    } catch (failure) {
      if (!check()) return
      if (failedAccess(failure)) { uncertain.current = false; clearPrivate() }
      else if (failure instanceof SpaceError && [409, 422].includes(failure.status)) {
        uncertain.current = false; setSelection(null); setData(null); setContext(null)
      }
      setError(spaceMessage(failure))
    } finally { flight.current = false; if (check()) setBusy(false) }
  }
  function abandon() { uncertain.current = false; setSelection(null); setNotice(''); void load() }
  function find() {
    setError('')
    try { setFilter(address.trim() ? readReference(address) : null) }
    catch (failure) { setError(failure instanceof Error ? failure.message : 'Check the invitation address.') }
  }
  const rows = data?.members.filter(member => !filter || member.authority === filter.authority && member.account_id === filter.account_id) ?? []
  return <>
    <Link to={`/s/${space}`} className="text-sm underline">Back to Postern</Link>
    <Heading title="Manage members" description={context ? `People with access to ${context.title}.` : 'Check current membership and permissions.'} />
    <Message text={error} />{notice ? <p role="status">{notice}</p> : null}
    {!data && !busy ? <Button onClick={() => void load()}>Reload members</Button> : null}
    {busy && !selection ? <p role="status">Loading members…</p> : null}
    {selection ? <Panel className="space-y-4">
      <h2 className="text-xl font-semibold">{selection.role ? `Change role to ${roleLabel(selection.role)}?` : 'Remove from this Postern?'}</h2>
      <p className="break-all font-medium">{accountLabel(selection.member)}</p>
      <p className={muted}>{selection.role === 'admin' ? 'Administrators can access all resources in this Postern and manage ordinary members.' : selection.role === 'member' ? 'This person will lose administrator access. Their explicit resource grants remain in place.' : 'This person will lose access to this Postern. Their account and memberships in other Posterns will remain. Rejoining requires a new invitation.'}</p>
      <p className={`text-sm ${muted}`}>Current role: {roleLabel(selection.member.role)}. Pending invitations issued by this membership will be revoked.</p>
      {selection.attempted && error ? <p className="text-sm">The response may have been lost. Retry this same request, or reload members to check the outcome.</p> : null}
      <div className="flex flex-wrap gap-3"><Button onClick={() => void confirm()} disabled={busy}>{busy ? 'Working…' : selection.attempted ? 'Retry change' : selection.role ? 'Confirm role change' : 'Confirm removal'}</Button><Button variant="secondary" disabled={busy} onClick={() => selection.attempted ? abandon() : setSelection(null)}>{selection.attempted ? 'Reload members' : 'Cancel'}</Button></div>
    </Panel> : null}
    {data && !selection ? <>
      <Panel className="space-y-4"><h2 className="text-lg font-semibold">Find a member</h2><form className="space-y-3" onSubmit={e => { e.preventDefault(); find() }}><Field label="Invitation address" hint="Paste the address this person shared with you. Account identifiers stay the same when a profile changes."><textarea rows={2} className={`${input} font-mono text-sm`} value={address} maxLength={2048} onChange={e => setAddress(e.target.value)} /></Field><div className="flex gap-3"><Button type="submit" variant="secondary" disabled={busy}>Find member</Button>{filter ? <Button type="button" variant="quiet" onClick={() => { setAddress(''); setFilter(null) }}>Show everyone</Button> : null}</div></form></Panel>
      <div className="space-y-4">{rows.map(member => <Panel key={member.membership_id} className="space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-3"><h2 className="min-w-0 break-all text-lg font-semibold">{accountLabel(member)}</h2><span className="rounded-full bg-neutral-100 px-3 py-1 text-sm dark:bg-neutral-800">{roleLabel(member.role)}</span></div>
        <details className="text-sm"><summary className="cursor-pointer py-2">Account address</summary><p className="mb-3 break-all font-mono text-xs">{referenceCode({ authority: member.authority, account_id: member.account_id })}</p><CopyCode code={referenceCode({ authority: member.authority, account_id: member.account_id })} label="Copy invitation address" /></details>
        <div className="flex flex-wrap gap-3">{member.can_change_role ? <Button variant="secondary" disabled={busy} onClick={() => choose(member, member.role === 'admin' ? 'member' : 'admin')}>{member.role === 'admin' ? 'Make member' : 'Make administrator'}</Button> : null}{member.can_remove ? <Button variant="quiet" disabled={busy} onClick={() => choose(member, null)}>Remove from Postern</Button> : null}{member.is_self ? <Link to={`/s/${space}/leave`} className={buttonClass('quiet')}>Leave Postern</Link> : null}</div>
      </Panel>)}</div>
      {!rows.length ? <Panel><p>{filter ? data.next_cursor ? 'No match in the loaded members. Load more to continue searching.' : 'No active membership matches this address.' : 'No active members were returned.'}</p></Panel> : null}
      <div className="flex flex-wrap gap-3">{data.next_cursor ? <Button variant="secondary" disabled={busy} onClick={() => void load(data.next_cursor)}>Load more members</Button> : null}<Button variant="quiet" disabled={busy} onClick={() => void load()}>Refresh members</Button></div>
    </> : null}
  </>
}

export function LeaveSpace({ credential, space }: { credential: string; space: string }) {
  const client = useClient(credential)
  const [context, setContext] = useState<SpaceContext | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [attempted, setAttempted] = useState(false)
  const [reload, setReload] = useState(0)
  const operation = useRef<{ id: string; member: string } | null>(null)
  const flight = useRef(false)
  useEffect(() => {
    const check = client.guard(); setContext(null); setError('')
    client.resource<SpaceContext>(space, '/session').then(value => { if (check()) setContext(value) }).catch(failure => { if (check()) setError(spaceMessage(failure)) })
  }, [client, space, reload])
  async function leave() {
    if (flight.current || !context?.can_leave) return
    operation.current ??= { id: crypto.randomUUID(), member: context.membership_id }
    const attempt = operation.current, check = client.guard()
    flight.current = true; setBusy(true); setAttempted(true); setError('')
    try { await client.leave(space, attempt.member, attempt.id); if (check()) navigate('/home') }
    catch (failure) { if (check()) { if (failedAccess(failure)) setContext(null); setError(failure instanceof Error ? failure.message : 'The request could not be completed.') } }
    finally { flight.current = false; if (check()) setBusy(false) }
  }
  return <><Link to="/home" className="text-sm underline">My Posterns</Link><Heading title="Leave this Postern" description="Your account and other Posterns will remain available." /><Message text={error} />
    {!context ? error ? <Button onClick={() => setReload(v => v + 1)}>Check access again</Button> : <p role="status">Checking your membership…</p> : <Panel className="space-y-5">
      <h2 className="break-words text-xl font-semibold">{context.title}</h2>
      {context.can_leave ? <><p className={muted}>You will lose access to this Postern and its shared resources. Your previous activity stays with the space. To rejoin, you will need a new invitation.</p><p className={`text-sm ${muted}`}>Any pending invitations you issued here will be revoked.</p>{attempted && error ? <p className="text-sm">Retry this same request safely, or check My Posterns to see whether you have already left.</p> : null}<div className="flex flex-wrap gap-3"><Button disabled={busy} onClick={() => void leave()}>{busy ? 'Leaving…' : attempted ? 'Retry leaving' : 'Confirm leave'} </Button><Link to={`/s/${space}`} className={buttonClass('secondary')}>Stay in Postern</Link></div></> : <><p>You are the last owner. Another active owner is required before you can leave.</p><p className={`text-sm ${muted}`}>Transfer ownership to an existing member. They must accept before you can leave.</p><Link to={`/s/${space}/ownership`} className={buttonClass('secondary')}>Transfer ownership</Link><Link to={`/s/${space}`} className={buttonClass('secondary')}>Back to Postern</Link></>}
    </Panel>}
  </>
}
