import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { App } from './app'
import { api, ApiError, onRefusal, setToken } from './api'
import { SelfieEnrollment } from './selfie'
import { calls, installApi, world } from './testing'

const settings = { ...world().config, selfie_enabled: true, selfie_notice_version: 'selfie-1', selfie_retention_days: 90 }
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

function camera(getUserMedia: () => Promise<MediaStream>) {
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } })
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
}

function consent() { fireEvent.click(screen.getByRole('checkbox')) }

test('camera requires consent, denial is recoverable, and no photo uploads', async () => {
  const capture = vi.spyOn(api, 'captureSelfie')
  const get = vi.fn().mockRejectedValue(new DOMException('Denied', 'NotAllowedError'))
  camera(get)
  render(<SelfieEnrollment config={settings} onSaved={vi.fn()} onLeave={vi.fn()} />)
  expect((screen.getByRole('button', { name: /^Open\ camera/ }) as HTMLButtonElement).disabled).toBe(true)
  expect(get).not.toHaveBeenCalled()
  consent()
  fireEvent.click(screen.getByRole('button', { name: /^Open\ camera/ }))
  await screen.findByText(/Camera access was denied/)
  expect(capture).not.toHaveBeenCalled()
  expect(screen.getByText(/90 days/)).toBeTruthy()
  // The instructions put their Thai on its own lang-tagged line, like every refusal below them.
  expect(screen.getByText('กรุณาถ่ายภาพใบหน้าของคุณในที่สว่าง หันหน้าเข้ากล้อง ให้ใบหน้าทั้งหมดอยู่ในกรอบ และไม่มีผู้อื่นในภาพ').getAttribute('lang')).toBe('th')
})

test('late camera permission after leaving stops every track', async () => {
  let resolve!: (value: MediaStream) => void
  const stop = vi.fn()
  camera(() => new Promise(done => { resolve = done }))
  const mounted = render(<SelfieEnrollment config={settings} onSaved={vi.fn()} onLeave={vi.fn()} />)
  consent()
  fireEvent.click(screen.getByRole('button', { name: /^Open\ camera/ }))
  mounted.unmount()
  await act(async () => resolve({ getTracks: () => [{ stop }] } as unknown as MediaStream))
  expect(stop).toHaveBeenCalledOnce()
})

