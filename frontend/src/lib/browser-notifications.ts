export const BROWSER_NOTIFICATIONS_KEY = 'panwatch_browser_notifications_enabled'
export const BROWSER_NOTIFICATIONS_LAST_ID_KEY = 'panwatch_browser_notifications_last_id'
export const BROWSER_NOTIFICATIONS_CHANGED_EVENT = 'panwatch-browser-notifications-changed'

export interface BrowserNotificationItem {
  id: number
  title: string
  body?: string
  link?: string
}

export function browserNotificationsSupported(): boolean {
  return typeof window !== 'undefined' && window.isSecureContext && 'Notification' in window
}

export function browserNotificationsEnabled(): boolean {
  return browserNotificationsSupported()
    && localStorage.getItem(BROWSER_NOTIFICATIONS_KEY) === 'true'
    && Notification.permission === 'granted'
}

export function setBrowserNotificationsEnabled(enabled: boolean, baselineId?: number): void {
  localStorage.setItem(BROWSER_NOTIFICATIONS_KEY, enabled ? 'true' : 'false')
  if (typeof baselineId === 'number') {
    localStorage.setItem(BROWSER_NOTIFICATIONS_LAST_ID_KEY, String(baselineId))
  }
  window.dispatchEvent(new Event(BROWSER_NOTIFICATIONS_CHANGED_EVENT))
}

export async function requestBrowserNotificationPermission(): Promise<NotificationPermission> {
  if (!browserNotificationsSupported()) return 'denied'
  return Notification.requestPermission()
}

export async function showBrowserNotification(item: BrowserNotificationItem): Promise<boolean> {
  if (!browserNotificationsSupported() || Notification.permission !== 'granted') return false

  const options: NotificationOptions = {
    body: (item.body || '').slice(0, 400),
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: `panwatch-${item.id}`,
    data: { url: item.link || '/' },
  }

  if ('serviceWorker' in navigator) {
    const registration = await navigator.serviceWorker.getRegistration()
    if (registration) {
      await registration.showNotification(item.title || 'SIDA 通知', options)
      return true
    }
  }

  const notification = new Notification(item.title || 'SIDA 通知', options)
  notification.onclick = () => {
    window.focus()
    if (item.link) window.location.assign(item.link)
    notification.close()
  }
  return true
}

export async function showNewBrowserNotifications(items: BrowserNotificationItem[]): Promise<void> {
  if (!browserNotificationsEnabled() || items.length === 0) return

  const newestId = Math.max(...items.map(item => item.id))
  const stored = localStorage.getItem(BROWSER_NOTIFICATIONS_LAST_ID_KEY)
  if (stored === null) {
    localStorage.setItem(BROWSER_NOTIFICATIONS_LAST_ID_KEY, String(newestId))
    return
  }

  const lastId = Number.parseInt(stored, 10) || 0
  const newItems = items.filter(item => item.id > lastId).sort((a, b) => a.id - b.id)
  for (const item of newItems) {
    await showBrowserNotification(item)
  }
  if (newestId > lastId) {
    localStorage.setItem(BROWSER_NOTIFICATIONS_LAST_ID_KEY, String(newestId))
  }
}
