import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { RefreshCw, Search, Loader2 } from 'lucide-react'
import { insightApi } from '@panwatch/api'
import { toAmount } from '@/lib/format'

/**
 * L2 盘口资金页（立项: 诚实口径五档+L2成品，不叫十档）。
 *
 * 数据源（三端点现成，后端零新增）:
 * - /api/orderbook-ob: thsdk 盘口 → 十档买卖额(bid_amt10/ask_amt10) + OB失衡序列 + 演变事件 + 幽灵单
 * - /klines/{s}/summary.orderbook: 托压形态/买盘占比/最优价/价差
 * - /quotes/{s}/more-info: L2成品(l2_tick_num/l2_order_num/total_buy_vol/total_sell_vol/cancel_buy/cancel_sell/raw)
 *
 * 无十档逐笔明细（通达信官方未给TQ该能力）→ 页面不设逐笔表，不编造。
 * 非交易时段/thsdk未接 → 各区独立降级，展示后端 note 原文。
 */

interface ObSnapshot {
  ob?: number | null
  label?: string | null
  bid_amt10?: number | null
  ask_amt10?: number | null
  ts?: number | null
  dt?: string | null
}

interface ObResp {
  available?: boolean
  ob_series?: ObSnapshot[]
  events?: Array<{ type?: string; label?: string; dt?: string; detail?: string } | string>
  ghost_ratio?: number | null
  note?: string | null
}

interface SummaryOb {
  available?: boolean
  shape?: string | null
  best_bid?: number | null
  best_ask?: number | null
  spread?: number | null
  bid_pressure?: number | null
  note?: string | null
}

interface MoreInfo {
  l2_tick_num?: number | null
  l2_order_num?: number | null
  total_buy_vol?: number | null
  total_sell_vol?: number | null
  cancel_buy?: number | null
  cancel_sell?: number | null
  /** 主力净额(万元, 同花顺口径) / 主买净额(万元) — 后端 2026-09-09 起随 payload 下发 */
  zjl_hb?: number | null
  zjl?: number | null
  quote_time?: string | null
  raw?: Record<string, any> | null
}

/* toAmount 统一走 @/lib/format(P3 收敛, 旧手抄版删除) */

function num(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '--'
  return String(v)
}

/** more-info raw 容错取 L2 主力字段（大小写/命名多版本兼容; TQ raw 值为字符串, 需转数值） */
function rawPick(raw: Record<string, any> | null | undefined, ...keys: string[]): number | null {
  if (!raw) return null
  for (const k of keys) {
    const v = raw[k]
    if (typeof v === 'number' && Number.isFinite(v)) return v
    if (typeof v === 'string' && v.trim() !== '') {
      const n = Number(v)
      if (Number.isFinite(n)) return n
    }
  }
  return null
}

