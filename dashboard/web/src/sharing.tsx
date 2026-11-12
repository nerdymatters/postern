import { useEffect, useRef, useState } from 'react'
import { Link } from './routes'
import { Button, Field, Message, Panel, input } from './ui'
import { Heading, expiry, muted, roleLabel, useClient } from './spaces-ui'
import { SpaceError, spaceMessage, type Catalog, type MemberList, type SpaceContext, type SpaceRole } from './spaces-api'

type Page<T> = { items: T[]; next_cursor: string | null; policy_revision: number }
type Audience = { id: string; label: string; member_count: number }
type AudienceMember = { id: string; role: SpaceRole; authority: string; account_id: string }
type Grant = { id: string; membership_id: string | null; audience_id: string | null; audience_label: string | null; all_members: boolean; actions: string[]; expires_at: number | null; authority: string | null; account_id: string | null; stale_reason: string | null }
type Access = { role: SpaceRole; actions: string[]; sources: Grant[]; more_sources: boolean; policy_revision: number }
type Choice = { group: string; resource: string; member: string }
type Snapshot = { context: SpaceContext; audiences: Page<Audience>; members: MemberList; catalog: Catalog; cleanup: Page<{ id: string; reason: string }>; groupMembers: Page<AudienceMember> | null; grants: Page<Grant> | null; access: Access | null }
type Change = { path: string; title: string; description: string; body: Record<string, unknown>; attempted: boolean }
const recipient = (g: Grant) => g.all_members ? 'All members' : g.audience_id ? `Group: ${g.audience_label}` : `Account ${g.account_id} (${g.authority})`
const reasons: Record<string, string> = { expired: 'Expired', no_discover: 'No discover permission', unpublished: 'Resource unpublished or archived', membership_ended: 'Membership ended', audience_archived: 'Group archived' }

