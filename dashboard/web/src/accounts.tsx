import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { api, currentToken, setToken, tokensForSignOut, Refusal, type Config, type SignIn } from './api'
import { accountMessage, accounts, accountToken, AccountError, sameIdentity, setAccountToken, subscribeAccount, type Account, type AccountConfig, type Profile, type ProfileEdit, type PublicProfile, type TermsAcceptance } from './accounts-api'
import { AccountTerms, TermsGate, TermsLink } from './terms'
import { App } from './app'
import { releaseAll } from './blobs'
import { inAppBrowser } from './browser'
import { loadIdentity } from './google'
import { Link, navigate, usePathname } from './routes'
import { ThemeToggle, useTheme } from './theme'
import { Button, Field, Message, Panel, Wordmark, buttonClass, input } from './ui'
import { forgetViews } from './views'
import { SpaceOnboarding } from './spaces'
import { nativeSpacesEnabled, onboardingDestination } from './spaces-api'

function clearSpace() { setToken(null); forgetViews(); releaseAll() }
function legacySpaceRoute(path: string) { return /^\/(?:post|quiz|final-exam|badges|certificate|admin)(?:\/|$)/.test(path) }
function spaceRoute(path: string) { return /^\/s\/pilot(?:\/|$)/.test(path) || legacySpaceRoute(path) }
const publicHandle = (path: string) => /^\/@([^/]+)\/?$/.exec(path)?.[1] ?? null

