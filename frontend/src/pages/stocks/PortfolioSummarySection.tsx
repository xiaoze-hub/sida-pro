import ErrorBanner from '@/components/ErrorBanner'
import { Skeleton } from '@sida/base-ui/components/ui/skeleton'
import { Card } from '@sida/base-ui/components/ui/card'
import Stat from '@sida/biz-ui/components/Stat'
import { dailyPnlDisplayLabel } from './shared'
import type { DailyPnlCoverage, QuotesCoverage, TotalAssetsBasis } from './shared'
import { safeFixed, safeMoneyUnsigned } from '@/lib/format'
import { safeNum } from '@/lib/format'
import { useStocks } from './context'
import { useI18n } from '@/hooks/useI18n'

/**
 * 持仓页金额口径(2026-09-14 走查缺陷修复):
 * - **存量/规模读数**(总市值、可用资金、总资产、持仓市值/总资产明细) → `safeMoneyUnsigned`
 *   (不带 '+'): 它们是"有多少", 旧代码走 formatMoney(= safeMoney) 会渲染出 `+4.50万`,
 *   被读成变化量; 且总市值为 0 时旧代码渲染 `0`(无号) —— 同一行里一个带 + 一个不带,
 *   自相矛盾。
 * - **涨跌/盈亏读数**(总盈亏、当日盈亏) → 保留 `formatMoney` 的带符号口径(红涨绿跌需要方向)。
 *
 * 2026-10-29 缺行情修复: 总市值/总盈亏/总资产可能是 **null = 未知**(有持仓但一只都没行情)。
 * 未知一律渲染 `--`、**不给涨跌色**、也**不参与减法**(旧代码 `null - dayPnl` 会得 0 再算出
 * `+0.00%`, `null >= 0` 会把"不知道"染成上涨色)。
 *
 * 2026-10-29 三次复审(总资产口径收紧): 部分持仓缺价时,**总资产不是"已定价市值 + 资金"** ——
 * 缺价那部分市值根本没进来。后端契约(380bdabd)因此给 `total_assets=null` +
 * `total_assets_priced_subset`; 这里必须照做, 只能把它标成「已定价子集」显示,
 * 不许当总资产, 也不许拿它算仓位比例。
 */
const fmtAmount = (v: unknown) => safeMoneyUnsigned(v)

/** 未知不当 0: 只有拿到数值才给涨跌色, 否则交给默认中性色。 */
const pnlTone = (v: unknown): string | undefined => {
  const n = safeNum(v)
  if (n === null) return undefined
  return n > 0 ? 'text-stock-up' : n < 0 ? 'text-stock-down' : undefined
}

/** 总资产为什么不是数字（给用户一句人话，别只说 `--`）。 */
function totalAssetsHint(
  basis: TotalAssetsBasis | undefined,
  pricedSubset: number | null | undefined,
  missing: number | null | undefined,
): string {
  if (basis === 'available_funds_unknown') {
    return '可用资金未登记（未知），总资产不给数字；不按 0 元现金凑总额'
  }
  if (basis === 'priced_positions_only') {
    const subset = fmtAmount(pricedSubset)
    const miss = missing != null && missing > 0 ? `${missing} 条未定价持仓` : '未定价持仓'
    return `缺行情，总额未知；已定价子集 ${subset}（不含 ${miss}）`
  }
  if (basis === 'no_priced_positions') return '缺行情，市值与总额均未知'
  if (basis === 'not_requested') return '未计价'
  return ''
}

/**
 * 行情覆盖度提示（2026-10-29 缺行情修复）。
 *
 * 合计金额只在**已定价子集**上算 —— 只要有一条持仓没有行情，就必须说出来，
 * 否则用户会把"部分持仓的盈亏"当成"全部持仓的盈亏"。全覆盖时不显示（不制造噪声）。
 * 单位写「条持仓」而不是「只」：同一只票在两个账户就是两条，冒称"只"会把行数说错。
 */