test.each([false, true])('capture stops camera; retake does not upload; confirmation waits for cloud (lost response: %s)', async lost => {
  const stop = vi.fn()
  camera(async () => ({ getTracks: () => [{ stop }] }) as unknown as MediaStream)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['photo'], { type: 'image/jpeg' })))
  const capture = vi.spyOn(api, 'captureSelfie').mockResolvedValue({ pending_id: 'pending' })
  if (lost) capture.mockRejectedValueOnce(new Error('Upload response was lost'))
  const status = vi.spyOn(api, 'selfieState').mockResolvedValue({ enabled: true, required: true, pending_id: 'pending', error: 'Private backup is delayed.' })
  const saved = vi.fn()
  const mounted = render(<SelfieEnrollment config={settings} onSaved={saved} onLeave={vi.fn()} />)
  consent()
  fireEvent.click(screen.getByRole('button', { name: /^Open\ camera/ }))
  const video = screen.getByLabelText('Camera preview')
  Object.defineProperties(video, { videoWidth: { value: 640 }, videoHeight: { value: 480 } })
  await screen.findByRole('button', { name: 'Take photo' })
  fireEvent.loadedMetadata(video)
  fireEvent.click(screen.getByRole('button', { name: 'Take photo' }))
  await screen.findByAltText('Your captured selfie')
  expect(stop).toHaveBeenCalledOnce()
  expect(capture).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Retake' }))
  await screen.findByRole('button', { name: 'Take photo' })
  fireEvent.loadedMetadata(video)
  fireEvent.click(screen.getByRole('button', { name: 'Take photo' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Use this photo' }))
  await screen.findByText('Saving your selfie')
  await waitFor(() => expect(status).toHaveBeenCalled())
  expect(saved).not.toHaveBeenCalled()
  expect(capture).toHaveBeenCalledOnce()
  status.mockResolvedValue({ enabled: true, required: false, id: 'confirmed' })
  await waitFor(() => expect(saved).toHaveBeenCalledOnce(), { timeout: 3000 })
  mounted.unmount()
  expect(stop).toHaveBeenCalledTimes(2)
})

test('session gate never fetches content before selfie enrollment', async () => {
  const state = world()
  state.config = settings
  state.session.selfie = { enabled: true, required: true }
  installApi(state)
  setToken('synthetic-token')
  render(<App />)
  await screen.findByText('Your enrollment selfie')
  expect(calls.filter(call => call.url === '/api/posts')).toHaveLength(0)
  expect(calls.filter(call => call.url.endsWith('.jpg'))).toHaveLength(0)
})

// Value: protects=every face the check refuses, including outcomes the server adds later, needs a new photo, a
//   refusal of the photo itself skips the state check, and either server fault (detector or storage) keeps the photo
//   usable; fails_when=the web app lists face codes again, reconciles after a definite refusal, or a storage fault
//   loses the photo; why_new=the code list was a third hand-kept copy; seam=none
test.each(['face_required', 'multiple_faces', 'face_too_small', 'face_out_of_frame', 'face_blurry', 'invalid_selfie', 'face_check_unavailable', 'selfie_unavailable'])(
  '%s keeps the user on the capture screen with a recoverable message', async code => {
  camera(async () => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['photo'], { type: 'image/jpeg' })))
  const unavailable = code.endsWith('_unavailable')
  const capture = vi.spyOn(api, 'captureSelfie').mockRejectedValue(new ApiError(unavailable ? 503 : 422, code, 'Try again with one clear face.'))
  const reconciled = vi.spyOn(api, 'selfieState').mockResolvedValue({ enabled: true, required: false, id: 'old' })
  const saved = vi.fn(), cancel = vi.fn()
  render(<SelfieEnrollment config={settings} initial={{ enabled: true, required: false, id: 'old' }}
    onSaved={saved} onCancel={cancel} onLeave={vi.fn()} />)
  consent()
  fireEvent.click(screen.getByRole('button', { name: /^Open camera/ }))
  const video = screen.getByLabelText('Camera preview')
  Object.defineProperties(video, { videoWidth: { value: 640 }, videoHeight: { value: 480 } })
  await screen.findByRole('button', { name: 'Take photo' })
  fireEvent.loadedMetadata(video)
  fireEvent.click(screen.getByRole('button', { name: 'Take photo' }))
  const submit = await screen.findByRole('button', { name: 'Use this photo' }) as HTMLButtonElement
  // Start a manually controlled clock after camera setup; flush the rejected save and its effect before ticking.
  // Mixing real-time auto-advance with a manual 15-second jump made the boundary assertion intermittent.
  vi.useFakeTimers()
  await act(async () => { fireEvent.click(submit) })
  expect(screen.getByText('Try again with one clear face.')).toBeTruthy()
  // Only an outage might have left a pending capture behind; a refused photo created nothing.
  expect(reconciled).toHaveBeenCalledTimes(unavailable ? 1 : 0)
  expect(saved).not.toHaveBeenCalled()
  expect(screen.getByAltText('Your captured selfie')).toBeTruthy()
  if (unavailable) {
    // A server fault invites a retry after a short pause, never a tap every second.
    expect((screen.getByRole('button', { name: 'Use this photo' }) as HTMLButtonElement).disabled).toBe(true)
    const announcement = screen.getByRole('status').textContent
    expect(announcement).toContain('Please wait briefly before trying again.')
    expect(screen.getByText(/Try again in \d+ seconds/).getAttribute('aria-hidden')).toBe('true')
    await act(async () => { vi.advanceTimersByTime(14_999) })
    expect(submit.disabled).toBe(true)
    expect(screen.getByRole('status').textContent).toBe(announcement)
    await act(async () => { vi.advanceTimersByTime(1) })
    expect(submit.disabled).toBe(false)
    expect(screen.getByRole('status').textContent).toContain('You can try again now.')
  } else {
    expect((screen.getByRole('button', { name: 'Use this photo' }) as HTMLButtonElement).disabled).toBe(true)
  }
  expect((screen.getByRole('button', { name: 'Keep current selfie' }) as HTMLButtonElement).disabled).toBe(false)
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Retake' })) })
  fireEvent.loadedMetadata(video)
  fireEvent.click(screen.getByRole('button', { name: 'Take photo' }))
  expect((screen.getByRole('button', { name: 'Use this photo' }) as HTMLButtonElement).disabled).toBe(false)
  expect(capture).toHaveBeenCalledOnce()
})