/** Account routes never mount the private app until its separate session is bound. */
export function AccountsApp() {
  const path = usePathname()
  const handle = publicHandle(path)
  const publicPage = handle !== null
  const token = useSyncExternalStore(subscribeAccount, accountToken)
  const [loaded, setLoaded] = useState<{ token: string; account: Account } | null>(null)
  const account = loaded?.token === token ? loaded.account : null
  const setAccount = (value: Account | null) => {
    const current = accountToken()
    setLoaded(value && current ? { token: current, account: value } : null)
  }
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [space, setSpace] = useState<{ accountToken: string; token: string } | null>(null)
  const [checkingSpace, setCheckingSpace] = useState(() => Boolean(currentToken()))
  const [spaceProblem, setSpaceProblem] = useState(false)
  const [spaceAttempt, setSpaceAttempt] = useState(0)
  const [spaceNotice, setSpaceNotice] = useState('')
  const previous = useRef(token)
  const authGeneration = useRef(0)

  useEffect(() => {
    const changed = () => setAttempt(value => value + 1)
    window.addEventListener('postern-terms-change', changed)
    return () => window.removeEventListener('postern-terms-change', changed)
  }, [])

  useEffect(() => {
    // Count every navigation, including away-and-back transitions batched into
    // one render. A callback from an abandoned Google prompt must stay abandoned.
    const invalidate = () => { authGeneration.current++ }
    window.addEventListener('popstate', invalidate)
    const unsubscribe = subscribeAccount(invalidate)
    return () => { window.removeEventListener('popstate', invalidate); unsubscribe(); invalidate() }
  }, [])

  useEffect(() => {
    if (!legacySpaceRoute(path)) return
    // Old shared training links enter the same namespace as new space links,
    // so their Back/Home actions stay in training. Replace rather than push to
    // preserve browser Back, and leave standalone and account routes untouched.
    window.history.replaceState(window.history.state, '', `/s/pilot${path}${window.location.search}${window.location.hash}`)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }, [path])

  useEffect(() => {
    if (previous.current !== token) {
      // Every tab owns its cached bearer, including one no longer in shared storage.
      const outgoing = currentToken()
      clearSpace(); setSpace(null); setAccount(null); setSpaceNotice(''); setSpaceProblem(false)
      if (outgoing) void api.revokeForAccount(outgoing).catch(() => {})
      previous.current = token
    }
  }, [token])

  useEffect(() => {
    let live = true
    if (publicPage) return
    setError('')
    setAccount(null)
    if (!token) { setLoading(false); return }
    setLoading(true)
    accounts.session().then(result => {
      if (live && accountToken() === token) setAccount(result.account)
    }).catch(failure => {
      if (!live || accountToken() !== token) return
      if (failure instanceof AccountError && (failure.status === 401 || failure.status === 403)) {
        setAccountToken(null)
      }
      setError(accountMessage(failure))
    }).finally(() => { if (live && accountToken() === token) setLoading(false) })
    return () => { live = false }
  }, [token, attempt, publicPage])

  useEffect(() => {
    let live = true
    if (!account || !token || publicPage || account.terms?.required || account.terms_unavailable) return
    if (nativeSpacesEnabled() && !spaceRoute(path)) { setCheckingSpace(false); return }
    const candidate = currentToken()
    if (!candidate) { setCheckingSpace(false); return }
    setCheckingSpace(true); setSpaceProblem(false)
    api.sessionForAccount().then(session => {
      if (!live || accountToken() !== token || currentToken() !== candidate) return
      if (!sameIdentity(account.identity, session.identity)) {
        clearSpace(); setSpace(null); setSpaceNotice('Sign in to the private space with the same Google account as your Postern account.')
        return
      }
      setSpace({ accountToken: token, token: candidate }); setSpaceNotice('')
    }).catch(failure => {
      if (!live || accountToken() !== token || currentToken() !== candidate) return
      setSpace(null); forgetViews(); releaseAll()
      if (failure instanceof Refusal && (failure.status === 401 || failure.status === 403)) {
        setToken(null); setSpaceNotice('Private access needs to be checked again. Your Postern account is still available.')
      } else {
        setSpaceProblem(true); setSpaceNotice('The private space could not be reached. Your Postern account is still available. Try checking access again.')
      }
    }).finally(() => { if (live) setCheckingSpace(false) })
    return () => { live = false }
  }, [account?.id, account?.identity?.issuer, account?.identity?.subject, account?.terms?.required, account?.terms_unavailable, token, publicPage, spaceAttempt, path])

  const spaceLost = useCallback(() => {
    clearSpace(); setSpace(null); setSpaceNotice('Your private-space session is no longer available. You are still signed in to Postern.')
  }, [])

  function leave() {
    authGeneration.current++
    const credential = accountToken()
    const privateCredentials = tokensForSignOut()
    clearSpace(); setSpace(null); setAccount(null); setAccountToken(null)
    setError(''); navigate('/home')
    if (credential) void accounts.signOut(credential).catch(() => {})
    for (const privateCredential of privateCredentials) void api.revokeForAccount(privateCredential).catch(() => {})
  }

  async function signedIn(credential: string, terms?: TermsAcceptance) {
    const generation = ++authGeneration.current
    const original = accountToken()
    const destination = window.location.pathname
    const result = await accounts.signIn(credential, terms)
    if (generation !== authGeneration.current || accountToken() !== original || window.location.pathname !== destination) {
      void accounts.signOut(result.token).catch(() => {}); return
    }
    const outgoing = currentToken()
    clearSpace(); setSpace(null); setAccountToken(result.token)
    if (outgoing) void api.revokeForAccount(outgoing).catch(() => {})
    if (!spaceRoute(destination) && destination !== '/settings/profile' && !(nativeSpacesEnabled() && onboardingDestination(destination))) navigate('/home')
  }

  async function enterSpace(credential: string) {
    const generation = ++authGeneration.current
    const original = accountToken()
    const destination = window.location.pathname
    if (!account || !original) return
    const result: SignIn = await api.signInGoogleForAccount(credential)
    if (generation !== authGeneration.current || accountToken() !== original || window.location.pathname !== destination) {
      void api.revokeForAccount(result.token).catch(() => {}); return
    }
    if (!sameIdentity(account.identity, result.identity)) {
      clearSpace(); setSpace(null)
      void api.revokeForAccount(result.token).catch(() => {})
      throw new Error('Choose the same Google account you used for Postern. This private session was not connected.')
    }
    const outgoing = currentToken()
    clearSpace(); setToken(result.token)
    if (outgoing && outgoing !== result.token) void api.revokeForAccount(outgoing).catch(() => {})
    setSpace({ accountToken: original, token: result.token }); setSpaceNotice('')
  }

  const ready = account && token && space?.accountToken === token && space.token === currentToken()
  if (publicPage) return <AccountFrame account={account} onLeave={leave}><PublicProfilePage key={handle} handle={handle} /></AccountFrame>
  if (path === '/account/terms') return <AccountFrame account={account} onLeave={leave}><AccountTerms /></AccountFrame>
  if (path === '/privacy' || path === '/account/privacy') return <AccountFrame account={account} onLeave={leave}><AccountPrivacy /></AccountFrame>
  if ((loading || (loaded !== null && loaded.token !== token)) && token) return <AccountFrame><p role="status">Loading your account…</p></AccountFrame>
  if (!account) return <AccountFrame><Message text={error} />{token ? <Panel className="space-y-4"><p>Your account could not be loaded.</p><Button onClick={() => setAttempt(value => value + 1)}>Try again</Button><Button variant="quiet" onClick={leave}>Sign out</Button></Panel> : <AccountSignIn onCredential={signedIn} />}</AccountFrame>
  if (account.terms_unavailable || account.terms?.required) return <AccountFrame account={account} onLeave={leave}><TermsGate key={`${account.id}:${account.terms_unavailable ? 'unavailable' : `${account.terms?.version}:${account.terms?.sha256}`}`} terms={account.terms_unavailable ? null : account.terms!} onAccepted={setAccount} onDeleted={leave} onReload={() => setAttempt(value => value + 1)} /></AccountFrame>
  if (spaceRoute(path)) {
    if (checkingSpace) return <AccountFrame account={account} onLeave={leave}><p role="status">Checking private access…</p></AccountFrame>
    if (spaceProblem) return <AccountFrame account={account} onLeave={leave}><Panel className="space-y-4"><h1 className="text-2xl font-semibold">Private space is unavailable</h1><Message text={spaceNotice} /><Button onClick={() => setSpaceAttempt(value => value + 1)}>Check again</Button><Link to="/home" className="block underline">Back to My Posterns</Link></Panel></AccountFrame>
    if (ready) return <><div className="border-b border-neutral-200 bg-neutral-50 px-4 py-2 text-sm dark:border-neutral-800 dark:bg-neutral-950"><Link to="/home" className="underline">My Posterns</Link><span className="mx-2 text-neutral-400">/</span>Private Postern</div><App key={`${account.id}:${space.token}`} expectedIdentity={account.identity} onSessionLost={spaceLost} onAccountSignOut={leave} /></>
    return <AccountFrame account={account} onLeave={leave}><SpaceEntry key={account.id} notice={spaceNotice} onCredential={enterSpace} /></AccountFrame>
  }
  if (path === '/settings/profile') return <AccountFrame account={account} onLeave={leave}><ProfileEditor key={account.id} account={account} onSaved={setAccount} onDeleted={leave} /></AccountFrame>
  if (nativeSpacesEnabled()) return <AccountFrame account={account} onLeave={leave}><SpaceOnboarding key={`${token}:${path}`} account={account} credential={token!} path={path} /></AccountFrame>
  return <AccountFrame account={account} onLeave={leave}>
    <div className="space-y-2"><p className="text-sm text-neutral-500 dark:text-neutral-400">Your account</p><h1 className="text-3xl font-semibold tracking-tight">My Posterns</h1><p className="text-neutral-600 dark:text-neutral-300">Private spaces and resources shared with you.</p></div>
    <Panel className="flex flex-wrap items-center gap-4"><Avatar profile={account.profile} /><div className="min-w-0 flex-1"><p className="font-semibold">{account.profile.display_name}</p><p className="text-sm text-neutral-500">{account.profile.handle ? `@${account.profile.handle}` : 'Choose a handle to make this yours.'}</p></div><Link to="/settings/profile" className={buttonClass('secondary')}>Edit profile</Link></Panel>
    <Message text={spaceNotice} />
    {spaceProblem ? <Button onClick={() => setSpaceAttempt(value => value + 1)}>Check again</Button> : null}
    {ready ? <Panel className="space-y-3"><h2 className="text-lg font-semibold">Private Postern</h2><p className="text-neutral-600 dark:text-neutral-300">Your access will be checked when you enter.</p><Link to="/s/pilot" className={buttonClass()}>Open Postern</Link></Panel> : <Panel className="space-y-4"><h2 className="text-xl font-semibold">{checkingSpace ? 'Checking your Posterns…' : 'No Posterns yet'}</h2><p className="text-neutral-600 dark:text-neutral-300">Posterns are private spaces shared by invitation. Your account and profile are ready even before someone shares a Postern with you.</p><Link to="/s/pilot" className={buttonClass('secondary')}>Have an invitation? Check access</Link></Panel>}
  </AccountFrame>
}