function CoverageNote({ coverage }: { coverage?: QuotesCoverage }) {
  if (!coverage || !coverage.requested) return null
  if (coverage.positions_total === 0 || coverage.complete) return null
  const costPct =
    coverage.cost_coverage_pct === null ? null : `${safeFixed(coverage.cost_coverage_pct, 0)}%`
  return (
    <p
      className="mb-2 text-[12px] text-role-watch"
      data-testid="portfolio-coverage-note"
    >
      行情覆盖 {coverage.positions_priced ?? 0}/{coverage.positions_total} 条持仓，
      {coverage.positions_missing ?? 0} 条缺行情：市值与盈亏只按已定价持仓计算
      {costPct ? `（成本覆盖率 ${costPct}）` : ''}，缺行情的持仓未计入合计，总资产不给数字。
    </p>
  )
}

/**
 * 当日盈亏覆盖度提示：`change_pct` / 昨收缺失的持仓算不出当日盈亏，
 * 旧实现直接跳过 → 合计悄悄少算却仍顶着「今日盈亏」的标题。这里显式说明覆盖了几条。
 */
function DailyPnlNote({ coverage }: { coverage?: DailyPnlCoverage }) {
  if (!coverage || !coverage.requested) return null
  if (coverage.positions_total === 0 || coverage.complete) return null
  const observed = coverage.positions_observed ?? 0
  if (observed === 0) {
    return (
      <p className="mb-2 text-[12px] text-role-watch" data-testid="portfolio-daily-pnl-note">
        当日盈亏未知：{coverage.positions_total} 条持仓都缺现价或昨收，不按 0 涨跌计。
      </p>
    )
  }
  return (
    <p className="mb-2 text-[12px] text-role-watch" data-testid="portfolio-daily-pnl-note">
      当日盈亏只统计 {observed}/{coverage.positions_total} 条持仓（其余缺现价或昨收，未按 0 计）。
    </p>
  )
}

