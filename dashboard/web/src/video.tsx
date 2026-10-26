import { useEffect, useRef, useState, type ReactNode } from 'react'
import { api, inline, type Item } from './api'
import { SelfieImage } from './selfie'
import { openView } from './views'
import { Button, Label, Message, Thai } from './ui'
import { EndOfContent, videoFinished } from './guide'
import { IconArrowRight } from './PosternIcons'
import { hold, release } from './blobs'

/** Only continuous playback counts: seeks, pauses and repeated sections add no coverage. */
export function playbackInterval(previous: { time: number; wall: number } | null, time: number, wall: number, rate: number): number[] | null {
  if (!previous) return null
  const delta = time - previous.time
  const elapsed = (wall - previous.wall) / 1000
  return delta > 0 && elapsed > 0 && elapsed < 4 && delta <= elapsed * rate + .5 ? [previous.time, time] : null
}

/** How long to wait for the server's watched figure after a video ends: the last report is sent, then
 *  the reading room refreshes. Watching split across two breaks is only complete in that figure, and a
 *  slow connection must not turn a full watch into a skip warning, so the wait is generous. */
export const SERVER_FIGURE_WAIT_MS = 8000

export function Video({ item, nextLabel, nextLabelTh = '', end, onNext, onProgress, requireFinish = false }: { item: Item; nextLabel: string; nextLabelTh?: string; end?: ReactNode; onNext: () => void; onProgress?: () => void; requireFinish?: boolean }) {
  const player = useRef<HTMLVideoElement>(null)
  const box = useRef<HTMLDivElement>(null)
  const previous = useRef<{ time: number; wall: number } | null>(null)
  const pending = useRef<number[][]>([])
  const flush = useRef<() => void>(() => {})
  const refreshed = useRef(onProgress)
  refreshed.current = onProgress
  const [media, setMedia] = useState<{ url: string; email: string; stamp: string; selfie_id?: string; subtitles?: string } | null>(null)
  const [captionUrl, setCaptionUrl] = useState('')
  const [captionsOn, setCaptionsOn] = useState(true)
  useEffect(() => {
    if (!media?.subtitles) return
    const url = hold(new Blob([media.subtitles], { type: 'text/vtt' }))
    setCaptionUrl(url)
    return () => { release(url) }
  }, [media?.subtitles])
  useEffect(() => {
    const tracks = player.current?.textTracks
    if (!tracks || !captionUrl) return
    for (const track of Array.from(tracks)) {
      if (track.language === 'en') track.mode = 'showing'
    }
    const sync = () => {
      const english = Array.from(tracks).find(track => track.language === 'en')
      if (english) setCaptionsOn(english.mode === 'showing')
    }
    tracks.addEventListener('change', sync)
    return () => tracks.removeEventListener('change', sync)
  }, [captionUrl])
  const setCaptions = (enabled: boolean) => {
    setCaptionsOn(enabled)
    for (const track of Array.from(player.current?.textTracks ?? [])) {
      if (track.language === 'en') track.mode = enabled ? 'showing' : 'disabled'
    }
  }
  const [error, setError] = useState('')
  const [hint, setHint] = useState('')
  const [ended, setEnded] = useState(false)
  const [playing, setPlaying] = useState(false)
  // Continuous playback this visit, so a full watch counts at once, before the server's figure returns.
  const watched = useRef<number[][]>([])
  const [local, setLocal] = useState(0)
  const duration = item.duration_seconds ?? 0
  const seen = Math.max(item.progress?.watched_seconds ?? 0, local)
  const share = duration ? Math.min(100, Math.floor(seen / duration * 100)) : null
  const complete = videoFinished(duration, seen)
  // Watching across two breaks is only complete in the server's figure, which arrives just after the
  // end is reported: wait for it before saying anything was skipped.
  const [checking, setChecking] = useState(false)
  const skipped = ended && requireFinish && !complete && !checking
  const warning = useRef<HTMLParagraphElement>(null)
  useEffect(() => {
    if (!checking) return
    const timer = window.setTimeout(() => setChecking(false), SERVER_FIGURE_WAIT_MS)
    return () => window.clearTimeout(timer)
  }, [checking])
  useEffect(() => { if (skipped) warning.current?.scrollIntoView?.({ block: 'nearest' }) }, [skipped])

  useEffect(() => {
    let live = true
    let busy = false
    let drain = false
    let viewId = ''
    const send = async () => {
      if (!viewId) return
      if (busy) { drain = true; return }
      busy = true
      const ranges = pending.current.splice(0, 32)
      try {
        await api.videoProgress(viewId, ranges)
        if (ranges.length) refreshed.current?.()
      } catch (failure) {
        // Keep playback the server never received (offline, busy); drop only ranges it rejected.
        const status = (failure as { status?: number }).status ?? 0
        if (status === 0 || status >= 500) pending.current.unshift(...ranges)
        if (live) {
          player.current?.pause()
          setError(inline(failure) || 'Playback paused. Reopen this video to continue.')
        }
      } finally {
        busy = false
        if (drain) { drain = false; if (pending.current.length) void send() }
      }
    }
    flush.current = () => { void send() }
    openView(item.id).then(async view => {
      if (!live) return
      viewId = view.id
      const prepared = await api.video(view.id)
      if (live) setMedia(prepared)
    }).catch(failure => { if (live) setError(inline(failure) || 'This video could not be opened.') })
    const timer = window.setInterval(() => { void send() }, 15000)
    const visibility = () => { if (document.hidden) { player.current?.pause(); void send() } }
    document.addEventListener('visibilitychange', visibility)
    const element = player.current
    return () => {
      live = false
      clearInterval(timer)
      document.removeEventListener('visibilitychange', visibility)
      void send()
      if (element) { element.pause(); element.removeAttribute('src'); element.load() }
    }
  }, [item.id])

  const sample = (includePaused = false) => {
    const video = player.current
    if (!video || video.seeking || (video.paused && !includePaused)) { previous.current = null; return }
    const now = { time: video.currentTime, wall: performance.now() }
    const range = playbackInterval(previous.current, now.time, now.wall, video.playbackRate)
    if (range) {
      setLocal(Math.floor(merge(watched.current, range)))
      const last = pending.current.at(-1)
      if (last && Math.abs(last[1] - range[0]) < .001) last[1] = range[1]
      else if (pending.current.length < 32) pending.current.push(range)
    }
    previous.current = now
  }
  return <div className="space-y-3">
    <div ref={box} className="relative overflow-hidden rounded-xl bg-black" onContextMenu={event => event.preventDefault()}>
      <video ref={player} aria-label={item.filename} src={error ? undefined : media?.url} controls playsInline preload="metadata"
        controlsList="nodownload nofullscreen noremoteplayback" disablePictureInPicture disableRemotePlayback
        className="block max-h-[80vh] w-full" onTimeUpdate={() => sample(true)}
        onPlay={() => { setPlaying(true); previous.current = player.current ? { time: player.current.currentTime < .05 ? 0 : player.current.currentTime, wall: performance.now() } : null; setEnded(false) }}
        onSeeking={() => { previous.current = null }} onSeeked={() => { previous.current = player.current && !player.current.paused ? { time: player.current.currentTime, wall: performance.now() } : null }}
        onPause={() => { setPlaying(false); sample(true); previous.current = null; flush.current() }}
        onEnded={() => { setPlaying(false); sample(true); previous.current = null; setEnded(true); setChecking(true); flush.current() }}
        onError={() => setError('This video could not be played. Reopen it to retry; if it persists, contact an administrator.')}>
        {captionUrl ? <track kind="subtitles" src={captionUrl} srcLang="en" label="English" default onLoad={() => setCaptions(captionsOn)} /> : null}
      </video>
      {media ? <span aria-label="Personal watermark" className="pointer-events-none absolute right-2 top-2 max-w-[90%] rounded bg-black/65 px-2 py-1 text-right text-[10px] leading-tight text-white">{media.selfie_id ? <SelfieImage id={media.selfie_id} className="float-left mr-2 size-7" /> : null}{media.email}<br />{media.stamp}</span> : !error ? <p className="p-8 text-center text-sm text-white">Preparing video…</p> : null}
    </div>
    {/* One region that is already mounted, so its changes are announced: checking, then the warning. */}
    {requireFinish ? <p role="status" className="sr-only">{skipped ? <>You skipped part of this video ({share ?? 0}% watched). Watch the rest to open the quiz. <span lang="th">คุณกรอข้ามบางช่วงของวิดีโอ กรุณาดูช่วงที่เหลือให้ครบ จึงจะทำแบบทดสอบได้</span></>
      : ended && !error && !complete ? <>Checking what you have watched… <span lang="th">กำลังตรวจส่วนที่ดูแล้ว…</span></> : null}</p> : null}
    {skipped ? <p ref={warning} aria-hidden="true" className="rounded-xl bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">You skipped part of this video ({share ?? 0}% watched). Watch the rest to open the quiz.
      <Thai tone="inherit" className="mt-1">คุณกรอข้ามบางช่วงของวิดีโอ (ดูแล้ว {share ?? 0}%) กรุณาดูช่วงที่เหลือให้ครบ จึงจะทำแบบทดสอบได้</Thai></p> : null}
    <div className="flex flex-wrap items-center justify-between gap-3">
      <Button variant="secondary" disabled={!media || !!error} onClick={() => {
        if (playing) player.current?.pause()
        else void player.current?.play().catch(() => setHint('Press play in the video controls to start.'))
      }}><Label en={playing ? 'Pause video' : ended ? 'Replay video' : 'Play video'} th={playing ? 'หยุดวิดีโอ' : ended ? 'ดูวิดีโออีกครั้ง' : 'เล่นวิดีโอ'} /></Button>
      {captionUrl ? <Button variant="secondary" aria-pressed={captionsOn} onClick={() => setCaptions(!captionsOn)}><Label en={`English subtitles: ${captionsOn ? 'On' : 'Off'}`} th={`คำบรรยายภาษาอังกฤษ: ${captionsOn ? 'เปิด' : 'ปิด'}`} /></Button> : null}
      <Button variant="quiet" onClick={() => {
        if (document.fullscreenElement) void document.exitFullscreen()
        else if (box.current?.requestFullscreen) void box.current.requestFullscreen().catch(() => setHint('Fullscreen is unavailable in this browser.'))
        else setHint('Fullscreen is unavailable in this browser.')
      }}><Label en="Full screen" th="เต็มจอ" /></Button>
    </div>
    <p className="text-sm text-neutral-600 dark:text-neutral-300">{duration ? `Watched ${clock(Math.min(seen, duration))} of ${clock(duration)}. Watch to the end without skipping; skipped parts do not count.` : 'Your watched sections are saved automatically.'}
      <Thai className="mt-0.5">{duration ? `ดูแล้ว ${clock(Math.min(seen, duration))} จาก ${clock(duration)} · ดูให้จบโดยไม่กรอข้าม ช่วงที่กรอข้ามจะไม่นับ` : 'ระบบบันทึกส่วนที่ดูแล้วให้อัตโนมัติ'}</Thai></p>
    <Message text={error || hint} />
    {ended && !error && !skipped ? <div className="border-t border-neutral-200 dark:border-neutral-800 pt-4">
      {requireFinish && !complete ? <p aria-hidden="true" className="text-center text-sm text-neutral-600 dark:text-neutral-300">Checking what you have watched…<Thai>กำลังตรวจส่วนที่ดูแล้ว…</Thai></p>
        : nextLabel ? <Button onClick={onNext} className="w-full"><Label en={nextLabel} th={nextLabelTh} /><IconArrowRight className="size-5 shrink-0" /></Button> : end ?? <EndOfContent />}
    </div> : null}
  </div>
}

/** Add one played range to this visit's ranges and return the seconds they cover. */
export function merge(ranges: number[][], range: number[]) {
  ranges.push([...range])
  ranges.sort((a, b) => a[0] - b[0])
  const joined: number[][] = []
  for (const [start, end] of ranges) {
    const last = joined.at(-1)
    if (last && start <= last[1] + .001) last[1] = Math.max(last[1], end)
    else joined.push([start, end])
  }
  ranges.splice(0, ranges.length, ...joined)
  return joined.reduce((total, [start, end]) => total + end - start, 0)
}

/** 75 seconds reads 1:15. */
export function clock(seconds: number) {
  const whole = Math.floor(seconds)
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
}
