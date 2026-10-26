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
  expect(calls).toHaveLength(0)
})
