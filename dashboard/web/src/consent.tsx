import { useState } from 'react'
import { api, ApiError, inline, type Config } from './api'
import { Button, Centred, Message, Panel } from './ui'

export function Consent({ config: initialConfig, onAccepted, onDeclined }: { config: Config; onAccepted: () => void; onDeclined: () => void }) {
  const [config, setConfig] = useState(initialConfig)
  const compatible = Boolean(config.ip_notice && config.ip_notice_version)
  const [agreed, setAgreed] = useState(false)
  const [ipAgreed, setIpAgreed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function accept() {
    if (!compatible || !agreed || !ipAgreed || busy) return
    setBusy(true)
    setError('')
    try {
      await api.consent(config.notice_version, config.ip_notice_version, agreed, ipAgreed)
      onAccepted()
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === 'notice_changed') {
        setAgreed(false)
        setIpAgreed(false)
        try { setConfig(await api.config()) } catch { /* Keep retry instructions visible. */ }
        setError('The agreements changed. Review both notices and agree again. If this continues, refresh this page.')
      } else setError(inline(failure))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Centred title="Before you start">
      {config.preview ? <p className="rounded-lg bg-amber-50 dark:bg-amber-950 p-3 text-sm text-amber-900 dark:text-amber-200">Selfie preview · Separate from staff training</p> : null}
      <Panel className="space-y-4">
        <p className="whitespace-pre-line text-neutral-800 dark:text-neutral-200">{config.notice}</p>
        <label className="flex items-start gap-3 text-sm text-neutral-800 dark:text-neutral-200">
          <input type="checkbox" className="mt-1 size-5 shrink-0" checked={agreed} disabled={busy} onChange={event => setAgreed(event.target.checked)} />
          <span>I agree that my sign-ins and the items I open are recorded and visible to company admins.</span>
        </label>
        <div className="space-y-3 border-t border-neutral-200 dark:border-neutral-800 pt-4">
          <h2 className="font-semibold text-neutral-900 dark:text-neutral-100">Protecting our intellectual property</h2>
          <p className="text-sm text-neutral-800 dark:text-neutral-200">{config.ip_notice}</p>
          <label className="flex items-start gap-3 text-sm text-neutral-800 dark:text-neutral-200">
            <input type="checkbox" className="mt-1 size-5 shrink-0" checked={ipAgreed} disabled={busy} onChange={event => setIpAgreed(event.target.checked)} />
            <span>I agree to these intellectual property restrictions.</span>
          </label>
        </div>
        <Message text={error || (!compatible ? 'Postern is being updated. Please refresh this page in a moment.' : '')} />
        <Button onClick={accept} disabled={!compatible || !agreed || !ipAgreed || busy} className="w-full">Agree and start training</Button>
        <Button variant="secondary" onClick={onDeclined} className="w-full">Not now</Button>
      </Panel>
      <p className="text-sm text-neutral-500 dark:text-neutral-400">Activity notice {config.notice_version} · IP agreement {config.ip_notice_version}. Both agreements are required to enter.</p>
    </Centred>
  )
}
