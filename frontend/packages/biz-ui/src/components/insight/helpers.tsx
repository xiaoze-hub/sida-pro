import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { parseServerTime } from '@/lib/utils'
import { SuggestionBadge, type KlineSummary, type SuggestionInfo } from '@panwatch/biz-ui/components/suggestion-badge'
import { KlineIndicators } from '@panwatch/biz-ui/components/kline-indicators'
import { TechnicalBadge } from '@panwatch/biz-ui/components/technical-badge'

export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value == null) return '--'
  return value.toFixed(digits)
}

export function formatCompactNumber(value: number | null | undefined): string {
  if (value == null) return '--'
  const n = Number(value)
  if (!isFinite(n)) return '--'
  const abs = Math.abs(n)
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(n / 1e4).toFixed(2)}万`
  return n.toFixed(0)
}

export function formatMarketCap(value: number | null | undefined, market?: string): string {
  if (value == null) return '--'
  const n = Number(value)
  if (!isFinite(n)) return '--'
  const m = String(market || '').toUpperCase()
  const abs = Math.abs(n)

  // 腾讯 A 股字段常见为“亿元”口径（如 808 表示 808 亿元）
  if (m === 'CN' && abs > 0 && abs < 100000) {
    return `${n.toFixed(2)}亿元`
  }

  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)}亿元`
  if (abs >= 1e4) return `${(n / 1e4).toFixed(2)}万元`
  return `${n.toFixed(0)}元`
}

export const MORE_INFO_TIPS: Record<string, string> = {
  commission_ratio: "委比=(委买手数-委卖手数)/(委买+委卖)×100%。+100%全买盘，-100%全卖盘。+40%以上偏多，-40%以下偏空，但需结合价格位置看",
  limit_up_amount: "封单额=涨停价上的封单资金(元)。越大越强势，封单1亿以上为强封",
  limit_up_ratio: "封成比=封单额/流通市值。衡量封单强度，>5%为超强封板",
  open_amount: "竞价金额=09:15-09:25集合竞价成交额。放量高开易冲高回落",
  open_limit_buy: "竞价涨停买=竞价阶段涨停价的买单金额。预判开盘强度",
  consecutive_limit_days: "连板天数=连续涨停天数。≥3板为强势连板，注意炸板风险",
  consecutive_up_days: "连涨天数=连续上涨天数（含非涨停）。看趋势延续性",
  change_pct_5d: "5日涨幅=近5交易日累计涨跌。>15%短期过热，<-10%超跌",
  change_pct_20d: "20日涨幅=近月累计。看中期趋势",
  change_pct_ytd: "年初至今=今年以来累计。看年内主升/主跌",
  pe_dynamic: "动态PE=股价/预估每股收益。越低越便宜，<15低估，>40高估，结合行业对比",
  pb: "市净率=股价/每股净资产。<2破净附近，>5品牌溢价高",
  dividend_yield: "股息率=年分红/股价。>3%媲美理财，适合红利策略",
  beta: "Beta=相对大盘弹性。1跟大盘同步，>1.2更敏感，<0.8更稳健",
  ma5_price: "5日均价=近5日收盘均值。站上偏多，跌破偏空",
  high_52w_low_52w: "52周高/低=近一年最高/最低。接近新高压力大，接近新低有反弹可能",
  l2_tick_num: "L2逐笔数=Level2逐笔成交笔数（需L2权限）。数值大说明交投活跃",
  l2_order_num: "L2委托数=Level2委托队列笔数。看盘口深度",
  total_buy_vol: "总买量=全天委托买入总量（手）。与总卖量对比看多空力量",
  total_sell_vol: "总卖量=全天委托卖出总量。与总买量对比，卖>买为抛压重",
  cancel_buy: "撤买=撤销的买单数。撤单多为假单诱多，需警惕虚假买盘",
  cancel_sell: "撤卖=撤销的卖单数。撤卖多为假单诱空或洗盘",
  zjl: "主买净额=主动买入净额（万元，=主动买-主动卖）。正为多方占优，负为空方占优",
  zjl_hb: "主力净流入=主力净额（万元，同花顺口径）。衡量大单/主力资金整体方向，持续为正代表主力建仓",
}

export function InfoTip({ k }: { k: string }) {
  const tip = MORE_INFO_TIPS[k]
  if (!tip) return null
  return (
    <span
      className="ml-1 inline-flex h-3 w-3 items-center justify-center rounded-full bg-muted-foreground/15 text-[8px] text-muted-foreground cursor-help"
      title={tip}
    >
      ?
    </span>
  )
}

export function formatTime(isoTime?: string): string {
  if (!isoTime) return ''
  const d = parseServerTime(isoTime)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

export function parseToMs(input?: string): number | null {
  if (!input) return null
  const d = parseServerTime(input)
  if (!isNaN(d.getTime())) return d.getTime()
  const m = input.match(/^(\d{4})-(\d{2})-(\d{2})$/)
  if (!m) return null
  const dt = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 0, 0, 0)
  return isNaN(dt.getTime()) ? null : dt.getTime()
}

