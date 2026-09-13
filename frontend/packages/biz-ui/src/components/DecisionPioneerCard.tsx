import { useCallback, useEffect, useRef, useState } from 'react'
import { RefreshCw, ShieldAlert } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { activityLevelColor } from '../lib/stock-colors'
import ActivitySparkline from './ActivitySparkline'

/**
 * 数智决策三指标卡片(2026-08-30)
 * 数据来自 GET /api/decision-pioneer/{symbol}?market=CN
 * (fetchAPI 自动补 /api 前缀)。盘中每 30 秒自动刷新, 与分时图节奏一致。
 * 三指标 = GS策略(趋势) × AI机构活跃度(强度) × L2主力净流入(资金, 明盘口径, 非暗盘)。
 */

export interface InstitutionActivity {
  activity: number | null
  level: '大牛' | '强势' | '生命' | '弱' | string
  life_line: number
  strong_line: number
  bull_line: number
  streak_days: number
  ma5: number | null
  is_yang?: boolean
}

export interface GsSignal {
  signal: 'G' | 'S' | null | string
  state: 'G区' | 'S区' | string
  bb0: number
  a0: number
}

export interface L2Flow {
  available: boolean
  zjl_hb: number | null
  direction: string | null
  l2_tick_num: number | null
  l2_order_num: number | null
}

export interface DecisionPioneerResp {
  symbol: string
  institution_activity: InstitutionActivity | null
  gs: GsSignal | null
  l2: L2Flow | null
  data_time?: string
}

function fmtWan(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(v)) return '--'
  const wan = v / 10000
  return `${wan > 0 ? '+' : ''}${wan.toFixed(digits)}万`
}

function upColor(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return 'text-muted-foreground'
  // 涨跌色统一走设计令牌 --stock-up/--stock-down (红涨绿跌, A股口径)
  return v > 0 ? 'text-stock-up' : v < 0 ? 'text-stock-down' : 'text-muted-foreground'
}

/** 活跃度档位色 (09-03 收敛): 走三色 token, 不再硬编码 fuchsia/rose/orange */
function activityColor(level: string): string {
  return activityLevelColor(level)
}

export interface DecisionPioneerCardProps {
  symbol: string
  market: string
  /**
   * bare=true: 只出数据内容, 不出自己的卡壳(`mt-3 rounded-xl border border-border/50 bg-card p-3`)
   * 与自带标题/副标题行 —— 供「数智决策」合并卡(workbench/DecisionCard)在一张外层卡里合成,
   * 避免"卡中卡"双层边框与三级标题。默认 false, 既有调用点(DarkFlowCards/StockWorkbench)行为不变。
   */
  bare?: boolean
}

