import { useState, useEffect } from 'react'

/** 用户选择的主题模式(system = 跟随系统)。 */
export type ThemeMode = 'light' | 'dark' | 'system'
/** 实际生效的主题(system 解析后的结果)。 */
export type Theme = 'light' | 'dark'
/** 界面密度档位:compact 紧凑 / normal 标准 / comfortable 舒适。 */
export type Density = 'compact' | 'normal' | 'comfortable'

const STORAGE_KEY = 'panwatch-theme'
const DENSITY_STORAGE_KEY = 'panwatch-density'

function readMode(): ThemeMode {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'light' || stored === 'dark' || stored === 'system') return stored
  return 'system'
}

function readDensity(): Density {
  const stored = localStorage.getItem(DENSITY_STORAGE_KEY)
  if (stored === 'compact' || stored === 'normal' || stored === 'comfortable') return stored
  return 'normal'
}

export function useTheme() {
  const [mode, setMode] = useState<ThemeMode>(readMode)
  const [density, setDensity] = useState<Density>(readDensity)
  const [systemDark, setSystemDark] = useState(
    () => window.matchMedia('(prefers-color-scheme: dark)').matches,
  )

  // 跟随系统:监听 OS 主题变化,实时反映
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  // 生效主题:显式 light/dark 直接用,system 则跟随当前系统
  const theme: Theme = mode === 'system' ? (systemDark ? 'dark' : 'light') : mode

  useEffect(() => {
    const root = document.documentElement
    root.classList.remove('light', 'dark')
    root.classList.add(theme)
    localStorage.setItem(STORAGE_KEY, mode)
  }, [theme, mode])

  // 密度档位:写到 <html data-density="...">,由 index.css 的 [data-density] 规则驱动
  useEffect(() => {
    const root = document.documentElement
    root.setAttribute('data-density', density)
    localStorage.setItem(DENSITY_STORAGE_KEY, density)
  }, [density])

  // 兼容旧调用:在亮/暗间切换(会把模式落为显式 light/dark)
  const toggleTheme = () => setMode(theme === 'dark' ? 'light' : 'dark')

  return { theme, mode, setMode, toggleTheme, density, setDensity }
}
