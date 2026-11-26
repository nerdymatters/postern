import { useEffect, useMemo } from 'react'
import { Button, ManualCopy, useCopy } from './ui'
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
  // useCopy keeps the result with the code it belongs to and has no reset effect
  // (one that ran on mount after an early click would wipe the result).
  const { copy, copied, manual } = useCopy(code)
  return <div className="space-y-2">
    <Button variant="secondary" onClick={() => void copy()}>{label}</Button>
    {copied ? <p role="status" className="text-sm">Copied. {secret ? 'Share it privately with the invited person.' : 'Share this address with a Postern owner.'}</p> : null}
    {manual ? <ManualCopy label={secret ? 'Invitation code — select and copy' : 'Invitation address — select and copy'} text={code} /> : null}
  </div>
}