export function PortfolioSummarySection() {
  const { t } = useI18n()
  const {
    loadError,
    setLoadError,
    portfolio,
    portfolioLoading,
    load,
    loadPortfolio,
    positionRatio,
    portfolioMarketStatusLabel,
    formatMoney,
  } = useStocks()
  return (
    <>
{/* Portfolio Total Summary */}
{/* 2026-08-17: 加载失败横幅统一为 ErrorBanner(闭环修正 P0-3: 错误体系统一) */}
<ErrorBanner
  errors={loadError && !loadError.includes('429') ? [{ source: t('stocks.loadError'), message: loadError, retry: () => { void load(); void loadPortfolio() } }] : []}
  onDismiss={() => setLoadError && setLoadError(null)}
/>
{portfolioLoading && !portfolio ? (
  // 首次加载时显示骨架屏
  <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4 mb-6">
    {[...Array(4)].map((_, i) => (
      <div key={i}>
        <div className="flex items-center gap-2 mb-2">
          <Skeleton className="h-4 w-4 rounded" />
          <Skeleton className="h-3 w-12" />
        </div>
        <Skeleton className="h-6 w-20" />
      </div>
    ))}
  </div>
) : portfolio ? (
  <>
  <DailyPnlNote coverage={portfolio.total.daily_pnl_coverage} />
  <CoverageNote coverage={portfolio.total.quotes_coverage} />
  <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 md:gap-4 mb-6">
    <Card variant="plain" className=" p-3">
      <Stat
        label={t('stocks.totalMarketValue')}
        value={fmtAmount(portfolio.total.total_market_value)}
        className="[&>div:nth-child(2)]:text-[20px]"
      />
    </Card>
    <Card variant="plain" className=" p-3">
      <Stat
        label={t('stocks.totalPnl')}
        value={
          <>
            {formatMoney(portfolio.total.total_pnl)}
            <span className="text-[14px] ml-1.5">
              ({safeNum(portfolio.total.total_pnl_pct) === null ? '--' : `${portfolio.total.total_pnl_pct! >= 0 ? '+' : ''}${safeFixed(portfolio.total.total_pnl_pct)}%`})
            </span>
          </>
        }
        tone={pnlTone(portfolio.total.total_pnl)}
        className="[&>div:nth-child(2)]:text-[20px]"
      />
    </Card>

    {(() => {
      // 当日盈亏可能是 null(一只都没观测到) —— 那时"上日市值"也无从反推, 一律 --。
      const dayPnl = safeNum(portfolio.total.total_daily_pnl)
      // 2026-10-29 独立复审 F4: 反推上日市值必须用**与当日盈亏同一个篮子**的市值
      // (`daily_pnl_basket_value` = 参与当日盈亏的那几条持仓的市值), 不能用总市值:
      // 部分持仓缺昨收时, 总市值里那部分根本没有对应的当日盈亏, 减出来的分母是错的
      // (例: 全仓 16000 里只有 11000 参与当日 +1000, 旧算法给 6.67%, 正确是 10%)。
      // 拿不到篮子(老响应/后端没给) ⇒ 百分比未知, 绝不退回总市值硬算。
      const basket = safeNum(portfolio.total.daily_pnl_basket_value)
      const prevMv = basket !== null && dayPnl !== null ? basket - dayPnl : null
      const pct = prevMv !== null && prevMv > 0 && dayPnl !== null ? (dayPnl / prevMv) * 100 : null
      const dayCoverage = portfolio.total.daily_pnl_coverage
      const daySub =
        dayPnl === null
          ? '当日盈亏未知（缺现价或昨收）'
          : dayCoverage && !dayCoverage.complete
            ? `仅 ${dayCoverage.positions_observed ?? 0}/${dayCoverage.positions_total} 条持仓有当日盈亏，涨跌幅按这 ${dayCoverage.positions_observed ?? 0} 条的市值算`
            : portfolioMarketStatusLabel || undefined
      return (
        <Card variant="plain" className=" p-3">
          <Stat
            // 6 栏网格里全称会换行/截断成 3 行；紧凑档「上日 10-23」单行可读，全称仍在 hover 语义里
            label={dailyPnlDisplayLabel(portfolio.total, true)}
            value={
              <>
                {formatMoney(portfolio.total.total_daily_pnl)}
                <span className="text-[14px] ml-1.5">({pct != null && Number.isFinite(pct) ? `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%` : '--'})</span>
              </>
            }
            sub={daySub}
            tone={pnlTone(portfolio.total.total_daily_pnl)}
            className="[&>div:nth-child(2)]:text-[20px]"
          />
        </Card>
      )
    })()}

    <Card variant="plain" className=" p-3">
      <Stat
        label={t('stocks.availableCash')}
        value={fmtAmount(portfolio.total.available_funds)}
        sub={
          portfolio.total.available_funds == null
            ? '未登记（未知），不按 0 计'
            : undefined
        }
        className="[&>div:nth-child(2)]:text-[20px]"
      />
    </Card>
    <Card variant="plain" className=" p-3">
      <Stat
        label={t('stocks.totalAssets')}
        value={fmtAmount(portfolio.total.total_assets)}
        sub={
          portfolio.total.total_assets_complete === false
            ? totalAssetsHint(
                portfolio.total.total_assets_basis,
                portfolio.total.total_assets_priced_subset,
                portfolio.total.quotes_coverage?.positions_missing,
              )
            : undefined
        }
        className="[&>div:nth-child(2)]:text-[20px]"
      />
    </Card>

    <Card variant="plain" className=" p-3">
      <Stat
        label={t('stocks.positionRatio')}
        value={positionRatio && safeNum(positionRatio.pct) !== null ? `${safeFixed(positionRatio.pct, 1)}%` : '--'}
        sub={
          positionRatio && safeNum(positionRatio.pct) !== null
            ? `${t('stocks.portfolio')} ${fmtAmount(positionRatio.mv)} / ${t('stocks.totalAssets')} ${fmtAmount(positionRatio.assets)}`
            : portfolio.total.total_assets_basis === 'available_funds_unknown'
              ? '可用资金未知，总资产不给数字，比例也不给'
              : portfolio.total.total_assets_complete === false
                ? '总资产未知（缺行情），比例不给数字'
                : '总资产或市值未知，比例不给数字'
        }
        className="[&>div:nth-child(2)]:text-[20px]"
      />
    </Card>
  </div>
</>) : null}
    </>
  )
}
