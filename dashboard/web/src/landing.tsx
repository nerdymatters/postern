import { useEffect, useMemo, useRef, useState } from 'react'
import { api, inline, type Config, type SignIn } from './api'
import { inAppBrowser } from './browser'
import { copyText } from './clipboard'
import { loadIdentity } from './google'
import { Button, Centred, Field, Message, Panel, Wordmark, input, Thai } from './ui'
import { Link } from './routes'
import { IconArrowRight, IconPrivacy } from './PosternIcons'
import { useBranding } from './branding'

export function Landing({ config, notice, onSignedIn }: { config: Config; notice: string; onSignedIn: (result: SignIn) => void }) {
  const { organization_name: organization } = useBranding()
  const embedded = useMemo(() => inAppBrowser(navigator.userAgent), [])
  const [error, setError] = useState('')
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const target = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (embedded || config.mode !== 'google') return
    let live = true
    loadIdentity().then(identity => {
      if (!live || !target.current) return
      identity.accounts.id.initialize({
        client_id: config.google_client_id,
        callback: response => { void enter(api.signInGoogle(response.credential)) },
      })
      identity.accounts.id.renderButton(target.current, { theme: 'outline', size: 'large', width: 320, text: 'signin_with', shape: 'pill' })
    }).catch(() => { if (live) setError('Google sign-in could not load. Check your connection and reload.') })
    return () => { live = false }
  }, [config, embedded])

  async function enter(attempt: Promise<SignIn>) {
    setBusy(true)
    setError('')
    try {
      onSignedIn(await attempt)
    } catch (failure) {
      setError(inline(failure))
    } finally {
      setBusy(false)
    }
  }

  if (embedded) {
    return (
      <Centred title="Open in Safari or Chrome">
        <Panel className="space-y-3">
          <p className="text-neutral-800 dark:text-neutral-200">
            This link opened inside a chat app's own browser, and Google sign-in does not work there.
          </p>
          <p className="text-sm text-neutral-600 dark:text-neutral-300">
            Tap the menu in the corner of this screen and choose "Open in Safari" or "Open in Chrome", or copy the
            link and paste it into a browser.
          </p>
          <p className="break-all rounded-xl bg-neutral-100 dark:bg-neutral-800 px-4 py-3 font-mono text-xs text-neutral-700 dark:text-neutral-300">{window.location.href}</p>
          <Button variant="secondary" onClick={async () => setCopied(await copyText(window.location.href))} className="w-full">
            {copied ? 'Link copied' : 'Copy the link'}
          </Button>
        </Panel>
      </Centred>
    )
  }

  return (
    <Centred title={<Wordmark />}>
      {config.preview ? <p className="rounded-lg bg-amber-50 dark:bg-amber-950 p-3 text-sm text-amber-900 dark:text-amber-200">Selfie preview · Separate from staff training</p> : null}
      <Panel className="space-y-4">
        <p className="text-neutral-800 dark:text-neutral-200">Staff training{organization ? ` for ${organization}` : ''}: finish each chapter, then pass its quiz.<Thai className="mt-1">ระบบฝึกอบรมพนักงาน{organization ? ` ${organization}` : ''}: เรียนให้ครบทีละบท แล้วทำแบบทดสอบท้ายบทให้ผ่าน</Thai></p>
        <Message text={error || notice} />
        {config.mode === 'demo' ? (
          <form
            className="space-y-4"
            onSubmit={event => { event.preventDefault(); void enter(api.signInDemo(email.trim())) }}
          >
            <Field label="Work email" hint={config.preview ? "Use reader@example.invalid or admin@example.invalid. No Google sign-in needed for this preview." : "Pilot sign-in. The staff list still decides who gets in."}>
              <input
                className={input} type="email" name="email" required autoCapitalize="off" autoCorrect="off"
                value={email} onChange={event => setEmail(event.target.value)}
              />
            </Field>
            <Button type="submit" disabled={busy || !email.trim()} className="w-full">Continue<IconArrowRight className="size-5" /></Button>
          </form>
        ) : (
          <div ref={target} className="flex justify-center py-2" />
        )}
      </Panel>
      <p className="text-sm text-neutral-500 dark:text-neutral-400">Sign in with the Google account your manager put on the staff list.</p>
      <p className="text-xs text-neutral-500 dark:text-neutral-400"><Link to="/privacy" className="inline-flex items-center gap-1 underline"><IconPrivacy className="size-3.5" />Privacy</Link></p>
    </Centred>
  )
}
