import { Link2, Lock } from 'lucide-react'

/**
 * 面板"跟随/锁定"开关 (A6, 2026-09-10 借鉴 OpenTerminal 面板联动):
 * 默认跟随当前标的(未锁定); 点一下锁定到当前标的 —— 之后切换标的本面板不再跟随,
 * 用于盘中对比(一个面板看 A, 页面其余部分看 B)。再点解锁恢复跟随。
 * 语义: locked=null 表示跟随; locked=<code> 表示锁定标的。
 */
export function PanelLockToggle({
  symbol,
  locked,
  onToggle,
  className = '',
}: {
  /** 当前浏览标的 */
  symbol: string
  /** 锁定标的; null=跟随当前标的 */
  locked: string | null
  onToggle: () => void
  className?: string
}) {
  const isLocked = locked != null
  const diverged = isLocked && locked !== symbol
  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`.trim()}>
      <button
        type="button"
        onClick={onToggle}
        data-locked={isLocked ? 'true' : 'false'}
        title={isLocked ? `已锁定 ${locked}, 点击解锁恢复跟随` : `锁定本面板到 ${symbol}(切换标的后不再跟随)`}
        className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 transition-colors ${
          isLocked
            ? 'border-amber-500/40 bg-amber-500/10 text-amber-600'
            : 'border-border/40 text-muted-foreground hover:text-foreground'
        }`}
      >
        {isLocked ? <Lock className="h-3 w-3" /> : <Link2 className="h-3 w-3" />}
        {isLocked ? `已锁定 ${locked}` : `跟随 ${symbol}`}
      </button>
      {diverged && (
        <span className="text-[10px] text-muted-foreground">当前浏览 {symbol}（本面板未跟随）</span>
      )}
    </span>
  )
}

export default PanelLockToggle
