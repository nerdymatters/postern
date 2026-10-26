import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { api } from './api'
import { playbackInterval, Video } from './video'
import { Reader } from './reader'
import { canvasOps, installApi, post, world } from './testing'

const videoItem = () => ({ ...post('lesson', 'Lesson').items[0], id: 'video-item', kind: 'video' as const,
  filename: 'Live9_16_26.mp4', pages: 0, duration_seconds: 100 })

test('continuous playback is measured, but seek jumps, stalls and backwards replay jumps are not', () => {
  expect(playbackInterval({ time: 10, wall: 1000 }, 10.5, 1500, 1)).toEqual([10, 10.5])
  expect(playbackInterval({ time: 10, wall: 1000 }, 11, 1500, 2)).toEqual([10, 11])
  expect(playbackInterval({ time: 10, wall: 1000 }, 90, 1500, 1)).toBeNull()
  expect(playbackInterval({ time: 10, wall: 1000 }, 5, 1500, 1)).toBeNull()
  expect(playbackInterval({ time: 10, wall: 1000 }, 15, 12000, 1)).toBeNull()
})

test('a direct attachment selection opens its video, with a personal overlay and next content after ending', async () => {
  const lesson = { ...post('lesson', 'Lesson'), category: 'Module 1' }
  lesson.items.push(videoItem())
  const next = { ...post('next', 'Next lesson'), category: 'Module 2' }
  installApi(world({ posts: [lesson, next] }))
  vi.spyOn(api, 'video').mockResolvedValue({ path: '/api/video/ticket', url: '/api/video/ticket', email: 'reader@example.invalid', stamp: '2026-09-22 12:00 UTC' })
  vi.spyOn(api, 'videoProgress').mockResolvedValue({ saved: true })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  render(<Reader post={lesson} posts={[lesson, next]} selectedItem="video-item" onBack={() => {}} onOpen={() => {}} />)
  await waitFor(() => screen.getByLabelText('Personal watermark'))
  const player = screen.getByLabelText('Live9_16_26.mp4', { selector: 'video' }) as HTMLVideoElement
  expect(player.src).toContain('/api/video/ticket')
  expect(screen.getByLabelText('Personal watermark').textContent).toContain('reader@example.invalid')
  expect(canvasOps.drawn).toBe(0)
  expect(screen.queryByRole('button', { name: /Next module:/ })).toBeNull()
  fireEvent.ended(player)
  screen.getByRole('button', { name: /Next module: Next lesson/ })
})

test('pause saves played sections and seek jumps do not enter the progress request', async () => {
  const item = videoItem()
  installApi(world())
  vi.spyOn(api, 'createView').mockResolvedValue({ id: 'video-view', item_id: item.id, post_id: item.post_id, attempt_id: 'attempt' })
  vi.spyOn(api, 'video').mockResolvedValue({ path: '/api/video/ticket', url: '/api/video/ticket', email: 'reader@example.invalid', stamp: 'time' })
  const save = vi.spyOn(api, 'videoProgress').mockResolvedValue({ saved: true })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  let wall = 0
  vi.spyOn(performance, 'now').mockImplementation(() => wall)
  render(<Video item={item} nextLabel="" onNext={() => {}} />)
  await waitFor(() => screen.getByLabelText('Personal watermark'))
  const player = screen.getByLabelText(item.filename) as HTMLVideoElement
  Object.defineProperty(player, 'paused', { configurable: true, value: false })
  fireEvent.play(player)
  wall = 1000; player.currentTime = 1; fireEvent.timeUpdate(player)
  fireEvent.seeking(player)
  player.currentTime = 90; fireEvent.seeked(player)
  wall = 2000; player.currentTime = 91; fireEvent.timeUpdate(player)
  await act(async () => fireEvent.pause(player))
  expect(save).toHaveBeenCalledWith('video-view', [[0, 1], [90, 91]])
})

