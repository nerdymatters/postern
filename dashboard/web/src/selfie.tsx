import { useEffect, useRef, useState } from 'react'
import { api, ApiError, inline, type Config, type SelfieRecord, type SelfieState } from './api'
import { hold, release } from './blobs'
import { moment } from './format'
import { Button, Centred, Message, Panel, Thai } from './ui'
import { IconAdmin, IconCamera, IconCheck, IconEnrollmentPhotos, IconRetake, IconSignOut } from './PosternIcons'

export function SelfieImage({ id, className = '' }: { id: string; className?: string }) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    let live = true
    let owned = ''
    setUrl('')
    api.selfieImage(id).then(blob => {
      if (live) { owned = hold(blob); setUrl(owned) }
    }).catch(() => { /* The photo may have reached its retention deadline. */ })
    return () => { live = false; if (owned) release(owned) }
  }, [id])
  return url ? <img src={url} alt="Enrollment selfie" className={`rounded object-cover ${className}`} /> : <span aria-label="Selfie unavailable" className={`inline-block rounded bg-neutral-200 dark:bg-neutral-700 ${className}`} />
}

export function SelfieEnrollment({ config, initial, onSaved, onLeave, onCancel, onAdmin }: {
  config: Config; initial?: SelfieState; onSaved: () => void; onLeave: () => void; onCancel?: () => void; onAdmin?: () => void
}) {
  const video = useRef<HTMLVideoElement>(null)
  const tracks = useRef<MediaStream | null>(null)
  const generation = useRef(0)
  const live = useRef(true)
  const [agreed, setAgreed] = useState(false)
  const [starting, setStarting] = useState(false)
  const [camera, setCamera] = useState(false)
  const [ready, setReady] = useState(false)
  const [photo, setPhoto] = useState<Blob | null>(null)
  const [preview, setPreview] = useState('')
  const [busy, setBusy] = useState(false)
  const [needsRetake, setNeedsRetake] = useState(false)
  // Retry restrictions belong to the account's attempt, not the captured photo: Retake keeps them.
  const [dailyLimit, setDailyLimit] = useState('')
  const [retryAt, setRetryAt] = useState(0)
  const [now, setNow] = useState(Date.now)
  const cooldown = Math.max(0, Math.ceil((retryAt - now) / 1000))
  useEffect(() => {
    if (!retryAt) return
    const refresh = () => {
      const current = Date.now()
      setNow(current)
      if (current >= retryAt) window.clearInterval(timer)
    }
    const timer = window.setInterval(refresh, 1000)
    // Phone browsers suspend timers in the background; elapsed time still counts on return.
    document.addEventListener('visibilitychange', refresh)
    window.addEventListener('focus', refresh)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', refresh)
      window.removeEventListener('focus', refresh)
    }
  }, [retryAt])
  const [pending, setPending] = useState(!!initial?.pending_id)
  const [error, setError] = useState('')

  function stopCamera() {
    generation.current++
    tracks.current?.getTracks().forEach(track => track.stop())
    tracks.current = null
    if (video.current) video.current.srcObject = null
  }

  useEffect(() => {
    live.current = true
    return () => { live.current = false; stopCamera() }
  }, [])
  useEffect(() => {
    if (!photo) { setPreview(''); return }
    const url = hold(photo)
    setPreview(url)
    return () => release(url)
  }, [photo])
  useEffect(() => {
    if (!pending) return
    let active = true
    let timer = 0
    const check = async () => {
      try {
        const state = await api.selfieState()
        if (!active) return
        if (!state.pending_id && !state.required && state.id !== initial?.id) { onSaved(); return }
        if (!state.pending_id) {
          setPending(false)
          setError('The enrollment could not be completed. Please take a new photo.')
          return
        }
        setError(state.error ?? '')
      } catch (failure) {
        if (active) setError(inline(failure))
      }
      if (active) timer = window.setTimeout(check, 2000)
    }
    void check()
    return () => { active = false; window.clearTimeout(timer) }
  }, [pending, onSaved, initial?.id])

  async function start() {
    stopCamera()
    const attempt = generation.current
    setPhoto(null); setError(''); setNeedsRetake(false); setStarting(true); setReady(false)
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('unsupported')
      const stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 640 } } })
      if (!live.current || generation.current !== attempt) { stream.getTracks().forEach(track => track.stop()); return }
      tracks.current = stream
      setCamera(true)
      if (video.current) { video.current.srcObject = stream; await video.current.play() }
    } catch (failure) {
      if (!live.current || generation.current !== attempt) return
      stopCamera(); setCamera(false)
      const name = (failure as DOMException).name
      setError(name === 'NotAllowedError'
        ? 'Camera access was denied. Allow camera access in your browser settings, then try again.'
        : name === 'NotFoundError'
          ? 'No camera was found. Open this preview on a phone or a device with a camera.'
          : 'The camera could not start. Use HTTPS or localhost, close other camera apps, and try again.')
    } finally {
      if (live.current) setStarting(false)
    }
  }

  function take() {
    const element = video.current
    if (!element?.videoWidth || !element.videoHeight) { setError('Wait for the camera image, then try again.'); return }
    const canvas = document.createElement('canvas')
    canvas.width = canvas.height = 512
    const context = canvas.getContext('2d')
    if (!context) { setError('This browser could not capture a photo. Try another browser.'); return }
    const edge = Math.min(element.videoWidth, element.videoHeight)
    context.drawImage(element, (element.videoWidth-edge)/2, (element.videoHeight-edge)/2, edge, edge, 0, 0, 512, 512)
    const attempt = generation.current
    canvas.toBlob(blob => {
      if (!live.current || generation.current !== attempt) return
      if (!blob) { setError('The photo could not be captured. Please try again.'); return }
      setPhoto(blob); setCamera(false); setReady(false); stopCamera()
    }, 'image/jpeg', .85)
  }

  async function save() {
    if (!photo || !agreed || busy || needsRetake || dailyLimit || cooldown > 0) return
    setBusy(true); setError('')
    try {
      await api.captureSelfie(photo, config.selfie_notice_version ?? 'selfie-1')
      if (live.current) { setPending(true); setPhoto(null) }
    } catch (failure) {
      // A refusal of the photo itself (too large, not accepted, too many today) created nothing to reconcile.
      const refused = failure instanceof ApiError && [413, 422, 429].includes(failure.status)
      // A dropped response may still have created a durable pending capture.
      // Reconcile before offering another upload, so retries do not trap users. A storage outage on this
      // check stays here too: the capture screen, not a whole-screen refusal, shows it with the photo kept.
      let retryFailure = failure
      const state = refused ? null : await api.selfieState(true).catch(reconcileFailure => {
        if (reconcileFailure instanceof ApiError && reconcileFailure.status === 503) retryFailure = reconcileFailure
        return null
      })
      if (!live.current) return
      if (state?.pending_id) { setPending(true); setPhoto(null) }
      else if (state && !state.required && state.id !== initial?.id) onSaved()
      else {
        setError(inline(retryFailure))
        // A refused photo needs a retake; an account at its daily limit cannot submit even a new photo.
        if (failure instanceof ApiError && failure.status === 422) setNeedsRetake(true)
        if (failure instanceof ApiError && failure.status === 429) setDailyLimit(inline(failure))
        // A server fault invites a retry, but not a tap every second: a short pause keeps an outage from spending
        // the day's reservations.
        if (retryFailure instanceof ApiError && retryFailure.status === 503) {
          const current = Date.now()
          setNow(current); setRetryAt(current + 15_000)
        }
      }
    } finally {
      if (live.current) setBusy(false)
    }
  }

  return <Centred title={pending ? 'Saving your selfie' : 'Your enrollment selfie'}>
    {config.preview ? <p className="rounded-lg bg-amber-50 dark:bg-amber-950 p-3 text-sm text-amber-900 dark:text-amber-200">Selfie preview · Separate from staff training</p> : null}
    <Panel className="space-y-4">
      {pending ? <p role="status">Confirming your photo in private storage. This page will continue automatically when it is ready.</p> : <>
        <p className="text-sm text-neutral-700 dark:text-neutral-300">Take a clear photo of yourself, facing the camera in good light. Keep your whole face in the frame, with nobody else beside you.<Thai tone="inherit" className="mt-1">กรุณาถ่ายภาพใบหน้าของคุณในที่สว่าง หันหน้าเข้ากล้อง ให้ใบหน้าทั้งหมดอยู่ในกรอบ และไม่มีผู้อื่นในภาพ</Thai></p>
        <p className="text-sm text-neutral-700 dark:text-neutral-300">We check for one clear face before saving. A small copy appears beside your email and time in documents and videos. Company admins can view it with your activity.</p>
        <p className="text-sm text-neutral-500 dark:text-neutral-400">We ask again after 30 days or in a new browser. Replaced or expired photos are removed after {config.selfie_retention_days ?? 90} days; activity records remain. Face detection runs on Postern’s server. It does not match your identity or check that you are live in front of the camera.</p>
        <label className="flex items-start gap-3 text-sm">
          <input type="checkbox" className="mt-1 size-5" checked={agreed} disabled={camera || starting || busy} onChange={event => setAgreed(event.target.checked)} />
          <span>I agree to capture and use my selfie as described above.</span>
        </label>
        <video ref={video} aria-label="Camera preview" autoPlay muted playsInline onLoadedMetadata={() => setReady(true)}
          className={`aspect-square w-full rounded-xl bg-neutral-950 object-cover ${camera ? '' : 'hidden'}`} style={{ transform: 'scaleX(-1)' }} />
        {preview ? <img src={preview} alt="Your captured selfie" className="aspect-square w-full rounded-xl object-cover" /> : null}
        {photo ? <div className="flex flex-col gap-3 sm:flex-row">
          <Button variant="secondary" disabled={busy} onClick={() => void start()} className="flex-1"><IconRetake className="size-5" />Retake</Button>
          <Button disabled={busy || !agreed || needsRetake || !!dailyLimit || cooldown > 0} aria-describedby={dailyLimit ? 'selfie-error' : 'selfie-retry-status'} onClick={() => void save()} className="flex-1"><IconCheck className="size-5" />{busy ? 'Checking photo…' : 'Use this photo'}</Button>
        </div> : camera ? <Button disabled={!ready} onClick={take} className="w-full"><IconCamera className="size-5" />Take photo</Button>
          : <Button disabled={!agreed || starting} onClick={() => void start()} className="w-full"><IconCamera className="size-5" />{starting ? 'Starting camera…' : 'Open camera'}</Button>}
        <Message id="selfie-error" text={dailyLimit || error} />
        {/* Keep the live region mounted, but announce only the pause and its end, not every ticking second. */}
        <p id="selfie-retry-status" role="status" aria-live="polite" aria-atomic="true" className="sr-only">
          {retryAt && !dailyLimit ? cooldown > 0
            ? <>Please wait briefly before trying again.<Thai>กรุณารอสักครู่ก่อนลองอีกครั้ง</Thai></>
            : <>You can try again now.<Thai>ลองใหม่ได้แล้ว</Thai></> : null}
        </p>
        {!!retryAt && !dailyLimit ? <p aria-hidden="true" className="text-sm font-medium text-neutral-700 dark:text-neutral-300">
          {cooldown > 0 ? <>Try again in {cooldown} seconds.<Thai tone="inherit" className="mt-1">ลองใหม่ได้ในอีก {cooldown} วินาที</Thai></>
            : <>You can try again now.<Thai tone="inherit" className="mt-1">ลองใหม่ได้แล้ว</Thai></>}
        </p> : null}
        <p className="text-xs text-neutral-500 dark:text-neutral-400">Your camera is used only to take this photo. Nothing is uploaded until you choose “Use this photo”.</p>
      </>}
      {pending ? <Message text={error} /> : null}
      {onCancel && !pending ? <Button variant="secondary" disabled={busy} onClick={onCancel} className="w-full">Keep current selfie</Button> : null}
      {onAdmin ? <div className="space-y-2 border-t border-neutral-200 dark:border-neutral-800 pt-4"><p className="text-neutral-600 dark:text-neutral-300">Admins can manage posts and review progress without a selfie. Opening training materials still requires one.</p><Button variant="secondary" onClick={onAdmin} disabled={busy} className="w-full"><IconAdmin className="size-5" />Continue to admin without a selfie</Button></div> : null}
      <Button variant="quiet" onClick={onLeave} disabled={busy} className="w-full"><IconSignOut className="size-5" />Sign out</Button>
    </Panel>
  </Centred>
}

