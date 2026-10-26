import { useCallback, useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { usePausedInterval } from '@panwatch/base-ui'
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

  // 页签 keep-alive(2026-10-15): 隐藏页签停表, 回前台补一次; 未启用时不停表也不回调。
  usePausedInterval(() => void poll(), enabled ? 20_000 : 0, { deps: [enabled, poll] })

  return null
}