test('leaving during an in-flight save drains the final watched section after it finishes', async () => {
  const item = videoItem()
  installApi(world())
  vi.spyOn(api, 'createView').mockResolvedValue({ id: 'video-view', item_id: item.id, post_id: item.post_id, attempt_id: 'attempt' })
  vi.spyOn(api, 'video').mockResolvedValue({ path: '/api/video/ticket', url: '/api/video/ticket', email: 'reader@example.invalid', stamp: 'time' })
  let finish!: (value: { saved: boolean }) => void
  const save = vi.spyOn(api, 'videoProgress').mockImplementationOnce(() => new Promise(resolve => { finish = resolve })).mockResolvedValue({ saved: true })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  let wall = 0
  vi.spyOn(performance, 'now').mockImplementation(() => wall)
  const rendered = render(<Video item={item} nextLabel="" onNext={() => {}} />)
  await waitFor(() => screen.getByLabelText('Personal watermark'))
  const player = screen.getByLabelText(item.filename) as HTMLVideoElement
  Object.defineProperty(player, 'paused', { configurable: true, value: false })
  fireEvent.play(player)
  wall = 1000; player.currentTime = 1; fireEvent.timeUpdate(player)
  fireEvent.pause(player)
  expect(save).toHaveBeenCalledTimes(1)
  fireEvent.play(player)
  wall = 2000; player.currentTime = 2; fireEvent.timeUpdate(player)
  rendered.unmount()
  await act(async () => finish({ saved: true }))
  expect(save).toHaveBeenCalledTimes(2)
  expect(save).toHaveBeenLastCalledWith('video-view', [[1, 2]])
})

test('fullscreen failure shows a message without discarding playable video', async () => {
  installApi(world())
  const item = videoItem()
  vi.spyOn(api, 'createView').mockResolvedValue({ id: 'video-view', item_id: item.id, post_id: item.post_id, attempt_id: 'attempt' })
  vi.spyOn(api, 'video').mockResolvedValue({ path: '/api/video/ticket', url: '/api/video/ticket', email: 'reader@example.invalid', stamp: 'time' })
  vi.spyOn(api, 'videoProgress').mockResolvedValue({ saved: true })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  render(<Video item={item} nextLabel="" onNext={() => {}} />)
  await waitFor(() => screen.getByLabelText('Personal watermark'))
  fireEvent.click(screen.getByRole('button', { name: /^Full\ screen/ }))
  await waitFor(() => screen.getByText('Fullscreen is unavailable in this browser.'))
  expect((screen.getByLabelText(item.filename) as HTMLVideoElement).src).toContain('/api/video/ticket')
})

test('failed player setup displays a recoverable inline error', async () => {
  installApi(world())
  vi.spyOn(api, 'createView').mockRejectedValue(new Error('offline'))
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  render(<Video item={videoItem()} nextLabel="" onNext={() => {}} />)
  await waitFor(() => screen.getByText('This video could not be opened.'))
  expect(screen.queryByText('Preparing video…')).toBeNull()
})

test('the final paused timeupdate retains the last played fraction at the end', async () => {
  installApi(world())
  const item = videoItem()
  vi.spyOn(api, 'createView').mockResolvedValue({ id: 'video-view', item_id: item.id, post_id: item.post_id, attempt_id: 'attempt' })
  vi.spyOn(api, 'video').mockResolvedValue({ path: '/api/video/ticket', url: '/api/video/ticket', email: 'reader@example.invalid', stamp: 'time' })
  const save = vi.spyOn(api, 'videoProgress').mockResolvedValue({ saved: true })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  let wall = 0
  vi.spyOn(performance, 'now').mockImplementation(() => wall)
  render(<Video item={item} nextLabel="" onNext={() => {}} />)
  await waitFor(() => screen.getByLabelText('Personal watermark'))
  const player = screen.getByLabelText(item.filename) as HTMLVideoElement
  Object.defineProperty(player, 'paused', { configurable: true, value: false })
  player.currentTime = .004
  fireEvent.play(player)
  wall = 1000; player.currentTime = 1; fireEvent.timeUpdate(player)
  Object.defineProperty(player, 'paused', { configurable: true, value: true })
  wall = 1250; player.currentTime = 1.25; fireEvent.timeUpdate(player)
  await act(async () => fireEvent.ended(player))
  expect(save).toHaveBeenLastCalledWith('video-view', [[0, 1.25]])
})

