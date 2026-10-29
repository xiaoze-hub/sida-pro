import { useMemo } from 'react'
import { AlertOctagon, ShieldAlert, ShieldPlus, CircleSlash, ClockAlert } from 'lucide-react'

import { safePercent } from '@/lib/format'
import {
  buildPositionRiskReport,
  formatPriceWithCurrency,
  humanizeAgeMinutes,
  NEAR_LINE_PCT,
  QUOTE_FRESH_WINDOW_MINUTES,
  quoteStampNote,
  type MarketSessionMap,
  type PositionRiskReport,
  type PositionRiskRow,
} from '@/lib/position-risk'
import { useStocks } from './context'

/**
 * 持仓风险检查（P2 持仓风险切片, 2026-10-29）—— 持仓页的逐只纪律清单。
 *
 * ## 与既有能力的关系（先盘点再动手，不重复建设）
 * | 既有 | 覆盖 | 缺什么 |
 * |---|---|---|
 * | `PortfolioSummarySection` | 组合合计（市值/盈亏/仓位） | 逐只状态零视图 |
 * | `AccountsSection`（下方持仓表） | 现价/成本/市值/盈亏/当日盈亏 | **没有防线列**，不判纪律位 |
 * | `DefenseLineBanner`（工作台） | **击穿时**弹一条 | 持仓页看不到；未设防线/缺现价不显示 |
 * | `GET /portfolio/diagnostics` | 集中度 HHI / 单仓上限（**无前端消费者**） | 无逐只纪律判定 |
 *
 * 所以本组件**只补防线这一维**：不重复列成本/浮亏（那两列在下方持仓表里，重复摆两遍
 * 只会让两张表互相打架）。表头写清分工，用户知道去哪看价格、去哪看纪律位。
 *
 * 本组件不新建数据源：只消费持仓页**已经加载**的 `GET /portfolio/summary`（`useStocks().portfolio`）
 * 与 `GET /stocks/markets/status`（`useStocks().marketStatus`，服务器市场墙钟/状态），
 * 判定收敛在 `@/lib/position-risk`（`DefenseLineBanner` 也已改为共用同一判定）。
 *
 * ## 六条不许越的线
 * 1. **不告警未设防线**：没设就是「未设防线」，不拿成本折算成纪律位（方案 B）。
 * 2. **缺现价说缺**：`无现价` 单独成态，不混进「安全」，也不从分母里消失。
 * 3. **旧报价/陈旧报价不判破位**：日期层（后端 `daily_pnl_period`）或时刻层
 *    （服务器市场墙钟 - 报价时刻 > 推断阈值）任一不过 ⇒ `时效未证实`，不进破位/逼近计数。
 *    证明不了时点（无报价时刻 / 无市场时钟）同样不判 —— 不冒充实时。
 * 4. **不产出结论**：只列状态 / 距离 / 缺口，不写买卖、目标价、加减仓。
 * 5. **每账户一行**：同一标的多账户各自一条防线，不做任何合并或「取第一条」。
 * 6. **计数与表格同源**：指标里的「破位 / 逼近 / 时效未证实 / 未设防线 / 无现价」
 *    与表格每个状态桶严格同数（同一个 `defenseState` / `gaps`），不另起一套口径。
 *    单位一律写「行」（一个账户的一条持仓），不冒称「只」标的。
 */

const STATE_META: Record<
  PositionRiskRow['defenseState'],
  { label: string; tone: string; icon: typeof ShieldAlert; hint: string }
> = {
  breached: {
    label: '已破位',
    tone: 'text-rose-400',
    icon: AlertOctagon,
    hint: '现价在你的防线之下（时效已证实）',
  },
  near: {
    label: `逼近（${NEAR_LINE_PCT}%内）`,
    tone: 'text-amber-400',
    icon: ShieldAlert,
    hint: '现价距防线不超过该阈值（时效已证实）',
  },
  safe: {
    label: '防线之上',
    tone: 'text-muted-foreground',
    icon: ShieldAlert,
    hint: '现价在防线之上，且时效已证实',
  },
  unset: { label: '未设防线', tone: 'text-muted-foreground', icon: ShieldPlus, hint: '你没设防线，不告警' },
  unknown: { label: '无现价', tone: 'text-role-watch', icon: CircleSlash, hint: '没有可用现价，不判防线' },
  stale: {
    label: '时效未证实',
    tone: 'text-role-watch',
    icon: ClockAlert,
    hint: '报价时点无法证明是当下的，因此不判破位/逼近',
  },
}