// Value: protects=a photo refused as too large or over the daily limit created nothing, so no state check runs; the
//   photo stays usable after a 413 but the main button does not invite a tap after a 429; fails_when=413 or 429
//   leaves the refused list, a reconcile after them hides the refusal behind onSaved, or a 429 leaves 'Use this photo'
//   enabled; why_new=the refusal table only rejected with 422 and 503; seam=none
test.each([[413, 'file_too_large', false], [429, 'rate_limited', true]])('%i %s keeps the photo and skips the state check', async (status, code, blocked) => {
  camera(async () => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['photo'], { type: 'image/jpeg' })))
  const capture = vi.spyOn(api, 'captureSelfie').mockRejectedValue(new ApiError(status, code, 'Refused.'))
  // An enrollment saved elsewhere: reconciling here would call onSaved and hide the refusal.
  const reconciled = vi.spyOn(api, 'selfieState').mockResolvedValue({ enabled: true, required: false, id: 'elsewhere' })
  const saved = vi.fn()
  render(<SelfieEnrollment config={settings} initial={{ enabled: true, required: false, id: 'old' }} onSaved={saved} onLeave={vi.fn()} />)
  consent()
  fireEvent.click(screen.getByRole('button', { name: /^Open camera/ }))
  const video = screen.getByLabelText('Camera preview')
  Object.defineProperties(video, { videoWidth: { value: 640 }, videoHeight: { value: 480 } })
  await screen.findByRole('button', { name: 'Take photo' })
  fireEvent.loadedMetadata(video)
  fireEvent.click(screen.getByRole('button', { name: 'Take photo' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Use this photo' }))
  expect((await screen.findByRole('alert')).textContent).toBe('Refused.')
  expect(reconciled).not.toHaveBeenCalled()
  expect(saved).not.toHaveBeenCalled()
  expect((screen.getByRole('button', { name: 'Use this photo' }) as HTMLButtonElement).disabled).toBe(blocked)
  if (blocked) {
    expect(screen.getByRole('status').textContent).toBe('')
    expect(screen.getByRole('button', { name: 'Use this photo' }).getAttribute('aria-describedby')).toBe(screen.getByRole('alert').id)
    expect(screen.getAllByText('Refused.')).toHaveLength(1)
  }
  fireEvent.click(screen.getByRole('button', { name: 'Retake' }))
  await screen.findByRole('button', { name: 'Take photo' })
  fireEvent.loadedMetadata(video)
  fireEvent.click(screen.getByRole('button', { name: 'Take photo' }))
  expect((await screen.findByRole('button', { name: 'Use this photo' }) as HTMLButtonElement).disabled).toBe(blocked)
  if (blocked) {
    expect(screen.getByRole('alert').textContent).toBe('Refused.')
    fireEvent.click(screen.getByRole('button', { name: 'Use this photo' }))
    expect(capture).toHaveBeenCalledOnce()
  }
})

// Value: protects=a save whose response was lost, but which did enroll, continues without an error; fails_when=the
//   reconcile stops recognizing a new enrollment id or shows it as an error; why_new=the lost-response test only
//   reconciles to a pending capture; seam=none
test('a lost response that did enroll continues without an error', async () => {
  camera(async () => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['photo'], { type: 'image/jpeg' })))
  vi.spyOn(api, 'captureSelfie').mockRejectedValue(new Error('Upload response was lost'))
  vi.spyOn(api, 'selfieState').mockResolvedValue({ enabled: true, required: false, id: 'new' })
  const saved = vi.fn()
  render(<SelfieEnrollment config={settings} initial={{ enabled: true, required: false, id: 'old' }} onSaved={saved} onLeave={vi.fn()} />)
  consent()
  fireEvent.click(screen.getByRole('button', { name: /^Open camera/ }))
  const video = screen.getByLabelText('Camera preview')
  Object.defineProperties(video, { videoWidth: { value: 640 }, videoHeight: { value: 480 } })
  await screen.findByRole('button', { name: 'Take photo' })
  fireEvent.loadedMetadata(video)
  fireEvent.click(screen.getByRole('button', { name: 'Take photo' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Use this photo' }))
  await waitFor(() => expect(saved).toHaveBeenCalledOnce())
  expect(screen.queryByRole('alert')).toBeNull()
})


test('cancelled renewal does not report the previous selfie as newly saved', async () => {
  const saved = vi.fn()
  vi.spyOn(api, 'selfieState').mockResolvedValue({ enabled: true, required: false, id: 'old' })
  render(<SelfieEnrollment config={settings} initial={{ enabled: true, required: false, id: 'old', pending_id: 'replacement' }} onSaved={saved} onLeave={vi.fn()} />)
  await screen.findByText('The enrollment could not be completed. Please take a new photo.')
  expect(saved).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: /^Open\ camera/ })).toBeTruthy()
})


