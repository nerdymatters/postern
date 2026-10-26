import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { expect, test } from 'vitest'
import { Consent } from './consent'
import { calls, installApi, world } from './testing'

const consents = () => calls.filter(call => call.url.endsWith('/api/consent'))
const activity = () => screen.getByRole('checkbox', { name: /sign-ins/ })
const intellectualProperty = () => screen.getByRole('checkbox', { name: /intellectual property restrictions/ })

test('both independent agreements are required, sent explicitly, and a changed notice resets both agreements', async () => {
  const state = world()
  installApi(state)
  let accepted = 0
  let declined = 0
  const first = render(<Consent config={state.config} onAccepted={() => { accepted++ }} onDeclined={() => { declined++ }} />)

  screen.getByText(state.config.notice)
  screen.getByText(state.config.ip_notice)
  const enter = screen.getByRole('button', { name: /^Agree\ and\ start\ training/ })
  // English only until the agreements themselves are translated (owner decision D10).
  expect(enter.textContent).toBe('Agree and start training')
  expect(enter).toHaveProperty('disabled', true)
  fireEvent.click(enter)
  expect(consents()).toHaveLength(0)

  fireEvent.click(activity())
  expect(enter).toHaveProperty('disabled', true)
  fireEvent.click(intellectualProperty())
  expect(enter).toHaveProperty('disabled', false)
  fireEvent.click(activity())
  expect(enter).toHaveProperty('disabled', true)
  fireEvent.click(enter)
  expect(consents()).toHaveLength(0)
  fireEvent.click(activity())
  fireEvent.click(enter)
  await waitFor(() => expect(accepted).toBe(1))
  expect(consents()).toHaveLength(1)
  expect(JSON.parse(consents()[0].body)).toEqual({ version: '1', ip_version: 'ip-1', monitoring_accepted: true, ip_accepted: true })

  fireEvent.click(screen.getByRole('button', { name: 'Not now' }))
  expect(declined).toBe(1)
  first.unmount()

  state.refuse = { status: 409, error: 'notice_changed', message: 'Please review the latest agreements.', path: /\/api\/consent$/ }
  render(<Consent config={state.config} onAccepted={() => { accepted++ }} onDeclined={() => { declined++ }} />)
  fireEvent.click(activity())
  fireEvent.click(intellectualProperty())
  fireEvent.click(screen.getByRole('button', { name: /^Agree\ and\ start\ training/ }))
  await waitFor(() => screen.getByRole('alert'))
  expect(screen.getByRole('alert').textContent).toContain('The agreements changed.')
  expect(accepted).toBe(1)
  expect(screen.getByRole('button', { name: /^Agree\ and\ start\ training/ })).toHaveProperty('disabled', true)
  expect(activity()).toHaveProperty('checked', false)
  expect(intellectualProperty()).toHaveProperty('checked', false)
})
