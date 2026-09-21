import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { Search, TrendingUp, Plus, Zap } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { navBackState } from '@/lib/nav-back'

// 设计稿 v2.0 §4.4: 全局搜索(Ctrl+K)命令面板 —— 搜股票跳行情, 搜功能跳页面。
// A2 (2026-09-10): 结果动作化 —— Enter 跳转, 股票项 Shift+Enter 直接加自选(不离开面板流程)。

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
  | { type: 'action'; id: string; label: string; group: string; hint?: string }

// 6 项主导航对应页面命令(与 App.tsx desktopNavGroups 对齐)
const PAGE_COMMANDS: PageCommand[] = [
  { to: '/', label: '首页 · 驾驶舱', group: '驾驶舱' },
  { to: '/forecast', label: '预测', group: '行情' },
  { to: '/opportunities', label: '机会 · 选股池', group: '机会' },
  // §4.3 补齐(2026-09-01): 历史/模拟盘/提醒 收纳后指向新地址(带 ?tab= 直达对应页签)
  { to: '/reports?tab=reports', label: '报告中心', group: '投研' },
  { to: '/reports?tab=history', label: '历史分析', group: '投研' },
  { to: '/portfolio', label: '持仓 · 自选', group: '我的' },
  { to: '/shadow?tab=shadow', label: '影子账户', group: '我的' },
  { to: '/shadow?tab=paper', label: '模拟盘', group: '我的' },
  { to: '/profile', label: '个人中心', group: '我的' },
  { to: '/api-keys', label: 'API Key 控制台', group: '我的' },
  // §4.3: 指向收纳后的新地址(带 ?tab= 直达对应页签)
  { to: '/system?tab=agents', label: 'Agent 管理', group: '系统' },
  { to: '/system?tab=datasources', label: '数据源', group: '系统' },
  { to: '/notifications?tab=notifications', label: '通知中心', group: '系统' },
  { to: '/notifications?tab=alerts', label: '价格提醒', group: '系统' },
  { to: '/settings?tab=settings', label: '设置', group: '系统' },
  { to: '/settings?tab=audit', label: '日志审计', group: '系统' },
  { to: '/settings?tab=help', label: '帮助 · 快捷键', group: '系统' },
]

