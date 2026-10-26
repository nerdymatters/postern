import { api, type View } from './api'

const ATTEMPT = 'postern.attempt.'
const opening = new Map<string, Promise<View>>()

/** The reading attempt id survives a remount and a refresh, so the server's
 *  idempotency key is the same one (ENG-D11). */
function attemptFor(itemId: string): string {
  const stored = sessionStorage.getItem(ATTEMPT + itemId)
  if (stored) return stored
  const fresh = crypto.randomUUID()
  sessionStorage.setItem(ATTEMPT + itemId, fresh)
  return fresh
}

/** Exactly one view POST per reading attempt: a second caller joins the first
 *  request instead of starting another, whatever the viewer does on screen. */
export function openView(itemId: string): Promise<View> {
  const key = `${itemId}:${attemptFor(itemId)}`
  const pending = opening.get(key)
  if (pending) return pending
  const request = api.createView(itemId, attemptFor(itemId)).catch(error => {
    opening.delete(key)
    throw error
  })
  opening.set(key, request)
  return request
}

export function forgetViews() {
  opening.clear()
  for (const key of Object.keys(sessionStorage)) {
    if (key.startsWith(ATTEMPT)) sessionStorage.removeItem(key)
  }
}
