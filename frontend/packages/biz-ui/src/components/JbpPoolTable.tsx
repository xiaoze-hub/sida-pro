import { useCallback, useState } from 'react'
import { Loader2, ScanSearch } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'

/**
 * 聚宝盆选股(数智决策 规格 §4.2, 2026-10-10)。
 *
 * 官方选股流程: **暗盘资金流入 AND AI机构活跃度>6(>12更佳) AND GS在G区+G信号 AND 问财关键词**。
 * 逐条件 AND 联合筛选, 表格给出**逐条件通过/未过/降级明细**(证据非建议); 任一必需条件缺数据
 * → 显式降级、该股不出池(后端已保证, 前端只如实展示)。
 * embedded: 嵌入选股工具卡等宿主容器时为 true —— 去掉自带 card 壳与标题。
 */

type CondStatus = 'pass' | 'fail' | 'degraded' | 'na'

interface JbpCondition {
  key: string
  name: string
  status: CondStatus
  met: boolean | null
  evidence: Record<string, unknown>
  detail: string
}

interface JbpRow {
  symbol: string
  in_pool: boolean
  conditions: JbpCondition[]
  dark_net: number | null
  activity: number | null
  activity_level: string | null
  better: boolean
  gs_zone: string | null
  gs_signal: string | null
}

interface JbpResponse {
  universe: number
  scanned: number
  in_pool: number
  rows: JbpRow[]
  wencai: { provided: boolean; available: boolean | null; hits: number; note: string }
  filters: Record<string, string>
  note: string | null
}

const DEFAULT_SYMBOLS = '002361, 600519, 300750'

/** 数字格式化(R6: 禁裸 toFixed —— 不走棘轮基线)。 */
function fmtNum(v: unknown, digits = 2): string {
  const n = Number(v)
  if (!Number.isFinite(n)) return '--'
  const p = 10 ** digits
  return String(Math.round(n * p) / p)
}

/** 元 → 万元(带符号); 缺数据显式 '--'。 */
function fmtWan(v: unknown): string {
  const n = Number(v)
  if (!Number.isFinite(n)) return '--'
  const w = n / 1e4
  const s = Math.round(Math.abs(w) * 10) / 10
  return `${w >= 0 ? '+' : '-'}${s}万`
}

const STATUS_META: Record<CondStatus, { label: string; cls: string }> = {
  pass: { label: '✓ 通过', cls: 'text-emerald-600 dark:text-emerald-500' },
  fail: { label: '✗ 未过', cls: 'text-muted-foreground' },
  degraded: { label: '⚠ 降级', cls: 'text-amber-600 dark:text-amber-500' },
  na: { label: '— 未启用', cls: 'text-muted-foreground/70' },
}

function condCell(row: JbpRow, key: string) {
  const c = row.conditions.find((x) => x.key === key)
  if (!c) return <span className="text-[12px] text-muted-foreground">--</span>
  const meta = STATUS_META[c.status] || STATUS_META.fail
  return (
    <div className="leading-tight">
      <span className={`text-[12px] ${meta.cls}`} data-testid={`jbp-cond-${row.symbol}-${key}`}>
        {meta.label}
      </span>
      <div className="text-[10px] text-muted-foreground/80 max-w-[220px] truncate" title={c.detail}>
        {c.detail}
      </div>
    </div>
  )
}

