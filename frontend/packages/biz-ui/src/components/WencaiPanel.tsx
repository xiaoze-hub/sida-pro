import { useCallback, useEffect, useState } from 'react'
import { Loader2, Search, Sparkles } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'

interface WencaiRow {
  symbol: string
  name: string
  [key: string]: string | number | null | undefined
}

interface WencaiResponse {
  available: boolean
  rows: WencaiRow[]
  note: string
}

/** 预设问财条件 chips */
const PRESET_QUERIES = [
  '主力净流入为负,但股价逆势上涨,非ST',
  '近5日主力净流入为正',
  '今日涨停,非ST',
]

const MAX_DISPLAY_ROWS = 50
const MAX_EXTRA_COLS = 3

/** 资金流类列(元) → 亿/万 可读格式 */
function fmtMoney(v: unknown): string {
  const n = Number(v)
  if (!Number.isFinite(n)) return '--'
  const abs = Math.abs(n)
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(n / 1e4).toFixed(1)}万`
  return n.toFixed(0)
}

/** 百分比类列(后端已 *100) → 带 % */
function fmtPct(v: unknown): string {
  const n = Number(v)
  if (!Number.isFinite(n)) return '--'
  return `${Number(n.toFixed(2))}%`
}

function fmtCell(col: string, v: unknown): string {
  if (v == null || v === '') return '--'
  if (typeof v === 'number') {
    if (/资金|流向|净额|流入|流出/.test(col)) return fmtMoney(v)
    if (/涨跌幅|涨幅|跌幅/.test(col)) return fmtPct(v)
    return Number(v.toFixed(2)).toString()
  }
  return String(v)
}

// embedded: 嵌入选股工具卡等宿主容器时为 true — 去掉自带 card 壳与标题(宿主已提供)
export default function WencaiPanel({ embedded = false }: { embedded?: boolean }) {
  const [query, setQuery] = useState('')
  const [rows, setRows] = useState<WencaiRow[]>([])
  const [available, setAvailable] = useState<boolean | null>(null)
  const [note, setNote] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [lastQuery, setLastQuery] = useState('')

  const run = useCallback(async (q: string) => {
    const trimmed = q.trim()
    if (!trimmed) {
      setError('请输入问财选股条件')
      return
    }
    setLoading(true)
    setError('')
    try {
      const res = await fetchAPI<WencaiResponse>(
        `/wencai?query=${encodeURIComponent(trimmed)}`,
        { cacheMode: false, timeoutMs: 30000 },
      )
      setAvailable(!!res?.available)
      setRows(res?.rows || [])
      setNote(res?.note || '')
      setLastQuery(trimmed)
    } catch (e) {
      setAvailable(false)
      setRows([])
      setError(e instanceof Error ? e.message : '问财查询失败')
      setNote('')
    } finally {
      setLoading(false)
    }
  }, [])

  const applyPreset = useCallback((q: string) => {
    setQuery(q)
    void run(q)
  }, [run])

  // 组件挂载后先跑一次默认预设, 面板立即可见能力
  useEffect(() => {
    void run(PRESET_QUERIES[0])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const shownRows = rows.slice(0, MAX_DISPLAY_ROWS)
  const extraCols = rows.length > 0
    ? Object.keys(rows[0]).filter(k => k !== 'symbol' && k !== 'name').slice(0, MAX_EXTRA_COLS)
    : []

  const dataSourceDown = available === false && !error && note.includes('未接入')

  return (
    <div className={embedded ? '' : 'card p-4 mb-4'}>
      {!embedded && (
        <div className="flex items-center gap-2 mb-3">
          <Sparkles className="w-4 h-4 text-primary" />
          <h2 className="text-[14px] font-semibold text-foreground">问财选股</h2>
          <span className="text-[11px] text-muted-foreground">同花顺 AI 自然语言选股(需 L2 问财数据源)</span>
        </div>
      )}

      {/* 预设条件 chips */}
      <div className="flex items-center gap-2 flex-wrap mb-3">
        {PRESET_QUERIES.map(q => (
          <button
            key={q}
            type="button"
            onClick={() => applyPreset(q)}
            disabled={loading}
            className={`rounded-full border px-2.5 py-1 text-[11px] transition-colors ${
              lastQuery === q
                ? 'border-primary/50 bg-primary/10 text-primary'
                : 'border-border/50 bg-accent/30 text-muted-foreground hover:border-primary/35 hover:text-foreground'
            } disabled:opacity-50`}
          >
            {q}
          </button>
        ))}
      </div>

      {/* 自定义输入 + 查询按钮 */}
      <div className="flex items-center gap-2">
        <Input
          className="h-8 text-[12px] flex-1"
          placeholder="输入问财条件, 如: 均线多头排列,MACD金叉,非ST"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') void run(query) }}
        />
        <Button size="sm" className="h-8 text-[12px]" onClick={() => void run(query)} disabled={loading}>
          {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Search className="w-3.5 h-3.5" />}
          {loading ? '查询中...' : '问财选股'}
        </Button>
      </div>

      {/* 数据源未接入: 置灰说明 */}
      {dataSourceDown && (
        <div className="mt-3 rounded-lg border border-border/40 bg-accent/20 p-3 text-[12px] text-muted-foreground opacity-70">
          <div className="font-medium text-foreground/80">L2 问财数据源未接入</div>
          <div className="mt-1 text-[11px]">{note || '服务端未启用同花顺 L2 问财能力, 面板暂不可用'}</div>
        </div>
      )}

      {/* 查询失败 */}
      {error && (
        <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-[12px] text-red-400">
          {error}
        </div>
      )}

      {/* 加载中 */}
      {loading && (
        <div className="flex items-center justify-center gap-2 py-6 text-[12px] text-muted-foreground">
          <Loader2 className="w-4 h-4 animate-spin" />
          问财查询中...
        </div>
      )}

      {/* 空态 */}
      {!loading && available === true && rows.length === 0 && (
        <div className="py-6 text-center text-[12px] text-muted-foreground">
          {note || '未命中任何股票'}
        </div>
      )}

      {/* 结果列表(上限 50 行) */}
      {!loading && available === true && rows.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-[11px] text-muted-foreground border-b border-border/50">
                <th className="text-left py-1.5 pr-2">代码</th>
                <th className="text-left py-1.5 pr-2">名称</th>
                {extraCols.map(c => (
                  <th key={c} className="text-right py-1.5 pr-2 whitespace-nowrap">{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shownRows.map(r => (
                <tr key={r.symbol || `${r.name}-${Math.random()}`} className="border-b border-border/30 hover:bg-accent/40">
                  <td className="py-1.5 pr-2 font-mono text-muted-foreground">{r.symbol || '--'}</td>
                  <td className="py-1.5 pr-2 font-medium text-foreground whitespace-nowrap">{r.name || '--'}</td>
                  {extraCols.map(c => (
                    <td key={c} className="py-1.5 pr-2 text-right font-mono whitespace-nowrap">
                      {fmtCell(c, r[c])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length > MAX_DISPLAY_ROWS && (
            <div className="mt-2 text-[11px] text-muted-foreground">
              共命中 {rows.length} 只, 仅展示前 {MAX_DISPLAY_ROWS} 只
            </div>
          )}
          <div className="mt-1 text-[11px] text-muted-foreground/80">{note}</div>
        </div>
      )}
    </div>
  )
}
