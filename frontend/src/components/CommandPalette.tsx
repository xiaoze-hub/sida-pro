import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { Search, TrendingUp } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'

// 设计稿 v2.0 §4.4: 全局搜索(Ctrl+K)命令面板 —— 搜股票跳行情, 搜功能跳页面。

interface StockHit {
  symbol: string
  name: string
  market: string
}

interface PageCommand {
  to: string
  label: string
  group: string
}

type Item =
  | { type: 'page'; to: string; label: string; group: string }
  | { type: 'stock'; symbol: string; name: string; market: string }

// 6 项主导航对应页面命令(与 App.tsx desktopNavGroups 对齐)
const PAGE_COMMANDS: PageCommand[] = [
  { to: '/', label: '首页 · 驾驶舱', group: '驾驶舱' },
  { to: '/forecast', label: '预测', group: '行情' },
  { to: '/opportunities', label: '机会 · 选股池', group: '机会' },
  { to: '/reports', label: '报告中心', group: '投研' },
  { to: '/history', label: '历史分析', group: '投研' },
  { to: '/portfolio', label: '持仓 · 自选', group: '我的' },
  { to: '/shadow', label: '影子账户', group: '我的' },
  { to: '/paper-trading', label: '模拟盘', group: '我的' },
  { to: '/profile', label: '个人中心', group: '我的' },
  // §4.3: 指向收纳后的新地址(带 ?tab= 直达对应页签)
  { to: '/system?tab=agents', label: 'Agent 管理', group: '系统' },
  { to: '/system?tab=datasources', label: '数据源', group: '系统' },
  { to: '/notifications', label: '通知中心', group: '系统' },
  { to: '/alerts', label: '价格提醒', group: '系统' },
  { to: '/settings?tab=settings', label: '设置', group: '系统' },
  { to: '/settings?tab=audit', label: '日志审计', group: '系统' },
  { to: '/settings?tab=help', label: '帮助 · 快捷键', group: '系统' },
]

function localDate(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

export default function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  const [q, setQ] = useState('')
  const [stocks, setStocks] = useState<StockHit[]>([])
  const [loading, setLoading] = useState(false)
  const [active, setActive] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  // 打开时重置
  useEffect(() => {
    if (open) {
      setQ('')
      setStocks([])
      setActive(0)
      const t = setTimeout(() => inputRef.current?.focus(), 0)
      return () => clearTimeout(t)
    }
  }, [open])

  // 防抖股票搜索
  useEffect(() => {
    if (!open) return
    const qq = q.trim()
    if (!qq) {
      setStocks([])
      setLoading(false)
      return
    }
    setLoading(true)
    const t = setTimeout(async () => {
      try {
        const res = await fetchAPI<StockHit[]>(`/stocks/search?q=${encodeURIComponent(qq)}`)
        setStocks(Array.isArray(res) ? res.slice(0, 8) : [])
      } catch {
        setStocks([])
      } finally {
        setLoading(false)
      }
    }, 200)
    return () => clearTimeout(t)
  }, [q, open])

  const pageHits = PAGE_COMMANDS.filter((c) => !q.trim() || c.label.includes(q.trim()))
  const items: Item[] = [
    ...pageHits.map((c): Item => ({ type: 'page', to: c.to, label: c.label, group: c.group })),
    ...stocks.map((s): Item => ({ type: 'stock', symbol: s.symbol, name: s.name, market: s.market })),
  ].slice(0, 12)

  useEffect(() => {
    setActive(0)
  }, [q])

  if (!open) return null

  const run = (item: Item) => {
    if (item.type === 'stock') {
      navigate(`/analysis/${item.symbol}/${localDate()}`)
    } else {
      navigate(item.to)
    }
    onClose()
  }

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((a) => Math.min(a + 1, items.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((a) => Math.max(a - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (items[active]) run(items[active])
    } else if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
    }
  }

  return (
    <div
      className="fixed inset-0 z-[100] flex items-start justify-center px-4 pt-[12vh]"
      onClick={onClose}
    >
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" aria-hidden="true" />
      <div
        className="relative w-full max-w-lg overflow-hidden rounded-2xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-border px-4 py-3">
          <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="搜索股票(代码/名称) 或 功能…"
            className="flex-1 bg-transparent text-[14px] text-foreground outline-none placeholder:text-muted-foreground/60"
            data-search-input
          />
          <kbd className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">ESC</kbd>
        </div>
        <div className="max-h-[50vh] overflow-y-auto py-2">
          {loading && <div className="px-4 py-2 text-[12px] text-muted-foreground">搜索中…</div>}
          {!loading && items.length === 0 && (
            <div className="px-4 py-3 text-[12px] text-muted-foreground">
              {q.trim() ? '无匹配结果' : '输入关键字搜索股票，或搜功能跳转页面'}
            </div>
          )}
          {items.map((it, i) => (
            <button
              key={it.type === 'stock' ? `s-${it.symbol}` : `p-${it.to}`}
              onMouseEnter={() => setActive(i)}
              onClick={() => run(it)}
              className={`flex w-full items-center gap-2.5 px-4 py-2 text-left ${i === active ? 'bg-accent' : ''}`}
            >
              {it.type === 'stock' ? (
                <>
                  <TrendingUp className="h-4 w-4 shrink-0 text-primary" />
                  <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">{it.name}</span>
                  <span className="shrink-0 text-[11px] text-muted-foreground">
                    {it.symbol} · {it.market}
                  </span>
                </>
              ) : (
                <>
                  <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">{it.label}</span>
                  <span className="shrink-0 text-[11px] text-muted-foreground">{it.group}</span>
                </>
              )}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3 border-t border-border px-4 py-2 text-[10px] text-muted-foreground">
          <span>
            <kbd className="rounded bg-muted px-1">↑↓</kbd> 选择
          </span>
          <span>
            <kbd className="rounded bg-muted px-1">↵</kbd> 打开
          </span>
          <span>
            <kbd className="rounded bg-muted px-1">esc</kbd> 关闭
          </span>
        </div>
      </div>
    </div>
  )
}
