import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { App } from './app'
import { calls } from './testing'

test('the privacy page is public and needs no server', async () => {
  window.history.pushState(null, '', '/privacy')
  globalThis.fetch = (() => Promise.reject(new Error('server down'))) as typeof fetch
  render(<App />)
  expect(screen.getByText('What is recorded')).toBeTruthy()
  expect(screen.getByText(/Google API Services\s+User Data Policy/)).toBeTruthy()
  // Value: protects=staff are told exports can leave the office computer and Drive; fails_when=the sentence is dropped while Export report remains; why_new=the page's storage claim was untested; seam=none; no_claim=legal sufficiency of the wording
  expect(screen.getByText(/Company administrators can also save staff progress reports/)).toBeTruthy()
  expect(calls).toHaveLength(0)
})
