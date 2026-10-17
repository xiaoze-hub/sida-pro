import { useCallback, useEffect, useRef, useState } from 'react'
import { usePausedInterval } from '@panwatch/base-ui'
import { RefreshCw, UserPlus, Eye } from 'lucide-react'
import { fetchAPI, stocksApi } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { chaseBadge, type GapStudyResp } from '@/lib/gap-study'
import { safeFixed } from '@/lib/format'
import CaliberBadge from '@panwatch/biz-ui/components/CaliberBadge'

/**
 * 竞价异动池 Tab(2026-08-24, v0.3.1 修复字段口径)
 * 挂在机会页 Opportunities 的竞价异动 Tab 内。
 * 数据 GET /api/auction/anomaly?market=CN (fetchAPI 自动补 /api 前缀)。
 *
 * 口径(2026-08-24):
 *   - gap_pct       竞价相对昨收涨幅%(二次计算: (价格/昨收 - 1)*100, 昨收取
 *                   PG klines hypertable 该 symbol ts<今天 的最近一条 close)。
 *                   脏数据过滤: 涨停试盘/跌停试盘 且 价格<=1.01 且 |gap|>30% -> 置 None。
 *   - withdraw_rate 撤单率%(数据源不提供, 固定 None, 表格显 '—')。
 *   - volume_ratio  竞价量比(数据源不提供, 固定 None, 表格显 '—')。
 * 响应带 missing_fields / note, 表头加 tooltip 注明数据源不含此字段。
 *
 * 行色: 高开 ≥+3% 染红, 低开 ≤-3% 染绿(红=上涨, 绿=下跌, A股口径)。
 * 交互: 行点击"查看详情"(onOpenDetail 回调, 由宿主注入); "+自选" 调 stocksApi.create。
 * 30s 轮询刷新。
 */

export interface AuctionAnomalyItem {
  code?: string
  symbol?: string
  name?: string
  gap_pct?: number | null
  withdraw_rate?: number | null
  volume_ratio?: number | null
  open_amount?: number | null
  open_zt_buy?: number | null
  demon_score?: number | null
  demon_grade?: string | null
  demon_streak?: number | null
  verdict_tag?: string | null
  verdict_action?: string | null
  verdict_level?: string | null
  note?: string
  [key: string]: unknown
}

export interface AuctionAnomalyResp {
  available: boolean
  count: number
  records: AuctionAnomalyItem[]
  note?: string
  /** 2026-08-24: 后端声明"本接口不提供"的字段,前端对应列显 '—' */
  missing_fields?: string[]
}

interface AuctionAnomalyTabProps {
  market?: string
  /** 点击行"查看详情"回调(宿主注入, 挂载 StockInsightModal) */
  onOpenDetail?: (symbol: string, market: string, name?: string) => void
}

function fmtMoney(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v) || v <= 0) return '—'
  if (v >= 1e8) return `${safeFixed(v / 1e8, 2)} 亿`
  if (v >= 1e4) return `${safeFixed(v / 1e4, 0)} 万`
  return `${safeFixed(v, 0)} 元`
}

function fmtPct(v: number | null | undefined, plus = true): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const sign = plus ? (v > 0 ? '+' : '') : ''
  return `${sign}${safeFixed(v, 2)}%`
}

function gapRowClass(gap: number | null | undefined): string {
  if (gap == null || !Number.isFinite(gap)) return ''
  // 涨跌色统一走设计令牌 --stock-up/--stock-down (红涨绿跌, A股口径)
  if (gap >= 3) return 'bg-stock-up/[0.08]'
  if (gap <= -3) return 'bg-stock-down/10'
  return ''
}

function gapColor(gap: number | null | undefined): string {
  if (gap == null || !Number.isFinite(gap)) return 'text-muted-foreground'
  return gap > 0 ? 'text-stock-up' : gap < 0 ? 'text-stock-down' : 'text-muted-foreground'
}

/** 高开分档实证(v0.5.80 A7)刷新频率: 结论以周计变化, 半天一次足够(后端另有 1 天缓存)。 */
const GAP_STUDY_REFRESH_MS = 6 * 3600_000

