import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import zhCN, { type Messages } from '@/locales/zh-CN'
import enUS from '@/locales/en-US'

export type Locale = 'zh-CN' | 'en-US'

const DICTS: Record<Locale, Messages> = {
  'zh-CN': zhCN,
  'en-US': enUS,
}

const STORAGE_KEY = 'sida.locale'

export function detectLocale(): Locale {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'zh-CN' || saved === 'en-US') return saved
  } catch {
    /* ignore */
  }
  const nav = typeof navigator !== 'undefined' ? navigator.language : 'zh-CN'
  return nav.toLowerCase().startsWith('zh') ? 'zh-CN' : 'en-US'
}

/** 支持 {name} 占位符插值 */
function interpolate(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template
  return template.replace(/\{(\w+)\}/g, (_, k: string) => (vars[k] != null ? String(vars[k]) : `{${k}}`))
}

type PathImpl<T, K extends keyof T> = K extends string
  ? T[K] extends Record<string, unknown>
    ? T[K] extends ArrayLike<unknown>
      ? K
      : `${K}.${PathImpl<T[K], keyof T[K]>}`
    : K
  : never

export type MessageKey = PathImpl<Messages, keyof Messages>

export interface I18nContextValue {
  locale: Locale
  setLocale: (l: Locale) => void
  t: (key: string, vars?: Record<string, string | number>) => string
  dict: Messages
}

const I18nContext = createContext<I18nContextValue | null>(null)

function getByPath(obj: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, k) => {
    if (acc && typeof acc === 'object') return (acc as Record<string, unknown>)[k]
    return undefined
  }, obj)
}

export function I18nProvider({ children, initialLocale }: { children: ReactNode; initialLocale?: Locale }) {
  const [locale, setLocaleState] = useState<Locale>(() => initialLocale ?? detectLocale())

  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l)
    try {
      localStorage.setItem(STORAGE_KEY, l)
    } catch {
      /* ignore */
    }
    if (typeof document !== 'undefined') {
      document.documentElement.lang = l
    }
  }, [])

  useEffect(() => {
    if (typeof document !== 'undefined') document.documentElement.lang = locale
  }, [locale])

  const value = useMemo<I18nContextValue>(() => {
    const dict = DICTS[locale]
    const fallback = DICTS['zh-CN']
    const t = (key: string, vars?: Record<string, string | number>) => {
      const v = getByPath(dict, key) ?? getByPath(fallback, key)
      if (typeof v === 'string') return interpolate(v, vars)
      return key
    }
    return { locale, setLocale, t, dict }
  }, [locale, setLocale])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext)
  if (!ctx) {
    // 兜底: 未挂 Provider 时仍可用(开发/测试), 恒定中文
    const t = (key: string, vars?: Record<string, string | number>) => {
      const v = getByPath(zhCN, key)
      return typeof v === 'string' ? interpolate(v, vars) : key
    }
    return { locale: 'zh-CN', setLocale: () => {}, t, dict: zhCN }
  }
  return ctx
}