function localDate(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/**
 * 动作条目(2026-09-20 设计系统 P0: Cmd/Ctrl+K 补齐"动作"层)。
 * 依据: 面板此前只搜股票与页面 —— 用户能"跳过去", 但"就地做一件事"必须离开键盘去点 UI。
 * 这里的每条动作都**真的执行**(不是占位): 密度/主题/侧栏/焦点模式/复制链接。
 */
const ACTION_COMMANDS: { id: string; label: string; group: string; hint?: string }[] = [
  { id: 'density-compact', label: '密度: 紧凑', group: '外观', hint: '行高收紧' },
  { id: 'density-normal', label: '密度: 标准', group: '外观' },
  { id: 'density-comfortable', label: '密度: 宽松', group: '外观' },
  { id: 'theme-dark', label: '主题: 暗色', group: '外观' },
  { id: 'theme-light', label: '主题: 亮色', group: '外观' },
  { id: 'theme-dim', label: '主题: Dim(长时盯盘)', group: '外观', hint: '只降对比度' },
  { id: 'focus-toggle', label: '焦点模式: 开关', group: '外观', hint: '收起侧栏/细节' },
  { id: 'copy-link', label: '复制当前页链接', group: '操作' },
]

/** 执行动作。密度/主题写 html data 属性 + localStorage, 与既有设置页同一套存储。 */
function runAction(id: string, toast: (m: string, t?: 'success' | 'info' | 'error') => void) {
  const html = document.documentElement
  const set = (k: string, v: string) => {
    html.setAttribute(k, v)
    try {
      localStorage.setItem(`sida_${k.replace('data-', '')}`, v)
    } catch {
      /* 隐私模式忽略 */
    }
  }
  if (id.startsWith('density-')) {
    set('data-density', id.replace('density-', ''))
    toast(`密度已切到 ${id.replace('density-', '')}`, 'success')
    return
  }
  if (id.startsWith('theme-')) {
    const want = id.replace('theme-', '')
    // 与 useTheme 同一套存储(panwatch-theme / panwatch-dim), 保证下次启动读到的一致
    html.classList.remove('light', 'dark')
    if (want === 'dim') {
      html.classList.add('dark')
      html.setAttribute('data-theme', 'dim')
      localStorage.setItem('panwatch-dim', '1')
    } else {
      html.classList.add(want)
      html.removeAttribute('data-theme')
      localStorage.setItem('panwatch-dim', '0')
      localStorage.setItem('panwatch-theme', want)
    }
    toast(want === 'dim' ? '已切到 Dim(长时盯盘)' : '主题已切换', 'success')
    return
  }
  if (id === 'focus-toggle') {
    const on = html.getAttribute('data-focus-mode') === '1'
    set('data-focus-mode', on ? '0' : '1')
    window.dispatchEvent(new CustomEvent('sida:focus-mode', { detail: !on }))
    toast(on ? '已退出焦点模式' : '已进入焦点模式(再执行一次可退出)', 'success')
    return
  }
  if (id === 'copy-link') {
    void navigator.clipboard?.writeText(window.location.href).then(
      () => toast('链接已复制', 'success'),
      () => toast('复制失败(浏览器未授权剪贴板)', 'error'),
    )
  }
}

// ── frecency(2026-09-20): 最近使用项 ──────────────────────────────────────────
const RECENT_KEY = 'sida_palette_recent'
const RECENT_MAX = 8

function itemKey(it: Item): string {
  return it.type === 'stock' ? `stock:${it.symbol}` : it.type === 'page' ? `page:${it.to}` : `action:${it.id}`
}

function readRecent(): string[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY)
    const arr = raw ? (JSON.parse(raw) as unknown) : []
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

function pushRecent(key: string) {
  try {
    const next = [key, ...readRecent().filter((k) => k !== key)].slice(0, RECENT_MAX)
    localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch {
    /* 隐私模式忽略 */
  }
}

export default function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  const { toast } = useToast()
  const [q, setQ] = useState('')
  const [stocks, setStocks] = useState<StockHit[]>([])
  const [loading, setLoading] = useState(false)
  const [active, setActive] = useState(0)
  const [busy, setBusy] = useState(false)
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
  const actionHits = ACTION_COMMANDS.filter((c) => !q.trim() || c.label.includes(q.trim()))
  // frecency(2026-09-20): 最近用过的排在前面 —— 面板的价值在"常用项零思考命中"
  const recent = readRecent()
  const rank = (key: string) => {
    const i = recent.indexOf(key)
    return i === -1 ? Number.MAX_SAFE_INTEGER : recent.length - i
  }
  const items: Item[] = [
    ...pageHits.map((c): Item => ({ type: 'page', to: c.to, label: c.label, group: c.group })),
    ...actionHits.map((c): Item => ({ type: 'action', id: c.id, label: c.label, group: c.group, hint: c.hint })),
    ...stocks.map((s): Item => ({ type: 'stock', symbol: s.symbol, name: s.name, market: s.market })),
  ]
    .sort((a, b) => rank(itemKey(b)) - rank(itemKey(a)))
    .slice(0, 12)

  useEffect(() => {
    setActive(0)
  }, [q])

  if (!open) return null

  const run = (item: Item) => {
    pushRecent(itemKey(item))
    if (item.type === 'stock') {
      // 详情页也带来路(2026-09-20): 用户从面板跳进去同样要能一步回来
      navigate(`/analysis/${item.symbol}/${localDate()}`, { state: navBackState('/', '首页') })
    } else if (item.type === 'action') {
      runAction(item.id, toast)
    } else {
      navigate(item.to)
    }
    onClose()
  }

  /** A2: Shift+Enter 直接加自选; 重复添加给显式提示而非报错。 */
  const addToWatchlist = async (item: { symbol: string; name: string; market: string }) => {
    if (busy) return
    setBusy(true)
    try {
      await fetchAPI('/stocks', {
        method: 'POST',
        body: JSON.stringify({ symbol: item.symbol, name: item.name, market: item.market }),
      })
      toast(`已加自选：${item.name}`, 'success')
      onClose()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      if (msg.includes('已存在')) {
        toast(`已在自选：${item.name}`, 'info')
      } else {
        toast(`加自选失败：${msg}`, 'error')
      }
    } finally {
      setBusy(false)
    }
  }

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    // 输入框内**不劫持**裸 j/k(那是打字); 用 Ctrl/Alt+J/K 承担"下/上一项"
    if ((e.ctrlKey || e.altKey) && (e.key.toLowerCase() === 'j' || e.key.toLowerCase() === 'k')) {
      e.preventDefault()
      const dir = e.key.toLowerCase() === 'j' ? 1 : -1
      setActive((a) => Math.min(Math.max(a + dir, 0), items.length - 1))
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((a) => Math.min(a + 1, items.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((a) => Math.max(a - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const it = items[active]
      if (!it) return
      if (e.shiftKey && it.type === 'stock') {
        void addToWatchlist(it)
      } else {
        run(it)
      }
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
        className="relative w-full max-w-lg overflow-hidden rounded-xl border border-border bg-s4 shadow-float"
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
            className="flex-1 bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted-foreground/60"
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
              key={itemKey(it)}
              onMouseEnter={() => setActive(i)}
              onClick={() => run(it)}
              className={`flex w-full items-center gap-2.5 px-4 py-2 text-left ${i === active ? 'bg-accent' : ''}`}
            >
              {it.type === 'stock' ? (
                <>
                  <TrendingUp className="h-4 w-4 shrink-0 text-primary" />
                  <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">{it.name}</span>
                  {i === active && (
                    <span className="flex shrink-0 items-center gap-0.5 text-[10px] text-primary/80">
                      <Plus className="h-3 w-3" />
                      ⇧↵ 加自选
                    </span>
                  )}
                  <span className="shrink-0 text-[11px] text-muted-foreground">
                    {it.symbol} · {it.market}
                  </span>
                </>
              ) : it.type === 'action' ? (
                <>
                  <Zap className="h-4 w-4 shrink-0 text-[hsl(var(--role-opp))]" />
                  <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">{it.label}</span>
                  {it.hint && <span className="shrink-0 text-[10px] text-muted-foreground/70">{it.hint}</span>}
                  <span className="shrink-0 text-[11px] text-muted-foreground">{it.group}</span>
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
            <kbd className="rounded bg-muted px-1">⇧↵</kbd> 加自选
          </span>
          <span>
            <kbd className="rounded bg-muted px-1">esc</kbd> 关闭
          </span>
        </div>
      </div>
    </div>
  )
}