export default function AuctionAnomalyTab({ market = 'CN', onOpenDetail }: AuctionAnomalyTabProps) {
  const { toast } = useToast()
  const [data, setData] = useState<AuctionAnomalyResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [adding, setAdding] = useState<Set<string>>(new Set())
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [gapStudy, setGapStudy] = useState<GapStudyResp | null>(null)
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    setError('')
    try {
      const res = await fetchAPI<AuctionAnomalyResp>(`/auction/anomaly?market=${encodeURIComponent(market)}`, {
        cacheMode: 'reload',
      })
      if (mountedRef.current) setData(res)
      if (mountedRef.current) setUpdatedAt(new Date())
    } catch (e) {
      if (mountedRef.current) setError(e instanceof Error ? e.message : '竞价异动池加载失败')
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [market])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])
  // 页签 keep-alive(2026-10-15): 隐藏页签停表, 回前台补一次(30s 刷新)。
  usePausedInterval(() => {
    setLoading(true)
    void load()
  }, 30000, { deps: [load] })

  // 高开分档实证: 拉不到就只是不贴徽标, 绝不影响竞价池本身
  const loadStudy = useCallback(async () => {
    try {
      const r = await fetchAPI<GapStudyResp>('/gap-study?days=250')
      if (mountedRef.current) setGapStudy(r?.available ? r : null)
    } catch {
      if (mountedRef.current) setGapStudy(null)
    }
  }, [])
  // 页签 keep-alive(2026-10-15): 隐藏页签停表, 回前台补一次。
  usePausedInterval(loadStudy, GAP_STUDY_REFRESH_MS)

  const addToWatchlist = useCallback(
    async (item: AuctionAnomalyItem) => {
      const code = String(item.symbol || item.code || '').trim()
      if (!code) return
      if (adding.has(code)) return
      const next = new Set(adding)
      next.add(code)
      setAdding(next)
      try {
        await stocksApi.create({ symbol: code, name: String(item.name || code).trim(), market })
        toast(`已加入自选 ${item.name || code}`, 'success')
      } catch (e) {
        toast(e instanceof Error ? e.message : '加入自选失败', 'error')
      } finally {
        setAdding((prev) => {
          const n = new Set(prev)
          n.delete(code)
          return n
        })
      }
    },
    [adding, market, toast]
  )

  const records = data?.records ?? []
  const total = data?.count ?? records.length
  const syncAt = updatedAt
    ? updatedAt.toLocaleTimeString('zh-CN', { hour12: false })
    : '--'

  return (
    <div className="card p-3 md:p-4">
      {/* 顶栏: 09:25 同步时间 + 总数 + 刷新 */}
      <div className="flex items-center justify-between gap-2 flex-wrap mb-2">
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <Eye className="w-3.5 h-3.5" />
          <span>
            09:25 集合竞价异动池 · 同步 <span className="font-mono text-foreground">{syncAt}</span>
          </span>
          <span className="opacity-40">|</span>
          <span>
            共 <span className="font-mono text-foreground">{total}</span> 只
          </span>
          <CaliberBadge caliber="ths" label="thsdk 竞价" className="text-[10px]" />
        </div>
        <Button variant="secondary" size="sm" className="h-7 text-[12px]" onClick={() => { setLoading(true); void load() }} disabled={loading}>
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} /> 刷新
        </Button>
      </div>

      {error && <div className="text-[11px] text-amber-600 dark:text-amber-500 mb-2">{error}</div>}
      {data && !data.available && <div className="text-[11px] text-muted-foreground mb-2">{data.note || '竞价异动数据暂不可用'}</div>}

      <div className="overflow-x-auto">
        <table className="w-full text-[12px]">
          <thead>
            <tr className="text-[11px] text-muted-foreground border-b border-border/60">
              <th className="text-left py-1.5 pr-2 w-9">#</th>
              <th className="text-left py-1.5 pr-2">代码</th>
              <th className="text-left py-1.5 pr-2">名称</th>
              <th className="text-right py-1.5 pr-2">竞价涨幅</th>
              <th className="text-right py-1.5 pr-2">竞价成交额</th>
              <th className="text-right py-1.5 pr-2">竞价量比</th>
              <th className="text-right py-1.5 pr-2">撤单率</th>
              <th className="text-left py-1.5 px-2">9:25 主力意图与买卖建议</th>
              <th className="text-right py-1.5">操作</th>
            </tr>
          </thead>
          <tbody>
            {records.map((r, i) => {
              const code = String(r.symbol || r.code || '')
              const name = String(r.name || code || '')
              const gap = typeof r.gap_pct === 'number' ? r.gap_pct : null
              const withdraw = typeof r.withdraw_rate === 'number' ? r.withdraw_rate : null
              const vol = typeof r.volume_ratio === 'number' ? r.volume_ratio : null
              const openAmt = typeof r.open_amount === 'number' ? r.open_amount : null
              const isAdding = !!code && adding.has(code)
              return (
                <tr
                  key={code || i}
                  className={`border-b border-border/30 hover:bg-accent/40 ${gapRowClass(gap)}`}
                >
                  <td className="py-1.5 pr-2 text-[10px] text-muted-foreground">{i + 1}</td>
                  <td
                    className="py-1.5 pr-2 font-mono text-muted-foreground cursor-pointer hover:text-primary"
                    title="查看详情"
                    onClick={() => code && onOpenDetail?.(code, market, name)}
                  >
                    {code}
                  </td>
                  <td
                    className="py-1.5 pr-2 font-medium text-foreground cursor-pointer hover:text-primary"
                    title="查看详情"
                    onClick={() => code && onOpenDetail?.(code, market, name)}
                  >
                    <div className="flex items-center gap-1.5">
                      <span>{name}</span>
                      {r.demon_score && r.demon_score >= 75 ? (
                        <span className="rounded bg-red-500/10 px-1 py-0.5 text-[10px] font-semibold text-red-500 border border-red-500/20">
                          {r.demon_grade || '极妖'}
                        </span>
                      ) : null}
                    </div>
                  </td>
                  <td className={`py-1.5 pr-2 text-right font-mono tabular-nums ${gapColor(gap)}`}>
                    {fmtPct(gap)}
                    {(() => {
                      const badge = chaseBadge(gapStudy, gap)
                      return badge ? (
                        <span
                          className="mt-0.5 block text-[10px] font-sans text-red-600 dark:text-red-400"
                          title={badge.note}
                        >
                          {badge.label}
                        </span>
                      ) : null
                    })()}
                  </td>
                  <td className="py-1.5 pr-2 text-right font-mono tabular-nums text-foreground font-medium">
                    {fmtMoney(openAmt)}
                  </td>
                  <td className={`py-1.5 pr-2 text-right font-mono tabular-nums ${vol && vol >= 3 ? 'text-amber-500 font-bold' : 'text-foreground'}`}>
                    {vol == null ? '—' : `${safeFixed(vol, 1)} 倍`}
                  </td>
                  <td className="py-1.5 pr-2 text-right font-mono tabular-nums text-muted-foreground">
                    {withdraw == null ? '—' : `${safeFixed(withdraw, 1)}%`}
                  </td>
                  <td className="py-1.5 px-2 text-left">
                    {r.verdict_tag ? (
                      <div className="flex items-center gap-1.5">
                        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                          r.verdict_level === 'strong_buy' ? 'bg-red-500/20 text-red-500 font-bold' :
                          r.verdict_level === 'buy' ? 'bg-amber-500/20 text-amber-500 font-semibold' :
                          r.verdict_level === 'warning' ? 'bg-yellow-500/20 text-yellow-600 dark:text-yellow-400' :
                          r.verdict_level === 'risk' ? 'bg-green-500/20 text-green-500' :
                          'bg-muted text-muted-foreground'
                        }`}>
                          {r.verdict_tag}
                        </span>
                        <span className="text-[11px] text-muted-foreground truncate max-w-[200px]" title={r.verdict_action || ''}>
                          {r.verdict_action}
                        </span>
                      </div>
                    ) : (
                      <span className="text-[11px] text-muted-foreground">常态观察</span>
                    )}
                  </td>
                  <td className="py-1.5 text-right">
                    {code ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 px-1.5 text-[10px]"
                        disabled={isAdding}
                        onClick={() => void addToWatchlist(r)}
                      >
                        {isAdding ? (
                          <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                        ) : (
                          <UserPlus className="w-3 h-3" />
                        )}
                        自选
                      </Button>
                    ) : (
                      <span className="inline-block w-6" />
                    )}
                  </td>
                </tr>
              )
            })}
            {records.length === 0 && !loading && (
              <tr>
                <td colSpan={7} className="py-8 text-center text-[11px] text-muted-foreground">
                  暂无竞价异动 (非交易时段 / 数据源未接入 / 无高波动标的)
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {loading && records.length === 0 && (
        <div className="mt-3 h-[120px] rounded-xl border border-border/50 animate-pulse bg-accent/20" />
      )}
    </div>
  )
}