export default function DecisionPioneerCard({ symbol, market, bare = false }: DecisionPioneerCardProps) {
  const [data, setData] = useState<DecisionPioneerResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>('')
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    if (!symbol) return
    setError('')
    try {
      const res = await fetchAPI<DecisionPioneerResp>(
        `/decision-pioneer/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}`,
        { cacheMode: 'reload' },
      )
      if (mountedRef.current) setData(res)
    } catch (e) {
      if (mountedRef.current) setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [symbol, market])

  useEffect(() => {
    mountedRef.current = true
    setLoading(true)
    void load()
    const timer = window.setInterval(() => void load(), 30000)
    return () => {
      mountedRef.current = false
      window.clearInterval(timer)
    }
  }, [load])

  // 手动刷新按钮: 非 bare 挂在标题行右侧; bare 时标题行被外层卡替代, 挪到卡尾
  // (与「更新于 …」同行), 否则 bare 化会静默丢掉手动刷新入口(30s 自动轮询仍在)。
  const refreshButton = (
    <button
      type="button"
      title="刷新"
      onClick={() => {
        setLoading(true)
        void load()
      }}
      className="text-muted-foreground hover:text-foreground transition-colors"
    >
      <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
    </button>
  )

  if (loading && !data) {
    return bare ? (
      <div className="h-[110px] rounded-lg bg-accent/20 animate-pulse" />
    ) : (
      <div className="mt-3 h-[110px] rounded-xl border border-border/50 bg-card animate-pulse bg-accent/20" />
    )
  }

  if (error && !data) {
    const msg = (
      <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
        <ShieldAlert className="w-3.5 h-3.5" />
        <span>数智决策三指标暂不可用({error})</span>
      </div>
    )
    return bare ? msg : <div className="mt-3 rounded-xl border border-border/50 bg-card p-3">{msg}</div>
  }

  const act = data?.institution_activity ?? null
  const gs = data?.gs ?? null
  const l2 = data?.l2 ?? null

  const body = (
    <>
      {/* 标题行(含手动刷新): 非 bare 才有 —— bare 时标题与刷新入口由外层卡承载 */}
      {!bare ? (
        <div className="flex items-center justify-between gap-2 mb-2">
          <div className="flex items-center gap-2">
            <div className="text-[13px] font-semibold text-foreground">🧭 数智决策三指标</div>
            <span className="text-[10px] text-muted-foreground">GS趋势 × 机构活跃度 × L2资金</span>
          </div>
          {refreshButton}
        </div>
      ) : null}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-2 text-[11px]">
        {/* AI机构活跃度 */}
        <div className="rounded-lg bg-accent/20 px-2.5 py-1.5">
          <div className="text-muted-foreground mb-0.5">AI机构活跃度</div>
          {act ? (
            <>
              <div className="font-mono text-[15px] font-semibold" style={{ color: activityColor(act.level) }}>
                {act.activity != null ? act.activity.toFixed(2) : '--'}
                <span className="text-[10px] ml-1">{act.level}</span>
              </div>
              <div className="text-muted-foreground mt-0.5">
                连强{act.streak_days}日{act.ma5 != null ? ` · 5日均${act.ma5.toFixed(2)}` : ''}
              </div>
              <div className="text-[9px] text-muted-foreground/70 mt-0.5">
                生命1.56/强势3/大牛6
              </div>
            </>
          ) : (
            <div className="text-muted-foreground">无数据</div>
          )}
        </div>

        {/* GS策略(趋势过滤) */}
        <div className="rounded-lg bg-accent/20 px-2.5 py-1.5">
          <div className="text-muted-foreground mb-0.5">GS策略(趋势过滤)</div>
          {gs ? (
            <>
              <div className="font-mono text-[15px] font-semibold text-foreground">{gs.state}</div>
              <div className="text-muted-foreground mt-0.5">
                快线{gs.a0?.toFixed(2)} / 慢线{gs.bb0?.toFixed(2)}
              </div>
              <div className="text-[9px] text-muted-foreground/70 mt-0.5">
                方向过滤 · 买卖点滞后仅参考
              </div>
            </>
          ) : (
            <div className="text-muted-foreground">无数据</div>
          )}
        </div>

        {/* L2 主力净流入 */}
        <div className="rounded-lg bg-accent/20 px-2.5 py-1.5">
          <div className="text-muted-foreground mb-0.5">主力净流入(L2·TQ)</div>
          {l2?.available && l2.zjl_hb != null ? (
            <>
              <div className={`font-mono text-[15px] font-semibold ${upColor(l2.zjl_hb)}`}>
                {fmtWan(l2.zjl_hb)}
              </div>
              <div className="text-muted-foreground mt-0.5">{l2.direction ?? '平衡'}</div>
              <div className="text-[9px] text-muted-foreground/70 mt-0.5">
                逐笔{l2.l2_tick_num ?? 0}笔 · 委托{l2.l2_order_num ?? 0}笔
              </div>
            </>
          ) : (
            <div className="text-muted-foreground">无数据(休市/TQ未连接)</div>
          )}
        </div>
      </div>

      {/* 机构活跃度副图(2026-09-11 升级 C): 逐日活跃度 + 生命/强势/大牛三线 + 共振日红点 */}
      <div className="mt-3 border-t border-border/40 pt-2">
        <ActivitySparkline symbol={symbol} />
      </div>

      {/* 卡尾: 非 bare 只出更新时间; bare 时把标题行挪走的手动刷新按钮并到这一行 */}
      {bare ? (
        <div className="mt-2 flex items-center justify-end gap-2">
          {data?.data_time ? (
            <span className="font-mono text-[9px] text-muted-foreground/60">更新于 {data.data_time}</span>
          ) : null}
          {refreshButton}
        </div>
      ) : data?.data_time ? (
        <div className="text-[9px] text-muted-foreground/60 mt-2 text-right font-mono">
          更新于 {data.data_time}
        </div>
      ) : null}
    </>
  )

  // bare: 不出自己的卡壳(边框/底色/内边距), 只把内容交给外层卡(外层决定间距与边框)
  return bare ? body : <div className="mt-3 rounded-xl border border-border/50 bg-card p-3">{body}</div>
}
