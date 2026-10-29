import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { App } from './app'
import { Refusal, setToken } from './api'
import { installApi, world, type World } from './testing'
import { Refused } from './refusal'

test.each(['access_denied', 'authorization_source_unavailable', 'audit_unavailable', 'selfie_unavailable'] as const)('%s has Thai guidance and working recovery controls', code => {
  const retry = vi.fn(), signOut = vi.fn()
  render(<Refused refusal={new Refusal(code, 503, 'Please try again. / กรุณาลองใหม่')} onRetry={retry} onSignOut={signOut} />)
  expect(screen.getByRole('heading').querySelector('[lang="th"]')?.textContent).toBeTruthy()
  expect(document.querySelector('section p:last-child [lang="th"]')?.textContent).toBeTruthy()
  for (const [name, callback] of [['Try again', retry], ['Sign out', signOut]] as const) {
    const button = screen.getByRole('button', { name: new RegExp(name) })
    expect(within(button).getByText(/^[ก-๛]/).getAttribute('lang')).toBe('th')
    fireEvent.click(button)
    expect(callback).toHaveBeenCalledOnce()
  }
})

function signedIn(refuse: World['refuse']) {
  const state = world({ refuse })
  installApi(state)
  setToken('session-token')
  render(<App />)
  return state
}

test('401 sends the reader back to the landing page to sign in again', async () => {
  signedIn({ status: 401, error: 'sign_in_required', message: 'Please sign in to continue.' })
  await waitFor(() => screen.getByText('Please sign in again.'))
  screen.getByRole('button', { name: 'Continue' })
})

test('403 access_denied says the access is gone', async () => {
  signedIn({ status: 403, error: 'access_denied', message: 'Your access is no longer available.' })
  await waitFor(() => screen.getByText('Your access is no longer available'))
  screen.getByRole('button', { name: /^Try\ again/ })
})

test('403 consent_required shows the monitoring notice', async () => {
  signedIn({ status: 403, error: 'consent_required', message: 'Please review the monitoring notice before entering.', path: /\/api\/posts/ })
  await waitFor(() => screen.getByText('Before you start'))
  screen.getByRole('button', { name: /^Agree\ and\ start\ training/ })
})

test('503 authorization_source_unavailable shows the paused state', async () => {
  signedIn({ status: 503, error: 'authorization_source_unavailable', message: 'Access temporarily paused while we verify permissions.' })
  await waitFor(() => screen.getByText('Access temporarily paused while we verify permissions'))
})

test('503 audit_unavailable says the audit record could not be saved', async () => {
  signedIn({ status: 503, error: 'audit_unavailable', message: 'Content is paused because its audit record could not be saved.' })
  await waitFor(() => screen.getByText('Content is paused because its audit record could not be saved'))
})

test('an ordinary error is not one of the four and never takes the screen', async () => {
  signedIn({ status: 409, error: 'preparing', message: 'This attachment is still preparing.', path: /\/api\/posts/ })
  await waitFor(() => screen.getByText('Signed in as maria.santos@example.invalid'))
  expect(screen.queryByText('Your access is no longer available')).toBeNull()
  expect(screen.queryByRole('button', { name: /^Try\ again/ })).toBeNull()
})

// Value: protects=a bilingual refusal shows its Thai on its own lang-tagged line, as every other message does;
//   fails_when=the Refused page prints the raw "English / ไทย" string again; why_new=only Message split it; seam=none
test('a bilingual refusal puts its Thai on its own line', async () => {
  signedIn({ status: 503, error: 'selfie_unavailable', message: 'Your private photo is temporarily unavailable. / รูปภาพส่วนตัวของคุณไม่พร้อมใช้งานชั่วคราว', path: /\/api\/posts/ })
  const thai = await screen.findByText('รูปภาพส่วนตัวของคุณไม่พร้อมใช้งานชั่วคราว')
  expect(thai.getAttribute('lang')).toBe('th')
  expect(screen.queryByText(/ \/ /)).toBeNull()
})

test('503 selfie_unavailable keeps a cloud portrait outage retryable', async () => {
  signedIn({ status: 503, error: 'selfie_unavailable', message: 'Your private photo is temporarily unavailable.', path: /\/api\/posts/ })
  await waitFor(() => screen.getByText('Your enrollment photo is temporarily unavailable'))
  screen.getByRole('button', { name: /^Try\ again/ })
  expect(screen.queryByText('Your access is no longer available')).toBeNull()
})