function AccountFrame({ account, onLeave, children }: { account?: Account | null; onLeave?: () => void; children: ReactNode }) {
  const { dark, toggle } = useTheme()
  return <div className="min-h-screen text-neutral-900 dark:text-neutral-100"><header className="border-b border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900"><div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-3 px-5 py-4"><Link to="/home"><Wordmark /></Link><nav aria-label="Account" className="flex items-center gap-3 text-sm"><ThemeToggle dark={dark} onToggle={toggle} />{account ? <><Link to="/home" className="py-2">Posterns</Link><Link to="/settings/profile" className="py-2">Profile</Link><button onClick={onLeave} className="py-2 underline">Sign out</button></> : <Link to="/home" className="py-2 underline">Sign in</Link>}</nav></div></header><main className="mx-auto max-w-3xl space-y-6 px-5 py-10">{children}</main><footer className="mx-auto max-w-3xl px-5 pb-8 text-sm text-neutral-500"><Link to="/account/privacy" className="underline">Account privacy</Link><span className="mx-3">·</span><Link to="/account/terms" className="underline">Terms of Service</Link></footer></div>
}

function GoogleButton({ clientId, onCredential }: { clientId: string; onCredential: (credential: string) => Promise<void> }) {
  const target = useRef<HTMLDivElement>(null)
  const pending = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const callback = useRef(onCredential)
  callback.current = onCredential
  useEffect(() => {
    let live = true
    if (!clientId) { setError('Google sign-in has not been configured. Please contact the operator.'); return }
    loadIdentity().then(identity => {
      if (!live || !target.current) return
      identity.accounts.id.initialize({ client_id: clientId, callback: response => {
        if (!live || pending.current) return
        pending.current = true; setBusy(true); setError('')
        void callback.current(response.credential).catch(failure => { if (live) setError(accountMessage(failure)) }).finally(() => { pending.current = false; if (live) setBusy(false) })
      } })
      target.current.replaceChildren()
      identity.accounts.id.renderButton(target.current, { theme: 'outline', size: 'large', width: Math.max(200, Math.min(280, target.current.clientWidth || 280)), text: 'signin_with', shape: 'pill' })
    }).catch(() => { if (live) setError('Google sign-in could not load. Check your connection and reload.') })
    return () => { live = false }
  }, [clientId])
  if (inAppBrowser(navigator.userAgent)) return <Message text="Open this page in Safari or Chrome to sign in with Google." />
  return <div className="space-y-3"><div ref={target} aria-label="Sign in with Google" className={busy ? 'pointer-events-none opacity-50' : ''} />{busy ? <p role="status">Signing in…</p> : null}<Message text={error} /></div>
}

