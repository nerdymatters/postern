import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { inAppBrowser } from './browser'
import { Landing } from './landing'
import { world } from './testing'
import { publishedTerms } from './published-terms'

const MESSENGER = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 [FBAN/MessengerForiOS;FBAV/470.0]'
const SAFARI = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1'

function agent(value: string) {
  Object.defineProperty(window.navigator, 'userAgent', { value, configurable: true })
}

// Value: protects=anonymous landing readers can open the published hosted-account Terms;
// fails_when=the legal link is removed or points at a different revision;
// why_new=descriptor tests never render the landing entry point; seam=none;
// no_claim=destination HTTP availability or Vercel routing (DOM only).
test('the landing page links directly to the published hosted-account Terms', () => {
  agent(SAFARI)
  render(<Landing config={world().config} notice="" onSignedIn={() => {}} />)
  expect(screen.getByRole('link', { name: 'Hosted account Terms' }).getAttribute('href')).toBe(publishedTerms.url)
})

test('recognises the chat apps that block Google sign-in', () => {
  expect(inAppBrowser(MESSENGER)).toBe(true)
  expect(inAppBrowser('Mozilla/5.0 (iPhone) Instagram 300.1.0 (iPhone14,2; iOS 18_5)')).toBe(true)
  expect(inAppBrowser('Mozilla/5.0 (Linux; Android 14) Line/14.6.0')).toBe(true)
  expect(inAppBrowser('Mozilla/5.0 (iPhone) BytedanceWebview/d8a21c musical_ly_34.5.0')).toBe(true)
  expect(inAppBrowser(SAFARI)).toBe(false)
})

test('an in-app browser gets the open-in-Safari page instead of a sign-in box', () => {
  agent(MESSENGER)
  render(<Landing config={world().config} notice="" onSignedIn={() => {}} />)
  screen.getByText('Open in Safari or Chrome')
  expect(screen.queryByRole('button', { name: 'Continue' })).toBeNull()
})

test('a real browser in demo mode gets the email box', () => {
  agent(SAFARI)
  render(<Landing config={world().config} notice="" onSignedIn={() => {}} />)
  screen.getByRole('button', { name: 'Continue' })
  expect(screen.queryByText('Open in Safari or Chrome')).toBeNull()
})

afterEach(() => { vi.unstubAllGlobals() })

function clipboard(write: () => Promise<void>) {
  const writeText = vi.fn(write)
  vi.stubGlobal('navigator', Object.create(navigator, { clipboard: { value: { writeText } } }))
  return writeText
}

// Value: protects=a reader stuck in a chat app's browser can still take the link when copying is blocked;
//   fails_when=Copy the link fails silently with no manual copy field;
//   why_new=no test clicked Copy the link; seam=navigator.clipboard stubbed; no_claim=text selection on focus
test('in an in-app browser, a blocked clipboard offers the link for manual copy', async () => {
  agent(MESSENGER)
  clipboard(async () => { throw new Error('blocked') })
  render(<Landing config={world().config} notice="" onSignedIn={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: 'Copy the link' }))
  expect(await screen.findByLabelText('Link — select and copy')).toHaveProperty('value', window.location.href)
})

// Value: protects=a copy that works on a retry says so and takes the manual field away;
//   fails_when=the success label is lost, or the fallback stays up after a successful copy;
//   why_new=no test clicked Copy the link; seam=navigator.clipboard stubbed; no_claim=real clipboard access
test('in an in-app browser, a successful retry after a blocked copy says the link was copied', async () => {
  agent(MESSENGER)
  let blocked = true
  const writeText = clipboard(async () => { if (blocked) throw new Error('blocked') })
  render(<Landing config={world().config} notice="" onSignedIn={() => {}} />)
  fireEvent.click(screen.getByRole('button', { name: 'Copy the link' }))
  await screen.findByLabelText('Link — select and copy')
  blocked = false
  fireEvent.click(screen.getByRole('button', { name: 'Copy the link' }))
  await screen.findByRole('button', { name: 'Link copied' })
  expect(writeText).toHaveBeenCalledWith(window.location.href)
  expect(screen.queryByLabelText('Link — select and copy')).toBeNull()
})