export default function L2OrderbookPage() {
  const [params, setParams] = useSearchParams()
  const symbol = (params.get('symbol') || '002361').trim()
  const [input, setInput] = useState(symbol)
  const [ob, setOb] = useState<ObResp | null>(null)
  const [sumOb, setSumOb] = useState<SummaryOb | null>(null)
  const [mi, setMi] = useState<MoreInfo | null>(null)
  const [loading, setLoading] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<string>('')
  // P1-9 (2026-09-05 28号审计): 请求序号守卫, 防切股旧响应覆盖
  const seqRef = useRef(0)

  const load = useCallback(async (sym: string) => {
    const seq = ++seqRef.current
    setLoading(true)
    try {
      const [o, s, m] = await Promise.all([
        insightApi.orderbookOb<ObResp>(sym).catch(() => null),
        insightApi.klineSummary<{ orderbook?: SummaryOb }>(sym, 'CN').catch(() => null),
        insightApi.moreInfo<MoreInfo>(sym, 'CN').catch(() => null),
      ])
      if (seq !== seqRef.current) return
      if (o) setOb(o)
      if (s) setSumOb(s.orderbook ?? null)
      if (m) setMi(m)
      setUpdatedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
    } finally {
      if (seq === seqRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => { setInput(symbol) }, [symbol])
  useEffect(() => {
    load(symbol)
    const t = setInterval(() => load(symbol), 30000) // 轮询≥30s先行，不做WS
    return () => clearInterval(t)
  }, [symbol, load])

  const submit = () => {
    const v = input.trim()
    if (!v) return
    const next = new URLSearchParams(params)
    next.set('symbol', v)
    setParams(next)
  }

  const latest = ob?.ob_series?.slice(-1)?.[0]
  const bid10 = latest?.bid_amt10 ?? null
  const ask10 = latest?.ask_amt10 ?? null
  const tot10 = (bid10 ?? 0) + (ask10 ?? 0)
  const bidPct = tot10 > 0 ? ((bid10 ?? 0) / tot10) * 100 : null

  // 优先用后端已解析字段(数值), 回退 raw(字符串, 兼容旧后端)
  const zjlHb = mi?.zjl_hb ?? rawPick(mi?.raw, 'Zjl_HB', 'zjl_hb', 'ZJL_HB')
  const zjl = mi?.zjl ?? rawPick(mi?.raw, 'Zjl', 'zjl', 'ZJL')

  return (
    <div className="sida-page-enter w-full space-y-3">
      {/* === 顶部条 === */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border/40 pb-2">
        <h1 className="text-base font-semibold text-foreground">盘口资金</h1>
        <span className="text-[11px] text-muted-foreground">五档 + L2成品（无十档逐笔明细）</span>
        <div className="flex flex-1 items-center gap-1 min-w-[200px]">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
            placeholder="代码, 如 002361"
            className="h-7 min-w-0 flex-1 rounded-md border border-border/50 bg-background px-2 text-[12px] outline-none focus:border-primary/50"
          />
          <button
            type="button"
            onClick={submit}
            className="inline-flex h-7 shrink-0 items-center gap-1 rounded-md border border-border/50 bg-background px-2 text-[11px] text-muted-foreground hover:text-foreground"
          >
            <Search className="h-3 w-3" /> 查询
          </button>
          <button
            type="button"
            onClick={() => load(symbol)}
            disabled={loading}
            className="inline-flex h-7 shrink-0 items-center gap-1 rounded-md border border-border/50 bg-background px-2 text-[11px] text-muted-foreground hover:text-foreground"
          >
            <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} /> 刷新
          </button>
        </div>
        {updatedAt && <span className="text-[10px] text-muted-foreground font-mono">更新 {updatedAt} · 30s轮询</span>}
      </div>

      {/* === 形态条带 === */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-border/40 pb-2 text-[12px]">
        <span className="text-muted-foreground" title="托压形态: 按十档买卖额结构判定(均衡/买盘占优/卖盘占优)">形态:</span>
        <span className="font-medium text-foreground">{sumOb?.shape ?? '--'}</span>
        <span className="text-border/60">|</span>
        <span className="text-muted-foreground" title="买盘占比 = 十档买额 /(十档买额 + 十档卖额)">买盘占比:</span>
        <span className="font-mono">{sumOb?.bid_pressure != null ? `${(sumOb.bid_pressure * 100).toFixed(1)}%` : '--'}</span>
        <span className="text-border/60">|</span>
        <span className="text-muted-foreground" title="最优买价(买一) / 最优卖价(卖一)">最优:</span>
        <span className="font-mono">{sumOb?.best_bid ?? '--'} / {sumOb?.best_ask ?? '--'}</span>
        <span className="text-border/60">|</span>
        <span className="text-muted-foreground" title="价差 = 卖一价 - 买一价">价差:</span>
        <span className="font-mono">{sumOb?.spread ?? '--'}</span>
        {sumOb && !sumOb.available && sumOb.note && (
          <span className="text-[11px] text-amber-500">⚠ {sumOb.note}</span>
        )}
      </div>

      <div className="grid grid-cols-12 gap-3">
        <div className="col-span-12 lg:col-span-7 space-y-3">
          {/* === 十档买卖额双向条 === */}
          <div className="border-b border-border/40 pb-3">
            <div className="text-[11px] text-muted-foreground mb-2" title="十档买额 / 卖额合计(元), 来自 thsdk 盘口快照聚合">十档买卖额（thsdk 盘口聚合）</div>
            {!ob?.available ? (
              <div className="text-[12px] text-muted-foreground py-4 text-center">
                {ob?.note ?? '盘口无数据（非交易时段或 thsdk 未接）'}
              </div>
            ) : (
              <div className="space-y-1.5">
                <div className="flex items-center gap-2 text-[12px]">
                  <span className="w-8 shrink-0 text-muted-foreground">买</span>
                  <div className="h-4 flex-1 bg-accent/20">
                    <div className="h-4 bg-stock-up" style={{ width: `${bidPct ?? 0}%` }} />
                  </div>
                  <span className="w-24 shrink-0 text-right font-mono text-stock-up">{toAmount(bid10)}</span>
                </div>
                <div className="flex items-center gap-2 text-[12px]">
                  <span className="w-8 shrink-0 text-muted-foreground">卖</span>
                  <div className="h-4 flex-1 bg-accent/20">
                    <div className="h-4 bg-stock-down" style={{ width: `${bidPct != null ? 100 - bidPct : 0}%` }} />
                  </div>
                  <span className="w-24 shrink-0 text-right font-mono text-stock-down">{toAmount(ask10)}</span>
                </div>
                <div className="text-[10px] text-muted-foreground/70">
                  {latest?.dt ? `快照 ${latest.dt}` : '无快照时间'} · 买占比 {bidPct != null ? `${bidPct.toFixed(1)}%` : '--'}
                </div>
              </div>
            )}
          </div>

          {/* === L2成品资金 === */}
          <div className="border-b border-border/40 pb-3">
            <div className="text-[11px] text-muted-foreground mb-2">
              L2成品资金（TQ get_more_info，明盘口径{mi?.quote_time ? ` · ${mi.quote_time.slice(0, 19).replace('T', ' ')}` : ''}）
            </div>
            {!mi ? (
              <div className="flex items-center gap-2 py-4 text-[12px] text-muted-foreground">
                {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null} 加载中…
              </div>
            ) : (
              <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-2 text-[12px]">
                <div>
                  <div className="text-muted-foreground text-[11px]" title="主力净额(万元, 同花顺口径)= 大单 + 超大单净额; 正为净流入, 持续为正代表主力吸筹">主力净额</div>
                  <div className={`font-mono ${zjlHb == null ? '' : zjlHb > 0 ? 'text-stock-up' : zjlHb < 0 ? 'text-stock-down' : ''}`}>
                    {zjlHb != null ? toAmount(zjlHb * 1e4) : '--'}
                  </div>
                </div>
                <div>
                  <div className="text-muted-foreground text-[11px]" title="主买净额(万元)= 主动买入 - 主动卖出; 正为多方占优">主买净额</div>
                  <div className={`font-mono ${zjl == null ? '' : zjl > 0 ? 'text-stock-up' : zjl < 0 ? 'text-stock-down' : ''}`}>
                    {zjl != null ? toAmount(zjl * 1e4) : '--'}
                  </div>
                </div>
                <div>
                  <div className="text-muted-foreground text-[11px]" title="当日累计总买量 / 总卖量(手), TQ 明盘口径">总买/总卖量</div>
                  <div className="font-mono">{num(mi.total_buy_vol)} / {num(mi.total_sell_vol)}</div>
                </div>
                <div>
                  <div className="text-muted-foreground text-[11px]" title="当日累计撤买 / 撤卖量, 撤单量大代表挂单意愿不稳">撤买/撤卖量</div>
                  <div className="font-mono">{num(mi.cancel_buy)} / {num(mi.cancel_sell)}</div>
                </div>
                <div>
                  <div className="text-muted-foreground text-[11px]" title="L2 逐笔成交笔数 / 委托笔数">逐笔成交/委托笔数</div>
                  <div className="font-mono">{num(mi.l2_tick_num)} / {num(mi.l2_order_num)}</div>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* === 右栏: OB演变事件 === */}
        <div className="col-span-12 lg:col-span-5 space-y-3 border-l border-border/40 pl-3 text-[12px]">
          <div className="border-b border-border/40 pb-2">
            <div className="text-[11px] text-muted-foreground">
              盘口演变事件
              {ob?.ghost_ratio != null && (
                <span className="ml-1 font-mono text-[10px] opacity-70" title="幽灵单占比: 由撤单率异常估算的虚拟挂单比例, 越高越可能是假挂单">幽灵单 {(ob.ghost_ratio * 100).toFixed(1)}%</span>
              )}
            </div>
            {!ob?.available ? (
              <div className="mt-1 text-muted-foreground">{ob?.note ?? '无事件（盘口不可用）'}</div>
            ) : (ob.events ?? []).length === 0 ? (
              <div className="mt-1 text-muted-foreground">暂无托单/压单/撤单事件</div>
            ) : (
              <ul className="mt-1 space-y-0.5">
                {(ob.events ?? []).slice(0, 10).map((e, i) => (
                  <li key={i} className="flex items-center gap-1.5">
                    <span className="text-[10px] text-muted-foreground font-mono w-16 shrink-0">
                      {typeof e === 'string' ? '' : (e.dt ?? '').slice(0, 10)}
                    </span>
                    <span className="text-foreground truncate flex-1">
                      {typeof e === 'string' ? e : `${e.label ?? e.type ?? ''}${e.detail ? ` · ${e.detail}` : ''}`}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {ob?.note && ob.available && (
              <div className="mt-1 text-[10px] text-muted-foreground/70">{ob.note}</div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