/**
 * 行情时点：只说你**证明得了**的事。
 *
 * 三层事实，缺一层就往下退，绝不拿「今天是交易日」冒充「这条报价是现在的」：
 *  1. 没有现价 → 没有报价时点可说（后端可能残留旧的 `daily_pnl_period='today'`，先看价）；
 *  2. 日期层：`daily_pnl_period != 'today'` → 非当日（后端只按「早于今天」归类，
 *     隔日与隔周共用同一个标签，所以写真实日期，不写「上一交易日」）；
 *  3. 时刻层：服务器市场墙钟 - 报价时刻 > 推断阈值 → 滞后；
 *     没有报价时刻 / 没有市场时钟 / 报价晚于市场时钟 → 时点未知。
 */
function freshnessLabel(row: PositionRiskRow): string {
  if (row.currentPrice === null) return '无行情'
  const at = row.quoteLocalTime ? ` ${row.quoteLocalTime}` : ''
  const age = humanizeAgeMinutes(row.quoteAgeMinutes)
  // 源报时语义按市场分（CN=已证实的快照刷新；港美=只到"供应商报时"），都由后端下发。
  const kindNote = quoteStampNote(row.quoteTimeKind) ? ` · ${quoteStampNote(row.quoteTimeKind)}` : ''
  if (row.freshness === 'current') {
    // 同样是「窗口内」，服务器说交易中才是真·实时；非交易时段里只是刚定格的最新价。
    const head = row.marketStatus === 'trading' ? '交易中' : '非交易时段 · 最近报价'
    return `${head}${at}${age ? ` · ${age}` : ''}${kindNote}`
  }
  if (row.staleReason === 'date_only') {
    // TQ 日线口径：源只给到日期（现价与同日线收盘价配对证明），没有成交时刻。
    // 非交易时段才敢说「最近成交价」，且必须写明"非实时"（过去成交价 ≠ 当下价）；
    // 交易中一律「时点未知」。
    if (row.freshness === 'session_frozen') {
      return `非交易时段 · 最近成交价（日线口径 ${row.quoteDate ?? '——'}，无成交时刻，非实时）`
    }
    return `时点未知（源只提供日期 ${row.quoteDate ?? '——'}，未提供成交时刻）`
  }
  if (row.freshness === 'session_frozen') {
    return `非交易时段 · 最近报价${at || '（时刻未知）'}${age ? ` · ${age}` : ''}${kindNote}`
  }
  if (row.staleReason === 'period') {
    const stamp = row.quoteDate ?? (row.quoteTime ? row.quoteTime.slice(0, 10) : '')
    return `非当日报价 ${stamp || '（日期未知）'}${at}`
  }
  if (row.staleReason === 'intraday_age') {
    return `滞后${age ? ` ${age}` : ''} · 报价${at || '（时刻未知）'}`
  }
  if (row.staleReason === 'no_time') return '时点未知（报价无时间）'
  if (row.staleReason === 'period_unknown') {
    // 2026-10-29 独立复审 F3：WS 只换价不带时刻/期间时，不能写成「非当日报价」
    // （那是把"不知道"讲成"旧"），要说清是"确认不了属于当日"。
    return `时点未知（无法确认报价属于当日）${at}`
  }
  if (row.staleReason === 'refresh_failed') return '时效未核实（刷新失败，不作时效证明）'
  if (row.staleReason === 'clock_skew') return '时点存疑（报价晚于市场时钟）'
  return '时点未证实（缺服务器市场时钟）'
}

