import { expect, test } from 'vitest'
import { hold, liveUrls, release, releaseAll } from './blobs'
import { objectUrls } from './testing'

const jpeg = () => new Blob(['jpeg'], { type: 'image/jpeg' })

test('releaseAll revokes every url still held, and release revokes only once', () => {
  const first = hold(jpeg())
  const second = hold(jpeg())
  expect(liveUrls()).toBe(2)

  release(first)
  release(first)
  expect(objectUrls.revoked).toEqual([first])

  releaseAll()
  expect(new Set(objectUrls.revoked)).toEqual(new Set([first, second]))
  expect(liveUrls()).toBe(0)
})
