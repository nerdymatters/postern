import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { App } from './app'
import { BrandingProvider, publicBranding } from './branding'
import { CertificateCard, PrintableCertificate } from './honors'
import { Privacy } from './privacy'
import { installApi, world } from './testing'

afterEach(() => vi.unstubAllEnvs())

const identity = { organization_name: 'Example Academy', privacy_contact_email: 'privacy@example.invalid',
  certificate_title: 'Academy Course Certificate' }
const certificate = { number: 'EX-2026-00000001', name: 'Example Student', awarded_at: 1791326400, name_confirmed: true }

test('privacy retains public deployment identity when the API is unavailable', () => {
  vi.stubEnv('VITE_POSTERN_ORGANIZATION_NAME', 'Offline Academy')
  vi.stubEnv('VITE_POSTERN_PRIVACY_CONTACT_EMAIL', 'contact@example.invalid')
  window.history.pushState(null, '', '/privacy')
  globalThis.fetch = vi.fn().mockRejectedValue(new Error('offline'))
  render(<App />)
  expect(screen.getByText(/Offline Academy runs for its own staff/)).toBeTruthy()
  expect(screen.getByRole('link', { name: 'contact@example.invalid' }).getAttribute('href')).toBe('mailto:contact%40example.invalid')
  expect(screen.getByRole('link', { name: 'Licenses and notices' }).getAttribute('href')).toBe('/third-party-licenses.txt')
})

test('neutral privacy defaults have no operator email and remain useful', () => {
  render(<BrandingProvider value={publicBranding()}><Privacy /></BrandingProvider>)
  expect(screen.getByText(/administrator of this Postern installation/)).toBeTruthy()
  expect(document.querySelector('a[href^="mailto:"]')).toBeNull()
})

test('runtime organization is used on the landing page', async () => {
  const state = world()
  state.config.branding = identity
  installApi(state)
  render(<App />)
  await waitFor(() => expect(screen.getByText('Staff training for Example Academy: finish each chapter, then pass its quiz.')).toBeTruthy())
})

test('certificate issuer, seal and saved PDF title share the installation settings', () => {
  const before = document.title
  const { unmount } = render(<BrandingProvider value={identity}>
    <CertificateCard certificate={certificate} /><PrintableCertificate certificate={certificate} />
  </BrandingProvider>)
  expect(screen.getAllByText('Example Academy')).toHaveLength(2)
  expect(screen.getAllByText(/EXAMPLE ACADEMY • FINAL EXAM PASSED/)).toHaveLength(2)
  expect(document.title).toBe('Academy Course Certificate')
  unmount()
  expect(document.title).toBe(before)
})

test('branding stays text, rejects invalid contacts and supports bounded long organization names', () => {
  const organization = 'Example International Training and Learning Academy'
  const value = publicBranding({ organization_name: organization, privacy_contact_email: 'bad\n@example.invalid',
    certificate_title: 'Course Certificate' })
  expect(value.privacy_contact_email).toBe('')
  render(<BrandingProvider value={value}><CertificateCard certificate={certificate} /></BrandingProvider>)
  expect(screen.getByText(organization)).toBeTruthy()
})
