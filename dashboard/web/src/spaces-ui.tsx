import { useEffect, useMemo, useRef, useState } from 'react'
import { copyText } from './clipboard'
import { Button, Field, input } from './ui'
import { SpacesClient, type SpaceRole } from './spaces-api'

export const muted = 'text-neutral-600 dark:text-neutral-300'
export const roleLabel = (role: SpaceRole) => role === 'admin' ? 'Administrator' : role === 'owner' ? 'Owner' : 'Member'
export const expiry = (seconds: number) => new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })

export function useClient(credential: string) {
  const client = useMemo(() => new SpacesClient(credential), [credential])
  useEffect(() => { client.activate(); return () => client.close() }, [client])
  return client
}

export function Heading({ title, description }: { title: string; description: string }) {
  return <div className="space-y-2"><h1 className="break-words text-3xl font-semibold tracking-tight">{title}</h1><p className={muted}>{description}</p></div>
}

export function CopyCode({ code, label, secret = false }: { code: string; label: string; secret?: boolean }) {
  const [copied, setCopied] = useState(false)
  const [manual, setManual] = useState(false)
  const generation = useRef(0)
  useEffect(() => { generation.current++; setCopied(false); setManual(false); return () => { generation.current++ } }, [code])
  async function copy() {
    const current = generation.current
    const ok = await copyText(code)
    if (current !== generation.current) return
    setCopied(ok); setManual(!ok)
  }
  return <div className="space-y-2">
    <Button variant="secondary" onClick={() => void copy()}>{label}</Button>
    {copied ? <p role="status" className="text-sm">Copied. {secret ? 'Share it privately with the invited person.' : 'Share this address with a Postern owner.'}</p> : null}
    {manual ? <Field label={secret ? 'Invitation code — select and copy' : 'Invitation address — select and copy'}><textarea className={`${input} break-all font-mono text-sm`} rows={4} readOnly value={code} onFocus={event => event.currentTarget.select()} /></Field> : null}
  </div>
}