export function parseSuggestionJson(raw: unknown): Record<string, any> | null {
  if (typeof raw !== 'string') return null
  const s = raw.trim()
  if (!s) return null
  const candidates: string[] = [s]
  const fence = s.match(/```(?:json)?\s*([\s\S]*?)```/i)
  if (fence?.[1]) candidates.unshift(fence[1].trim())
  if (/^json\s*[\r\n]/i.test(s)) candidates.unshift(s.replace(/^json\s*[\r\n]/i, '').trim())
  for (const c of candidates) {
    if (!c) continue
    const direct = c
    const sliceStart = c.indexOf('{')
    const sliceEnd = c.lastIndexOf('}')
    const sliced = sliceStart >= 0 && sliceEnd > sliceStart ? c.slice(sliceStart, sliceEnd + 1) : ''
    for (const text of [direct, sliced]) {
      if (!text || !text.startsWith('{') || !text.endsWith('}')) continue
      try {
        const obj = JSON.parse(text)
        if (obj && typeof obj === 'object') return obj as Record<string, any>
      } catch {
        // try next candidate
      }
    }
  }
  return null
}

export function normalizeSuggestionAction(action?: string, actionLabel?: string): string {
  const a = String(action || '').trim().toLowerCase()
  const l = String(actionLabel || '').trim()
  if (a === 'buy/add' || a === 'add/buy') return /加仓|增持|补仓/.test(l) ? 'add' : 'buy'
  if (a === 'sell/reduce' || a === 'reduce/sell') return /减仓|减持/.test(l) ? 'reduce' : 'sell'
  return a || 'watch'
}

export function pickSuggestionText(raw: unknown, field: 'signal' | 'reason'): string {
  const plain = String(raw || '').trim()
  const obj = parseSuggestionJson(plain)
  if (obj) {
    const v = String(obj[field] || '').trim()
    if (v) return v
    if (field === 'reason') {
      const rv = String(obj['raw'] || '').trim()
      if (rv) return rv
    }
    return ''
  }
  return plain
}

export function normalizeTextList(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw.map(x => String(x || '').trim()).filter(Boolean)
  const s = String(raw || '').trim()
  if (!s) return []
  const bySep = s.split(/[；;、|]/).map(x => x.trim()).filter(Boolean)
  return bySep.length > 1 ? bySep : [s]
}

export function markdownToPlainText(input?: string): string {
  const raw = String(input || '').trim()
  if (!raw) return ''
  return raw
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)]\([^)]*\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    .replace(/\*\*|__|\*|_/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

export function StockReportMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        table: ({ children }) => (
          <div className="my-4 max-w-full overflow-x-auto rounded-lg border border-border/60">
            <table className="m-0 w-max min-w-full border-collapse text-[12px]">{children}</table>
          </div>
        ),
        th: ({ children }) => (
          <th className="whitespace-nowrap border-b border-r border-border/60 bg-accent/50 px-3 py-2 text-left font-semibold last:border-r-0">
            {children}
          </th>
        ),
        td: ({ children }) => (
          <td className="min-w-[96px] border-b border-r border-border/40 px-3 py-2 align-top last:border-r-0">
            {children}
          </td>
        ),
        a: ({ children, href }) => (
          <a href={href} target="_blank" rel="noopener noreferrer" className="break-all text-primary underline underline-offset-2">
            {children}
          </a>
        ),
      }}
    >
      {content}
    </ReactMarkdown>
  )
}

export function firstNonEmptyText(...vals: unknown[]): string {
  for (const v of vals) {
    const s = String(v || '').trim()
    if (s) return s
  }
  return ''
}

export function buildShareTechnicalRisks(kline: KlineSummary | null): string[] {
  if (!kline) return []
  const out: string[] = []
  const rsi = String(kline.rsi_status || '')
  const macd = `${kline.macd_cross || ''} ${kline.macd_status || ''}`
  const vol = String(kline.volume_trend || '')
  if (rsi.includes('超买')) out.push('短线过热回撤风险')
  if (rsi.includes('超卖')) out.push('弱势延续风险')
  if (macd.includes('死叉')) out.push('趋势转弱风险')
  if (macd.includes('顶背离')) out.push('动能背离风险')
  if (vol.includes('放量')) out.push('波动放大风险')
  return out.slice(0, 3)
}

export function TechnicalIndicatorStrip(props: {
  klineSummary: KlineSummary | null
  technicalSuggestion: SuggestionInfo | null
  stockName: string
  stockSymbol: string
  market: string
  hasPosition: boolean
  score?: number
  evidence?: Array<{ text: string; delta: number }>
}) {
  const { klineSummary, technicalSuggestion, stockName, stockSymbol, market, hasPosition, score, evidence = [] } = props
  if (!klineSummary) {
    return <div className="text-[12px] text-muted-foreground py-3">暂无技术指标</div>
  }
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[12px] text-muted-foreground">技术指标建议</span>
        <SuggestionBadge
          suggestion={technicalSuggestion}
          stockName={stockName}
          stockSymbol={stockSymbol}
          market={market}
          kline={klineSummary}
          hasPosition={hasPosition}
        />
        <TechnicalBadge label={`评分 ${Number(score ?? 0).toFixed(1)}`} tone="neutral" size="xs" className="text-foreground" />
      </div>
      {evidence.length > 0 && (
        <div className="flex flex-wrap gap-1.5 text-[10px]">
          {evidence.slice(0, 6).map((item, idx) => (
            <TechnicalBadge
              key={`${item.text}-${idx}`}
              label={`${item.text} ${item.delta > 0 ? `+${item.delta}` : item.delta}`}
              tone={item.delta > 0 ? 'bullish' : item.delta < 0 ? 'bearish' : 'neutral'}
              size="xs"
            />
          ))}
        </div>
      )}
      <KlineIndicators summary={klineSummary as any} />
    </div>
  )
}
