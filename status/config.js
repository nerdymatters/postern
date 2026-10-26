// Deployment URLs are operator settings. There is no shared production target.
export function publicUrl(value) {
  if (typeof value !== 'string' || !value || /[\s\u0000-\u001f\u007f]/.test(value)) return null
  try {
    const url = new URL(value)
    if (url.protocol !== 'https:' || !url.hostname || url.username || url.password || url.hash) return null
    return url.href
  } catch { return null }
}

export function targets(env) {
  return {
    website: publicUrl(env.WEBSITE_URL),
    api: publicUrl(env.API_HEALTH_URL),
    dedicated: publicUrl(env.SECONDARY_HEALTH_URL),
  }
}
