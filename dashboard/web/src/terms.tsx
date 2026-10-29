import { useEffect, useRef, useState } from 'react'
import { accountMessage, accounts, accountToken, type Account, type AccountConfig, type Terms } from './accounts-api'
import { Button, Field, Message, Panel, input } from './ui'
import { publishedTerms } from './published-terms'

export function TermsLink({ terms }: { terms: Terms }) {
  return <a href={terms.url} target="_blank" rel="noopener noreferrer" className="underline">Terms of Service (version {terms.version})</a>
}

export function AccountTerms() {
  const [config, setConfig] = useState<AccountConfig | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let live = true
    setError('')
    accounts.config().then(value => { if (live) setConfig(value) }).catch(failure => { if (live) setError(accountMessage(failure)) })
    return () => { live = false }
  }, [attempt])
  return <Panel className="space-y-4"><h1 className="text-3xl font-semibold">Terms of Service</h1><Message text={error} />
    <p>Read the published hosted account Terms:</p><TermsLink terms={publishedTerms} />
    {error ? <Button onClick={() => setAttempt(value => value + 1)}>Try again</Button> : !config ? <p role="status">Loading account Terms status…</p> : config.terms_unavailable ? <><p>Account Terms status is temporarily unavailable. Please try again later.</p><Button onClick={() => setAttempt(value => value + 1)}>Try again</Button></> : config.terms ? <><p>The account service currently requires this revision:</p><TermsLink terms={config.terms} /></> : <p>Account acceptance is not active yet. New registration remains closed.</p>}
    <p>Private spaces have their own access rules and agreements.</p>
  </Panel>
}

export function TermsGate({ terms, onAccepted, onDeleted, onReload }: {
  terms: Terms | null; onAccepted: (account: Account) => void; onDeleted: () => void; onReload: () => void
}) {
  const [accepted, setAccepted] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const live = useRef(true)
  useEffect(() => { live.current = true; return () => { live.current = false } }, [])
  async function submit(remove = false) {
    if (busy || (remove ? confirmation !== 'DELETE' : !accepted || !terms)) return
    const identity = accountToken()
    setBusy(true); setError('')
    try {
      if (remove) {
        await accounts.delete()
        if (live.current && accountToken() === identity) onDeleted()
      } else {
        if (!terms) return
        const result = await accounts.acceptTerms({ accepted: true, version: terms.version, sha256: terms.sha256 })
        if (live.current && accountToken() === identity) onAccepted(result.account)
      }
    } catch (failure) { if (live.current && accountToken() === identity) setError(accountMessage(failure)) }
    finally { if (live.current && accountToken() === identity) setBusy(false) }
  }
  return <Panel className="space-y-5"><h1 className="text-3xl font-semibold">{terms ? 'Review the Terms of Service' : 'Terms are temporarily unavailable'}</h1>
    {terms ? <>
    <p>Please review and accept the current Terms to continue using your Postern account. This is separate from any private space’s agreements.</p>
    <p><TermsLink terms={terms} /></p><Message text={error} />
    <label className="flex items-start gap-3"><input type="checkbox" className="mt-1 size-4" disabled={busy} checked={accepted} onChange={event => setAccepted(event.target.checked)} /><span>I have read and agree to these Terms of Service.</span></label>
    <div className="flex flex-wrap gap-3"><Button disabled={!accepted || busy} onClick={() => void submit()}>{busy ? 'Saving…' : 'Accept and continue'}</Button>{error ? <Button variant="secondary" disabled={busy} onClick={onReload}>Reload current Terms</Button> : null}</div></> : <><p>Your account is still signed in. Profile changes and private-space entry are paused until the Terms can be loaded.</p><Message text={error} /><Button disabled={busy} onClick={onReload}>Try again</Button></>}
    <p>You can sign out or delete your account. Private spaces retain their separately owned activity records.</p>
    {deleting ? <><Field label="Type DELETE to confirm"><input className={input} value={confirmation} disabled={busy} onChange={event => setConfirmation(event.target.value)} autoComplete="off" /></Field><Button disabled={busy || confirmation !== 'DELETE'} onClick={() => void submit(true)}>Permanently delete account</Button><Button variant="quiet" disabled={busy} onClick={() => { setDeleting(false); setConfirmation('') }}>Cancel</Button></> : <Button variant="secondary" onClick={() => setDeleting(true)}>Delete account</Button>}
  </Panel>
}
