import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { insightApi } from '@panwatch/api'
import { RefreshCw } from 'lucide-react'
import InsightProvider from '@/pages/workbench/InsightProvider'
import { useInsight } from '@panwatch/biz-ui/components/insight/context'
import type { DarkFlowTqResponse, MoreInfoResponse } from '@panwatch/biz-ui/components/insight/types'
import type { MainIntentStructured } from '@panwatch/biz-ui/components/InteractiveKline'
import { safeFixed, safeInt, safeNum, toAmount } from '@/lib/format'

/**
 * 工作台标签「盘口资金」(工作台 v2 三合一, Task 11)。
 *
 * 归属(spec §三 去重表): 十档盘口/盘口演变/幽灵单 · L2成品资金(主力净额/主买/总买卖/撤买卖/逐笔)
 * · 主力意图 · 封单成色 · 资金流水(明盘/暗盘净额表) · 暗盘资金TQ · 筹码 —— **仅本标签**拥有;
 * 右栏「盘口速览」只保留五档 + 主力净额摘要; 带1 拥有 现价/涨停价/封单额/个股名 —— 本文件一律不渲染。
 *
 * 取数(全部真接口, 无 mock、无编造):
 *  - `insightApi.orderbookOb`(`/orderbook-ob`)—— 十档买/卖额、OB 失衡序列、盘口演变事件、幽灵单占比;
 *  - `useInsight()`(由本文件内的 `<InsightProvider keys={['core']}>` 提供)——
 *    `moreInfo`(`/quotes/{s}/more-info` L2 成品) · `darkFlowTq`(`/quotes/{s}/dark-flow-tq` 盘后还原)
 *    · `mainIntent`/`fundFlow`(均由 `/klines/{s}/summary` 的 `main_intent_structured`/`fund_flow` 派生);
 *  - `insightApi.sealQuality`(`/seal-quality/{s}`)—— 封单成色(盘中 60s 采样)。
 *
 * 惰性: 本标签默认导出即自带 `InsightProvider keys={['core']}` —— 标签**挂载才取数**
 * (`TabPanel` 只渲染当前激活标签), 且只取 core 六端点(不带 watchlist/news/suggestions/reports/deep/fundamentals)。
 *
 * 降级(`--` / note, 永不伪造):
 *  - 盘口源不可用(`available=false`)→ 原样展示后端 `note` + 全部 `--`;
 *  - `moreInfo`/`mainIntent`/`fundFlow` 缺字段 → 对应单元格 `--`(走 `@/lib/format` safe* 系列,
 *    后端 DECIMAL 序列化成字符串也不会崩);
 *  - 封单成色 `available=false` → 展示 `reason` 原文 + `--`;
 *  - 暗盘 TQ `data_status !== 'complete'`(未采/样本不足)→ 展示状态原文 + `--`。
 *
 * 与退役 `/l2` 页(`L2Orderbook.tsx`)的差异(只搬内容, 不复刻页面壳): 无股票输入框/查询按钮/页面标题
 * (工作台带1 已拥有)、无 `/klines/summary` 的二次取数; 形态条改由 OB 序列 label 判定 —— 旧页取自
 * `summary.orderbook.shape`, 该字段**不在** `InsightProvider` 暴露的 `data.summary` 内(见报告 concern),
 * 不为此重复打一遍 summary 接口。
 *
 * 复用 vs 自建: 未复用 `OrderBookObBar` —— 它是**自取数黑盒**(内部 fetchAPI `/orderbook-ob` + 30s 轮询,
 * props 只有 `{ symbol }`), 本标签还需要同一响应的 `ob_series`/`events`/`ghost_ratio`
 * (形态条 + 演变事件明细), 复用它会让同一端点在 30s 内被打两遍; 故直接按既有视觉语言渲染十档双向条。
 */

/* ------------------------------------------------------------------ *
 * 后端契约(仅取本页用到的字段; 缺失一律可选, 由渲染层走 '--')
 * ------------------------------------------------------------------ */

