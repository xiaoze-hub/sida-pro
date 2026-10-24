import { useCallback, useEffect, useRef, useState } from 'react'
import { usePausedInterval } from '@panwatch/base-ui'
import { RefreshCw, UserPlus, Eye } from 'lucide-react'
import { fetchAPI, stocksApi } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { chaseBadge, type GapStudyResp } from '@/lib/gap-study'
import { safeFixed } from '@/lib/format'
import { normSymbol } from '@/lib/symbol'
import CaliberBadge from '@panwatch/biz-ui/components/CaliberBadge'

/**
 * 竞价异动池 Tab(2026-08-24, v0.3.1 修复字段口径)
 * 挂在机会页 Opportunities 的竞价异动 Tab 内。
 * 数据 GET /api/auction/anomaly?market=CN (fetchAPI 自动补 /api 前缀)。
 *
 * 口径(2026-08-24 v0.3.2 修正, 勿再按旧注释理解):
 *   - gap_pct       竞价涨跌幅%: 由"异动类型 + 价格列"直接推导(价格列是幅度小数, 不是价格)。
 *   - withdraw_rate 撤单率%: 仅"涨停撤单/跌停撤单"类型有值; TQ 增强成功后按
 *                   BCancel/(TotalBVol+BCancel) 覆盖为真实值。**不是固定 None**。
 *   - volume_ratio  竞价量比: 仅 thsdk 提供不了, TQ 逐票增强成功时才有值, 否则 '—'。
 * 响应带 missing_fields / note, 表头加 tooltip 注明数据源不含此字段。
 *
 * 2026-10-18 审计 A-03(P0): 表头"同步时刻"改为**后端给的 data_date / trade_phase /
 * is_live / data_clock**。旧实现用 `new Date()`(浏览器本地时钟), 周日打开会把上周五
 * 9:25 的池子说成"刚刚同步" —— 数据时刻永远由数据源/服务端给, 不许前端猜。
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
  // ── 2026-10-18 审计 A-03: 数据日期 / 交易阶段(严禁用浏览器时钟替代) ──
  /** 池子所属交易日(非交易日回看最近交易日) */
  data_date?: string | null
  trade_phase?: string
  phase_label?: string
  is_trading_day?: boolean
  /** 仅竞价窗口内(09:15-09:25)且数据就是今天的才为 true */
  is_live?: boolean
  is_today?: boolean
  /** 服务端组装时刻(不是浏览器时间) */
  as_of?: string
  /** 源端自带时间, 如 "09:25:03"; 源不给则 null */
  data_clock?: string | null
  /** 最近一次落库同步时刻 */
  data_synced_at?: string | null
  /** 空池原因码: source_unavailable / non_trading_day / non_auction_session / no_anomaly */
  reason?: string | null
  reason_text?: string
  /** live / replay(盘前盘后回放归档) / snapshot(今日已定型) / empty */
  mode?: 'live' | 'replay' | 'snapshot' | 'empty'
  source?: string
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
  const [gapStudy, setGapStudy] = useState<GapStudyResp | null>(null)
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    setError('')
    try {
      const res = await fetchAPI<AuctionAnomalyResp>(`/auction/anomaly?market=${encodeURIComponent(market)}`, {
        cacheMode: 'reload',
      })
      if (mountedRef.current) setData(res)
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
      const code = normSymbol(String(item.symbol || item.code || ''))
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
  // A-03: 数据时刻只认后端 —— 实时看源端时钟(data_clock), 非实时看数据日 + 阶段
  const liveClock = data?.data_clock || (data?.as_of ? data.as_of.slice(11, 19) : '--')
  const dataDate = data?.data_date || '--'
  const phaseLabel = data?.phase_label || '阶段未知'
  // 空态原因（2026-10-24 L2-C）：三种原因三种文案，且空态必须给「何时有数据」+ 一个动作出口，
  // 不许只留一块空表让用户以为「今天没有异动」。
  const emptyReason = !data
    ? '未取到竞价数据（请求未返回或超时），请点右上「刷新」重试'
    : data.reason_text ||
      (data.reason === 'source_unavailable'
        ? '数据源不可用(thsdk 竞价异动拉取失败)，非「今日无异常」'
        : data.reason === 'non_trading_day'
          ? '非交易日，竞价池无当日数据'
          : data.reason === 'non_auction_session'
            ? `非竞价时段（当前${phaseLabel}），无当日竞价快照`
            : data.reason === 'no_anomaly'
              ? '今日集合竞价无异动标的'
              : '暂无竞价异动数据')

  return (
    <div className="card p-3 md:p-4">
      {/* 顶栏: 09:25 同步时间 + 总数 + 刷新 */}
      <div className="flex items-center justify-between gap-2 flex-wrap mb-2">
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <Eye className="w-3.5 h-3.5" />
          {data?.is_live ? (
            <span>
              竞价池 · <span className="font-semibold text-stock-up">实时</span>{' '}
              <span className="font-mono text-foreground">{liveClock}</span>
            </span>
          ) : (
            <span>
              竞价池 · 数据日 <span className="font-mono text-foreground">{dataDate}</span>
              <span className="ml-1 rounded bg-muted px-1.5 py-0.5 text-[10px]">
                {data?.mode === 'replay'
                  ? '盘后回放'
                  : data?.is_today
                    ? `${phaseLabel}(已定型)`
                    : `${phaseLabel}快照`}
              </span>
            </span>
          )}
          <span className="opacity-40">|</span>
          <span>
            {/* 2026-10-24: 首屏冷查询实测 31s（/api/auction/anomaly 冷缓存），
                期间 data=null、total=0 —— 旧实现如实渲染成「共 0 只」，
                用户读作「今天没有异动」，与「还在加载」是两件事。 */}
            {loading && !data ? (
              <>加载中<span className="font-mono text-foreground">…</span></>
            ) : (
              <>
                共 <span className="font-mono text-foreground">{total}</span> 只
              </>
            )}
          </span>
          <CaliberBadge caliber="ths" label="thsdk 竞价" className="text-[10px]" />
        </div>
        <Button variant="secondary" size="sm" className="h-7 text-[12px]" onClick={() => { setLoading(true); void load() }} disabled={loading}>
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} /> 刷新
        </Button>
      </div>

      {error && <div className="text-[11px] text-amber-600 dark:text-amber-500 mb-2">{error}</div>}
      {data && !data.available && <div className="text-[11px] text-muted-foreground mb-2">{data.note || '竞价异动数据暂不可用'}</div>}

      {records.length === 0 && !loading ? (
        <div className="rounded-xl border border-dashed border-border/70 px-4 py-5 text-center" data-auction-empty="1">
          <div className="text-[13px] font-medium text-foreground">{emptyReason}</div>
          <div className="mt-1 text-[12px] text-muted-foreground">
            竞价池每天 09:15 开始刷新、09:25 撮合定型；非交易日展示最近一个交易日的快照。
          </div>
          <div className="mt-3">
            <Button variant="outline" size="sm" className="h-8 text-[12px]" onClick={() => { setLoading(true); void load() }} disabled={loading}>
              <RefreshCw className="w-3.5 h-3.5" /> 重新查询
            </Button>
          </div>
        </div>
      ) : (
      <div className="overflow-x-auto">
        <table className="w-full text-[12px]">
          <thead>
            <tr className="text-[11px] text-muted-foreground border-b border-border/60">
              <th className="text-left py-1.5 pr-2 w-9">#</th>
              <th className="text-left py-1.5 pr-2" title="证券代码；点击可查看详情">代码</th>
              <th className="text-left py-1.5 pr-2" title="证券简称；带「极妖」徽标表示妖股评分 ≥75">名称</th>
              <th className="text-right py-1.5 pr-2" title="竞价涨幅 = 开盘价相对昨收；与「异动幅度」不是同一口径">竞价涨幅</th>
              <th className="text-right py-1.5 pr-2" title="9:25 竞价撮合成交额（自动按 亿/万/元 取档）">竞价成交额</th>
              <th className="text-right py-1.5 pr-2" title="竞价量比 = 竞价成交量 ÷ 过去 5 日同时段均量；仅 TQ 逐票增强成功时有值，否则「—」">竞价量比</th>
              <th className="text-right py-1.5 pr-2" title="撤单率 = BCancel ÷ (总买量 + BCancel)，仅涨跌停撤单类型有值">撤单率</th>
              <th className="text-left py-1.5 px-2" title="9:25 定型后的盘口判读：强买 / 买入 / 警示 / 风险；「常态观察」= 无异动特征">9:25 盘口意图判定</th>
              <th className="text-right py-1.5" title="加入自选（写入你的自选池，不改变竞价数据）">操作</th>
            </tr>
          </thead>
          <tbody>
            {records.map((r, i) => {
              const code = normSymbol(String(r.symbol || r.code || ''))
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
                    {/* 2026-10-20 口径修正: gap 现在只认「开盘价 vs 昨收」。THS 异动幅度
                        另列一行小字 —— 它可以是 +4.87% 而实际低开 -1.75%(金健米业事故)。 */}
                    {(() => {
                      const ratio = (r as { anomaly_ratio?: number | null }).anomaly_ratio
                      if (ratio == null || gap == null || Math.abs(ratio - gap) < 0.05) return null
                      return (
                        <span className="mt-0.5 block text-[10px] font-sans text-muted-foreground/80"
                              title="异动幅度：9:25 那一瞬的涨速，不等于相对昨收的竞价涨幅">
                          异动 {ratio > 0 ? '+' : ''}{safeFixed(ratio, 2)}%
                        </span>
                      )
                    })()}
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
          </tbody>
        </table>
      </div>
      )}
      {loading && records.length === 0 && (
        <div className="mt-3 h-[120px] rounded-xl border border-border/50 animate-pulse bg-accent/20" />
      )}
    </div>
  )
}
