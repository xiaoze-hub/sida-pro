import type { ReactNode } from 'react'
import { SystemStatusContext, useLocalSystemStatus } from '@/hooks/useSystemStatus'

/** 常驻图标与状态页共用一个刷新流，避免每个入口重复探测。 */
export function SystemStatusProvider({ children, enabled }: { children: ReactNode; enabled: boolean }) {
  const state = useLocalSystemStatus(enabled)
  return <SystemStatusContext.Provider value={state}>{children}</SystemStatusContext.Provider>
}
