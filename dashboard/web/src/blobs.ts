/** Every object URL the app hands to an <img> or a canvas is registered here, so
 *  sign-out and session revocation can revoke all of them (ENG-D1). */
const live = new Set<string>()

export function hold(blob: Blob): string {
  const url = URL.createObjectURL(blob)
  live.add(url)
  return url
}

export function release(url: string) {
  if (live.delete(url)) URL.revokeObjectURL(url)
}

export function releaseAll() {
  for (const url of live) URL.revokeObjectURL(url)
  live.clear()
}

export function liveUrls() { return live.size }
