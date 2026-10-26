import { expect, test } from 'vitest'
import { parse } from './routes'

test('an attachment deep link restores the selected content while old post links still work', () => {
  expect(parse('/post/module-one/item/video-one')).toEqual({ name: 'post', id: 'module-one', item: 'video-one' })
  expect(parse('/post/module-one')).toEqual({ name: 'post', id: 'module-one' })
})
