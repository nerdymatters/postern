import { useEffect, useState, type ReactNode } from 'react'

export type Route =
  | { name: 'grid' }
  | { name: 'post'; id: string; item?: string }
  | { name: 'quiz'; chapter: number }
  | { name: 'badges' }
  | { name: 'certificate' }
  | { name: 'admin' }
  | { name: 'progress' }
  | { name: 'report'; id: string }
  | { name: 'denials' }
  | { name: 'privacy' }

/** Real paths, so a copied link to a post survives sign-in and a reload. */
export function parse(path: string): Route {
  path = path.replace(/^\/s\/pilot(?=\/|$)/, '') || '/'
  if (path.startsWith('/privacy')) return { name: 'privacy' }
  const report = /^\/admin\/posts\/([^/]+)/.exec(path)
  if (report) return { name: 'report', id: decodeURIComponent(report[1]) }
  if (path === '/admin/progress' || path === '/admin/progress/') return { name: 'progress' }
  if (path.startsWith('/admin/denials')) return { name: 'denials' }
  if (path.startsWith('/admin')) return { name: 'admin' }
  if (path === '/final-exam' || path === '/final-exam/') return { name: 'quiz', chapter: 0 }
  if (path === '/badges' || path === '/badges/') return { name: 'badges' }
  if (path === '/certificate' || path === '/certificate/') return { name: 'certificate' }
  const quiz = /^\/quiz\/([0-9]+)\/?$/.exec(path)
  if (quiz) return { name: 'quiz', chapter: Number(quiz[1]) }
  const post = /^\/post\/([^/]+)(?:\/item\/([^/]+))?/.exec(path)
  if (post) return { name: 'post', id: decodeURIComponent(post[1]), ...(post[2] ? { item: decodeURIComponent(post[2]) } : {}) }
  return { name: 'grid' }
}

function routedPath(path: string) {
  if (/^\/s\/pilot(?:\/|$)/.test(window.location.pathname) && (path === '/' || /^\/(?:post|quiz|final-exam|badges|certificate|admin|privacy)(?:\/|$)/.test(path))) return '/s/pilot' + (path === '/' ? '' : path)
  return path
}

export function navigate(path: string) {
  path = routedPath(path)
  window.history.pushState(null, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
  // A new screen starts at its top: the home card's next step, or the viewer's title.
  ;(document.scrollingElement ?? document.documentElement).scrollTop = 0
}

export function usePathname(): string {
  const [path, setPath] = useState(() => window.location.pathname)
  useEffect(() => {
    const update = () => setPath(window.location.pathname)
    window.addEventListener('popstate', update)
    return () => window.removeEventListener('popstate', update)
  }, [])
  return path
}

export function useRoute(): Route { return parse(usePathname()) }

export function Link({ to, className = '', children }: { to: string; className?: string; children: ReactNode }) {
  return (
    <a href={routedPath(to)} className={className} onClick={event => { event.preventDefault(); navigate(to) }}>{children}</a>
  )
}
