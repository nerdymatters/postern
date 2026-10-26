import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { inAppBrowser } from './browser'
import { Landing } from './landing'
import { world } from './testing'

const MESSENGER = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 [FBAN/MessengerForiOS;FBAV/470.0]'
const SAFARI = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1'

function agent(value: string) {
  Object.defineProperty(window.navigator, 'userAgent', { value, configurable: true })
}

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
