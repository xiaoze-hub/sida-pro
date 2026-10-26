import { useState } from 'react'
import type { FundamentalsDetail } from '@panwatch/api'
import { formatCompactNumber } from './helpers'

/** 龙虎榜默认渲染行数上限(2026-10-18 P0 审计 F13/R17: 生产 68 行把整页撑到 6 个屏) */
const DRAGON_TIGER_DEFAULT_ROWS = 10

/**
 * 基本面明细面板(龙虎榜/融资融券/股东户数/分红/事件日历)。
 * 数据来自 GET /api/market-data/fundamentals-detail/{symbol}?market=CN。
 * 容错策略: 加载中→"加载中"; 失败/无数据→静默"暂无"(不 toast, 不阻断弹窗其他功能);
 * 各分区有数据才显示, 字段缺失显示 "--"。字段口径与后端 chat.py 渲染保持一致
 * (金额=元, 户数=户, change_ratio 为百分数值, transfer/bonus_ratio 为每10股股数)。
 */
export function FundamentalsPanel(props: {
  data: FundamentalsDetail | null
  loading: boolean
  loaded: boolean
}) {
  const { data, loading, loaded } = props
  // hooks 必须在任何 early return 之前(P0: 龙虎榜折叠状态)
  const [dtExpanded, setDtExpanded] = useState(false)
  // 防御性取值: 后端字段可能缺省/为 null, 统一规整为数组
  const d = data || ({} as FundamentalsDetail)
  const dtList = Array.isArray(d.dragon_tiger) ? d.dragon_tiger : []
  const mgList = Array.isArray(d.margin) ? d.margin : []
  const scList = Array.isArray(d.shareholders) ? d.shareholders : []
  const divList = Array.isArray(d.dividend) ? d.dividend : []
  const evList = Array.isArray(d.events) ? d.events : []
  const hasAny = dtList.length > 0 || mgList.length > 0 || scList.length > 0 || divList.length > 0 || evList.length > 0
  // P0(2026-10-18 审计 F13): tdx_tq 源不给"上榜原因", 生产 70/70 全 null —— 整列 68 个 `--`
  // 还吃掉 1/3 表宽。全空就不渲染该列(只在标题上注明源未提供), 有值才显示。
  const dtHasReason = dtList.some((x) => typeof x.reason === 'string' && x.reason.trim() !== '')
  const dtHasNetBuy = dtList.some((x) => x.net_buy != null)
  const dtShown = dtExpanded ? dtList : dtList.slice(0, DRAGON_TIGER_DEFAULT_ROWS)

  if (loading) {
    return <div className="card p-6 text-[12px] text-muted-foreground text-center">加载中...</div>
  }
  // 后端端点未就绪(404/超时)或该股确无数据: 静默降级为"暂无"
  if (!loaded || !hasAny) {
    return <div className="card p-6 text-[12px] text-muted-foreground text-center">暂无基本面数据</div>
  }

  /** 金额(元)→万/亿 紧凑展示; 龙虎榜净买入/股东变动用红涨绿跌配色 */
  const money = (v: number | null | undefined) => (v == null ? '--' : formatCompactNumber(v))
  const signedMoney = (v: number | null | undefined) =>
    v == null ? '--' : `${v >= 0 ? '+' : ''}${formatCompactNumber(v)}`
  const signedPct = (v: number | null | undefined) =>
    v == null ? '--' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`

  return (
    <div className="space-y-3">
      {/* 龙虎榜 */}
      {dtList.length > 0 && (
        <div className="card p-4" data-testid="dragon-tiger-section">
          <div className="text-[12px] text-muted-foreground mb-2">
            龙虎榜
            <span className="ml-2 text-[12px] text-muted-foreground">
              共 {dtList.length} 条{dtList.length > DRAGON_TIGER_DEFAULT_ROWS && !dtExpanded
                ? ` · 显示最近 ${DRAGON_TIGER_DEFAULT_ROWS} 条` : ''}
            </span>
            {!dtHasReason && (
              <span className="ml-2 text-[12px] text-muted-foreground" data-testid="dragon-tiger-reason-missing">
                · 上榜原因源未提供(该列已隐藏)
              </span>
            )}
          </div>
          <div className="overflow-x-auto -mx-1">
            <table className="w-full text-[12px]">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="text-left px-1 py-1 font-normal whitespace-nowrap">日期</th>
                  {dtHasNetBuy && (
                    <th className="text-right px-1 py-1 font-normal whitespace-nowrap">净买入</th>
                  )}
                  {dtHasReason && (
                    <th className="text-left px-1 py-1 font-normal">上榜原因</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {dtShown.map((item, i) => (
                  <tr key={`dt-${item.trade_date || i}-${i}`} className="border-b border-border hover:bg-s2">
                    <td className="px-1 py-1 text-muted-foreground whitespace-nowrap">{item.trade_date || '--'}</td>
                    {dtHasNetBuy && (
                      <td className={`px-1 py-1 text-right font-mono tabular-nums whitespace-nowrap ${(item.net_buy ?? 0) >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                        {signedMoney(item.net_buy)}
                      </td>
                    )}
                    {dtHasReason && (
                      <td className="px-1 py-1 text-foreground">{item.reason || '--'}</td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {dtList.length > DRAGON_TIGER_DEFAULT_ROWS && (
            <button
              type="button"
              className="mt-2 text-[12px] text-muted-foreground hover:text-primary"
              data-testid="dragon-tiger-toggle"
              onClick={() => setDtExpanded((v) => !v)}
            >
              {dtExpanded
                ? `收起(只看最近 ${DRAGON_TIGER_DEFAULT_ROWS} 条)`
                : `展开全部 ${dtList.length} 条`}
            </button>
          )}
        </div>
      )}

      {/* 融资融券 */}
      {mgList.length > 0 && (
        <div className="card p-4">
          <div className="text-[12px] text-muted-foreground mb-2">融资融券</div>
          <div className="overflow-x-auto -mx-1">
            <table className="w-full text-[12px]">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="text-left px-1 py-1 font-normal whitespace-nowrap">日期</th>
                  <th className="text-right px-1 py-1 font-normal whitespace-nowrap">融资余额</th>
                  <th className="text-right px-1 py-1 font-normal whitespace-nowrap">融券余额</th>
                  <th className="text-right px-1 py-1 font-normal whitespace-nowrap">融资买入</th>
                  <th className="text-right px-1 py-1 font-normal whitespace-nowrap">两融合计</th>
                </tr>
              </thead>
              <tbody>
                {mgList.map((item, i) => (
                  <tr key={`mg-${item.date || i}-${i}`} className="border-b border-border hover:bg-s2">
                    <td className="px-1 py-1 text-muted-foreground whitespace-nowrap">{item.date || '--'}</td>
                    <td className="px-1 py-1 text-right font-mono whitespace-nowrap">{money(item.rz_balance)}</td>
                    <td className="px-1 py-1 text-right font-mono whitespace-nowrap">{money(item.rq_balance)}</td>
                    <td className="px-1 py-1 text-right font-mono whitespace-nowrap">{money(item.rz_buy)}</td>
                    <td className="px-1 py-1 text-right font-mono whitespace-nowrap">{money(item.total_balance)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 股东户数 */}
      {scList.length > 0 && (
        <div className="card p-4">
          <div className="text-[12px] text-muted-foreground mb-2">股东户数</div>
          <div className="overflow-x-auto -mx-1">
            <table className="w-full text-[12px]">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="text-left px-1 py-1 font-normal whitespace-nowrap">日期</th>
                  <th className="text-right px-1 py-1 font-normal whitespace-nowrap">股东户数</th>
                  <th className="text-right px-1 py-1 font-normal whitespace-nowrap">变动</th>
                  <th className="text-right px-1 py-1 font-normal whitespace-nowrap">环比</th>
                </tr>
              </thead>
              <tbody>
                {scList.map((item, i) => (
                  <tr key={`sc-${item.report_date || i}-${i}`} className="border-b border-border hover:bg-s2">
                    <td className="px-1 py-1 text-muted-foreground whitespace-nowrap">{item.report_date || '--'}</td>
                    <td className="px-1 py-1 text-right font-mono whitespace-nowrap">{money(item.holder_num)}</td>
                    {/* 户数减少=筹码集中, 按 A 股习惯红涨绿跌配色 */}
                    <td className={`px-1 py-1 text-right font-mono whitespace-nowrap ${item.change_num == null ? 'text-foreground' : item.change_num < 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                      {signedMoney(item.change_num)}
                    </td>
                    <td className={`px-1 py-1 text-right font-mono whitespace-nowrap ${item.change_ratio == null ? 'text-foreground' : item.change_ratio < 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                      {signedPct(item.change_ratio)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 分红 */}
      {divList.length > 0 && (
        <div className="card p-4">
          <div className="text-[12px] text-muted-foreground mb-2">分红</div>
          <div className="overflow-x-auto -mx-1">
            <table className="w-full text-[12px]">
              <thead className="text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="text-left px-1 py-1 font-normal">分红方案</th>
                  <th className="text-left px-1 py-1 font-normal whitespace-nowrap">除权除息日</th>
                  <th className="text-right px-1 py-1 font-normal whitespace-nowrap">每股派息</th>
                  <th className="text-left px-1 py-1 font-normal whitespace-nowrap">进度</th>
                </tr>
              </thead>
              <tbody>
                {divList.map((item, i) => {
                  // 方案拼装: 10派X元 + 10转X + 10送X(与后端 chat.py 口径一致)
                  const parts: string[] = []
                  if (item.dividend_per_share != null) parts.push(`10派${(item.dividend_per_share * 10).toFixed(2)}元`)
                  if (item.transfer_ratio != null) parts.push(`10转${item.transfer_ratio}`)
                  if (item.bonus_ratio != null) parts.push(`10送${item.bonus_ratio}`)
                  return (
                    <tr key={`div-${item.ex_date || i}-${i}`} className="border-b border-border hover:bg-s2">
                      <td className="px-1 py-1 text-foreground whitespace-nowrap">{parts.length > 0 ? parts.join(' ') : '--'}</td>
                      <td className="px-1 py-1 text-muted-foreground whitespace-nowrap">{item.ex_date || '--'}</td>
                      <td className="px-1 py-1 text-right font-mono whitespace-nowrap">{item.dividend_per_share != null ? `${item.dividend_per_share.toFixed(2)}元` : '--'}</td>
                      <td className="px-1 py-1 text-muted-foreground whitespace-nowrap">{item.progress || '--'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 事件日历 */}
      {evList.length > 0 && (
        <div className="card p-4">
          <div className="text-[12px] text-muted-foreground mb-2">事件日历(近7日)</div>
          <div className="space-y-1.5">
            {evList.map((item, i) => {
              const evDate = typeof item.publish_time === 'string' ? item.publish_time.slice(0, 10) : ''
              const title = item.title || '--'
              return (
                <div key={`ev-${item.external_id || item.publish_time || i}-${i}`} className="flex items-start gap-2 rounded bg-s2 px-2 py-1.5">
                  <span className="text-[12px] text-muted-foreground whitespace-nowrap mt-px">{evDate || '--'}</span>
                  {item.event_type && (
                    <span className="shrink-0 rounded-full bg-s2 px-1.5 py-0.5 text-[12px] text-foreground">{item.event_type}</span>
                  )}
                  {item.url ? (
                    <a href={item.url} target="_blank" rel="noreferrer" className="text-[12px] text-foreground leading-snug hover:text-primary hover:underline line-clamp-2" title={title}>
                      {title}
                    </a>
                  ) : (
                    <span className="text-[12px] text-foreground leading-snug line-clamp-2" title={title}>{title}</span>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
