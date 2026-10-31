import { useEffect, useRef, useState } from 'react'
import type { Account } from './accounts-api'
import { Link, navigate } from './routes'
import { OwnershipPage } from './ownership'
import { MembersPage, LeaveSpace } from './memberships'
import { useClient, Heading, CopyCode, muted, roleLabel, expiry } from './spaces-ui'
import { Button, Field, Message, Panel, buttonClass, input } from './ui'
import { invitationCode, nativeSpaceId, readInvitation, readReference, referenceCode, spaceMessage, SpacesClient, type Catalog, type Invitation, type MySpaces, type PendingInvite, type Preview, type SpaceRole } from './spaces-api'

export function SpaceOnboarding({ account, credential, path }: { account: Account; credential: string; path: string }) {
  if (path === '/spaces/new') return <CreateSpace credential={credential} />
  if (path === '/invitations/join') return <JoinSpace account={account} credential={credential} />
  const space = nativeSpaceId(path)
  if (space && /\/members\/?$/.test(path)) return <MembersPage credential={credential} space={space} />
  if (space && /\/ownership\/?$/.test(path)) return <OwnershipPage credential={credential} space={space} />
  if (space && /\/leave\/?$/.test(path)) return <LeaveSpace credential={credential} space={space} />
  if (space) return <SpacePage credential={credential} space={space} />
  return <MyPosterns account={account} credential={credential} />
}

function MyPosterns({ account, credential }: { account: Account; credential: string }) {
  const client = useClient(credential)
  const [data, setData] = useState<MySpaces | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const sequence = useRef(0)
  async function load(after?: string | null) {
    const check = client.guard(), number = ++sequence.current
    setBusy(true); setError(''); if (!after) setData(null)
    try {
      const value = await client.mySpaces(after)
      if (!check() || sequence.current !== number) return
      setData(previous => after && previous ? { ...value, spaces: [...previous.spaces.filter(p => !value.spaces.some(n => n.space_id === p.space_id)), ...value.spaces] } : value)
    } catch (failure) { if (check() && sequence.current === number) { setData(null); setError(spaceMessage(failure)) } }
    finally { if (check() && sequence.current === number) setBusy(false) }
  }
  useEffect(() => { void load(); const refresh = () => { void load() }; window.addEventListener('focus', refresh); return () => { sequence.current++; window.removeEventListener('focus', refresh) } }, [client])
  return <>
    <Heading title="My Posterns" description="Your private spaces, and the people who share them with you." />
    <div className="flex flex-wrap items-center justify-between gap-3"><p className={muted}>Signed in as <strong className="font-medium text-neutral-900 dark:text-neutral-100">{account.profile.display_name}</strong></p><Link to="/invitations/join" className={buttonClass('secondary')}>Have an invitation?</Link></div>
    <Message text={error} />
    {error ? <Button onClick={() => void load()} disabled={busy}>Try again</Button> : null}
    {busy ? <p role="status">Loading your Posterns…</p> : null}
    {data ? <>
      {data.spaces.length ? <div className="grid gap-4 sm:grid-cols-2">{data.spaces.map(space => <Panel key={space.space_id} className="flex flex-col items-start gap-4"><span className="rounded-full bg-neutral-100 px-3 py-1 text-xs font-medium dark:bg-neutral-800">{roleLabel(space.role)}</span><h2 className="min-w-0 break-words text-xl font-semibold">{space.title}</h2><Link to={`/s/${space.space_id}`} className={buttonClass('secondary', 'mt-auto')}>Open Postern<span className="sr-only">: {space.title}</span></Link></Panel>)}</div> : <Panel className="space-y-3"><h2 className="text-xl font-semibold">No Posterns yet</h2><p className={muted}>Your account is ready. Use an invitation from a Postern owner to join a private space.</p></Panel>}
      {data.next_after ? <Button variant="secondary" onClick={() => void load(data.next_after)} disabled={busy}>Load more Posterns</Button> : null}
      <Panel className="space-y-4"><div className="flex flex-wrap items-start justify-between gap-4"><div className="space-y-1"><h2 className="text-lg font-semibold">A space of your own</h2><p className={`text-sm ${muted}`}>{data.creation.allowed ? `${data.creation.used} of ${data.creation.limit} approved spaces created.` : data.creation.limit ? 'You have used your approved space creations.' : 'Space creation is available to accounts approved by the operator.'}</p></div>{data.creation.allowed ? <Link to="/spaces/new" className={buttonClass()}>Create a Postern</Link> : null}</div></Panel>
      <Panel className="space-y-3"><h2 className="text-lg font-semibold">Your invitation address</h2><p className={`text-sm ${muted}`}>Share this address with an owner so they can invite this account. It stays the same when you edit your profile.</p><CopyCode code={referenceCode(data.account_reference)} label="Copy my invitation address" /></Panel>
    </> : null}
  </>
}

