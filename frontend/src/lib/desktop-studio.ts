/** Scope the prototype by active URL. Hidden keep-alive tabs must not activate it. */
export function desktopStudioPage(pathname: string): 'home' | 'quote' | 'settings' | undefined {
  const path = pathname.replace(/\/+$/, '') || '/'
  if (path === '/') return 'home'
  if (/^\/stocks\/[^/]+$/i.test(path)) return 'quote'
  if (path.toLowerCase() === '/settings') return 'settings'
  return undefined
}
