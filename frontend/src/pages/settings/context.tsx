import { createContext, useContext } from 'react'
import type { useSettingsState } from './useSettingsState'
import type { useSettingsData } from './useSettingsData'
import type { useSettingsDerived } from './useSettingsDerived'
import type { useSettingsActions } from './useSettingsActions'

export type SettingsCtx = ReturnType<typeof useSettingsState> &
  ReturnType<typeof useSettingsData> &
  ReturnType<typeof useSettingsDerived> &
  ReturnType<typeof useSettingsActions>

export const SettingsContext = createContext<SettingsCtx | null>(null)

export function useSettings(): SettingsCtx {
  const ctx = useContext(SettingsContext)
  if (!ctx) throw new Error('useSettings must be used inside SettingsPage')
  return ctx
}