function AccountSignIn({ onCredential }: { onCredential: (credential: string, terms?: TermsAcceptance) => Promise<void> }) {
  const [config, setConfig] = useState<AccountConfig | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [accepted, setAccepted] = useState(false)
  useEffect(() => { let live = true; setError(''); accounts.config().then(value => { if (live) setConfig(value) }).catch(failure => { if (live) setError(accountMessage(failure)) }); return () => { live = false } }, [attempt])
  return <Panel className="mx-auto max-w-lg space-y-5"><h1 className="text-3xl font-semibold tracking-tight">Your place on Postern</h1><p className="text-neutral-600 dark:text-neutral-300">Create your profile and find the resources people share with you. Private spaces decide access separately.</p><Message text={error} />{config ? <>{!config.registration_open ? <p>New account registration is currently closed. Existing accounts can still sign in.</p> : null}{config.terms && config.registration_open ? <label className="flex items-start gap-3"><input type="checkbox" className="mt-1 size-4" checked={accepted} onChange={event => setAccepted(event.target.checked)} /><span>I agree to the <TermsLink terms={config.terms} />.</span></label> : null}
      {notice ? <p role="status">{notice}</p> : null}
      {config.terms && config.registration_open ? <p>Accept the Terms to create a new account. Existing accounts can sign in to review the Terms or delete their account.</p> : null}
      <GoogleButton clientId={config.google_client_id} onCredential={async credential => {
        setNotice('')
        try { await onCredential(credential, accepted && config.terms ? { accepted: true, version: config.terms.version, sha256: config.terms.sha256 } : undefined) }
        catch (failure) { if (failure instanceof AccountError && failure.code === 'terms_changed') { setNotice('The Terms have changed. Please review the updated version before continuing.'); setAccepted(false); setConfig(null); setAttempt(value => value + 1) }; throw failure }
      }} /></> : error ? <Button onClick={() => setAttempt(value => value + 1)}>Try again</Button> : <p role="status">Loading sign-in…</p>}<p className="text-base text-neutral-500">Google supplies your verified identity and optional name/photo. Your profile stays private until you publish it. <Link to="/account/privacy" className="underline">Learn about account privacy</Link>.</p></Panel>
}

