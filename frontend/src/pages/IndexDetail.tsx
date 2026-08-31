import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, TrendingUp, RefreshCw, Activity, BarChart3, Flame, Droplets } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import InteractiveKline from '@panwatch/biz-ui/components/InteractiveKline'
import ErrorBanner from '@/components/ErrorBanner'
import { describeApiError } from '@/lib/api-error'
import { safeFixed, safeNum, safeNetInflow } from '@/lib/format'

interface MarketFlow {
  total_main_flow?: number
  sh_flow?: number
  sz_flow?: number
  cyb_flow?: number
  total_amount?: number
  up_count?: number
  down_count?: number
  flat_count?: number
  source?: string
  timestamp?: string
  inflow_boards?: { name: string; net_inflow: number; change_pct?: number | null }[]
  outflow_boards?: { name: string; net_inflow: number; change_pct?: number | null }[]
}

interface IndexDetail {
  symbol: string
  name: string
  market: string
  quote: {
    current_price: number
    change_pct: number
    change_amount: number
    prev_close: number
    open?: number | null
    high?: number | null
    low?: number | null
    volume?: number | null
    amount?: number | null
  } | null
  klines: { date: string; open: number; close: number; high: number; low: number; volume: number }[]
  amount_trend: { date: string; amount: number }[]
  note?: string
  error?: string
}

// 成交额柱状图(大盘资金流替代: 近20日成交额)
function AmountChart({ trend }: { trend: { date: string; amount: number }[] }) {
  if (trend.length === 0) return null
  const maxA = Math.max(...trend.map(t => t.amount))
  const W = 720, H = 90
  const bw = W / trend.length
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: 100 }}>
      {trend.map((t, i) => {
        const h = (t.amount / maxA) * (H - 16)
        return (
          <g key={t.date}>
            <rect x={i * bw + bw * 0.2} y={H - 8 - h} width={bw * 0.6} height={h} fill="#58a6ff" opacity={0.7} rx={1} />
            {i % 5 === 0 && (
              <text x={i * bw + bw / 2} y={H - 2} fontSize={8} fill="#8b949e" textAnchor="middle">
                {t.date.slice(5)}
              </text>
            )}
          </g>
        )
      })}
    </svg>
  )
}

