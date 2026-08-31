import { useEffect, useRef, type CSSProperties } from 'react'
import { Plus, Eye, Copy, TrendingUp } from 'lucide-react'

export interface StockContextTarget {
  symbol: string
  name: string
  market: string
  hasPosition?: boolean
}

export interface StockContextMenuState {
  x: number
  y: number
  stock: StockContextTarget
}

interface StockContextMenuProps {
  menu: StockContextMenuState | null
  onClose: () => void
  onAddWatchlist: (stock: StockContextTarget) => void
  onViewDetail: (stock: StockContextTarget) => void
  onPaperTrade: (stock: StockContextTarget) => void
}

const MENU_WIDTH = 150
const MENU_HEIGHT = 150

/**
 * PC 右键菜单:在股票行上 onContextMenu 打开,点击外部 / Esc 关闭。
 * 每页同时最多一个菜单(单例 menu state)。
 */
export default function StockContextMenu({
  menu,
  onClose,
  onAddWatchlist,
  onViewDetail,
  onPaperTrade,
}: StockContextMenuProps) {
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!menu) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    const onMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('mousedown', onMouseDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('mousedown', onMouseDown)
    }
  }, [menu, onClose])

  if (!menu) return null

  const { stock, x, y } = menu
  // 菜单位于鼠标位置,超出版心时收回到可视区内
  const style: CSSProperties = {
    position: 'fixed',
    left: Math.max(4, Math.min(x, window.innerWidth - MENU_WIDTH - 8)),
    top: Math.max(4, Math.min(y, window.innerHeight - MENU_HEIGHT - 8)),
    zIndex: 9999,
  }

  const items = [
    { label: '加入自选', icon: Plus, run: () => onAddWatchlist(stock) },
    { label: '查看详情', icon: Eye, run: () => onViewDetail(stock) },
    {
      label: '复制代码',
      icon: Copy,
      run: () => {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
          navigator.clipboard.writeText(stock.symbol).catch(() => {})
        }
      },
    },
    { label: '模拟买入', icon: TrendingUp, run: () => onPaperTrade(stock) },
  ]

  return (
    <div
      ref={ref}
      style={style}
      className="min-w-[140px] rounded-lg border border-border/60 bg-popover py-1 shadow-xl"
      onContextMenu={(e) => e.preventDefault()}
    >
      {items.map((item) => (
        <button
          key={item.label}
          type="button"
          onClick={(e) => {
            e.stopPropagation()
            item.run()
            onClose()
          }}
          className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12px] text-foreground transition-colors hover:bg-accent"
        >
          <item.icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          {item.label}
        </button>
      ))}
    </div>
  )
}