interface ObSnapshot {
  ts?: number | null
  dt?: string | null
  ob?: number | null
  label?: string | null
  bid_amt10?: number | null
  ask_amt10?: number | null
}

/** 盘口演变事件(src/core/orderbook_engine.py: order_book_evolution / ghost_order)。 */
interface ObEvent {
  type?: string | null
  side?: string | null
  price_level?: number | null
  price?: number | null
  delta_hands?: number | null
  duration_s?: number | null
  ts?: number | null
  note?: string | null
}

interface ObResp {
  available?: boolean
  ob_series?: ObSnapshot[]
  events?: ObEvent[]
  ghost_ratio?: number | null
  note?: string | null
}

/** 封单成色(src/core/seal_quality.py: compute_from_series)。 */
interface SealMetrics {
  available?: boolean
  reason?: string | null
  window_min?: number | null
  cancel_rate_5m?: number | null
  seal_quality?: number | null
  cancel_bias_5m?: number | null
  cancel_zscore?: number | null
  seal_success_rate?: number | null
  is_sealed?: boolean | null
}

interface SealResp {
  symbol?: string
  n_samples?: number
  metrics?: SealMetrics | null
}

/**
 * 资金流水行(`/klines/{s}/summary`.fund_flow)。
 * 后端实际下发 `ming_net`/`dark_net`, 而 `@panwatch/biz-ui` 的 `FundFlowBarLike` 写成 `open_net`
 * —— 两种键都读(见报告 concern), 不因字段命名漂移把明盘列渲染成 `--`。
 */
interface FundFlowRow {
  date?: string | null
  ming_net?: number | null
  open_net?: number | null
  dark_net?: number | null
}

const POLL_MS = 30_000
/** 资金流水表展示天数(与旧行情页 FundDetail 一致)。 */
const FUND_ROWS = 30

/* ------------------------------------------------------------------ *
 * 纯格式化(不写裸 .toFixed —— R6 棘轮对新文件直接失败)
 * ------------------------------------------------------------------ */

/** 元 → 万/亿(带符号); 无效值 `--`。 */
function money(v: unknown): string {
  const n = safeNum(v)
  return n == null ? '--' : toAmount(n)
}

/** 万元 → 万/亿(带符号); 无效值 `--`(moreInfo 的 zjl/zjl_hb 是万元口径)。 */
function wanMoney(v: unknown): string {
  const n = safeNum(v)
  return n == null ? '--' : toAmount(n * 1e4)
}

/** 比例(0~1)→ 百分数; 无效值 `--`。 */
function pct(v: unknown, digits = 1): string {
  const n = safeNum(v)
  return n == null ? '--' : `${safeFixed(n * 100, digits)}%`
}

/** 已是百分数(如 参与度 42.5)→ "42.5%"; 无效值 `--`。 */
function pctRaw(v: unknown, digits = 1): string {
  const n = safeNum(v)
  return n == null ? '--' : `${safeFixed(n, digits)}%`
}

/** 涨跌/资金方向着色(红涨绿跌走设计令牌); 缺数据中性。 */
function dirClass(v: unknown): string {
  const n = safeNum(v)
  if (n == null || n === 0) return 'text-foreground'
  return n > 0 ? 'text-stock-up' : 'text-stock-down'
}

