import { useEffect, useState } from 'react'
import { X, Info } from 'lucide-react'

/** 工作区状态行右侧的数据使用提示；关闭偏好按浏览器保存，跨标签页同步。 */

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
    <aside role="note" aria-label="数据使用提示" data-shell-disclaimer className="ml-auto flex min-w-0 items-center justify-end gap-2 text-[12px] text-muted-foreground">
      <Info className="h-3.5 w-3.5 shrink-0 text-role-opp" aria-hidden="true" />
      <p className="min-w-0 text-right leading-relaxed">本平台数据仅供参考，不构成投资建议。市场有风险，投资需谨慎。</p>
      <button type="button" onClick={dismiss} title="关闭并不再显示" aria-label="关闭数据使用提示" className="ui-icon-button">
        <X className="h-3.5 w-3.5" />
      </button>
    </aside>
  )
}
