import { useState } from 'react'
import { Monitor, Moon, Sun, ZoomIn } from 'lucide-react'
import { cn } from '@panwatch/base-ui'
import { useTheme, type Density } from '@/hooks/use-theme'

/**
 * 外观设置(2026-09-22): 密度 / 主题 / Dim / 焦点模式。
 *
 * 之前这四样**只**能通过命令面板(Cmd/Ctrl+K)改 —— 命令面板对熟手快, 但"设置页里找不到外观"
 * 是新用户的第一反应(而且外观类设置本来就该在设置里有一格)。
 * 这里只是把同一套存储(panwatch-theme / panwatch-density / panwatch-dim / sida_focus-mode)
 * 接到 UI 上 —— 不引入第二套状态源。
 */
const DENSITY_OPTIONS: { id: Density; label: string; hint: string }[] = [
  { id: 'compact', label: '紧凑', hint: '行更密, 一屏多几行' },
  { id: 'normal', label: '标准', hint: '默认' },
  { id: 'comfortable', label: '宽松', hint: '行更高, 好点' },
]

const THEME_OPTIONS = [
  { id: 'light', label: '亮色', icon: Sun },
  { id: 'dark', label: '暗色', icon: Moon },
  { id: 'system', label: '跟随系统', icon: Monitor },
] as const

export function AppearanceSection() {
  const { mode, setMode, density, setDensity, dim, setDim } = useTheme()
  // 焦点模式无 React 状态源(它只改 <html> 属性) ⇒ 这里用局部 state 记住并同步 DOM, 避免"点了不动"
  const [focusOn, setFocusOn] = useState(
    () => typeof document !== 'undefined' && document.documentElement.getAttribute('data-focus-mode') === '1',
  )

  const toggleFocus = () => {
    const next = !focusOn
    setFocusOn(next)
    document.documentElement.setAttribute('data-focus-mode', next ? '1' : '0')
    try {
      localStorage.setItem('sida_focus-mode', next ? '1' : '0')
    } catch {
      /* 隐私模式忽略 */
    }
    // 面板/其它订阅方要知道(与命令面板同一个事件名)
    window.dispatchEvent(new CustomEvent('sida:focus-mode', { detail: next }))
  }

  return (
    <section id="sec-appearance" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-7">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">外观</h3>
          <p className="text-[11px] text-muted-foreground mt-1">
            密度只收紧内容、不收紧可点热区(热区下限 32px 由门禁保证)
          </p>
        </div>
      </div>

      {/* 主题 */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="w-12 shrink-0 text-[11px] text-muted-foreground">主题</span>
        {THEME_OPTIONS.map((o) => {
          const Icon = o.icon
          const active = mode === o.id
          return (
            <button
              key={o.id}
              type="button"
              aria-pressed={active}
              onClick={() => setMode(o.id)}
              className={cn(
                'inline-flex items-center gap-1 rounded border px-2 py-1 text-[11px] transition-colors duration-fast',
                active ? 'border-[hsl(var(--ring))] bg-s3 text-foreground' : 'border-border text-muted-foreground hover:bg-s2',
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              {o.label}
            </button>
          )
        })}
      </div>

      {/* 密度 */}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="w-12 shrink-0 text-[11px] text-muted-foreground">密度</span>
        {DENSITY_OPTIONS.map((o) => {
          const active = density === o.id
          return (
            <button
              key={o.id}
              type="button"
              aria-pressed={active}
              title={o.hint}
              onClick={() => setDensity(o.id)}
              className={cn(
                'rounded border px-2 py-1 text-[11px] transition-colors duration-fast',
                active ? 'border-[hsl(var(--ring))] bg-s3 text-foreground' : 'border-border text-muted-foreground hover:bg-s2',
              )}
            >
              {o.label}
            </button>
          )
        })}
      </div>

      {/* Dim / 焦点模式 */}
      <div className="mt-3 flex flex-wrap items-center gap-4">
        <label className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <input type="checkbox" checked={dim} onChange={(e) => setDim(e.target.checked)} />
          <ZoomIn className="h-3.5 w-3.5" />
          Dim(长时盯盘: 只降对比度, 不动色相与涨跌语义)
        </label>
        <label className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <input type="checkbox" checked={focusOn} onChange={toggleFocus} />
          焦点模式(收起侧栏与免责条, 只留一枚 chip)
        </label>
      </div>
    </section>
  )
}

export default AppearanceSection
