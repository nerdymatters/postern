import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { Grid } from './grid'
import { installApi, post, thumbnailGets, world } from './testing'

test('the contents list each attachment once with progress, without loading preview cards', () => {
  const lesson = post('lesson', 'Lesson')
  lesson.items[0].progress = { opened_pages: [0, 1, 2, 3], history_incomplete: false }
  lesson.items.push({ ...lesson.items[0], id: 'video', filename: 'Live9_16_26.mp4', kind: 'video', pages: 0, duration_seconds: 100,
    progress: { opened_pages: [], watched_seconds: 25, history_incomplete: false } })
  installApi(world({ posts: [lesson] }))
  const onOpen = vi.fn()
  render(<Grid posts={[lesson]} onOpen={onOpen} />)
  // The next step first, then each attachment.
  expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual([expect.stringContaining('Continue training'), expect.stringContaining('lesson.pdf'), expect.stringContaining('Live9_16_26.mp4')])
  screen.getByText('50%')
  screen.getByText('25%')
  fireEvent.click(screen.getByRole('button', { name: 'Live9_16_26.mp4' }))
  expect(onOpen).toHaveBeenCalledWith(lesson, lesson.items[1])
  expect(thumbnailGets()).toHaveLength(0)
})

test('the home progress bar counts the SOP chapters only, and every item when there are none', () => {
  // Value: protects=the home bar measures the required chapters, so optional Modules neither fill it nor hold it back; fails_when=the bar counts every item again or loses its fallback; why_new=owner decision P2-D4 A; seam=none
  const lesson = { ...post('m1', 'Service basics', 2), category: 'Module 1', chapter: null }
  lesson.items[0].progress = { opened_pages: [0, 1], history_incomplete: false }
  const handout = { ...post('c1', 'Chapter 1 handout', 4), category: 'SOP Chapter 1: Welcome', chapter: 1 }
  handout.items[0].progress = { opened_pages: [0], history_incomplete: false }
  const view = render(<Grid posts={[lesson, handout]} onOpen={() => {}} />)
  expect(screen.getByRole('progressbar', { name: 'SOP chapters progress' }).getAttribute('aria-valuenow')).toBe('25')
  screen.getByText('ความคืบหน้าบท SOP')
  screen.getByText('Based on pages opened and video sections watched in the SOP chapters, with equal weight for each item. Activity does not confirm understanding.')
  expect(screen.queryByRole('progressbar', { name: 'Overall progress' })).toBeNull()
  view.unmount()
  render(<Grid posts={[lesson]} onOpen={() => {}} />)
  expect(screen.getByRole('progressbar', { name: 'Overall progress' }).getAttribute('aria-valuenow')).toBe('100')
  screen.getByText('ความคืบหน้าโดยรวม')
  screen.getByText('Based on pages opened and video sections watched, with equal weight for each item. Activity does not confirm understanding.')
})