function CreateSpace({ credential }: { credential: string }) {
  const client = useClient(credential)
  const [eligibility, setEligibility] = useState<MySpaces['creation'] | null>(null)
  const [title, setTitle] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)
  const [fixed, setFixed] = useState(false)
  const operation = useRef<{ id: string; title: string } | null>(null)
  const inFlight = useRef(false)
  useEffect(() => {
    const check = client.guard(); setEligibility(null); setError('')
    client.mySpaces().then(value => { if (check()) setEligibility(value.creation) }).catch(failure => { if (check()) setError(spaceMessage(failure)) })
  }, [client, reload])
  async function submit() {
    if (inFlight.current || !eligibility?.allowed) return
    const normalized = title.normalize('NFC').trim()
    if (!normalized || [...normalized].length > 200 || /[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(normalized)) { setError('Use a name with 1 to 200 characters, without control characters.'); return }
    operation.current ??= { id: crypto.randomUUID(), title: normalized }
    const attempt = operation.current, check = client.guard()
    inFlight.current = true; setBusy(true); setFixed(true); setError('')
    try { const result = await client.create(attempt.id, attempt.title); if (check()) navigate(`/s/${result.space_id}`) }
    catch (failure) { if (check()) setError(spaceMessage(failure)) }
    finally { inFlight.current = false; if (check()) setBusy(false) }
  }
  return <><Link to="/home" className="text-sm underline">Back to My Posterns</Link><Heading title="Create a Postern" description="Start a private space. You’ll be its owner and choose who can join." /><Message text={error} />
    {!eligibility ? error ? <Button onClick={() => setReload(v => v + 1)}>Check again</Button> : <p role="status">Checking creation availability…</p> : !eligibility.allowed ? <Panel><p>{eligibility.limit ? 'You have used your approved space creations.' : 'The operator needs to approve space creation for this account.'}</p></Panel> :
      <Panel><form onSubmit={event => { event.preventDefault(); void submit() }} className="space-y-5"><Field label="Postern name" hint="Choose a name the people you invite will recognize."><input className={input} value={title} onChange={e => setTitle(e.target.value)} disabled={fixed} autoFocus required /></Field><p className={`text-sm ${muted}`}>{eligibility.used} of {eligibility.limit} approved spaces created. Your new Postern starts private.</p><Button type="submit" disabled={busy}>{busy ? 'Creating…' : fixed ? 'Try again' : 'Create Postern'}</Button>{fixed && error ? <p className={`text-sm ${muted}`}>Retry to finish this request safely, or return to My Posterns to check whether it was created.</p> : null}</form></Panel>}
  </>
}

