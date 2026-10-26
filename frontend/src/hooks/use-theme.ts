import { useEffect, useState, useSyncExternalStore } from 'react'

export type ThemeMode = 'light' | 'dark' | 'system'
export type Theme = 'light' | 'dark'
export type Density = 'compact' | 'normal' | 'comfortable'

type Appearance = { mode: ThemeMode; density: Density; dim: boolean }
const CHANGE_EVENT = 'sida-appearance-change'
const KEYS = { mode: 'panwatch-theme', density: 'panwatch-density', dim: 'panwatch-dim' } as const
let fallback: Appearance = { mode: 'dark', density: 'normal', dim: false }

function readAppearance(): Appearance {
  try {
    const mode = localStorage.getItem(KEYS.mode)
    const density = localStorage.getItem(KEYS.density)
    return {
      mode: mode === 'light' || mode === 'system' ? mode : 'dark',
      density: density === 'compact' || density === 'comfortable' ? density : 'normal',
      dim: localStorage.getItem(KEYS.dim) === '1',
    }
  } catch {
    return fallback
  }
}

/** 所有入口共用同一个存储与通知机制，同窗口和其他窗口即时同步。 */
export function setAppearance(patch: Partial<Appearance>): void {
  const next = { ...readAppearance(), ...patch }
  fallback = next
  try {
    localStorage.setItem(KEYS.mode, next.mode)
    localStorage.setItem(KEYS.density, next.density)
    localStorage.setItem(KEYS.dim, next.dim ? '1' : '0')
  } catch { /* 存储被禁用时，本窗口仍可切换。 */ }
  window.dispatchEvent(new Event(CHANGE_EVENT))
}

function snapshot(): string {
  const value = readAppearance()
  return `${value.mode}|${value.density}|${value.dim ? '1' : '0'}`
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener(CHANGE_EVENT, onChange)
  window.addEventListener('storage', onChange)
  return () => {
    window.removeEventListener(CHANGE_EVENT, onChange)
    window.removeEventListener('storage', onChange)
  }
}

export function useTheme() {
  const stored = useSyncExternalStore(subscribe, snapshot, () => 'dark|normal|0')
  const [storedMode, storedDensity, storedDim] = stored.split('|')
  const mode = storedMode as ThemeMode
  const density = storedDensity as Density
  const dim = storedDim === '1'
  const [systemDark, setSystemDark] = useState(() => window.matchMedia('(prefers-color-scheme: dark)').matches)

  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches)
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])

  const theme: Theme = mode === 'system' ? (systemDark ? 'dark' : 'light') : mode
  useEffect(() => {
    const root = document.documentElement
    root.classList.toggle('dark', theme === 'dark')
    root.classList.toggle('light', theme === 'light')
    root.setAttribute('data-density', density)
    if (dim && theme === 'dark') root.setAttribute('data-theme', 'dim')
    else root.removeAttribute('data-theme')
    const meta = document.querySelector('meta[name="theme-color"]')
    meta?.setAttribute('content', theme === 'dark' ? '#10121a' : '#f8f9fb')
  }, [theme, density, dim])

  return {
    theme, mode, density, dim,
    setMode: (value: ThemeMode) => setAppearance({ mode: value }),
    setDensity: (value: Density) => setAppearance({ density: value }),
    setDim: (value: boolean) => setAppearance({ dim: value }),
    toggleTheme: () => setAppearance({ mode: theme === 'dark' ? 'light' : 'dark' }),
  }
}