export function SelfieHistory() {
  const [open, setOpen] = useState(false)
  const [photos, setPhotos] = useState<SelfieRecord[] | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!open) return
    let live = true
    api.selfies().then(result => { if (live) { setPhotos(result.selfies); setError(result.cleanup_error ?? '') } })
      .catch(failure => { if (live) setError(inline(failure)) })
    return () => { live = false }
  }, [open])
  return <Panel className="space-y-3">
    <Button variant="quiet" onClick={() => setOpen(value => !value)}><IconEnrollmentPhotos className="size-5" />{open ? 'Hide enrollment photos' : 'Enrollment photos'}</Button>
    {open ? <>
      <Message text={error} />
      {photos === null && !error ? <p className="text-sm">Loading photos…</p> : null}
      {photos?.length === 0 ? <p className="text-sm text-neutral-500 dark:text-neutral-400">No retained enrollment photos.</p> : null}
      {photos?.map(photo => <div key={photo.id} className="flex items-center gap-3 border-t border-neutral-100 dark:border-neutral-800 pt-3">
        <SelfieImage id={photo.id} className="size-12 shrink-0" />
        <div className="min-w-0 text-sm"><p className="truncate">{photo.email}</p>
          <p className="text-xs text-neutral-500 dark:text-neutral-400">Captured {moment(photo.captured_at)} · {photo.retired_at ? `retired ${moment(photo.retired_at)}` : `valid until ${moment(photo.expires_at)}`}</p>
        </div>
      </div>)}
    </> : null}
  </Panel>
}