function JoinSpace({ account, credential }: { account: Account; credential: string }) {
  const client = useClient(credential)
  const [code, setCode] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [invitation, setInvitation] = useState<Invitation | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  async function review() {
    if (inFlight.current) return
    const check = client.guard(); setError(''); setPreview(null)
    try {
      const value = readInvitation(code); inFlight.current = true; setBusy(true)
      const result = await client.preview(value)
      if (check()) { setInvitation(value); setPreview(result); setCode('') }
    } catch (failure) { if (check()) setError(spaceMessage(failure)) }
    finally { inFlight.current = false; if (check()) setBusy(false) }
  }
  async function join() {
    if (!invitation || !preview || inFlight.current) return
    const check = client.guard(); inFlight.current = true; setBusy(true); setError('')
    try { const result = await client.accept(invitation); if (check()) navigate(`/s/${result.space_id}`) }
    catch (failure) {
      if (check()) { setError(spaceMessage(failure)); if (failure && typeof failure === 'object' && 'status' in failure && [401, 403, 404, 409].includes(Number(failure.status))) { setPreview(null); setInvitation(null) } }
    } finally { inFlight.current = false; if (check()) setBusy(false) }
  }
  return <><Link to="/home" className="text-sm underline">Back to My Posterns</Link><Heading title="Join a Postern" description="Review a private invitation before you decide to join." /><Message text={error} />
    {!preview ? <Panel><form className="space-y-5" onSubmit={event => { event.preventDefault(); void review() }}><Field label="Invitation code" hint="Paste the complete code the owner or administrator shared with you."><textarea className={`${input} font-mono text-sm`} rows={4} value={code} onChange={e => setCode(e.target.value)} autoComplete="off" spellCheck={false} maxLength={2048} required disabled={busy} /></Field><Button type="submit" disabled={busy}>{busy ? 'Checking invitation…' : 'Review invitation'}</Button></form></Panel> :
      <Panel className="space-y-5"><div className="space-y-2"><p className="text-sm text-neutral-500">{preview.already_member ? 'You already belong to' : 'You’re invited to'}</p><h2 className="break-words text-2xl font-semibold">{preview.title}</h2><span className="inline-block rounded-full bg-neutral-100 px-3 py-1 text-sm dark:bg-neutral-800">{roleLabel(preview.role)}</span></div><p className={muted}>{preview.role === 'admin' ? 'Administrators can manage ordinary members and access the resources in this Postern.' : preview.role === 'owner' ? 'You are an owner of this Postern.' : 'You can access the resources shared with you.'}</p><dl className="space-y-2 text-sm"><div><dt className="text-neutral-500">Joining as</dt><dd className="break-words font-medium">{account.profile.display_name} · {account.email}</dd></div>{!preview.already_member ? <div><dt className="text-neutral-500">Invitation expires</dt><dd>{expiry(preview.expires_at)}</dd></div> : null}</dl><div className="flex flex-wrap items-center gap-4">{preview.already_member ? <Link to={`/s/${preview.space_id}`} className={buttonClass()}>Open Postern</Link> : <Button onClick={() => void join()} disabled={busy}>{busy ? 'Joining…' : error ? 'Try joining again' : 'Join Postern'}</Button>}<Link to="/home" className="underline">Not now</Link></div></Panel>}
  </>
}