function SpaceEntry({ notice, onCredential }: { notice: string; onCredential: (credential: string) => Promise<void> }) {
  const [config, setConfig] = useState<Config | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => { let live = true; setError(''); api.configForAccount().then(value => { if (live) setConfig(value) }).catch(() => { if (live) setError('The private space is unavailable. Your Postern account is still available.') }); return () => { live = false } }, [attempt])
  return <Panel className="mx-auto max-w-lg space-y-5"><h1 className="text-2xl font-semibold">Enter a private Postern</h1><p>Use the same Google account as your Postern account. The space will check whether you have been invited.</p><Message text={notice || error} />{config?.mode === 'google' ? <GoogleButton clientId={config.google_client_id} onCredential={onCredential} /> : config ? <Message text="This space does not support Google account access. Contact the space owner." /> : error ? <Button onClick={() => setAttempt(value => value + 1)}>Check again</Button> : <p role="status">Checking sign-in options…</p>}<Link to="/home" className="inline-block underline">Back to My Posterns</Link></Panel>
}

function safeWebsite(value: string) {
  try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url.href : null } catch { return null }
}
function avatarUrl(value: string | null) {
  if (!value) return null
  try { const url = new URL(value); return url.protocol === 'https:' && (url.hostname === 'googleusercontent.com' || url.hostname.endsWith('.googleusercontent.com')) && !url.username && !url.password ? url.href : null } catch { return null }
}
function Avatar({ profile }: { profile: Pick<Profile, 'avatar_url' | 'display_name'> }) {
  const [failed, setFailed] = useState(false)
  const src = avatarUrl(profile.avatar_url)
  return src && !failed ? <img src={src} alt="" referrerPolicy="no-referrer" onError={() => setFailed(true)} className="size-16 rounded-full object-cover" /> : <span aria-hidden="true" className="flex size-16 shrink-0 items-center justify-center rounded-full bg-neutral-100 text-2xl dark:bg-neutral-800">{Array.from(profile.display_name.trim())[0]?.toUpperCase() || 'P'}</span>
}
function ProfileCard({ profile, preview = false }: { profile: PublicProfile; preview?: boolean }) {
  const Heading = preview ? 'h2' : 'h1'
  const website = safeWebsite(profile.website)
  return <Panel className="space-y-5"><Avatar profile={profile} /><div><Heading className="break-words text-3xl font-semibold tracking-tight">{profile.display_name}</Heading><p className="text-neutral-500">@{profile.handle}</p></div>{profile.bio ? <p className="whitespace-pre-wrap break-words text-neutral-700 dark:text-neutral-300">{profile.bio}</p> : null}{website ? <a className="inline-block break-all underline" href={website} target="_blank" rel="noopener noreferrer">{website}</a> : null}</Panel>
}

