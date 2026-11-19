import { useCallback, useEffect, useRef, useState } from 'react'
import { accountToken, subscribeAccount } from './accounts-api'
import { Link } from './routes'
import { SpaceError, nativeContentEnabled, spaceMessage, type ContentRoute, type ResourceMetadata } from './spaces-api'
import { Heading, useClient } from './spaces-ui'
import { Button, Message, Panel } from './ui'

type Props = ContentRoute & { credential: string }
type Presentation = { title: string; pageCount?: number; image?: string; video?: string; viewerLabel?: string }

function clearVideo(element: HTMLVideoElement | null) {
  if (element) { element.pause(); element.removeAttribute('src'); element.load() }
}

/** A scope change unmounts all media before another Postern can reuse the screen. */
export function SpaceContentPage(props: Props) {
  if (!nativeContentEnabled()) return <Message text="Protected content is not enabled." />
  return <ContentReader key={`${props.credential}:${props.space}:${props.resource}:${props.kind}:${props.revision}`} {...props} />
}

function ContentReader({ credential, space, resource, kind, revision }: Props) {
  const client = useClient(credential)
  const [page, setPage] = useState(0)
  const [retry, setRetry] = useState(0)
  const [presentation, setPresentation] = useState<Presentation | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(true)
  const [minute, setMinute] = useState(() => Math.floor(Date.now()/60000))
  const video = useRef<HTMLVideoElement>(null)
  const videoRef = useCallback((element: HTMLVideoElement | null) => {
    if (!element) clearVideo(video.current)
    video.current = element
  }, [])
  const resume = useRef<{ position: number; playing: boolean; rate: number } | null>(null)
  const mediaFailure = useRef<() => void>(() => {})

  useEffect(() => {
    let closed = false, paused = document.visibilityState === 'hidden', stopped = false, inFlight = false
    let sequence = 0, ownedUrl = '', ticketExpires = 0, title = '', metadataKey = ''
    let controller: AbortController | null = null, deadline: ReturnType<typeof setTimeout> | undefined
    const stopVideo = () => clearVideo(video.current)
    const clear = (update = true) => {
      sequence++; controller?.abort(); controller = null; inFlight = false
      clearTimeout(deadline); deadline = undefined
      if (ownedUrl) { URL.revokeObjectURL(ownedUrl); ownedUrl = '' }
      stopVideo(); resume.current = null; ticketExpires = 0
      if (update) setPresentation(null)
    }
    const fail = (failure: unknown) => {
      if (closed) return
      stopped = true; clear(); setBusy(false); setError(spaceMessage(failure))
    }
    mediaFailure.current = () => fail(new SpaceError(503, 'media_unavailable', 'This content could not be displayed. Check access again.'))
    const armDeadline = () => {
      clearTimeout(deadline)
      // Permission checks begin every five seconds. If the authority stops
      // answering, retain no displayed media beyond ten seconds without proof.
      deadline = setTimeout(() => fail(new SpaceError(503, 'authority_unavailable', 'Current access could not be verified. Check access again.')), 10000)
    }
    async function load(initial: boolean) {
      if (closed || paused || stopped || inFlight) return
      inFlight = true
      const number = ++sequence, check = client.guard(), abort = new AbortController()
      controller = abort
      const current = () => !closed && !paused && !stopped && sequence === number && check() && !abort.signal.aborted
      if (initial) { setBusy(true); setError(''); armDeadline() }
      try {
        const metadata = kind === 'document'
          ? await client.document(space, resource, revision, abort.signal)
          : await client.video(space, resource, revision, abort.signal)
        if (!current()) return
        const key = JSON.stringify(metadata)
        if (!initial && key !== metadataKey) throw new SpaceError(404, 'content_changed', 'This resource has changed. Return to the Postern to open its current version.')
        if (initial) {
          const detail = await client.resource<ResourceMetadata>(space, `/resources/${resource}`, 'GET', undefined, abort.signal)
          if (!current()) return
          if (detail.id !== resource || detail.kind !== kind || detail.content_revision !== revision || detail.state !== 'published' || typeof detail.title !== 'string') throw new SpaceError(404, 'content_changed', 'This resource is unavailable.')
          title = detail.title; metadataKey = key
        }
        if (kind === 'document' && initial) {
          const count = 'page_count' in metadata ? metadata.page_count : 0
          if (page >= count) throw new SpaceError(404, 'page_unavailable', 'This page is unavailable.')
          const blob = await client.documentPage(space, resource, revision, page, abort.signal)
          if (!current()) return
          ownedUrl = URL.createObjectURL(blob)
          setPresentation({ title, pageCount: count, image: ownedUrl })
        } else if (kind === 'video' && (initial || ticketExpires <= Date.now()+10000)) {
          const started = Date.now(), ticket = await client.videoTicket(space, resource, revision, abort.signal)
          if (!current()) return
          const expires = started+ticket.expires_in_seconds*1000
          if (expires <= Date.now()+1000) throw new SpaceError(503, 'ticket_expired', 'Video access could not be refreshed. Check access again.')
          const source = client.videoSource(space, resource, revision, ticket.ticket)
          const element = video.current
          const previous = element ? { position: element.currentTime, playing: !element.paused && !element.ended, rate: element.playbackRate } : null
          stopVideo(); resume.current = previous; ticketExpires = expires
          setPresentation({ title, video: source, viewerLabel: 'viewer_label' in metadata ? metadata.viewer_label : undefined })
        }
        if (!current()) return
        setMinute(Math.floor(Date.now()/60000))
        armDeadline()
      } catch (failure) { if (current()) fail(failure) }
      finally { if (sequence === number) { inFlight = false; controller = null; if (!closed) setBusy(false) } }
    }
    const refresh = () => {
      if (document.visibilityState === 'hidden') return
      paused = false; stopped = false; clear(); void load(true)
    }
    const visibility = () => {
      if (document.visibilityState === 'hidden') { paused = true; clear(); setBusy(false); setError('') }
      else refresh()
    }
    const unsubscribe = subscribeAccount(() => {
      if (accountToken() !== credential) { stopped = true; clear(); client.close(); setBusy(false); setError('This account has changed. Return to My Posterns.') }
    })
    const interval = setInterval(() => { void load(false) }, 5000)
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', visibility)
    clear(); if (!paused) void load(true); else setBusy(false)
    return () => {
      closed = true; clear(false); clearInterval(interval); unsubscribe()
      window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', visibility)
    }
  }, [client, credential, space, resource, kind, revision, page, retry])

  const visible = accountToken() === credential && document.visibilityState !== 'hidden' ? presentation : null
  return <>
    <Link to={`/s/${space}`} className="text-sm underline">Back to Postern</Link>
    <Heading title={visible?.title ?? (kind === 'document' ? 'Protected document' : 'Protected video')} description="Access is checked while this page is open." />
    <Message text={error} />
    {error ? <Button onClick={() => setRetry(value => value+1)}>Check access again</Button> : null}
    {busy ? <p role="status">Checking access and preparing content…</p> : null}
    {!busy && !error && !visible ? <p role="status">Return to this tab to check access.</p> : null}
    {visible?.image ? <Panel className="space-y-4">
      <nav aria-label="Document pages" className="flex items-center justify-between gap-3">
        <Button variant="secondary" disabled={page === 0} onClick={() => setPage(value => value-1)}>Previous page</Button>
        <p aria-live="polite">Page {page+1} of {visible.pageCount}</p>
        <Button variant="secondary" disabled={page+1 >= (visible.pageCount ?? 0)} onClick={() => setPage(value => value+1)}>Next page</Button>
      </nav>
      <img src={visible.image} alt={`${visible.title}, page ${page+1}`} className="mx-auto h-auto max-w-full" draggable={false} onError={() => mediaFailure.current()} />
    </Panel> : null}
    {visible?.video ? <Panel className="space-y-3"><div className="relative overflow-hidden rounded-lg bg-black" onContextMenu={event => event.preventDefault()}>
      <video ref={videoRef} src={visible.video} aria-label={visible.title} controls controlsList="nodownload noplaybackrate nofullscreen noremoteplayback" disablePictureInPicture disableRemotePlayback playsInline preload="metadata" crossOrigin="anonymous" className="w-full"
        onError={() => mediaFailure.current()} onLoadedMetadata={event => {
          const previous = resume.current; resume.current = null
          if (!previous || accountToken() !== credential || document.visibilityState === 'hidden') return
          const element = event.currentTarget
          if (Number.isFinite(previous.position)) element.currentTime = Math.min(previous.position, Number.isFinite(element.duration) ? element.duration : previous.position)
          element.playbackRate = previous.rate
          if (previous.playing) void element.play().catch(() => {})
        }} />
      <span aria-label="Personal watermark" className="pointer-events-none absolute right-2 top-2 max-w-[90%] break-all rounded bg-black/65 px-2 py-1 text-right text-[10px] leading-tight text-white">{visible.viewerLabel}<br />{new Date(minute*60000).toISOString().slice(0,16).replace('T', ' ')} UTC</span>
      </div>
      <p className="text-sm text-neutral-500">Playback access refreshes while this tab is visible.</p>
    </Panel> : null}
  </>
}
