/** Google Identity Services, loaded only when the server is in Google mode. */
interface Identity {
  accounts: {
    id: {
      initialize(options: { client_id: string; callback: (response: { credential: string }) => void }): void
      renderButton(parent: HTMLElement, options: { theme: string; size: string; width: number; text: string; shape: string }): void
    }
  }
}

declare global {
  interface Window { google?: Identity }
}

const SRC = 'https://accounts.google.com/gsi/client'
let loading: Promise<Identity> | null = null

export function loadIdentity(): Promise<Identity> {
  loading ??= new Promise<Identity>((resolve, reject) => {
    if (window.google) return resolve(window.google)
    const script = document.createElement('script')
    script.src = SRC
    script.async = true
    script.onload = () => window.google ? resolve(window.google) : reject(new Error('Google sign-in did not load.'))
    script.onerror = () => { loading = null; reject(new Error('Google sign-in did not load.')) }
    document.head.appendChild(script)
  })
  return loading
}