function PublicProfilePage({ handle }: { handle: string }) {
  const [profile, setProfile] = useState<PublicProfile | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => { let live = true; setLoading(true); setError(''); accounts.profile(handle).then(result => { if (live) setProfile(result.profile) }).catch(failure => { if (live) setError(failure instanceof AccountError && failure.status === 404 ? 'This profile is not available.' : accountMessage(failure)) }).finally(() => { if (live) setLoading(false) }); return () => { live = false } }, [handle, attempt])
  if (loading) return <p role="status">Loading profile…</p>
  if (error) return <Panel className="space-y-4"><h1 className="text-2xl font-semibold">Profile unavailable</h1><Message text={error} /><Button variant="secondary" onClick={() => setAttempt(value => value + 1)}>Try again</Button></Panel>
  return profile ? <ProfileCard profile={profile} /> : null
}

function editable(profile: Profile): ProfileEdit {
  return { handle: profile.handle ?? '', display_name: profile.display_name, bio: profile.bio, website: profile.website, published: profile.published }
}

function ProfileEditor({ account, onSaved, onDeleted }: { account: Account; onSaved: (account: Account) => void; onDeleted: () => void }) {
  const [profile, setProfile] = useState<ProfileEdit>(editable(account.profile))
  const [preview, setPreview] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const live = useRef(true)
  useEffect(() => { live.current = true; return () => { live.current = false } }, [])
  async function save() {
    const identity = accountToken()
    if ([...profile.bio.normalize('NFC').replace(/\r\n?/g, '\n').trim()].length > 280) { setError('Use no more than 280 characters for your bio.'); return }
    if ([...profile.display_name.normalize('NFC').trim()].length > 80) { setError('Use no more than 80 characters for your display name.'); return }
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await accounts.save(profile)
      if (!live.current || accountToken() !== identity) return
      onSaved(result.account); setProfile(editable(result.account.profile)); setNotice('Profile saved.'); setPreview(false)
    } catch (failure) { if (live.current && accountToken() === identity) setError(accountMessage(failure)) }
    finally { if (live.current && accountToken() === identity) setBusy(false) }
  }
  async function remove() {
    if (confirmation !== 'DELETE') return
    const identity = accountToken(); setBusy(true); setError('')
    try { await accounts.delete(); if (live.current && accountToken() === identity) onDeleted() }
    catch (failure) { if (live.current && accountToken() === identity) setError(accountMessage(failure)) }
    finally { if (live.current && accountToken() === identity) setBusy(false) }
  }
  return <><div className="space-y-2"><h1 className="text-3xl font-semibold tracking-tight">Your profile</h1><p className="text-neutral-600 dark:text-neutral-300">Choose what other people see. Your email and private spaces stay private.</p></div><Message text={error} />{notice ? <p role="status" className="text-sm">{notice}</p> : null}<p className="text-sm font-medium">{account.profile.published ? 'Your profile is public.' : 'Your profile is unpublished.'}</p><Panel><h2 className="mb-5 text-lg font-semibold">Profile details</h2><form onSubmit={event => { event.preventDefault(); void save() }}><fieldset disabled={busy} className="space-y-5"><Avatar profile={account.profile} /><Field label="Handle" hint="3–30 letters, numbers or internal hyphens. Your public address will use this handle."><input className={input} value={profile.handle} onChange={event => setProfile({ ...profile, handle: event.target.value.toLowerCase() })} required minLength={3} maxLength={30} pattern="[a-z0-9][a-z0-9-]{1,28}[a-z0-9]" autoCapitalize="none" autoCorrect="off" spellCheck={false} /></Field><Field label="Display name"><input className={input} value={profile.display_name} onChange={event => setProfile({ ...profile, display_name: event.target.value })} required maxLength={160} /></Field><Field label="Bio" hint="Up to 280 characters."><textarea className={input} rows={4} value={profile.bio} onChange={event => setProfile({ ...profile, bio: event.target.value })} maxLength={560} /></Field><Field label="Website" hint="An optional https:// address."><input type="url" className={input} value={profile.website} onChange={event => setProfile({ ...profile, website: event.target.value })} maxLength={2048} /></Field><label className="flex items-start gap-3"><input type="checkbox" className="mt-1 size-4" checked={profile.published} onChange={event => setProfile({ ...profile, published: event.target.checked })} /><span><span className="block font-medium">Publish my profile</span><span className="block text-base text-neutral-500">Anyone with your profile address can see these fields. Uncheck to hide it.</span></span></label><div className="flex flex-wrap gap-3"><Button type="submit">{busy ? 'Saving…' : 'Save profile'}</Button><Button variant="secondary" type="button" onClick={() => setPreview(value => !value)}>Preview public profile</Button>{account.profile.published && account.profile.handle ? <Link to={`/@${account.profile.handle}`} className={buttonClass('quiet')}>View published profile</Link> : null}</div></fieldset></form></Panel>{preview ? <section aria-label="Public profile preview" className="space-y-3"><p className="text-sm font-medium">Preview · Only these fields appear publicly</p><ProfileCard preview profile={{ ...profile, handle: profile.handle || 'your-handle', avatar_url: account.profile.avatar_url }} /></section> : null}<Panel className="space-y-4"><h2 className="text-lg font-semibold">Delete your account</h2><p className="text-base text-neutral-600 dark:text-neutral-300">This removes your Postern profile and account sessions. Private spaces keep their separately owned training and activity records under their retention policies.</p>{deleting ? <><Field label="Type DELETE to confirm"><input className={input} value={confirmation} onChange={event => setConfirmation(event.target.value)} autoComplete="off" /></Field><div className="flex gap-3"><Button disabled={busy || confirmation !== 'DELETE'} onClick={() => void remove()}>Permanently delete account</Button><Button variant="quiet" disabled={busy} onClick={() => { setDeleting(false); setConfirmation('') }}>Cancel</Button></div></> : <Button variant="secondary" onClick={() => setDeleting(true)}>Delete account</Button>}</Panel></>
}

