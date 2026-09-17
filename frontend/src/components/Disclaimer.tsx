import { useEffect, useState } from 'react'
import { X, AlertTriangle } from 'lucide-react'

/**
 * 全局免责声明底部条(合规落地, 2026-09-16)。
 * - 仅在已登录后的页面挂载(App.tsx RequireAuth 内)。
 * - 关闭后写入 localStorage, 本机不再显示。
 * - 移动端底部导航高 14(56px), 故抬高 bottom 避免遮挡。
 */

const STORAGE_KEY = 'sida_disclaimer_dismissed'

function isDismissed(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

export default function Disclaimer() {
  const [visible, setVisible] = useState(() => !isDismissed())

  // 跨标签页同步关闭状态
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY && e.newValue === '1') setVisible(false)
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  if (!visible) return null

  const dismiss = () => {
    try {
      localStorage.setItem(STORAGE_KEY, '1')
    } catch {
      // ignore quota / private mode
    }
    setVisible(false)
  }

  return (
    <div
      role="status"
      className="pointer-events-none fixed inset-x-0 bottom-14 z-40 md:bottom-0"
    >
      <div className="pointer-events-auto mx-auto flex max-w-5xl items-center gap-2 border-t border-amber-600/25 dark:border-amber-500/20 bg-card/95 px-3 py-2 backdrop-blur md:px-4 md:py-2.5">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-500" />
        <p className="flex-1 text-[11px] leading-snug text-muted-foreground md:text-[12px]">
          本平台数据仅供参考，不构成投资建议。市场有风险，投资需谨慎。
        </p>
        <button
          type="button"
          onClick={dismiss}
          title="关闭并不再显示"
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  )
}