test('playback the server could not record is sent again, but ranges it rejected are not', async () => {
  const { ApiError, Refusal } = await import('./api')
  const item = videoItem()
  installApi(world())
  vi.spyOn(api, 'createView').mockResolvedValue({ id: 'video-view', item_id: item.id, post_id: item.post_id, attempt_id: 'attempt' })
  vi.spyOn(api, 'video').mockResolvedValue({ path: '/api/video/ticket', url: '/api/video/ticket', email: 'reader@example.invalid', stamp: 'time' })
  const save = vi.spyOn(api, 'videoProgress')
    .mockRejectedValueOnce(new Refusal('audit_unavailable', 503, 'Content is paused.'))
    .mockRejectedValueOnce(new ApiError(422, 'invalid_progress', 'Rejected.'))
    .mockResolvedValue({ saved: true })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  let wall = 0
  vi.spyOn(performance, 'now').mockImplementation(() => wall)
  render(<Video item={item} nextLabel="" onNext={() => {}} />)
  await waitFor(() => screen.getByLabelText('Personal watermark'))
  const player = screen.getByLabelText(item.filename) as HTMLVideoElement
  Object.defineProperty(player, 'paused', { configurable: true, value: false })
  const watch = async (from: number, to: number) => {
    fireEvent.play(player)
    wall += 1000; player.currentTime = from; fireEvent.timeUpdate(player)
    wall += 1000; player.currentTime = to; fireEvent.timeUpdate(player)
    await act(async () => fireEvent.pause(player))
  }
  await watch(0, 1)
  await watch(1, 2)
  await watch(2, 3)
  const sent = save.mock.calls.map(call => call[1])
  // The 503 batch comes back with the next report; the 422 batch is not retried.
  expect(sent[1]).toEqual(expect.arrayContaining(sent[0]))
  expect(sent[2]).not.toEqual(expect.arrayContaining(sent[1]))
})

test('English captions start on, follow both toggles, and revoke their private blob on leaving', async () => {
  installApi(world())
  const item = videoItem()
  vi.spyOn(api, 'createView').mockResolvedValue({ id: 'caption-view', item_id: item.id, post_id: item.post_id, attempt_id: 'attempt' })
  vi.spyOn(api, 'video').mockResolvedValue({ path: '/api/video/ticket', url: '/api/video/ticket', email: 'reader@example.invalid', stamp: 'time',
    subtitles: 'WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nWelcome.\n' })
  vi.spyOn(api, 'videoProgress').mockResolvedValue({ saved: true })
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  const english = { language: 'en', mode: 'disabled' }
  const events = new EventTarget()
  const tracks = Object.assign([english], { addEventListener: events.addEventListener.bind(events), removeEventListener: events.removeEventListener.bind(events) })
  vi.spyOn(HTMLMediaElement.prototype, 'textTracks', 'get').mockReturnValue(tracks as unknown as TextTrackList)
  const revoke = vi.spyOn(URL, 'revokeObjectURL')
  const mounted = render(<Video item={item} nextLabel="" onNext={() => {}} />)
  const toggle = await screen.findByRole('button', { name: /English subtitles: On/ })
  expect(toggle.getAttribute('aria-pressed')).toBe('true')
  expect(english.mode).toBe('showing')
  const track = mounted.container.querySelector('track')!
  expect(track.default).toBe(true)
  expect(track.srclang).toBe('en')
  expect(track.src).toMatch(/^blob:/)
  fireEvent.click(toggle)
  expect(english.mode).toBe('disabled')
  screen.getByRole('button', { name: /English subtitles: Off/ })
  act(() => { english.mode = 'showing'; events.dispatchEvent(new Event('change')) })
  screen.getByRole('button', { name: /English subtitles: On/ })
  const url = track.src
  mounted.unmount()
  expect(revoke).toHaveBeenCalledWith(url)
})
