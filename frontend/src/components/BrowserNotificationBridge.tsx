import { useCallback, useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import {
  BROWSER_NOTIFICATIONS_CHANGED_EVENT,
  browserNotificationsEnabled,
  showNewBrowserNotifications,
  type BrowserNotificationItem,
} from '@/lib/browser-notifications'

/** Poll station notifications once and mirror new items to the desktop browser. */
export default function BrowserNotificationBridge() {
  const [enabled, setEnabled] = useState(browserNotificationsEnabled)

  const poll = useCallback(async () => {
    if (!enabled) return
    try {
      const response = await fetchAPI<{ items: BrowserNotificationItem[] }>(
        '/notifications?limit=20&only_unread=true',
      )
      await showNewBrowserNotifications(response?.items || [])
    } catch {
      // Background notification polling must never interrupt the application.
    }
  }, [enabled])

  useEffect(() => {
    const syncEnabled = () => setEnabled(browserNotificationsEnabled())
    window.addEventListener(BROWSER_NOTIFICATIONS_CHANGED_EVENT, syncEnabled)
    return () => window.removeEventListener(BROWSER_NOTIFICATIONS_CHANGED_EVENT, syncEnabled)
  }, [])

  useEffect(() => {
    if (!enabled) return
    void poll()
    const timer = window.setInterval(() => void poll(), 20_000)
    return () => window.clearInterval(timer)
  }, [enabled, poll])

  return null
}