export function SharingPage({ credential, space }: { credential: string; space: string }) {
  const client = useClient(credential)
  const [data, setData] = useState<Snapshot | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [choice, setChoice] = useState<Choice>({ group: '', resource: '', member: '' })
  const [label, setLabel] = useState('')
  const [rename, setRename] = useState('')
  const [recipientType, setRecipientType] = useState('group')
  const [view, setView] = useState(true)
  const [until, setUntil] = useState('')
  const [change, setChange] = useState<Change | null>(null)
  const selected = useRef(choice)
  const displayed = useRef<Snapshot | null>(null)
  const flight = useRef(false)
  const pending = useRef<Change | null>(null)
  const sequence = useRef(0)
  const confirmation = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    if (change) { confirmation.current?.focus(); confirmation.current?.scrollIntoView?.({ block: 'center' }) }
  }, [change?.body.operation_id])

  function clear() { displayed.current = null; setData(null); setChoice({ group: '', resource: '', member: '' }); selected.current = { group: '', resource: '', member: '' }; setLabel(''); setRename(''); setUntil(''); setChange(null); pending.current = null }
  // keep: a selection change in this page leaves the current lists on screen,
  // disabled while busy, so the page and the focused control stay in place.
  async function load(next = selected.current, keep = false) {
    if (flight.current || pending.current?.attempted) return
    const check = client.guard(), number = ++sequence.current
    selected.current = next; setChoice(next); setBusy(true); if (!keep) setData(null); setError(''); setChange(null); pending.current = null
    try {
      const [context, audiences, members, catalog, cleanup, groupMembers, grants, access] = await Promise.all([
        client.resource<SpaceContext>(space, '/session'), client.resource<Page<Audience>>(space, '/audiences'),
        client.resource<MemberList>(space, '/memberships'), client.resource<Catalog>(space, '/resources'),
        client.resource<Snapshot['cleanup']>(space, '/grants/cleanup'),
        next.group ? client.resource<Page<AudienceMember>>(space, `/audiences/${next.group}/members`) : null,
        next.resource ? client.resource<Page<Grant>>(space, `/resources/${next.resource}/grants`) : null,
        next.resource && next.member ? client.resource<Access>(space, `/resources/${next.resource}/access/${next.member}`) : null,
      ])
      if (!check()) { if (number === sequence.current) clear(); return }
      if (number !== sequence.current) return
      if (!['owner', 'admin'].includes(context.role) || [audiences, members, cleanup, groupMembers, grants, access].some(x => x && x.policy_revision !== context.policy_revision)) throw new Error('Sharing changed. Reload to see current access.')
      const previous = displayed.current
      // Keep loaded choices while changing a selection in the same policy
      // revision. Otherwise selecting a resource loses a group on page two.
      const merge = <T,>(old: T[], fresh: T[], id: (row: T) => string) => [...new Map([...old, ...fresh].map(row => [id(row), row])).values()]
      const same = previous?.context.policy_revision === context.policy_revision
      const snapshot = { context, audiences: same ? { ...previous.audiences, items: merge(previous.audiences.items, audiences.items, a => a.id) } : audiences,
        members: same ? { ...previous.members, members: merge(previous.members.members, members.members, m => m.membership_id) } : members,
        catalog: same ? { ...previous.catalog, resources: merge(previous.catalog.resources, catalog.resources, r => r.id) } : catalog,
        cleanup, groupMembers, grants, access }
      displayed.current = snapshot; setData(snapshot)
    } catch (failure) { if (check() && number === sequence.current) { clear(); setError(spaceMessage(failure)) } }
    finally { if (check() && number === sequence.current) setBusy(false) }
  }
  useEffect(() => {
    void load(); const refresh = () => { if (!flight.current && !pending.current) void load() }
    window.addEventListener('focus', refresh)
    return () => { sequence.current++; window.removeEventListener('focus', refresh) }
  }, [client, space])

  async function more(kind: 'audiences' | 'members' | 'catalog' | 'groupMembers' | 'grants') {
    if (!data || flight.current || pending.current) return
    const page = data[kind], cursor = page?.next_cursor
    if (!cursor) return
    const paths = { audiences: '/audiences', members: '/memberships', catalog: '/resources', groupMembers: `/audiences/${choice.group}/members`, grants: `/resources/${choice.resource}/grants` }
    const check = client.guard(), number = ++sequence.current; setBusy(true); setError('')
    try {
      const result = await client.resource<Page<unknown> & MemberList & Catalog>(space, paths[kind]+'?cursor='+encodeURIComponent(cursor))
      if (!check() || number !== sequence.current) return
      if (kind !== 'catalog' && result.policy_revision !== data.context.policy_revision) throw new Error('Sharing changed. Reload to continue.')
      setData(previous => {
        if (!previous) return null
        const snapshot = kind === 'catalog' ? { ...previous, catalog: { ...result, resources: [...previous.catalog.resources, ...result.resources] } }
          : kind === 'members' ? { ...previous, members: { ...result, members: [...previous.members.members, ...result.members] } }
          : { ...previous, [kind]: { ...result, items: [...previous[kind]!.items, ...result.items] } }
        displayed.current = snapshot; return snapshot
      })
    } catch (failure) { if (check() && number === sequence.current) { clear(); setError(spaceMessage(failure)) } }
    finally { if (check() && number === sequence.current) setBusy(false) }
  }
  function choose(path: string, title: string, description: string, body: Record<string, unknown> = {}) {
    if (!data || busy || pending.current) return
    const value = { path, title, description, body: { ...body, operation_id: crypto.randomUUID(), expected_revision: data.context.policy_revision }, attempted: false }
    pending.current = value; setChange(value); setError(''); setNotice(''); sequence.current++
  }
  async function confirm() {
    if (!pending.current || flight.current) return
    const attempt = { ...pending.current, attempted: true }, check = client.guard()
    pending.current = attempt; setChange(attempt); flight.current = true; setBusy(true); setError(''); sequence.current++
    try {
      const result = await client.resource<{ action: string; target: string; retired_count: number }>(space, attempt.path, 'POST', attempt.body)
      if (!check()) return
      pending.current = null; setChange(null); setLabel(''); setRename(''); setUntil('')
      setNotice(result.action === 'grant.cleanup' ? `${result.retired_count} stale grants retired. Grant records and audit history are retained.` : 'Sharing updated. Current access is shown below.')
      flight.current = false
      await load({ ...selected.current, group: result.action === 'audience.archive' ? '' : result.action === 'audience.create' ? result.target : selected.current.group })
    } catch (failure) {
      if (!check()) return
      // Not shareable is the resource's state, not a stale page: keep the page.
      if (failure instanceof SpaceError && failure.code === 'not_shareable') { pending.current = null; setChange(null) }
      else if (failure instanceof SpaceError && [401, 403, 404, 409, 422].includes(failure.status)) clear()
      setError(spaceMessage(failure))
    } finally { flight.current = false; if (check()) setBusy(false) }
  }
  function reviewGrant() {
    if (!data || !choice.resource) return
    const expires = until ? Math.floor(new Date(until).getTime()/1000) : null
    if (expires !== null && (!Number.isFinite(expires) || expires <= Date.now()/1000)) { setError('Choose a future expiry, or leave it empty.'); return }
    if (recipientType === 'group' && !choice.group || recipientType === 'member' && !choice.member) { setError('Choose the recipient group or member first.'); return }
    const resource = data.catalog.resources.find(r => r.id === choice.resource)
    const name = recipientType === 'all' ? 'all current and future members' : recipientType === 'group' ? data.audiences.items.find(a => a.id === choice.group)?.label ?? choice.group : data.members.members.find(m => m.membership_id === choice.member)?.account_id ?? choice.member
    choose(`/resources/${choice.resource}/grants`, 'Share this resource?', `Share “${resource?.title ?? choice.resource}” with ${name}. ${view ? 'Discover and view.' : 'Discover only.'} ${expires ? `Expires ${expiry(expires)}.` : 'No expiry.'} Children need their own grants.`,
      { membership_id: recipientType === 'member' ? choice.member : null, audience_id: recipientType === 'group' ? choice.group : null, all_members: recipientType === 'all', actions: view ? ['discover', 'view'] : ['discover'], expires_at: expires })
  }
  const disabled = busy || !!change
  const groupName = data ? data.audiences.items.find(a => a.id === choice.group)?.label ?? choice.group : ''
  const memberName = data ? data.members.members.find(m => m.membership_id === choice.member)?.account_id ?? choice.member : ''
  const chosen = data?.catalog.resources.find(r => r.id === choice.resource)
  return <>
    <Link to={`/s/${space}`} className="text-sm underline">Back to Postern</Link>
    <Heading title="Groups and sharing" description={data ? `Choose who can discover and view resources in ${data.context.title}.` : 'Checking current sharing permissions.'} />
    <Message text={error} />{notice ? <p role="status">{notice}</p> : null}
    {busy ? <p role="status">Loading sharing…</p> : null}
    {change ? <Panel className="space-y-4"><h2 ref={confirmation} tabIndex={-1} className="text-xl font-semibold">{change.title}</h2><p className={muted}>{change.description}</p>
      {change.attempted && error ? <p>The response may have been lost. Retry the same change, or reload to check the outcome.</p> : null}
      <div className="flex flex-wrap gap-3"><Button disabled={busy} onClick={() => void confirm()}>{busy ? 'Working…' : change.attempted ? 'Retry change' : 'Confirm change'}</Button><Button variant="secondary" disabled={busy} onClick={() => { pending.current = null; setChange(null); if (change.attempted) void load() }}>{change.attempted ? 'Reload sharing' : 'Cancel'}</Button></div>
    </Panel> : null}
    {data ? <>
      <Panel className="space-y-4"><h2 className="text-lg font-semibold">Audience groups</h2><p className={muted}>Groups are flat lists of members in this Postern. Adding someone gives them access through this group’s existing grants.</p>
        <form className="flex flex-wrap items-end gap-3" onSubmit={e => { e.preventDefault(); choose('/audiences', 'Create audience group?', `Create “${label.trim()}” with no members.`, { label }) }}><Field label="New group name"><input className={input} value={label} onChange={e => setLabel(e.target.value)} maxLength={200} required disabled={disabled} /></Field><Button type="submit" disabled={disabled || !label.trim()}>Create group</Button></form>
        <Field label="Audience group"><select className={input} value={choice.group} disabled={disabled} onChange={e => { setRename(''); void load({ ...choice, group: e.target.value }, true) }}><option value="">Choose a group</option>{choice.group && !data.audiences.items.some(a => a.id === choice.group) ? <option value={choice.group}>Selected group ({choice.group})</option> : null}{data.audiences.items.map(a => <option key={a.id} value={a.id}>{a.label} ({a.member_count} members)</option>)}</select></Field>
        {data.audiences.next_cursor ? <Button variant="quiet" disabled={disabled} onClick={() => void more('audiences')}>Load more groups</Button> : null}
        {choice.group && data.groupMembers ? <div className="space-y-4 border-t border-neutral-200 pt-4 dark:border-neutral-800">
          <form className="flex flex-wrap items-end gap-3" onSubmit={e => { e.preventDefault(); choose(`/audiences/${choice.group}/rename`, 'Rename this group?', `Rename “${groupName}” to “${rename.trim()}”. Its members and grants stay in place.`, { label: rename }) }}><Field label="Rename group"><input className={input} value={rename} onChange={e => setRename(e.target.value)} maxLength={200} required disabled={disabled} /></Field><Button type="submit" variant="secondary" disabled={disabled || !rename.trim()}>Review rename</Button></form>
          <h3 className="font-medium">Group members</h3>{data.groupMembers.items.length ? <ul className="space-y-3">{data.groupMembers.items.map(m => <li key={m.id} className="flex flex-wrap items-center justify-between gap-3"><span className="min-w-0 break-all">Account {m.account_id} ({m.authority}) · {roleLabel(m.role)}</span><Button variant="quiet" disabled={disabled} onClick={() => choose(`/audiences/${choice.group}/remove`, 'Remove from this group?', `Account ${m.account_id} will lose access through “${groupName}”. Other grants and space roles still apply.`, { membership_id: m.id })}>Remove<span className="sr-only"> account {m.account_id}</span></Button></li>)}</ul> : <p className={muted}>This group has no members.</p>}
          {data.groupMembers.next_cursor ? <Button variant="quiet" disabled={disabled} onClick={() => void more('groupMembers')}>Load more group members</Button> : null}
          <Button variant="quiet" disabled={disabled} onClick={() => choose(`/audiences/${choice.group}/archive`, 'Archive this group?', `Remove everyone from “${groupName}” and revoke every grant to it. Other grants still apply. History is retained; recreating the name creates a new group.`)}>Archive group</Button>
        </div> : null}
      </Panel>
      <Panel className="space-y-4"><h2 className="text-lg font-semibold">Select a member</h2><Field label="Member"><select className={input} value={choice.member} disabled={disabled} onChange={e => void load({ ...choice, member: e.target.value }, true)}><option value="">Choose a member</option>{choice.member && !data.members.members.some(m => m.membership_id === choice.member) ? <option value={choice.member}>Selected membership ({choice.member})</option> : null}{data.members.members.map(m => <option key={m.membership_id} value={m.membership_id}>{m.account_id} ({m.authority}) · {roleLabel(m.role)}</option>)}</select></Field>
        {data.members.next_cursor ? <Button variant="quiet" disabled={disabled} onClick={() => void more('members')}>Load more members</Button> : null}
        {choice.group ? <Button variant="secondary" disabled={disabled || !choice.member} onClick={() => choose(`/audiences/${choice.group}/add`, 'Add to this group?', `Add account ${memberName} to “${groupName}”. They will receive access through every current and future grant to this group.`, { membership_id: choice.member })}>Add selected member to group</Button> : null}
      </Panel>
      <Panel className="space-y-4"><h2 className="text-lg font-semibold">Resource grants</h2><p className={muted}>Access is additive. Revoking one grant can leave access through another group, a direct grant, all members, or an owner or administrator role.</p>
        <Field label="Resource"><select className={input} value={choice.resource} disabled={disabled} onChange={e => void load({ ...choice, resource: e.target.value }, true)}><option value="">Choose a resource</option>{choice.resource && !data.catalog.resources.some(r => r.id === choice.resource) ? <option value={choice.resource}>Selected resource ({choice.resource})</option> : null}{data.catalog.resources.map(r => <option key={r.id} value={r.id}>{r.title} · {r.state}</option>)}</select></Field>
        {data.catalog.next_cursor ? <Button variant="quiet" disabled={disabled} onClick={() => void more('catalog')}>Load more resources</Button> : null}
        {choice.resource && data.grants ? <>
          {data.grants.items.length ? <ul className="space-y-3">{data.grants.items.map(g => <li key={g.id} className="space-y-2 rounded-xl border border-neutral-200 p-3 dark:border-neutral-800"><p className="break-all font-medium">{recipient(g)}</p><p className={`text-sm ${muted}`}>{g.actions.join(', ')} · {g.expires_at ? `Expires ${expiry(g.expires_at)}` : 'No expiry'}{g.stale_reason ? ` · ${reasons[g.stale_reason]}` : ''}</p><Button aria-label={`Revoke grant ${g.id}`} variant="quiet" disabled={disabled} onClick={() => choose(`/grants/${g.id}/revoke`, 'Revoke this grant?', `${recipient(g)} will lose this grant. Other grants and owner or administrator access still apply. Inspect the selected member’s access after this change.`)}>Revoke grant<span className="sr-only">{` ${g.id}`}</span></Button></li>)}</ul> : <p className={muted}>No active grants. Owners and administrators still have role access.</p>}
          {data.grants.next_cursor ? <Button variant="quiet" disabled={disabled} onClick={() => void more('grants')}>Load more grants</Button> : null}
          {chosen && chosen.state !== 'published' ? <p className={`border-t border-neutral-200 pt-4 text-sm dark:border-neutral-800 ${muted}`}>Only published resources can receive new grants. This resource is {chosen.state}.</p>
            : <form className="space-y-4 border-t border-neutral-200 pt-4 dark:border-neutral-800" onSubmit={e => { e.preventDefault(); reviewGrant() }}><h3 className="font-medium">New grant</h3><Field label="Share with"><select className={input} value={recipientType} onChange={e => setRecipientType(e.target.value)} disabled={disabled}><option value="group">Selected audience group</option><option value="member">Selected member</option><option value="all">All current and future members</option></select></Field><Field label="Access"><select className={input} value={view ? 'view' : 'discover'} onChange={e => setView(e.target.value === 'view')} disabled={disabled}><option value="view">Discover and view</option><option value="discover">Discover only</option></select></Field><Field label="Expires at (optional)" hint="Your local time. Leave empty for no expiry."><input className={input} type="datetime-local" value={until} onChange={e => setUntil(e.target.value)} disabled={disabled} /></Field><p className={`text-sm ${muted}`}>Only published resources can receive new grants. Resource previews and playback are not available in this version.</p><Button type="submit" disabled={disabled}>Review grant</Button></form>}
        </> : null}
        {data.access ? <div className="space-y-3 border-t border-neutral-200 pt-4 dark:border-neutral-800"><h3 className="font-medium">Selected member’s effective access</h3><p>{data.access.actions.length ? data.access.actions.join(' and ') : 'No access to this resource.'}</p>{['owner','admin'].includes(data.access.role) ? <p className={muted}>{roleLabel(data.access.role)} role permits access even without a grant.</p> : null}<ul className={`space-y-1 text-sm ${muted}`}>{data.access.sources.map(g => <li key={g.id}>{g.all_members ? 'All members' : g.audience_id ? `Group: ${g.audience_label}` : 'Direct grant'} · {g.actions.join(', ')}</li>)}</ul>{data.access.more_sources ? <p className={muted}>More grant paths exist. The access summary includes all of them.</p> : null}</div> : null}
      </Panel>
      <Panel className="space-y-4"><h2 className="text-lg font-semibold">Stale grants</h2><p className={muted}>Retire expired grants, grants without discover permission, and grants to unpublished resources or ended recipients. Publishing a resource again will require new grants. Grant records and audit history are retained.</p><p>{data.cleanup.items.length ? `${data.cleanup.items.length}${data.cleanup.next_cursor ? '+' : ''} stale grants found.` : 'No stale grants found.'}</p><Button variant="secondary" disabled={disabled || !data.cleanup.items.length} onClick={() => choose('/grants/cleanup', 'Retire stale grants?', 'Retire up to 500 stale grants across this Postern. This also clears old sharing on drafts and archives; publishing them again will require new grants. Repeat cleanup if more remain.')}>Review cleanup</Button></Panel>
    </> : null}
    {!change ? <Button variant="quiet" disabled={busy} onClick={() => void load()}>Reload sharing</Button> : null}
  </>
}