/** 时间戳(秒, 浮点)→ 本地时:分:秒; 无效值 `--`。 */
function clockOf(ts: unknown): string {
  const n = safeNum(ts)
  return n == null ? '--' : new Date(n * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}

/** more-info `raw` 容错取 L2 主力字段(TQ raw 值为字符串, 需转数值; 新旧命名兼容)。 */
function rawPick(raw: Record<string, string> | null | undefined, ...keys: string[]): number | null {
  if (!raw) return null
  for (const k of keys) {
    const n = safeNum(raw[k])
    if (n != null) return n
  }
  return null
}

/** 主力意图方向 → 中文标签(与 `InteractiveKline` 图例同口径)。 */
function intentLabel(mi: MainIntentStructured | null): string {
  if (!mi) return '--'
  if (mi.data_status === 'insufficient') return `数据不足(${mi.tick_count ?? 0}笔)`
  switch (mi.direction) {
    case 'buy':
      return '吸筹'
    case 'sell':
      return '派发'
    case 'wash':
      return '洗盘吸筹'
    case 'absorb':
      return '疑似吸筹'
    default:
      return '平衡'
  }
}

function intentClass(mi: MainIntentStructured | null): string {
  if (!mi || mi.data_status === 'insufficient') return 'text-muted-foreground'
  if (mi.direction === 'buy') return 'text-stock-up'
  if (mi.direction === 'sell') return 'text-stock-down'
  return 'text-muted-foreground'
}

/* ------------------------------------------------------------------ *
 * 展示原子(hairline 分节, 无卡片 —— 与退役盘口页视觉语言一致)
 * ------------------------------------------------------------------ */

function Section({
  id,
  title,
  hint,
  extra,
  children,
}: {
  id: string
  title: string
  hint?: string
  extra?: ReactNode
  children: ReactNode
}) {
  return (
    <section data-testid={`l2-section-${id}`} className="border-b border-border/40 pb-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[11px]">
        <span className="font-medium text-foreground">{title}</span>
        {hint ? (
          <span className="text-muted-foreground" title={hint}>
            {hint}
          </span>
        ) : null}
        {extra}
      </div>
      {children}
    </section>
  )
}

function Cell({
  label,
  value,
  valueClass,
  hint,
}: {
  label: string
  value: string
  valueClass?: string
  hint?: string
}) {
  return (
    <div title={hint}>
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className={`font-mono ${valueClass ?? 'text-foreground'}`}>{value}</div>
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * 取数(本标签自有的两个端点: 盘口 OB + 封单成色)
 * ------------------------------------------------------------------ */

interface L2Sources {
  ob: ObResp | null
  seal: SealResp | null
  loading: boolean
  updatedAt: string
  reload: () => void
}

/**
 * `/orderbook-ob`(30s 盘中节奏, 与退役 `/l2` 页一致) + `/seal-quality` 两端口一次拉。
 * 换股: 请求序号守卫 + 清空旧值(不把上一只票的盘口画到新标的上); 单端点失败独立静默降级。
 */
function useL2Sources(symbol: string): L2Sources {
  const [ob, setOb] = useState<ObResp | null>(null)
  const [seal, setSeal] = useState<SealResp | null>(null)
  const [loading, setLoading] = useState(false)
  const [updatedAt, setUpdatedAt] = useState('')
  const seqRef = useRef(0)

  const load = useCallback(async () => {
    if (!symbol) return
    const seq = ++seqRef.current
    setLoading(true)
    const [o, s] = await Promise.all([
      insightApi.orderbookOb<ObResp>(symbol).catch(() => null),
      insightApi.sealQuality<SealResp>(symbol).catch(() => null),
    ])
    if (seq !== seqRef.current) return
    setOb(o ?? null)
    setSeal(s ?? null)
    setUpdatedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
    setLoading(false)
  }, [symbol])

  useEffect(() => {
    setOb(null)
    setSeal(null)
    setUpdatedAt('')
    void load()
    const timer = window.setInterval(() => void load(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [load])

  return { ob, seal, loading, updatedAt, reload: load }
}

/* ------------------------------------------------------------------ *
 * ① 十档买卖额双向条 + 盘口形态
 * ------------------------------------------------------------------ */

function OrderbookSection({ ob, loading }: { ob: ObResp | null; loading: boolean }) {
  const series = ob?.ob_series ?? []
  const latest = series.length > 0 ? series[series.length - 1] : null
  const bid10 = safeNum(latest?.bid_amt10)
  const ask10 = safeNum(latest?.ask_amt10)
  const total = bid10 != null && ask10 != null ? bid10 + ask10 : 0
  const bidPct = total > 0 && bid10 != null ? (bid10 / total) * 100 : null
  const obVal = safeNum(latest?.ob)
  const available = ob?.available === true
  const shapeClass =
    obVal == null
      ? 'text-muted-foreground'
      : obVal > 0.3
        ? 'text-stock-up'
        : obVal < -0.3
          ? 'text-stock-down'
          : 'text-muted-foreground'

  return (
    <Section
      id="orderbook"
      title="十档买卖额"
      hint="thsdk 20 档盘口聚合 · 买/卖前 10 档金额合计(元)"
      extra={
        latest?.dt ? (
          <span className="ml-auto font-mono text-[10px] text-muted-foreground">快照 {latest.dt}</span>
        ) : null
      }
    >
      {!available ? (
        <div className="mb-2 text-[12px] text-muted-foreground">
          {loading && !ob ? '加载中…' : (ob?.note ?? '盘口无数据(非交易时段或 thsdk 未接)')}
        </div>
      ) : null}

      <div className="mb-2 grid grid-cols-3 gap-x-4 gap-y-1.5">
        <Cell
          label="盘口形态"
          value={latest?.label ?? '--'}
          valueClass={shapeClass}
          hint="OB 失衡口径: OB=(买十档额-卖十档额)/(两者和), >+0.3 买压 / <-0.3 卖压 / 其余中性"
        />
        <Cell
          label="买盘占比"
          value={bidPct == null ? '--' : `${safeFixed(bidPct, 1)}%`}
          hint="十档买额 /(十档买额 + 十档卖额)"
        />
        <Cell
          label="OB 失衡"
          value={obVal == null ? '--' : `${obVal > 0 ? '+' : ''}${safeFixed(obVal, 3)}`}
          valueClass={shapeClass}
          hint="OB ∈ [-1, 1], 正 = 买盘占优"
        />
      </div>

      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <span className="w-6 shrink-0 text-muted-foreground">买</span>
          <div className="h-3.5 flex-1 bg-accent/20">
            <div className="h-3.5 bg-stock-up" style={{ width: `${bidPct ?? 0}%` }} />
          </div>
          <span className="w-24 shrink-0 text-right font-mono text-stock-up">{toAmount(bid10)}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="w-6 shrink-0 text-muted-foreground">卖</span>
          <div className="h-3.5 flex-1 bg-accent/20">
            <div className="h-3.5 bg-stock-down" style={{ width: `${bidPct != null ? 100 - bidPct : 0}%` }} />
          </div>
          <span className="w-24 shrink-0 text-right font-mono text-stock-down">{toAmount(ask10)}</span>
        </div>
      </div>

      {available && ob?.note ? (
        <div className="mt-1.5 text-[10px] text-muted-foreground/70">{ob.note}</div>
      ) : null}
    </Section>
  )
}

/* ------------------------------------------------------------------ *
 * ② L2 成品资金(TQ get_more_info 明盘口径)
 * ------------------------------------------------------------------ */

function L2FundSection({ moreInfo, loading }: { moreInfo: MoreInfoResponse | null; loading: boolean }) {
  const zjlHb = safeNum(moreInfo?.zjl_hb) ?? rawPick(moreInfo?.raw, 'Zjl_HB', 'zjl_hb', 'ZJL_HB')
  const zjl = safeNum(moreInfo?.zjl) ?? rawPick(moreInfo?.raw, 'Zjl', 'zjl', 'ZJL')
  const quoteTime = moreInfo?.quote_time ? String(moreInfo.quote_time).slice(0, 19).replace('T', ' ') : ''

  return (
    <Section
      id="l2fund"
      title="L2 成品资金"
      hint="TQ get_more_info · 明盘口径"
      extra={
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          {loading && !moreInfo ? '加载中…' : quoteTime ? `快照 ${quoteTime}` : ''}
        </span>
      }
    >
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 md:grid-cols-4">
        <Cell
          label="主力净额"
          value={wanMoney(zjlHb)}
          valueClass={dirClass(zjlHb)}
          hint="同花顺口径主力净额(万元) = 超大单 + 大单净额; 正为净流入"
        />
        <Cell
          label="主买净额"
          value={wanMoney(zjl)}
          valueClass={dirClass(zjl)}
          hint="主动买入 - 主动卖出(万元); 正为多方占优"
        />
        <Cell
          label="总买/总卖量"
          value={`${safeInt(moreInfo?.total_buy_vol)} / ${safeInt(moreInfo?.total_sell_vol)}`}
          hint="当日累计总买量 / 总卖量(TQ TotalBVol / TotalSVol)"
        />
        <Cell
          label="撤买/撤卖量"
          value={`${safeInt(moreInfo?.cancel_buy)} / ${safeInt(moreInfo?.cancel_sell)}`}
          hint="当日累计撤买 / 撤卖量; 撤单量大代表挂单意愿不稳"
        />
        <Cell
          label="逐笔成交/委托笔数"
          value={`${safeInt(moreInfo?.l2_tick_num)} / ${safeInt(moreInfo?.l2_order_num)}`}
          hint="L2 逐笔成交笔数 / 委托笔数(需 Level-2 权限)"
        />
      </div>
    </Section>
  )
}

/* ------------------------------------------------------------------ *
 * ③ 盘口演变事件 + 幽灵单占比
 * ------------------------------------------------------------------ */

function EvolutionSection({ ob, loading }: { ob: ObResp | null; loading: boolean }) {
  const available = ob?.available === true
  const events = ob?.events ?? []
  const shown = events.slice(-8).reverse()

  return (
    <Section
      id="events"
      title="盘口演变事件"
      hint="托单/压单/撤单/幽灵单(跨快照跟踪)"
      extra={
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          幽灵单占比 {pct(ob?.ghost_ratio)} · 事件 {events.length} 条
        </span>
      }
    >
      {!available ? (
        <div className="text-[12px] text-muted-foreground">
          {loading && !ob ? '加载中…' : (ob?.note ?? '盘口不可用, 无演变事件')}
        </div>
      ) : shown.length === 0 ? (
        <div className="text-[12px] text-muted-foreground">暂无托单/压单/撤单/幽灵单事件</div>
      ) : (
        <ul className="space-y-1">
          {shown.map((e, i) => (
            <li key={`${e.type ?? 'e'}-${String(e.ts ?? i)}-${i}`} className="flex items-baseline gap-1.5">
              <span className="w-14 shrink-0 font-mono text-[10px] text-muted-foreground">{clockOf(e.ts)}</span>
              <span className="min-w-0 flex-1 truncate">
                <span className="text-foreground">{e.type ?? '--'}</span>
                <span className="text-muted-foreground">
                  {' · '}
                  {e.side === 'bid' ? '买' : e.side === 'ask' ? '卖' : '--'}
                  {e.price_level != null ? `${e.price_level}档` : ''} @{safeFixed(e.price, 2)}
                  {' · '}
                  {safeInt(e.delta_hands)}手
                  {e.duration_s != null ? ` / ${safeFixed(e.duration_s, 1)}s` : ''}
                  {e.note ? ` · ${e.note}` : ''}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

/* ------------------------------------------------------------------ *
 * ④ 主力意图 + 封单成色
 * ------------------------------------------------------------------ */

function IntentSealSection({
  mainIntent,
  seal,
  sealLoading,
}: {
  mainIntent: MainIntentStructured | null
  seal: SealResp | null
  sealLoading: boolean
}) {
  const m = seal?.metrics ?? null
  const sealAvailable = m?.available === true
  const mi = mainIntent

  return (
    <Section
      id="intent"
      title="主力意图 · 封单成色"
      hint="逐笔 V14 判据 · 涨停封单 60s 采样"
      extra={
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          {mi?.data_status === 'insufficient' ? '意图数据不足' : ''}
        </span>
      }
    >
      {/* 主力意图(逐笔口径, 来自 klineSummary.main_intent_structured) */}
      <div className="mb-2 grid grid-cols-2 gap-x-4 gap-y-2 md:grid-cols-3">
        <Cell label="方向" value={intentLabel(mi)} valueClass={intentClass(mi)} hint="V14 判据: 净额 + 参与度 + 买占比" />
        <Cell label="主力净额" value={money(mi?.main_net)} valueClass={dirClass(mi?.main_net)} hint="主力净额(元), 逐笔口径" />
        <Cell label="参与度" value={pctRaw(mi?.participation)} hint="主力成交额 / 当日总成交额(%)" />
        <Cell label="超大单净额" value={money(mi?.big_net)} valueClass={dirClass(mi?.big_net)} hint="超大单净额(元)" />
        <Cell label="大单净额" value={money(mi?.mid_net)} valueClass={dirClass(mi?.mid_net)} hint="大单净额(元)" />
        <Cell label="主力买占比" value={pctRaw(mi?.buy_ratio)} hint="主力买入 /(主力买入 + 主力卖出)(%)" />
        <Cell label="尾盘净额" value={money(mi?.tail_net)} valueClass={dirClass(mi?.tail_net)} hint="尾盘段净额(元)" />
        <Cell label="5 日阶段" value={mi?.phase ?? '--'} hint="近 5 日阶段(主力意图结构化输出)" />
      </div>
      {mi?.signal ? <div className="mb-2 text-[11px] text-muted-foreground">{mi.signal}</div> : null}
      {!mi ? (
        <div className="mb-2 text-[12px] text-muted-foreground">暂无主力意图数据(非 A 股/数据源不可用/未开盘)</div>
      ) : null}

      {/* 封单成色(A3 批次, /seal-quality) */}
      <div className="border-t border-border/30 pt-2">
        <div className="mb-1.5 text-[11px] text-muted-foreground">
          封单成色
          <span className="ml-1 text-[10px] opacity-70">
            {m?.window_min != null ? `近 ${safeFixed(m.window_min, 0)} 分钟 · ` : ''}盘中 60s 采样
            {seal?.n_samples != null ? ` · ${seal.n_samples} 样本` : ''}
          </span>
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-2 md:grid-cols-4">
          <Cell
            label="成色"
            value={pct(m?.seal_quality, 0)}
            valueClass={
              safeNum(m?.seal_quality) != null && (safeNum(m?.seal_quality) ?? 1) < 0.6 ? 'text-stock-down' : 'text-foreground'
            }
            hint="成色 = 1 - 窗口撤单率(0~1); <60% 或撤单异动 >=2 视为走弱"
          />
          <Cell label="撤单率" value={pct(m?.cancel_rate_5m)} hint="窗口内 撤单量 /(买卖量 + 撤单量)" />
          <Cell label="撤单异动" value={safeFixed(m?.cancel_zscore, 2)} hint="窗口撤单率对历史基线的 z-score" />
          <Cell label="封板成功率" value={pct(m?.seal_success_rate, 0)} hint="窗口内 is_sealed 为真的样本占比" />
          <Cell label="撤单偏向" value={safeFixed(m?.cancel_bias_5m, 2)} hint="(撤买-撤卖)/总撤单; >0 买方撤多, <0 卖方撤多" />
          <Cell
            label="当前封板"
            value={m?.is_sealed == null ? '--' : m.is_sealed ? '封住' : '未封住'}
            valueClass={m?.is_sealed === false ? 'text-stock-down' : 'text-foreground'}
            hint="最新样本是否处于封板状态"
          />
        </div>
        {!sealAvailable ? (
          <div className="mt-1.5 text-[10px] text-muted-foreground/70">
            {sealLoading && !seal ? '加载中…' : (m?.reason ?? '无封单成色样本(非涨停股或非交易时段)')}
          </div>
        ) : null}
      </div>
    </Section>
  )
}

/* ------------------------------------------------------------------ *
 * ⑤ 资金流水(明盘/暗盘净额表)
 * ------------------------------------------------------------------ */

function FundFlowSection({ rows }: { rows: FundFlowRow[] }) {
  const recent = rows.slice(-FUND_ROWS).reverse()

  return (
    <Section
      id="fundflow"
      title="资金流水"
      hint="明盘 / 暗盘净额(日级) · 明盘历史日无数据显 --"
      extra={
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          {rows.length > 0 ? `最近 ${recent.length} / ${rows.length} 行` : ''}
        </span>
      }
    >
      {recent.length === 0 ? (
        <div className="text-[12px] text-muted-foreground">暂无资金流水(-- 表示后端未输出该日净额)</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[11px]">
            <thead>
              <tr className="border-b border-border/40 text-muted-foreground">
                <th className="px-2 py-1 font-medium">日期</th>
                <th className="px-2 py-1 text-right font-medium">明盘净额</th>
                <th className="px-2 py-1 text-right font-medium">暗盘净额</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {recent.map((r, i) => {
                const ming = safeNum(r.ming_net) ?? safeNum(r.open_net)
                const dark = safeNum(r.dark_net)
                return (
                  <tr key={`${r.date ?? 'row'}-${i}`}>
                    <td className="px-2 py-1 font-mono text-muted-foreground">{r.date ?? '--'}</td>
                    <td className={`px-2 py-1 text-right font-mono ${dirClass(ming)}`}>{toAmount(ming)}</td>
                    <td className={`px-2 py-1 text-right font-mono ${dirClass(dark)}`}>{toAmount(dark)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  )
}

/* ------------------------------------------------------------------ *
 * ⑥ 暗盘资金 TQ(盘后逐笔还原)
 * ------------------------------------------------------------------ */

function DarkFlowTqSection({ data }: { data: DarkFlowTqResponse | null }) {
  const complete = data?.data_status === 'complete'

  return (
    <Section
      id="darktq"
      title="暗盘资金 TQ"
      hint="盘后逐笔还原 · 拆单识别"
      extra={
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">
          {data?.date ? `盘后 ${data.date}` : ''}
        </span>
      }
    >
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 md:grid-cols-4">
        <Cell label="超大单净额" value={safeFixed(data?.xl_net, 0, '--')} valueClass={dirClass(data?.xl_net)} hint="超大单净额(万元)" />
        <Cell label="大单净额" value={safeFixed(data?.large_net, 0, '--')} valueClass={dirClass(data?.large_net)} hint="大单净额(万元)" />
        <Cell label="中单净额" value={safeFixed(data?.mid_net, 0, '--')} valueClass={dirClass(data?.mid_net)} hint="中单净额(万元)" />
        <Cell label="小单净额" value={safeFixed(data?.small_net, 0, '--')} valueClass={dirClass(data?.small_net)} hint="小单净额(万元)" />
        <Cell label="拆单委托" value={safeInt(data?.split_order_count)} hint="疑似拆单的委托笔数" />
        <Cell label="平均拆单份数" value={safeFixed(data?.avg_split_parts, 1)} hint="单笔原单被拆成几份" />
        <Cell label="撤单比" value={pct(data?.cancel_ratio)} hint="撤单量 / 委托量(还原口径)" />
        <Cell label="撤买/撤卖量" value={`${safeInt(data?.cancel_buy_vol)} / ${safeInt(data?.cancel_sell_vol)}`} hint="撤销的买 / 卖委托量" />
      </div>
      {(data?.tuopan || data?.yapan || data?.suopan) && (
        <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
          {data?.tuopan ? <span className="rounded bg-accent/30 px-2 py-0.5 text-foreground">托盘</span> : null}
          {data?.yapan ? <span className="rounded bg-accent/30 px-2 py-0.5 text-foreground">压盘</span> : null}
          {data?.suopan ? <span className="rounded bg-accent/30 px-2 py-0.5 text-foreground">锁盘</span> : null}
        </div>
      )}
      {!complete ? (
        <div className="mt-1.5 text-[10px] text-muted-foreground/70">
          暗盘数据状态 {data?.data_status ?? '不可用'}(盘后采集, 当日无样本时全部 `--`)
        </div>
      ) : null}
    </Section>
  )
}

/* ------------------------------------------------------------------ *
 * ⑦ 筹码(近 10 日分价分布, 随 klineSummary.main_intent_structured 下发)
 * ------------------------------------------------------------------ */

function ChipsSection({ mainIntent }: { mainIntent: MainIntentStructured | null }) {
  const band = mainIntent?.chip_band ?? null
  return (
    <Section id="chips" title="筹码" hint="近 10 日分价分布(腾讯/新浪标准筹码接口)">
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 md:grid-cols-3">
        <Cell label="筹码峰" value={safeFixed(mainIntent?.chip_peak, 2)} hint="成交最密集价位" />
        <Cell
          label="成本带"
          value={band ? `${safeFixed(band.low, 2)} - ${safeFixed(band.high, 2)}` : '--'}
          hint="主要成本区间下沿 - 上沿"
        />
        <Cell label="获利盘比例" value={pct(mainIntent?.profit_ratio, 0)} hint="当前价上方获利筹码占比" />
      </div>
      {!mainIntent ? (
        <div className="mt-1.5 text-[10px] text-muted-foreground/70">暂无筹码数据(klineSummary 未下发或数据源不可用)</div>
      ) : null}
    </Section>
  )
}

/* ------------------------------------------------------------------ *
 * 标签正文(在 Provider 内消费 useInsight; 见文件头注)
 * ------------------------------------------------------------------ */

function L2TabBody({ symbol }: { symbol: string }) {
  const { moreInfo, moreInfoLoading, darkFlowTq, mainIntent, fundFlow } = useInsight()
  const { ob, seal, loading, updatedAt, reload } = useL2Sources(symbol)
  // `fundFlow` 的元素类型在 biz-ui 里声明为 `open_net`, 后端实际下发 `ming_net` —— 两种键都读。
  const fundRows = (fundFlow ?? []) as FundFlowRow[]

  return (
    <div className="mt-1 space-y-3 text-[12px]" data-testid="l2-tab">
      {/* 条: 口径说明 + 刷新/更新时间(无现价/涨停价/个股名 —— 带1 拥有) */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/40 pb-2 text-[11px] text-muted-foreground">
        <span>盘口资金</span>
        <span className="text-border/60">|</span>
        <span>十档盘口(thsdk 20 档) + L2 成品 + 暗盘还原(无十档逐笔明细)</span>
        <button
          type="button"
          onClick={reload}
          className="ml-auto inline-flex h-6 items-center gap-1 rounded border border-border/50 px-2 text-[11px] text-muted-foreground hover:text-foreground"
        >
          <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} /> 刷新
        </button>
        {updatedAt ? (
          <span className="font-mono text-[10px] text-muted-foreground">更新 {updatedAt} · 30s 轮询</span>
        ) : null}
      </div>

      <div className="grid grid-cols-1 gap-x-4 gap-y-3 lg:grid-cols-12">
        <div className="space-y-3 lg:col-span-7">
          <OrderbookSection ob={ob} loading={loading} />
          <L2FundSection moreInfo={moreInfo} loading={moreInfoLoading} />
          <FundFlowSection rows={fundRows} />
        </div>
        <div className="space-y-3 lg:col-span-5 lg:border-l lg:border-border/40 lg:pl-4">
          <EvolutionSection ob={ob} loading={loading} />
          <IntentSealSection mainIntent={mainIntent} seal={seal} sealLoading={loading} />
          <DarkFlowTqSection data={darkFlowTq} />
          <ChipsSection mainIntent={mainIntent} />
        </div>
      </div>
    </div>
  )
}

/**
 * 标签入口。`InsightProvider keys={['core']}`: 只启用带1+带2 的六个端点
 * (quote/moreInfo/darkFlowTq/klineSummary/klines/portfolioSummary), watchlist/news/suggestions/
 * reports/deep/fundamentals 一个都不发(惰性由 `TabPanel` 只渲染激活标签保证)。
 */
export default function L2Tab({
  symbol,
  market,
  hasPosition,
}: {
  symbol: string
  market: string
  hasPosition?: boolean
}) {
  return (
    <InsightProvider symbol={symbol} market={market} hasPosition={hasPosition} keys={['core']}>
      <L2TabBody symbol={symbol} />
    </InsightProvider>
  )
}