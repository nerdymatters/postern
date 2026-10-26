const DAY = 86400

export function when(seconds: number | null): string {
  if (!seconds) return 'Not posted yet'
  const age = Date.now() / 1000 - seconds
  if (age < DAY) return 'Today'
  if (age < 2 * DAY) return 'Yesterday'
  if (age < 7 * DAY) return `${Math.floor(age / DAY)} days ago`
  return day(seconds)
}

// Dates and times follow the American standard on every device, Thai lines included: a phone set to
// Thai would otherwise show Thai month names, a 24-hour clock and, with a year, the Buddhist era.
const US = 'en-US'
/** The certificate format's time zone. Badges and certificates are dated in it, so every copy agrees with the
 *  year in the certificate number (postern/honors.py), wherever it is opened or printed. */
export const EASTERN = 'America/New_York'

export function day(seconds: number | null): string {
  if (!seconds) return '—'
  return new Date(seconds * 1000).toLocaleDateString(US, { month: 'short', day: 'numeric' })
}

/** With the year, for things kept: "Oct 5, 2026", or with `long` "October 5, 2026". */
export function dated(seconds: number, month: 'short' | 'long' = 'short', timeZone?: string): string {
  return new Date(seconds * 1000).toLocaleDateString(US, { year: 'numeric', month, day: 'numeric', timeZone })
}

export function moment(seconds: number | null): string {
  if (!seconds) return '—'
  return new Date(seconds * 1000).toLocaleString(US, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

export function bytes(count: number): string {
  return count >= 1024 * 1024 ? `${(count / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(count / 1024))} KB`
}