test('optional portrait storage failure stays local while expired authentication remains global', async () => {
  const state = world({ refuse: { status: 503, error: 'selfie_unavailable', message: 'Private photo unavailable.' } })
  installApi(state)
  const announce = vi.fn()
  onRefusal(announce)
  await expect(api.selfieImage('portrait')).rejects.toMatchObject({ status: 503, code: 'selfie_unavailable' })
  expect(announce).not.toHaveBeenCalled()
  state.refuse = { status: 401, error: 'sign_in_required', message: 'Sign in again.' }
  await expect(api.selfieImage('portrait')).rejects.toMatchObject({ code: 'sign_in_required' })
  expect(announce).toHaveBeenCalledOnce()
  onRefusal(() => {})
})

// Value: protects=a storage outage during the save, and on the state check that follows it, both stay on the capture
//   screen with the photo kept and the message in both languages; fails_when=the reconcile request lets 503
//   selfie_unavailable announce a whole-screen refusal; why_new=the api-level test covers the upload call only;
//   seam=none (the fake API refuses both calls)
test('a storage outage on the save and on the state check keeps the capture screen', async () => {
  installApi(world({ refuse: { status: 503, error: 'selfie_unavailable', message: 'Your selfie could not be saved yet. / ยังบันทึกเซลฟีของคุณไม่ได้', path: /\/api\/selfie$/ } }))
  const announce = vi.fn()
  onRefusal(announce)
  camera(async () => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['photo'], { type: 'image/jpeg' })))
  const saved = vi.fn()
  render(<SelfieEnrollment config={settings} onSaved={saved} onLeave={vi.fn()} />)
  consent()
  fireEvent.click(screen.getByRole('button', { name: /^Open camera/ }))
  const video = screen.getByLabelText('Camera preview')
  Object.defineProperties(video, { videoWidth: { value: 640 }, videoHeight: { value: 480 } })
  await screen.findByRole('button', { name: 'Take photo' })
  fireEvent.loadedMetadata(video)
  fireEvent.click(screen.getByRole('button', { name: 'Take photo' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Use this photo' }))
  await screen.findByText(/Your selfie could not be saved yet/)
  expect((await screen.findByText('ยังบันทึกเซลฟีของคุณไม่ได้')).getAttribute('lang')).toBe('th')
  expect(announce).not.toHaveBeenCalled()
  expect(saved).not.toHaveBeenCalled()
  expect(screen.getByAltText('Your captured selfie')).toBeTruthy()
  expect((screen.getByRole('button', { name: 'Use this photo' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByRole('status').textContent).toContain('Please wait briefly before trying again.')
  onRefusal(() => {})
})

// Value: protects=outages on either request pause retries across retakes, with accessible bilingual feedback,
//   and time spent in a backgrounded phone browser counts toward the pause; fails_when=Retake resets it,
//   reconcile errors are discarded, or the timer counts callbacks rather than elapsed time; seam=clock and API
test.each(['capture', 'reconcile'])('a %s outage keeps its pause across retakes and a suspended browser', async source => {
  camera(async () => ({ getTracks: () => [{ stop: vi.fn() }] }) as unknown as MediaStream)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(callback => callback(new Blob(['photo'], { type: 'image/jpeg' })))
  vi.useFakeTimers({ shouldAdvanceTime: true })
  const outage = new ApiError(503, 'selfie_unavailable', 'Storage is unavailable. / พื้นที่จัดเก็บข้อมูลไม่พร้อมใช้งาน')
  const capture = vi.spyOn(api, 'captureSelfie').mockRejectedValue(source === 'capture' ? outage : new Error('Lost response'))
  const state = vi.spyOn(api, 'selfieState').mockResolvedValue({ enabled: true, required: true })
  if (source === 'reconcile') state.mockRejectedValue(outage)
  render(<SelfieEnrollment config={settings} onSaved={vi.fn()} onCancel={vi.fn()} onLeave={vi.fn()} />)
  consent()
  fireEvent.click(screen.getByRole('button', { name: /^Open camera/ }))
  const video = screen.getByLabelText('Camera preview')
  Object.defineProperties(video, { videoWidth: { value: 640 }, videoHeight: { value: 480 } })
  await screen.findByRole('button', { name: 'Take photo' })
  fireEvent.loadedMetadata(video)
  fireEvent.click(screen.getByRole('button', { name: 'Take photo' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Use this photo' }))
  await screen.findByText('Storage is unavailable.')
  const status = screen.getByRole('status')
  expect(status.getAttribute('aria-live')).toBe('polite')
  expect(status.getAttribute('aria-atomic')).toBe('true')
  expect(status.textContent).toContain('Please wait briefly before trying again.')
  expect(status.querySelector('[lang="th"]')?.textContent).toBe('กรุณารอสักครู่ก่อนลองอีกครั้ง')
  expect(screen.getByRole('button', { name: 'Use this photo' }).getAttribute('aria-describedby')).toBe(status.id)
  expect((screen.getByRole('button', { name: 'Keep current selfie' }) as HTMLButtonElement).disabled).toBe(false)
  expect((screen.getByRole('button', { name: 'Sign out' }) as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Retake' }))
  await screen.findByRole('button', { name: 'Take photo' })
  fireEvent.loadedMetadata(video)
  fireEvent.click(screen.getByRole('button', { name: 'Take photo' }))
  const submit = await screen.findByRole('button', { name: 'Use this photo' }) as HTMLButtonElement
  expect(submit.disabled).toBe(true)
  fireEvent.click(submit)
  expect(capture).toHaveBeenCalledOnce()
  // Move wall time without running callbacks, as a suspended browser does, then return to the page.
  vi.setSystemTime(Date.now() + 16_000)
  if (source === 'capture') fireEvent(document, new Event('visibilitychange'))
  else fireEvent(window, new Event('focus'))
  expect(submit.disabled).toBe(false)
  expect(status.textContent).toContain('You can try again now.')
  expect(status.querySelector('[lang="th"]')?.textContent).toBe('ลองใหม่ได้แล้ว')
})

// Value: protects=a storage or disk fault while saving a selfie is the capture screen's to show, with the photo kept,
//   instead of taking over the whole screen; fails_when=captureSelfie lets 503 selfie_unavailable become a global
//   refusal again; why_new=only the portrait fetch was kept local; seam=none
test('a capture storage outage stays on the capture screen instead of taking it over', async () => {
  const state = world({ refuse: { status: 503, error: 'selfie_unavailable', message: 'Your selfie could not be saved yet. / ยังบันทึกเซลฟีของคุณไม่ได้' } })
  installApi(state)
  const announce = vi.fn()
  onRefusal(announce)
  await expect(api.captureSelfie(new Blob(['photo'], { type: 'image/jpeg' }), 'selfie-1')).rejects.toMatchObject({ status: 503, code: 'selfie_unavailable' })
  expect(announce).not.toHaveBeenCalled()
  onRefusal(() => {})
})


test('unenrolled admins can manage posts while the reading room stays gated', async () => {
  const state = world()
  state.config = settings
  state.session.role = 'admin'
  state.session.selfie = { enabled: true, required: true }
  installApi(state)
  setToken('synthetic-admin')
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'Continue to admin without a selfie' }))
  await screen.findByRole('heading', { name: 'Admin' })
  expect(calls.some(call => call.url === '/api/admin/posts')).toBe(true)
  expect(calls.some(call => call.url === '/api/posts')).toBe(false)
  fireEvent.click(screen.getByRole('link', { name: 'Postern' }))
  await screen.findByText('Your enrollment selfie')
  expect(calls.some(call => call.url === '/api/posts')).toBe(false)
})

test('reader enrollment never offers the admin bypass', async () => {
  const state = world()
  state.config = settings
  state.session.selfie = { enabled: true, required: true }
  installApi(state)
  setToken('synthetic-reader')
  window.history.pushState(null, '', '/admin')
  render(<App />)
  await screen.findByText('Your enrollment selfie')
  expect(screen.queryByRole('button', { name: 'Continue to admin without a selfie' })).toBeNull()
  expect(calls.some(call => call.url.startsWith('/api/admin'))).toBe(false)
})