function SpacePage({ credential, space }: { credential: string; space: string }) {
  const client = useClient(credential)
  const [context, setContext] = useState<{ title: string; role: SpaceRole } | null>(null)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [query, setQuery] = useState('')
  const [applied, setApplied] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const sequence = useRef(0)
  async function load(search = '', cursor?: string | null) {
    const check = client.guard(), number = ++sequence.current
    setBusy(true); setError(''); if (!cursor) { setContext(null); setCatalog(null) }
    try {
      const [ctx, results] = await Promise.all([client.resource<{ title: string; role: SpaceRole }>(space, '/session'), client.resource<Catalog>(space, '/resources?' + new URLSearchParams({ query: search, ...(cursor ? { cursor } : {}) }))])
      if (!check() || number !== sequence.current) return
      setContext(ctx); setApplied(search); setCatalog(previous => cursor && previous ? { ...results, resources: [...previous.resources, ...results.resources] } : results)
    } catch (failure) { if (check() && number === sequence.current) { setContext(null); setCatalog(null); setError(spaceMessage(failure)) } }
    finally { if (check() && number === sequence.current) setBusy(false) }
  }
  useEffect(() => { void load(); const refresh = () => { void load() }; window.addEventListener('focus', refresh); return () => { sequence.current++; window.removeEventListener('focus', refresh) } }, [client, space])
  function refused(message: string) { sequence.current++; setContext(null); setCatalog(null); setError(message); setBusy(false) }
  return <><Link to="/home" className="text-sm underline">My Posterns</Link><Heading title={context?.title ?? 'Private Postern'} description={context ? `Your role: ${roleLabel(context.role)}` : 'Checking your current access to this space.'} /><Message text={error} />{error ? <Button onClick={() => void load()} disabled={busy}>Check access again</Button> : null}{busy ? <p role="status">Loading this Postern…</p> : null}
    {context && catalog ? <><div className="flex flex-wrap gap-3">{context.role === 'owner' || context.role === 'admin' ? <Link to={`/s/${space}/members`} className={buttonClass('secondary')}>Manage members</Link> : null}<Link to={`/s/${space}/ownership`} className={buttonClass('secondary')}>Ownership transfer</Link><Link to={`/s/${space}/leave`} className={buttonClass('quiet')}>Leave Postern</Link></div><Panel className="space-y-4"><h2 className="text-lg font-semibold">Resources</h2><form className="flex flex-wrap items-end gap-3" onSubmit={e => { e.preventDefault(); void load(query.trim()) }}><div className="min-w-0 flex-1"><Field label="Search resources"><input className={input} value={query} maxLength={200} onChange={e => setQuery(e.target.value)} /></Field></div><Button type="submit" variant="secondary" disabled={busy}>Search</Button></form>{catalog.resources.length ? <ul className="divide-y divide-neutral-200 dark:divide-neutral-800">{catalog.resources.map(resource => <li key={resource.id} className="py-3"><p className="break-words font-medium">{resource.title}</p><p className="text-sm text-neutral-500">{resource.kind} · {resource.state}</p></li>)}</ul> : <p className={muted}>{applied ? 'No shared resources match this search.' : 'No resources are shared here yet.'}</p>}<p className="text-sm text-neutral-500">Resource previews and playback are not available in this version.</p>{catalog.next_cursor ? <Button variant="secondary" disabled={busy} onClick={() => void load(applied, catalog.next_cursor)}>Load more resources</Button> : null}</Panel>
    {context.role === 'owner' || context.role === 'admin' ? <Invitations key={context.role} client={client} space={space} owner={context.role === 'owner'} onRefused={refused} /> : null}</> : null}
  </>
}