/** 只读展示层：给报告即可渲染，方便单测直接把边界值喂进来。 */
export function PositionRiskTable({ report }: { report: PositionRiskReport }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[620px] text-[12px]" data-testid="position-risk-table">
        <thead>
          <tr className="text-muted-foreground">
            <th className="py-2 pr-3 text-left font-normal">账户 / 标的</th>
            <th className="py-2 pr-3 text-right font-normal">你的防线</th>
            <th className="py-2 pr-3 text-right font-normal">现价</th>
            <th className="py-2 pr-3 text-right font-normal">距防线</th>
            <th className="py-2 pr-3 text-left font-normal">状态</th>
            <th className="py-2 text-left font-normal">行情时点</th>
          </tr>
        </thead>
        <tbody>
          {report.rows.map((row) => {
            const meta = STATE_META[row.defenseState]
            const Icon = meta.icon
            const stale = row.defenseState === 'stale'
            return (
              <tr
                key={row.key}
                className="row-hover border-t border-border"
                data-testid={`position-risk-row-${row.symbol}`}
                data-account={row.accountId ?? ''}
                data-state={row.defenseState}
                data-freshness={row.freshness}
              >
                <td className="py-2 pr-3">
                  {row.accountName && (
                    <span className="mr-1.5 rounded bg-s1 px-1 py-0.5 text-[12px] text-muted-foreground">
                      {row.accountName}
                    </span>
                  )}
                  <span className="font-medium text-foreground">{row.name}</span>
                  <span className="ml-1.5 text-muted-foreground tabular-nums">{row.symbol}</span>
                  {row.sharedSymbolConflict && (
                    <span
                      className="ml-1.5 text-[12px] text-role-watch"
                      title="同一标的多账户各自设了不同防线，这里按账户分行显示，不合并、不取其中一条"
                    >
                      多账户防线不同
                    </span>
                  )}
                </td>
                <td className="py-2 pr-3 text-right tabular-nums text-foreground">
                  {row.defensePrice === null ? '未设' : formatPriceWithCurrency(row.defensePrice, row.market)}
                </td>
                <td className="py-2 pr-3 text-right">
                  <div className="tabular-nums text-foreground">
                    {formatPriceWithCurrency(row.currentPrice, row.market)}
                  </div>
                  <div className="text-muted-foreground">
                    {row.currency === 'UNKNOWN' ? '币种未知' : row.currency}
                  </div>
                </td>
                <td className="py-2 pr-3 text-right tabular-nums text-foreground">
                  {row.distanceToDefensePct === null ? (
                    '--'
                  ) : stale ? (
                    <span className="text-muted-foreground">
                      旧价 {safePercent(row.distanceToDefensePct, 2)}
                    </span>
                  ) : (
                    safePercent(row.distanceToDefensePct, 2)
                  )}
                </td>
                <td className="py-2 pr-3">
                  <span className={`inline-flex items-center gap-1 ${meta.tone}`} title={meta.hint}>
                    <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    {meta.label}
                  </span>
                </td>
                <td className="py-2 text-muted-foreground" data-testid={`position-risk-quote-${row.symbol}`}>
                  {freshnessLabel(row)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function Metric({
  label,
  value,
  testId,
  muted,
  hint,
}: {
  label: string
  value: number
  testId: string
  muted?: boolean
  hint?: string
}) {
  return (
    <div className="flex items-baseline gap-2" data-testid={testId} title={hint}>
      <span className="text-[12px] text-muted-foreground">{label}</span>
      <span
        className={`text-[16px] tabular-nums ${muted ? 'text-muted-foreground' : 'text-foreground'}`}
      >
        {value}
      </span>
    </div>
  )
}

/** 把页面已加载的市场状态折成时效判定要用的「服务器市场时钟 + 日期 + 状态」。 */
export function buildMarketSessions(marketStatus: unknown): MarketSessionMap {
  const list = Array.isArray(marketStatus) ? marketStatus : []
  const map: Record<string, { localTime: string | null; localDate: string | null; status: string | null }> = {}
  for (const raw of list) {
    if (!raw || typeof raw !== 'object') continue
    const item = raw as { code?: string; local_time?: string; local_date?: string; status?: string }
    const code = String(item.code ?? '').trim().toUpperCase()
    if (!code) continue
    map[code] = {
      localTime: item.local_time ?? null,
      // 后端哪天把市场当地日期一起下发，跨日判定就直接用它（B3）；
      // 现在只有 HH:MM，日期层由 `daily_pnl_period` + 后端判好的 quote_date 承担。
      localDate: item.local_date ?? null,
      status: item.status ?? null,
    }
  }
  return map
}

/** 持仓页容器：读页面已加载的持仓汇总与市场状态，不额外发请求。 */
export function PositionRiskSection() {
  const { portfolio, viewTab, marketStatus, quoteEvidenceVerified } = useStocks()
  // 每账户一行：账户归属必须带进判定，否则同一 symbol 的多条防线无法区分。
  const positions = useMemo(
    () =>
      (portfolio?.accounts ?? []).flatMap((acc) =>
        (acc.positions ?? []).map((p) => ({
          ...p,
          account_id: acc.id,
          account_name: acc.name,
        })),
      ),
    [portfolio],
  )
  // 时效判定只吃服务器给的「市场墙钟 + 日期 + 状态」（`/stocks/markets/status`），不读本机时钟。
  const marketSessions = useMemo(() => buildMarketSessions(marketStatus), [marketStatus])
  // 2026-10-29 F2/B1: 续期失败（报价心跳 catch / 本地有效期到期）时 `quoteEvidenceVerified=false`
  // —— 手里这份快照已无从证明是当下的，清单必须降级「时效未核实」，不判破位。
  // 旧实现只把凭证放在 `quotes` 表里清字段，持仓汇总仍可能带旧时刻，缺了这道闸门。
  const verified = quoteEvidenceVerified !== false
  const report = useMemo(
    () => buildPositionRiskReport(positions, { marketSessions, verified }),
    [positions, marketSessions, verified],
  )

  if (viewTab !== 'positions' || report.total === 0) return null

  const pnlLeader = report.allProfitable ? report.bestByPnl : report.worstByPnl
  const leaderIsLoss = (pnlLeader?.pnlPct ?? 0) < 0

  return (
    <section
      className="mb-5 rounded-xl bg-card p-3"
      aria-labelledby="position-risk-title"
      data-testid="position-risk-section"
    >
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 id="position-risk-title" className="text-[16px] font-semibold text-foreground">
            持仓风险检查
          </h2>
          <span className="text-[12px] text-muted-foreground">
            口径：现价对比你自己设的防线价；未设防线不告警，系统不替你折算纪律位。
            价格 / 成本 / 盈亏以下方持仓表为准，这里只看纪律位与行情时点。
          </span>
        </div>
        <span className="text-[12px] tabular-nums text-muted-foreground">
          已定价 {report.priced}/{report.total} 行（{report.symbols} 只标的 · {report.accounts} 个账户）
        </span>
      </div>

      <div
        className="mb-2 flex flex-wrap items-baseline gap-x-5 gap-y-1"
        data-testid="position-risk-metrics"
      >
        <Metric label="已破位" value={report.breached} testId="metric-breached" hint="行数（一个账户的一条持仓）" />
        <Metric label="逼近" value={report.near} testId="metric-near" hint="行数" />
        <Metric
          label="时效未证实"
          value={report.stale}
          testId="metric-stale"
          muted
          hint="行数：已知陈旧 + 证明不了时点，都不判破位"
        />
        <Metric label="未设防线" value={report.noDefense} testId="metric-no-defense" muted hint="行数" />
        <Metric label="无现价" value={report.noPrice} testId="metric-no-price" muted hint="行数" />
      </div>

      <PositionRiskTable report={report} />

      <div className="mt-2 space-y-1 text-[12px] text-muted-foreground">
        <p data-testid="position-risk-freshness-rule">
          行情时点分两层判：日期层看后端给出的「当日 / 非当日」判定（后端已按市场时区算好，
          前端不复刻交易日历）；时刻层看「服务器市场时钟 − 报价时刻」，超过{' '}
          {QUOTE_FRESH_WINDOW_MINUTES} 分钟即不作实时（该阈值是推断值，不是交易所口径）。
          服务器说当日已收盘/午休/盘前时，最后一笔报价称「最近成交价」，可核对但不叫实时
          （源只给日期时另标「日线口径 · 无成交时刻」）。
        </p>
        {report.stale > 0 && (
          <p data-testid="position-risk-stale-note">
            {report.stale} 行时效未证实（其中已知陈旧 {report.staleKnown} 行、时点无法证明{' '}
            {report.staleUnverified} 行）：只陈列价位与距离，未计入破位/逼近。
          </p>
        )}
        {report.noPrice > 0 && (
          <p>
            {report.noPrice} 行没有可用现价：不参与破位/浮亏判定，也不会被算作安全。
          </p>
        )}
        {report.noDefense > 0 && (
          <p>
            {report.noDefense} 行未设防线：只有你在持仓里填了防线价才会判定破位，系统默认值仅作参考。
          </p>
        )}
        {report.conflictingSymbols.length > 0 && (
          <p>
            同一标的多账户防线不同（{report.conflictingSymbols.length} 只标的）：已按账户分行显示，
            不做合并、也不替你取其中一条。
          </p>
        )}
        {pnlLeader !== null && pnlLeader.pnlPct !== null && (
          <p>
            {leaderIsLoss
              ? `当前浮亏最重：${pnlLeader.name}（${safePercent(pnlLeader.pnlPct, 2)}）`
              : report.allProfitable
                ? `当前持仓全部浮盈，最强：${pnlLeader.name}（${safePercent(pnlLeader.pnlPct, 2)}）`
                : `当前盈亏最弱：${pnlLeader.name}（${safePercent(pnlLeader.pnlPct, 2)}）`}
            ，成本 <span className="tabular-nums">{formatPriceWithCurrency(pnlLeader.costPrice, pnlLeader.market)}</span>，
            现价 <span className="tabular-nums">{formatPriceWithCurrency(pnlLeader.currentPrice, pnlLeader.market)}</span>。
          </p>
        )}
        <p>仅陈列读数与你的防线距离，不构成投资建议。</p>
      </div>
    </section>
  )
}
