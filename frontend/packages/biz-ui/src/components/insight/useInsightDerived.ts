import { useCallback, useMemo } from 'react'
import { getMarketBadge } from '@panwatch/biz-ui'
import { readStockColors } from '@panwatch/biz-ui/lib/stock-colors'
import { buildKlineSuggestion } from '@/lib/kline-scorer'
import type { SuggestionInfo } from '@panwatch/biz-ui/components/suggestion-badge'
import type { StockInsightModalProps, HistoryRecord } from './types'
import {
  parseSuggestionJson,
  normalizeTextList,
  firstNonEmptyText,
  buildShareTechnicalRisks,
  formatNumber,
} from './helpers'
import type { useInsightData } from './useInsightData'

export function useInsightDerived(props: StockInsightModalProps, data: ReturnType<typeof useInsightData>) {
  const { symbol, market, resolvedName, quote, klineSummary, suggestions, holdingAgg, reports, reportTab } = data
const hasHolding = !!props.hasPosition || !!holdingAgg
const technicalScored = useMemo(() => {
  if (!klineSummary) return null
  return buildKlineSuggestion(klineSummary as any, hasHolding)
}, [klineSummary, hasHolding])
const technicalFallbackSuggestion = useMemo<SuggestionInfo | null>(() => {
  if (!klineSummary || !technicalScored) return null
  const topEvidence = (technicalScored.evidence || []).filter(e => e.delta !== 0).slice(0, 3).map(e => e.text)
  return {
    action: technicalScored.action,
    action_label: technicalScored.action_label,
    signal: technicalScored.signal || '技术面中性',
    reason: topEvidence.length > 0 ? topEvidence.join('；') : '基于K线技术指标自动生成的基础建议',
    should_alert: technicalScored.action === 'buy' || technicalScored.action === 'add' || technicalScored.action === 'sell' || technicalScored.action === 'reduce',
    agent_name: 'technical_fallback',
    agent_label: '技术指标',
    created_at: new Date().toISOString(),
    is_expired: false,
    meta: {
      fallback: true,
      score: technicalScored.score,
      evidence_count: technicalScored.evidence?.length || 0,
    },
  }
}, [klineSummary, technicalScored])
const buildPageContext = useCallback(() => {
  const parts: string[] = []
  if (quote) {
    const items = [`价格${quote.current_price}`, `涨跌幅${quote.change_pct}%`]
    if (quote.volume != null) items.push(`成交量${quote.volume}`)
    if (quote.turnover_rate != null) items.push(`换手率${quote.turnover_rate}%`)
    if (quote.pe_ratio != null) items.push(`市盈率${quote.pe_ratio}`)
    if (quote.total_market_value != null) items.push(`总市值${quote.total_market_value}`)
    parts.push(`实时行情：${items.join('，')}`)
  }
  if (klineSummary) {
    const k = klineSummary as any
    const items = []
    if (k.trend) items.push(`趋势${k.trend}`)
    if (k.macd_status) items.push(`MACD${k.macd_status}`)
    if (k.rsi_status) items.push(`RSI${k.rsi_status}${k.rsi6 != null ? `(${k.rsi6})` : ''}`)
    if (k.kdj_status) items.push(`KDJ${k.kdj_status}`)
    if (k.boll_status) items.push(`布林${k.boll_status}`)
    if (k.volume_trend) items.push(`量能${k.volume_trend}${k.volume_ratio != null ? `(${k.volume_ratio}x)` : ''}`)
    if (k.support != null) items.push(`支撑${k.support}`)
    if (k.resistance != null) items.push(`压力${k.resistance}`)
    if (items.length) parts.push(`技术面：${items.join('，')}`)
  }
  if (technicalScored) {
    parts.push(`技术评分：${technicalScored.action_label}(score=${technicalScored.score})，信号：${technicalScored.signal || '中性'}`)
    const evidence = (technicalScored.evidence || []).filter((e: any) => e.delta !== 0)
    if (evidence.length) {
      parts.push(`评分依据：${evidence.map((e: any) => `${e.text}(${e.delta > 0 ? '+' : ''}${e.delta})`).join('；')}`)
    }
  }
  if (suggestions.length > 0) {
    const lines = suggestions.slice(0, 3).map(s => `- [${s.agent_label || s.agent_name}] ${s.action_label}: ${s.signal}`)
    parts.push(`最近AI建议：\n${lines.join('\n')}`)
  }
  if (holdingAgg) {
    parts.push(`持仓：${holdingAgg.quantity}股，成本${holdingAgg.unitCost}，市值${holdingAgg.marketValue}，盈亏${holdingAgg.pnl}`)
  }
  return parts.join('\n')
}, [quote, klineSummary, technicalScored, suggestions, holdingAgg])

const quoteUp = (quote?.change_pct || 0) > 0
const quoteDown = (quote?.change_pct || 0) < 0
// 涨跌色统一走设计令牌 --stock-up/--stock-down (红涨绿跌, A股口径)
const changeColor = quoteUp ? 'text-stock-up' : quoteDown ? 'text-stock-down' : 'text-foreground'
const priceColor = quoteUp ? 'text-stock-up' : quoteDown ? 'text-stock-down' : 'text-foreground'
const levelColor = (value: number | null | undefined) => {
  if (value == null || quote?.prev_close == null) return 'text-foreground'
  if (value > quote.prev_close) return 'text-stock-up'
  if (value < quote.prev_close) return 'text-stock-down'
  return 'text-foreground'
}
// 图表/分享图用的运行时色值 (ECharts/SVG 无法消费 Tailwind 类)
const stockColors = readStockColors()
const badge = getMarketBadge(market)
const amplitudePct = useMemo(() => {
  const hi = quote?.high_price
  const lo = quote?.low_price
  const pre = quote?.prev_close
  if (hi == null || lo == null || pre == null || pre === 0) return null
  return ((hi - lo) / pre) * 100
}, [quote?.high_price, quote?.low_price, quote?.prev_close])

const reportMap = useMemo(() => {
  const out: Record<string, HistoryRecord | null> = {
    premarket_outlook: null,
    daily_report: null,
    news_digest: null,
  }
  for (const r of reports) {
    if (!out[r.agent_name]) out[r.agent_name] = r
  }
  return out
}, [reports])
const activeReport = reportMap[reportTab]
const latestReport = reports[0] || null
const latestShareSuggestion = suggestions[0] || technicalFallbackSuggestion
const shareCardPayload = useMemo(() => {
  const jsonSources = [
    parseSuggestionJson((latestShareSuggestion as any)?.signal),
    parseSuggestionJson((latestShareSuggestion as any)?.reason),
    parseSuggestionJson((latestShareSuggestion as any)?.raw),
    parseSuggestionJson((latestShareSuggestion as any)?.ai_response),
    parseSuggestionJson((latestShareSuggestion as any)?.prompt_context),
    (latestShareSuggestion as any)?.meta && typeof (latestShareSuggestion as any).meta === 'object'
      ? ((latestShareSuggestion as any).meta as Record<string, any>)
      : null,
  ].filter(Boolean) as Array<Record<string, any>>
  const pickFromJson = (...keys: string[]) => {
    for (const obj of jsonSources) {
      for (const key of keys) {
        const s = String(obj?.[key] || '').trim()
        if (s) return s
      }
    }
    return ''
  }
  const pickListFromJson = (...keys: string[]) => {
    for (const obj of jsonSources) {
      for (const key of keys) {
        const list = normalizeTextList(obj?.[key])
        if (list.length > 0) return list
      }
    }
    return [] as string[]
  }
  const marketLabel = badge.label
  const price = quote?.current_price != null ? formatNumber(quote.current_price) : '--'
  const chg = quote?.change_pct != null ? `${quote.change_pct >= 0 ? '+' : ''}${quote.change_pct.toFixed(2)}%` : '--'
  const action = latestShareSuggestion?.action_label || latestShareSuggestion?.action || '暂无'
  const signal = firstNonEmptyText(
    latestShareSuggestion?.signal,
    pickFromJson('signal', 'summary', 'core_view'),
    technicalScored?.signal,
    '技术面中性'
  ) || '--'
  const reason = firstNonEmptyText(
    latestShareSuggestion?.reason,
    pickFromJson('reason', 'thesis', 'core_judgement', 'core_judgment', 'analysis'),
    technicalFallbackSuggestion?.reason,
    '暂无'
  ) || '--'
  const risksList = [
    ...normalizeTextList((latestShareSuggestion as any)?.meta?.risks),
    ...pickListFromJson('risks', 'risk', 'risk_points'),
    ...buildShareTechnicalRisks(klineSummary),
  ].filter(Boolean)
  const dedupRisks = Array.from(new Set(risksList))
  const risks = dedupRisks.length > 0 ? dedupRisks.slice(0, 2).join('；') : '市场波动风险'
  const triggerList = pickListFromJson('triggers', 'trigger', 'signals')
  const invalidList = pickListFromJson('invalidations', 'invalidation', 'stop_conditions')
  const trigger = triggerList.length > 0 ? triggerList.slice(0, 2).join('；') : '--'
  const invalidation = invalidList.length > 0 ? invalidList.slice(0, 2).join('；') : '--'
  const technicalBrief = firstNonEmptyText(
    [klineSummary?.trend, klineSummary?.macd_status, klineSummary?.rsi_status].filter(Boolean).join(' / '),
    technicalScored?.signal
  ) || '--'
  const levelsBrief = (klineSummary?.support != null && klineSummary?.resistance != null)
    ? `支撑 ${formatNumber(klineSummary.support)} / 压力 ${formatNumber(klineSummary.resistance)}`
    : '--'
  const source = latestShareSuggestion?.agent_label || latestShareSuggestion?.agent_name || '技术指标'
  const ts = new Date().toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
  return { marketLabel, price, chg, action, signal, reason, risks, trigger, invalidation, technicalBrief, levelsBrief, source, ts }
}, [badge.label, klineSummary, latestShareSuggestion, quote?.change_pct, quote?.current_price, technicalFallbackSuggestion?.reason, technicalScored?.signal])

const shareText = useMemo(() => {
  const { marketLabel, price, chg, action, signal, reason, risks, trigger, invalidation, technicalBrief, levelsBrief, source, ts } = shareCardPayload
  const lines = [
    `【PanWatch 洞察】${resolvedName}（${symbol} · ${marketLabel}）`,
    `时间：${ts}`,
    `现价：${price}（${chg}）`,
    `建议：${action}`,
    `信号：${signal}`,
    `理由：${reason}`,
    `风险：${risks}`,
    `技术：${technicalBrief}`,
    `关键位：${levelsBrief}`,
    `来源：${source}`,
  ]
  if (trigger !== '--') lines.splice(7, 0, `触发：${trigger}`)
  if (invalidation !== '--') lines.splice(8, 0, `失效：${invalidation}`)
  return lines.join('\n')
}, [shareCardPayload, resolvedName, symbol])

  return {
    hasHolding,
    technicalScored,
    technicalFallbackSuggestion,
    buildPageContext,
    quoteUp,
    quoteDown,
    changeColor,
    priceColor,
    levelColor,
    stockColors,
    badge,
    amplitudePct,
    reportMap,
    activeReport,
    latestReport,
    latestShareSuggestion,
    shareCardPayload,
    shareText,
  }
}