function Invitations({ client, space, owner, onRefused }: { client: SpacesClient; space: string; owner: boolean; onRefused: (message: string) => void }) {
  const [pending, setPending] = useState<PendingInvite[] | null>(null)
  const [address, setAddress] = useState('')
  const [role, setRole] = useState<'member' | 'admin'>('member')
  const [hours, setHours] = useState('24')
  const [created, setCreated] = useState<Invitation | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [fixed, setFixed] = useState(false)
  const mounted = useRef(0)
  const flight = useRef(false)
  const attempt = useRef<object | null>(null)
  const revocations = useRef(new Map<string, string>())
  function guard() { const number = mounted.current, check = client.guard(); return () => number === mounted.current && check() }
  function failed(failure: unknown) {
    const message = spaceMessage(failure)
    if (failure && typeof failure === 'object' && 'status' in failure && [401, 403, 404].includes(Number(failure.status))) onRefused(message)
    else setError(message)
  }
  async function refresh() {
    const check = guard(); setPending(null)
    try { const result = await client.resource<{ invitations: PendingInvite[] }>(space, '/invitations'); if (check()) setPending(result.invitations) }
    catch (failure) { if (check()) failed(failure) }
  }
  useEffect(() => { mounted.current++; void refresh(); return () => { mounted.current++ } }, [client, space])
  async function issue() {
    if (flight.current) return
    const check = guard(); setError('')
    try {
      attempt.current ??= { operation_id: crypto.randomUUID(), recipient: readReference(address), role, expires_in_seconds: Number(hours) * 3600 }
      flight.current = true; setBusy(true); setFixed(true)
      const result = await client.resource<Invitation>(space, '/invitations', 'POST', attempt.current)
      if (check()) { setCreated(result); await refresh() }
    } catch (failure) { if (check()) failed(failure) }
    finally { flight.current = false; if (check()) setBusy(false) }
  }
  async function revoke(id: string) {
    if (flight.current) return
    const check = guard(); flight.current = true; setBusy(true); setError('')
    const key = revocations.current.get(id) ?? crypto.randomUUID(); revocations.current.set(id, key)
    try {
      await client.resource(space, `/invitations/${id}/revoke`, 'POST', { operation_id: key })
      if (check()) { if (created?.invitation_id === id) setCreated(null); await refresh() }
    } catch (failure) { if (check()) failed(failure) }
    finally { flight.current = false; if (check()) setBusy(false) }
  }
  return <Panel className="space-y-5"><h2 className="text-lg font-semibold">Invite someone</h2><Message text={error} />
    {created ? <div className="space-y-3"><p role="status" className="font-medium">Invitation ready</p><p className={`text-sm ${muted}`}>Copy the code and share it privately with the person you invited. They can review it from My Posterns before joining. No message has been sent.</p><CopyCode code={invitationCode(created)} label="Copy invitation code" secret /><Button variant="quiet" disabled={busy} onClick={() => { setCreated(null); setAddress(''); setFixed(false); attempt.current = null; setError('') }}>Invite another person</Button></div> : <form className="space-y-4" onSubmit={e => { e.preventDefault(); void issue() }}><Field label="Recipient’s invitation address" hint="Ask them to copy their invitation address from My Posterns."><textarea className={`${input} font-mono text-sm`} rows={3} maxLength={2048} value={address} onChange={e => setAddress(e.target.value)} autoComplete="off" spellCheck={false} required disabled={fixed} /></Field><div className="grid gap-4 sm:grid-cols-2"><Field label="Role"><select className={input} value={role} onChange={e => setRole(e.target.value as 'member' | 'admin')} disabled={fixed}><option value="member">Member</option>{owner ? <option value="admin">Administrator</option> : null}</select></Field><Field label="Invitation expires in"><select className={input} value={hours} onChange={e => setHours(e.target.value)} disabled={fixed}><option value="1">1 hour</option><option value="24">1 day</option><option value="168">7 days</option></select></Field></div><Button type="submit" disabled={busy}>{busy ? 'Working…' : fixed ? 'Try invitation again' : 'Create invitation'}</Button></form>}
    <div className="border-t border-neutral-200 pt-5 dark:border-neutral-800"><h3 className="font-medium">Pending invitations</h3>{pending === null ? <p className={`mt-2 text-sm ${muted}`}>Invitations could not be loaded yet. <button className="underline" onClick={() => void refresh()} disabled={busy}>Refresh</button></p> : !pending.length ? <p className={`mt-2 text-sm ${muted}`}>No pending invitations.</p> : <ul className="mt-3 space-y-3">{pending.map(inv => <li key={inv.invitation_id} className="space-y-2 rounded-xl border border-neutral-200 p-3 dark:border-neutral-800"><div className="flex flex-wrap items-center justify-between gap-3"><div><p className="font-medium">{roleLabel(inv.role)} invitation</p><p className="text-sm text-neutral-500">Expires {expiry(inv.expires_at)}</p></div><Button variant="quiet" disabled={busy} onClick={() => void revoke(inv.invitation_id)}>Revoke<span className="sr-only"> invitation {inv.invitation_id}</span></Button></div><details className="text-sm"><summary className="cursor-pointer py-2">Recipient’s address</summary><p className="break-all font-mono text-xs">{referenceCode({ authority: inv.authority, account_id: inv.account_id })}</p></details></li>)}</ul>}</div>
  </Panel>
}