function AccountPrivacy() {
  return <article className="space-y-5"><h1 className="text-3xl font-semibold">Account privacy</h1><Panel className="space-y-4"><h2 className="text-lg font-semibold">Your identity and profile</h2><p>Postern uses your verified Google identity and email to maintain your account. Your name and photo, when provided by Google, are suggestions for your profile. You choose a handle and decide whether to publish your profile.</p><p>A published profile contains your handle, chosen display name, avatar, bio and website. Your email, private-space memberships, training results and enrollment photos are not public profile fields.</p><h2 className="text-lg font-semibold">Account Terms</h2><p>We keep the version and document hash of the Terms you accept, your account identifier and the server time of acceptance. These acceptance records are removed with your account from active storage. Contact Nerdy Matters LLC at nerd@nerdymatters.com about the hosted account service.</p><h2 className="text-lg font-semibold">Private spaces</h2><p>Creating an account does not grant access to private resources. Each space decides membership and applies its own agreements, enrollment and retention requirements. Losing access to a space does not delete your Postern account.</p><h2 className="text-lg font-semibold">Account controls</h2><p>You can unpublish your profile or delete your account from profile settings. Deletion removes the account profile and account sessions. Organization-owned training or activity history is managed separately by its space owner. Deleted account data may remain in service backups until the configured retention period expires.</p><p>Postern stores a session credential in this browser so you can remain signed in. Sign out on shared devices. Account creation and edits are limited to protect the service; an operator may suspend abusive accounts or unpublish profiles.</p></Panel><Link to="/home" className="inline-block underline">Back to My Posterns</Link></article>
}