export default function IndexDetailPage() {
  const { symbol } = useParams<{ symbol: string }>()
  const navigate = useNavigate()
  const [data, setData] = useState<IndexDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // 大盘资金流(同花顺源, 东财502替代)
  const [marketFlow, setMarketFlow] = useState<MarketFlow | null>(null)

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const d = await fetchAPI<IndexDetail>(`/market/indices/${symbol}`)
      if (d?.error) setError(d.error)
      else setData(d)
    } catch (e: any) {
      // 2026-08-17: 错误分类 (B 报告 P1-9) — TIMEOUT / HTTP_5xx / NETWORK 分别给文案
      setError(describeApiError(e))
    } finally {
      setLoading(false)
    }
    // 大盘资金流(独立加载, 失败静默)
    fetchAPI<MarketFlow>('/market-data/market-capital-flow').then(setMarketFlow).catch(() => {})
  }

  useEffect(() => { load() }, [symbol])

  const q = data?.quote
  const up = (q?.change_pct || 0) >= 0

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" className="h-8" onClick={() => navigate(-1)}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <TrendingUp className="h-6 w-6" /> {data?.name || '大盘指数'}
          </h1>
          <div className="text-xs text-muted-foreground font-mono">{symbol}</div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-8" onClick={load} disabled={loading}>
            <RefreshCw className={`h-3.5 w-3.5 mr-1 ${loading ? 'animate-spin' : ''}`} /> 刷新
          </Button>
        </div>
      </div>

      {loading ? (
        <div className="text-center text-muted-foreground py-12">加载中...</div>
      ) : error ? (
        <ErrorBanner
          errors={[{ source: '指数详情', message: error, retry: () => void load() }]}
          onDismiss={() => setError('')}
        />
      ) : data ? (
        <>
          {/* 实时行情卡片(同个股详情风格) */}
          <div className="card p-4">
            <div className="flex items-end gap-4 flex-wrap">
              <div>
                <div className="text-3xl font-num font-bold tabular-nums">{safeFixed(q?.current_price)}</div>
                <div className={`text-sm font-num tabular-nums ${up ? 'text-red-600' : 'text-green-700'}`}>
                  {safeNum(q?.change_amount) !== null && Number(q?.change_amount) > 0 ? '+' : ''}{safeFixed(q?.change_amount)} ({safeFixed(q?.change_pct)}%)
                </div>
              </div>
              <div className="flex gap-6 text-sm text-muted-foreground">
                <div><span className="block text-[10px]">昨收</span><span className="font-mono text-foreground tabular-nums">{safeFixed(q?.prev_close)}</span></div>
                <div><span className="block text-[10px]">今开</span><span className="font-mono text-foreground tabular-nums">{safeFixed(q?.open)}</span></div>
                <div><span className="block text-[10px]">最高</span><span className="font-mono text-foreground tabular-nums">{safeFixed(q?.high)}</span></div>
                <div><span className="block text-[10px]">最低</span><span className="font-mono text-foreground tabular-nums">{safeFixed(q?.low)}</span></div>
                <div><span className="block text-[10px]">成交量</span><span className="font-mono text-foreground tabular-nums">{q?.volume != null && Number.isFinite(Number(q.volume)) ? safeFixed(Number(q.volume) / 1e8) + '亿' : '--'}</span></div>
              </div>
            </div>
          </div>

          {/* K线走势(复用个股同款 InteractiveKline: MA/成交量/MACD/RSI + 日周月) */}
          <div className="card p-4">
            <div className="flex items-center gap-2 mb-2">
              <Activity className="h-4 w-4" />
              <span className="font-bold">K线走势</span>
              <span className="text-[10px] text-muted-foreground">MA/成交量/MACD/RSI · 日K/周K/月K 切换</span>
            </div>
            <InteractiveKline symbol={symbol || ''} market={data.market || 'CN'} initialInterval="1d" initialDays="120" />
          </div>

          {/* 大盘资金流(东财两市主力净流入, 对齐同花顺APP) */}
          {marketFlow && (
            <div className="card-subtle p-3 mb-3">
              <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
                <div className="flex items-center gap-2">
                  <TrendingUp className="h-4 w-4 text-primary" />
                  <span className="text-[12px] font-semibold">大盘资金流</span>
                  <span className="text-[10px] text-muted-foreground">东财 · 两市主力</span>
                </div>
                <div className="flex items-center gap-4 text-[12px]">
                  <span className="text-muted-foreground">主力净流入
                    <b className={`font-mono ${(marketFlow.total_main_flow ?? 0) >= 0 ? 'text-red-600' : 'text-green-700'}`}>
                      {safeNetInflow(marketFlow.total_main_flow)}
                    </b>
                  </span>
                  <span className="text-muted-foreground">成交额 <b className="font-mono">{safeNum(marketFlow.total_amount) !== null ? `${(marketFlow.total_amount!).toFixed(0)}亿` : '--'}</b></span>
                  <span className="text-muted-foreground">涨 <b className="text-red-600 font-mono">{marketFlow.up_count ?? '--'}</b>
                    <span className="mx-1">/</span>跌 <b className="text-green-700 font-mono">{marketFlow.down_count ?? '--'}</b></span>
                  <span className="text-muted-foreground">沪 <b className="font-mono">{safeNum(marketFlow.sh_flow) !== null ? `${(marketFlow.sh_flow!).toFixed(1)}亿` : '--'}</b>
                    <span className="mx-1">/</span>深 <b className="font-mono">{safeNum(marketFlow.sz_flow) !== null ? `${(marketFlow.sz_flow!).toFixed(1)}亿` : '--'}</b></span>
                </div>
              </div>

              {/* 板块资金明细: 流入榜 / 流出榜 */}
              {(marketFlow.inflow_boards?.length || marketFlow.outflow_boards?.length) ? (
                <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-3">
                  {marketFlow.inflow_boards?.length ? (
                    <div className="card-subtle p-2.5">
                      <div className="text-[11px] font-semibold text-red-600 mb-1 flex items-center gap-1"><Flame className="w-3 h-3" />资金流入板块</div>
                      <div className="space-y-0.5">
                        {marketFlow.inflow_boards.map(b => (
                          <div key={b.name} className="flex justify-between text-[11px]">
                            <span className="text-muted-foreground truncate">{b.name}</span>
                            <span className="font-mono text-red-600">{safeNum(b.net_inflow) !== null ? `+${Number(b.net_inflow).toFixed(1)}亿` : '--'}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}
                  {marketFlow.outflow_boards?.length ? (
                    <div className="card-subtle p-2.5">
                      <div className="text-[11px] font-semibold text-green-700 mb-1 flex items-center gap-1"><Droplets className="w-3 h-3" />资金流出板块</div>
                      <div className="space-y-0.5">
                        {marketFlow.outflow_boards.map(b => (
                          <div key={b.name} className="flex justify-between text-[11px]">
                            <span className="text-muted-foreground truncate">{b.name}</span>
                            <span className="font-mono text-green-700">{safeNum(b.net_inflow) !== null ? `${Number(b.net_inflow).toFixed(1)}亿` : '--'}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          )}

          {/* 成交额趋势(大盘资金流替代) */}
          <div className="card p-4">
            <div className="flex items-center gap-2 mb-2">
              <BarChart3 className="h-4 w-4" />
              <span className="font-bold">成交额趋势(近20日)</span>
              <span className="text-[10px] text-muted-foreground">单位:亿元</span>
            </div>
            <AmountChart trend={data.amount_trend} />
            {data.note && <div className="text-[10px] text-amber-700 dark:text-amber-500 mt-2">{data.note}</div>}
          </div>
        </>
      ) : null}
    </div>
  )
}
