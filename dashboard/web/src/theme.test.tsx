import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { ThemeToggle, useTheme } from './theme'
import indexHtml from '../index.html?raw'

function Control() {
  const { dark, toggle } = useTheme()
  return <ThemeToggle dark={dark} onToggle={toggle} />
}

function systemTheme(dark: boolean) {
  const events = new EventTarget()
  const media = Object.assign(events, { matches: dark })
  vi.stubGlobal('matchMedia', () => media)
  return (next: boolean) => act(() => {
    media.matches = next
    media.dispatchEvent(new Event('change'))
  })
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  delete document.documentElement.dataset.theme
})

test('follows system changes until the reader chooses a theme', () => {
  const changeSystem = systemTheme(false)
  render(<Control />)
  expect(document.documentElement.dataset.theme).toBe('light')
  changeSystem(true)
  expect(document.documentElement.dataset.theme).toBe('dark')
  expect(screen.getByRole('button', { name: 'Toggle theme' }).getAttribute('aria-pressed')).toBe('true')
  fireEvent.click(screen.getByRole('button', { name: 'Toggle theme' }))
  expect(localStorage.getItem('postern-theme')).toBe('light')
  changeSystem(false)
  changeSystem(true)
  expect(document.documentElement.dataset.theme).toBe('light')
})

test('remembers the chosen theme across remounts and toggles back', () => {
  systemTheme(false)
  const first = render(<Control />)
  fireEvent.click(screen.getByRole('button', { name: 'Toggle theme' }))
  expect(document.documentElement.dataset.theme).toBe('dark')
  first.unmount()
  render(<Control />)
  expect(document.documentElement.dataset.theme).toBe('dark')
  expect(screen.getByTitle('Switch to day mode')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Toggle theme' }))
  expect(document.documentElement.dataset.theme).toBe('light')
  expect(localStorage.getItem('postern-theme')).toBe('light')
})

test('an invalid saved preference falls back to the system theme', () => {
  localStorage.setItem('postern-theme', 'invalid')
  systemTheme(true)
  render(<Control />)
  expect(document.documentElement.dataset.theme).toBe('dark')
})

test('blocked storage does not prevent rendering or switching themes', () => {
  systemTheme(false)
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('Storage blocked') })
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Storage blocked') })
  render(<Control />)
  fireEvent.click(screen.getByRole('button', { name: 'Toggle theme' }))
  expect(document.documentElement.dataset.theme).toBe('dark')
})

test('updates browser chrome to match the selected theme', () => {
  systemTheme(false)
  const meta = document.createElement('meta')
  meta.name = 'theme-color'
  document.head.append(meta)
  try {
    render(<Control />)
    expect(meta.content).toBe('#ffffff')
    fireEvent.click(screen.getByRole('button', { name: 'Toggle theme' }))
    expect(meta.content).toBe('#171717')
  } finally {
    meta.remove()
  }
})

test.each([
  [null, false, 'light'],
  [null, true, 'dark'],
  ['light', true, 'light'],
  ['dark', false, 'dark'],
  ['invalid', true, 'dark'],
  ['blocked', true, 'dark'],
])('applies saved preference %s with system dark=%s before React starts', (saved, systemDark, expected) => {
  const bootstrap = indexHtml.match(/<script>([\s\S]*?)<\/script>/)?.[1]
  expect(bootstrap).toBeTruthy()
  const meta = document.createElement('meta')
  meta.name = 'theme-color'
  document.head.append(meta)
  try {
    const storage = { getItem: () => {
      if (saved === 'blocked') throw new Error('Storage blocked')
      return saved
    } }
    // Execute the actual head script, independently of the React hook.
    new Function('document', 'localStorage', 'matchMedia', bootstrap!)(document, storage, () => ({ matches: systemDark }))
    expect(document.documentElement.dataset.theme).toBe(expected)
    expect(meta.content).toBe(expected === 'dark' ? '#171717' : '#ffffff')
  } finally {
    meta.remove()
  }
})
