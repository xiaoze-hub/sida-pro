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

/**
 * 兜底上下文: 未挂 `I18nProvider` 时使用(开发/测试直接渲染页面组件)。
 *
 * **必须是模块级常量**(2026-09-18 修): 原先每次调用 `useI18n()` 都新建一个对象 + 新 `t` 函数,
 * 而页面普遍把 `t` 放进 `useCallback`/`useEffect` 依赖(如 `DarkFundTop` 的 `load = useCallback(..., [t])`,
 * `useEffect(() => load(), [load])`) ⇒ 每次渲染依赖都变 ⇒ **effect 无限重跑 = 取数风暴**:
 * 界面永远停在骨架屏, 卸载后 promise 才落地(测试里表现为 `window is not defined` 的 unhandled error)。
 * 挂 Provider 时无此问题(`t` 由 useMemo 稳定), 所以只在"无 Provider"路径上炸 —— 正是测试路径。
 */
const FALLBACK_T = (key: string, vars?: Record<string, string | number>): string => {
  const v = getByPath(zhCN, key)
  return typeof v === 'string' ? interpolate(v, vars) : key
}

const FALLBACK_I18N: I18nContextValue = {
  locale: 'zh-CN',
  setLocale: () => {},
  t: FALLBACK_T,
  dict: zhCN,
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext)
  if (!ctx) {
    // 兜底: 未挂 Provider 时仍可用(开发/测试), 恒定中文, 且**引用稳定**
    return FALLBACK_I18N
  }
  return ctx
}