export default function JbpPoolTable({ embedded = false }: { embedded?: boolean }) {
  const [symbols, setSymbols] = useState(DEFAULT_SYMBOLS)
  const [keywords, setKeywords] = useState('')
  const [data, setData] = useState<JbpResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const run = useCallback(async () => {
    const codes = symbols
      .split(/[,，\s]+/)
      .map((s) => s.trim())
      .filter((s) => /^\d{6}$/.test(s))
    if (!codes.length) {
      setError('请输入至少一个6位股票代码')
      return
    }
    setLoading(true)
    setError('')
    try {
      const res = await fetchAPI<JbpResponse>('/stock-pool/jbp', {
        method: 'POST',
        body: JSON.stringify({ symbols: codes, wencai_keywords: keywords.trim() || undefined }),
      })
      setData(res)
    } catch (e) {
      setData(null)
      setError(e instanceof Error ? e.message : '聚宝盆选股失败')
    } finally {
      setLoading(false)
    }
  }, [symbols, keywords])

  const wencaiDown = !!data && data.wencai?.provided && data.wencai.available === false

  return (
    <div className={embedded ? '' : 'card p-4 mb-4'} data-testid="jbp-pool-table">
      {!embedded && (
        <div className="flex items-center gap-2 mb-3">
          <ScanSearch className="w-4 h-4 text-primary" />
          <h2 className="text-[13px] font-semibold text-foreground">聚宝盆选股</h2>
          <span className="text-[11px] text-muted-foreground">
            暗盘流入 + 活跃度&gt;6(更佳&gt;12) + GS在G区且G信号 + 问财
          </span>
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <Input
          className="h-8 text-[12px] flex-1 min-w-[220px]"
          placeholder="6位股票代码, 逗号分隔, 如 002361,600519"
          value={symbols}
          onChange={(e) => setSymbols(e.target.value)}
          aria-label="聚宝盆股票池"
        />
        <Input
          className="h-8 text-[12px] flex-1 min-w-[200px]"
          placeholder="问财关键词(可选), 如: 均线多头排列,非ST"
          value={keywords}
          onChange={(e) => setKeywords(e.target.value)}
          aria-label="聚宝盆问财关键词"
        />
        <Button size="sm" className="h-8 text-[12px]" onClick={() => void run()} disabled={loading}>
          {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ScanSearch className="w-3.5 h-3.5" />}
          {loading ? '扫描中...' : '聚宝盆扫描'}
        </Button>
      </div>

      <div className="mt-2 text-[11px] text-muted-foreground">
        逐条件 AND: 缺数据显式降级、不入池(证据非建议)。
      </div>

      {error && (
        <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-[12px] text-red-400">
          {error}
        </div>
      )}

      {wencaiDown && (
        <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-[12px] text-amber-500">
          问财数据源不可用, 问财条件已显式降级, 本次不出池。{data?.note || ''}
        </div>
      )}

      {data && (
        <>
          <div className="mt-3 flex items-center gap-3 text-[11px] text-muted-foreground" data-testid="jbp-summary">
            <span>
              入池 <span className="text-rose-600 font-semibold">{data.in_pool}</span> / 扫描 {data.scanned}
            </span>
            {data.wencai?.provided && (
              <span>
                问财: {data.wencai.available === false ? '不可用(降级)' : `命中 ${data.wencai.hits}`}
              </span>
            )}
          </div>

          {data.rows.length === 0 ? (
            <div className="py-6 text-center text-[12px] text-muted-foreground">无候选(或全部条件未过)</div>
          ) : (
            <div className="mt-2 overflow-x-auto">
              <table className="w-full text-[12px] border-collapse">
                <thead>
                  <tr className="text-left text-[11px] text-muted-foreground border-b border-border/50">
                    <th className="py-1.5 pr-2 font-medium">代码</th>
                    <th className="py-1.5 pr-2 font-medium">暗盘资金流入</th>
                    <th className="py-1.5 pr-2 font-medium">AI活跃度&gt;6</th>
                    <th className="py-1.5 pr-2 font-medium">GS(G区+G信号)</th>
                    <th className="py-1.5 pr-2 font-medium">问财</th>
                    <th className="py-1.5 font-medium">入池</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((r) => (
                    <tr
                      key={r.symbol}
                      className="border-b border-border/30 align-top"
                      data-testid={`jbp-row-${r.symbol}`}
                    >
                      <td className="py-1.5 pr-2 font-mono">
                        {r.symbol}
                        <div className="text-[10px] text-muted-foreground/80" data-testid={`jbp-level-${r.symbol}`}>
                          {r.activity != null ? fmtNum(r.activity, 2) : '--'}
                          {r.activity_level ? `(${r.activity_level})` : ''}
                          {r.better ? ' 更佳' : ''}
                        </div>
                      </td>
                      <td className="py-1.5 pr-2">{condCell(r, 'dark_inflow')}</td>
                      <td className="py-1.5 pr-2">{condCell(r, 'activity')}</td>
                      <td className="py-1.5 pr-2">{condCell(r, 'gs')}</td>
                      <td className="py-1.5 pr-2">{condCell(r, 'wencai')}</td>
                      <td className="py-1.5">
                        <span
                          className={`text-[12px] ${r.in_pool ? 'text-rose-600 font-semibold' : 'text-muted-foreground'}`}
                          data-testid={`jbp-inpool-${r.symbol}`}
                        >
                          {r.in_pool ? '入选' : '未入选'}
                        </span>
                        <div className="text-[10px] text-muted-foreground/80">暗盘 {fmtWan(r.dark_net)}</div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="mt-1 text-[10px] text-muted-foreground/80">
                口径: 暗盘净额&gt;0 · 活跃度&gt;6(更佳&gt;12) · GS在G区且G信号 · 问财命中(传入关键词时必过)。
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
