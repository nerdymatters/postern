/// <reference types="node" />
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { accounts } from './accounts-api'
import { publishedTerms } from './published-terms'
import { AccountTerms } from './terms'

// Value: protects=unavailable account Terms can recover to the exact active revision while published Terms remain readable;
// fails_when=retry is absent or the required revision link uses the wrong URL;
// why_new=existing page coverage covers network failure and inactive acceptance only; seam=none;
// no_claim=deployed account configuration or legal HTTP headers (mocked config).
test('unavailable account Terms retry to the active revision without replacing the published document', async () => {
  const current = { version: 'next', sha256: 'b'.repeat(64), url: 'https://example.invalid/legal/next.html' }
  const config = vi.spyOn(accounts, 'config')
    .mockResolvedValueOnce({ google_client_id: '', registration_open: false, terms: null, terms_unavailable: true })
    .mockResolvedValueOnce({ google_client_id: '', registration_open: false, terms: current })
  render(<AccountTerms />)
  await screen.findByText('Account Terms status is temporarily unavailable. Please try again later.')
  expect(screen.getByRole('link', { name: `Terms of Service (version ${publishedTerms.version})` }).getAttribute('href')).toBe(publishedTerms.url)
  fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
  const required = await screen.findByRole('link', { name: 'Terms of Service (version next)' })
  expect(required.getAttribute('href')).toBe(current.url)
  expect(required.getAttribute('rel')).toBe('noopener noreferrer')
  expect(screen.getByRole('link', { name: `Terms of Service (version ${publishedTerms.version})` }).getAttribute('href')).toBe(publishedTerms.url)
  expect(screen.queryByText('Account Terms status is temporarily unavailable. Please try again later.')).toBeNull()
  expect(config).toHaveBeenCalledTimes(2)
})

test('the independent legal page matches its acceptance hash and approved text', () => {
  const bytes = readFileSync(`public/legal/terms/${publishedTerms.version}.html`)
  expect(createHash('sha256').update(bytes).digest('hex')).toBe(publishedTerms.sha256)
  const page = new DOMParser().parseFromString(bytes.toString(), 'text/html')
  const approved = readFileSync(`../../docs/terms/${publishedTerms.version}.md`, 'utf8')
    .replace(/^#+ /gm, '').replace(/\*\*/g, '').replace(/\s+/g, ' ').trim()
  expect(page.querySelector('main')!.textContent!.replace(/\s+/g, ' ').trim()).toBe(approved)
  expect(page.querySelector('script')).toBeNull()
  expect(page.querySelector('link[rel="canonical"]')!.getAttribute('href')).toBe(publishedTerms.url)
})

test('published Terms stay readable while the account API is unavailable, with retry', async () => {
  const config = vi.spyOn(accounts, 'config').mockRejectedValueOnce(new Error('Offline'))
    .mockResolvedValueOnce({ google_client_id: '', registration_open: false, terms: null })
  render(<AccountTerms />)
  expect(screen.getByRole('link', { name: /Terms of Service/ }).getAttribute('href')).toBe(publishedTerms.url)
  fireEvent.click(await screen.findByRole('button', { name: 'Try again' }))
  await screen.findByText('Account acceptance is not active yet. New registration remains closed.')
  expect(config).toHaveBeenCalledTimes(2)
})
