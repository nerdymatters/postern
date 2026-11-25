import { useEffect, useMemo, useState } from 'react'
import { copyText } from './clipboard'
import { Button, Field, input } from './ui'
import { SpacesClient, type SpaceRole } from './spaces-api'

export const muted = 'text-neutral-600 dark:text-neutral-300'
export const roleLabel = (role: SpaceRole) => role === 'admin' ? 'Administrator' : role === 'owner' ? 'Owner' : 'Member'
// American dates on every device, as in format.ts: a browser set to Thai would
// otherwise show Thai month names and the Buddhist era.
export const expiry = (seconds: number) => new Date(seconds * 1000).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' })

export function useClient(credential: string) {
  const client = useMemo(() => new SpacesClient(credential), [credential])
  useEffect(() => { client.activate(); return () => client.close() }, [client])
  return client
}

export function Heading({ title, description }: { title: string; description: string }) {
  return <div className="space-y-2"><h1 className="break-words text-3xl font-semibold tracking-tight">{title}</h1><p className={muted}>{description}</p></div>
}

export function CopyCode({ code, label, secret = false }: { code: string; label: string; secret?: boolean }) {
  // The result remembers which code it belongs to, so a result for an older code
  // never shows. No reset effect: one that ran on mount after an early click
  // (before React flushed effects) would wipe the result for the same code.
  const [result, setResult] = useState<{ code: string; ok: boolean } | null>(null)
  async function copy() {
    const ok = await copyText(code)
    setResult({ code, ok })
  }
  const copied = result?.code === code && result.ok
  const manual = result?.code === code && !result.ok
  return <div className="space-y-2">
    <Button variant="secondary" onClick={() => void copy()}>{label}</Button>
    {copied ? <p role="status" className="text-sm">Copied. {secret ? 'Share it privately with the invited person.' : 'Share this address with a Postern owner.'}</p> : null}
    {manual ? <Field label={secret ? 'Invitation code — select and copy' : 'Invitation address — select and copy'}><textarea className={`${input} break-all font-mono text-sm`} rows={4} readOnly value={code} onFocus={event => event.currentTarget.select()} /></Field> : null}
  </div>
}
