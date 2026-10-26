import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { coverage, modules, Progress } from './contents'
import { post } from './testing'

test('documents have equal weight and duplicate pages cannot inflate progress', () => {
  const long = post('long', 'Long', 100).items[0]
  const short = post('short', 'Short', 4).items[0]
  long.progress = { opened_pages: Array.from({ length: 50 }, (_, i) => i), history_incomplete: false }
  short.progress = { opened_pages: [0, 1, 2, 3, 3, -1, 99], history_incomplete: false }
  expect(coverage([long, short]).value).toBe(75)
  render(<Progress items={[long, short]} label="Overall progress" />)
  expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('75')
})

test('missing history is explicit, while confirmed complete coverage remains complete', () => {
  const item = post('p', 'P', 2).items[0]
  expect(coverage([item]).label).toBe('Unavailable')
  item.progress = { opened_pages: [], history_incomplete: true }
  expect(coverage([item]).label).toBe('Unknown')
  item.progress.opened_pages = [0]
  expect(coverage([item]).label).toBe('At least 50%')
  item.progress.opened_pages = [0, 1]
  expect(coverage([item]).label).toBe('100%')
})

test('module order is numeric, spelling variants combine, and documents follow publication order', () => {
  const posts = ['Module10', 'Module 2', 'Module1', 'module 1'].map((category, i) => ({ ...post(`p${i}`, category), category, published_at: 10 - i }))
  const groups = modules(posts)
  expect(groups.map(group => group.name)).toEqual(['Module 1', 'Module 2', 'Module 10'])
  expect(groups[0].posts.map(post => post.id)).toEqual(['p3', 'p2'])
})
