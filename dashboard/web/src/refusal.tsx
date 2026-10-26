import type { Refusal, RefusalCode } from './api'
import { Bilingual, Button, Centred, Panel } from './ui'
import { IconSignOut } from './PosternIcons'

const STATES: Record<Exclude<RefusalCode, 'consent_required' | 'sign_in_required' | 'selfie_required'>, { title: string; hint: string }> = {
  access_denied: {
    title: 'Your access is no longer available',
    hint: 'If you think this is wrong, ask your manager to check the staff list.',
  },
  authorization_source_unavailable: {
    title: 'Access temporarily paused while we verify permissions',
    hint: 'Nothing is wrong with your account. This clears on its own, usually within a minute.',
  },
  selfie_unavailable: {
    title: 'Your enrollment photo is temporarily unavailable',
    hint: 'Please try again in a moment. Your reading progress is saved.',
  },
  audit_unavailable: {
    title: 'Content is paused because its audit record could not be saved',
    hint: 'Postern never shows a page it cannot record. Try again in a minute.',
  },
}

export function Refused({ refusal, onRetry, onSignOut }: { refusal: Refusal; onRetry: () => void; onSignOut: () => void }) {
  const state = STATES[refusal.code as keyof typeof STATES]
  return (
    <Centred title={state.title}>
      <Panel className="space-y-3">
        <p className="text-neutral-800 dark:text-neutral-200"><Bilingual text={refusal.message} /></p>
        <p className="text-sm text-neutral-500 dark:text-neutral-400">{state.hint}</p>
      </Panel>
      <div className="flex gap-3">
        <Button onClick={onRetry} className="flex-1">Try again</Button>
        <Button variant="secondary" onClick={onSignOut} className="flex-1"><IconSignOut className="size-5" />Sign out</Button>
      </div>
    </Centred>
  )
}
