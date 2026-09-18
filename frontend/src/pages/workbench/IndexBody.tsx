import { useCallback, useEffect, useRef, useState } from 'react'
import { TrendingUp, BarChart3, Flame, Droplets } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import KlineChart from '@panwatch/biz-ui/components/KlineChart'
import { useKlineLayer } from '@/hooks/useKlineLayer'
import SectionHeader from '@panwatch/biz-ui/components/SectionHeader'
import ErrorBanner from '@/components/ErrorBanner'
import { describeApiError } from '@/lib/api-error'
import { safeFixed, safeNum, safeNetInflow } from '@/lib/format'

/**
 * 指数正文(Task 7, 三合一 spec §1.3): 从 `pages/IndexDetail.tsx` **原样搬移**的取数与渲染。
 *
 * 归属 `src/pages/workbench/` 而非 `packages/biz-ui/.../workbench/` 的原因:
 * 正文用 `@/components/ErrorBanner`(app 层组件, 全仓仅 `src/pages/*` 引用) —— 放 biz-ui 会新增
 * **biz-ui → src/components 反向依赖**(现存为零); `@/lib/api-error` 同属 src。故整个 Body 留在
 * app 层, 包边界不变。兄弟 `BoardBody` 无 app 层依赖, 按计划落在 biz-ui/workbench。
 *
 * 与旧页的差异(仅两处, 均非业务逻辑):
 *  1. **去掉页面骨架**(返回按钮/标题/刷新): spec §1.3 末条「抽取前的页面骨架并入共享 HeaderBand,
 *     避免重复」—— 工作台带1 提供类型切换 + 刷新(刷新经 `onRefresh` 广播到本正文); **本正文
 *     自己拥有指数名称与数值**(`/market/indices/{s}`)—— 带1 对 `type !== 'stock'` **不取**
 *     `/quotes/{s}`(同代码 = 另一标的)也不渲染名称/现价, 见 `HeaderBand` 头注「同码不同标的闸门」;
 *  2. 数字格式化改走 `@/lib/format` 的 safe* 系列 (项目红线 #6 / R6 禁裸 toFixed):
 *     每处外部守卫不变, 输出字符串逐字相同。
 *  3. **新增可选 `refreshToken`(v0.6.0 遗留⑦)**: 页面级刷新改为 `refreshToken={refreshKey}`
 *     而**不是** `key={refreshKey}` —— 换 key 会卸载并重建整棵正文子树, 于是每次点刷新都重放
 *     `sida-page-enter` 入场动画(视觉"闪一下")并丢掉正文自己的内部 UI 状态。现在 token 变化只
 *     **重跑取数 effect**(见下), 组件实例与 DOM 节点都保持不动。不传该 prop 时行为与旧版一致
 *     (只在挂载/换标的时取数)。
 *
 * 真数据: `GET /market/indices/{symbol}` + `GET /market-data/market-capital-flow`(失败静默)。
 */
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
  /** C2 stale-on-error: 源故障回退旧快照时为 true */
  stale?: boolean
  stale_age_sec?: number
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

/**
 * 设计稿 v2.1 §5 接线(2026-09-18 审计断链修复): 指数正文的 K 线同样接上图层数据
 * (原先注释写"图层开关由 InteractiveKline 内部 DEFAULT_LAYERS 控制", 但数据没人传,
 * 开关开着也是空的)。指数用 GS 交叉/资金柱/事件标注同一口径, 取不到就不画。
 */
function IndexKline({ symbol, market }: { symbol: string; market: string }) {
  const layer = useKlineLayer(symbol, market)
  // P2(2026-09-18): 由 InteractiveKline 迁到 KlineChart —— props 一一对应
  // (只把 initialDays 从字符串改数字); enableMinute 保留原有的「分时」视图。
  return (
    <KlineChart
      symbol={symbol}
      market={market}
      initialInterval="1d"
      initialDays={120}
      gsSignals={layer.gsSignals}
      fundFlow={layer.fundFlow}
      events={layer.events}
      supportPressure={layer.supportPressure}
      enableMinute
    />
  )
}

function AmountChart({ trend }: { trend: { date: string; amount: number }[] }) {
  // B5 三态审计修复(2026-09-10): 空数据显式给文案 —— 标题下静默消失会被误读为渲染失败
  if (trend.length === 0) {
    return <div className="py-6 text-center text-[12px] text-muted-foreground">暂无成交额趋势数据</div>
  }
  const maxA = Math.max(...trend.map(t => t.amount))
  const W = 720, H = 90
  const bw = W / trend.length
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full text-muted-foreground" style={{ maxHeight: 100 }}>
      {trend.map((t, i) => {
        const h = (t.amount / maxA) * (H - 16)
        return (
          <g key={t.date}>
            <rect x={i * bw + bw * 0.2} y={H - 8 - h} width={bw * 0.6} height={h} fill="var(--primary)" opacity={0.7} rx={1} />
            {i % 5 === 0 && (
              <text x={i * bw + bw / 2} y={H - 2} fontSize={10} fill="currentColor" textAnchor="middle">
                {t.date.slice(5)}
              </text>
            )}
          </g>
        )
      })}
    </svg>
  )
}

export default function IndexBody({ symbol, refreshToken }: { symbol: string; refreshToken?: number }) {
  const [data, setData] = useState<IndexDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // 大盘资金流(同花顺源, 东财502替代)
  const [marketFlow, setMarketFlow] = useState<MarketFlow | null>(null)
  /** `load()` 竞态守卫的取号器(见 `load` 内注): 只认最新一次取数的结果。 */
  const seqRef = useRef(0)

  const load = useCallback(async () => {
    // 竞态守卫(遗留⑦ 复审 Finding 2): 改用 `refreshToken` 后本组件实例**常驻**(旧做法靠
    // `key` 重挂载把在飞请求连同实例一起丢弃), 连点刷新 ⇒ 多个 `load()` 同时在飞。
    // 本组件渲染顺序是 loading → **error → data**, 即 error 优先: 一次**过期**的失败足以把
    // 已到手的好数据整块换成错误横幅。故 await 之后只认最新号(形态同 `L2Tab.tsx` 的 seqRef)。
    const seq = ++seqRef.current
    setLoading(true)
    setError('')
    try {
      // `cacheMode: 'reload'`(生产走查实测缺陷): `fetchAPI` 默认有 **30s GET 缓存**
      // (`packages/api/src/client.ts:67` `_CACHE_TTL_DEFAULT`), 不传 reload 时点带1「刷新」
      // 虽然 `load()` 确实重跑了(遗留⑦ 的 token 生效), 但拿到的是**缓存响应** ⇒ 30s 内刷新
      // 等于什么都没发生。兄弟组件 `BoardBody` 的三条取数早就传了 `reload`, 两个分支不该不一致。
      // 实测证据: TTL 内点击 → 网络计数 0; 过 TTL 后点击 → 计数 1(同一按钮、同一实例)。
      const d = await fetchAPI<IndexDetail>(`/market/indices/${symbol}`, { cacheMode: 'reload' })
      if (seq !== seqRef.current) return
      if (d?.error) setError(d.error)
      else setData(d)
    } catch (e: any) {
      if (seq !== seqRef.current) return
      // 2026-08-17: 错误分类 (B 报告 P1-9) — TIMEOUT / HTTP_5xx / NETWORK 分别给文案
      setError(describeApiError(e))
    } finally {
      // 过期号不清 loading(交给最新那次), 否则新请求还在飞就提前显示"加载完成"
      if (seq === seqRef.current) setLoading(false)
    }
    // 大盘资金流(独立加载, 失败静默) —— 过期号不许写 state; 同样要 reload(否则刷新拿缓存)
    fetchAPI<MarketFlow>('/market-data/market-capital-flow', { cacheMode: 'reload' })
      .then((m) => { if (seq === seqRef.current) setMarketFlow(m) })
      .catch(() => {})
  }, [symbol])

  // 遗留⑦: `refreshToken` 变化 = 页面级刷新(带1 的刷新按钮)⇒ **只重跑取数**, 不重挂载组件。
  // 旧做法是页面给正文子树挂 `key={refreshKey}`: 换 key 会卸载并重建整棵子树 ⇒ 重放
  // `sida-page-enter` 入场动画(视觉上"闪一下")并丢掉正文自己的内部 UI 状态。
  useEffect(() => { load() }, [load, refreshToken])

  const q = data?.quote
  const up = (q?.change_pct || 0) >= 0

  return (
    <div className="sida-page-enter space-y-4">
      {loading ? (
        <div className="text-center text-muted-foreground py-12">加载中...</div>
      ) : error ? (
        <ErrorBanner
          errors={[{ source: '指数详情', message: error, retry: () => void load() }]}
          onDismiss={() => setError('')}
        />
      ) : data ? (
        <>
          {/* 实时行情条(去卡片: hairline 下分隔) */}
          <div className="border-b border-border/40 pb-3">
            <div className="flex items-end gap-4 flex-wrap">
              <div>
                <div className="text-[20px] font-num font-bold tabular-nums">{safeFixed(q?.current_price)}</div>
                <div className={`text-[13px] font-num tabular-nums ${up ? 'text-stock-up' : 'text-stock-down'}`}>
                  {safeNum(q?.change_amount) !== null && Number(q?.change_amount) > 0 ? '+' : ''}{safeFixed(q?.change_amount)} ({safeFixed(q?.change_pct)}%)
                </div>
              </div>
              <div className="flex gap-6 text-[13px] text-muted-foreground">
                <div><span className="block text-[10px]">昨收</span><span className="font-mono text-foreground tabular-nums">{safeFixed(q?.prev_close)}</span></div>
                {/* 走查 2026-09-18: 腾讯指数 quote 对 open/high/low 常返 null —— 用**当日**最后一根日K回填, 不编造 */}
                {(() => {
                  const last = data.klines[data.klines.length - 1]
                  const today = new Date().toISOString().slice(0, 10)
                  const sameDay = last && last.date === today
                  const open = q?.open ?? (sameDay ? last.open : null)
                  const high = q?.high ?? (sameDay ? last.high : null)
                  const low = q?.low ?? (sameDay ? last.low : null)
                  return (
                    <>
                      <div><span className="block text-[10px]">今开</span><span className="font-mono text-foreground tabular-nums">{safeFixed(open)}</span></div>
                      <div><span className="block text-[10px]">最高</span><span className="font-mono text-foreground tabular-nums">{safeFixed(high)}</span></div>
                      <div><span className="block text-[10px]">最低</span><span className="font-mono text-foreground tabular-nums">{safeFixed(low)}</span></div>
                    </>
                  )
                })()}
                <div><span className="block text-[10px]">成交量</span><span className="font-mono text-foreground tabular-nums">{q?.volume != null && Number.isFinite(Number(q.volume)) ? safeFixed(Number(q.volume) / 1e8) + '亿股' : '--'}</span></div>
              </div>
            </div>
          </div>

          {/* K线走势(主图裸放, 无卡片包) */}
          <div className="space-y-1">
            {/* P2-15: 收敛 SectionHeader(v0.5.2 收尾) */}
            <SectionHeader
              title="K线走势"
              action={<span className="text-[10px] text-muted-foreground">MA/成交量/MACD/RSI · 日K/周K/月K 切换</span>}
            />
            <IndexKline symbol={symbol || ''} market={data.market || 'CN'} />
          </div>

          {/* 大盘资金流(东财两市主力净流入, 对齐同花顺APP) */}
          {marketFlow && (
            <div className="border-b border-border/40 pb-2 mb-3">
              <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
                <div className="flex items-center gap-2">
                  <TrendingUp className="h-4 w-4 text-primary" />
                  <span className="text-[12px] font-semibold">大盘资金流</span>
                  <span className="text-[10px] text-muted-foreground">东财 · 两市主力</span>
                  {/* C2: 源故障回退旧快照的显式标注 */}
                  {marketFlow.stale && (
                    <span className="text-[10px] text-amber-600" title="数据源暂不可用, 当前展示最后一次成功快照(非实时)">
                      · 数据滞后{marketFlow.stale_age_sec != null ? ` ${Math.max(1, Math.round(marketFlow.stale_age_sec / 60))} 分钟` : ''}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-4 text-[12px]">
                  <span className="text-muted-foreground">主力净流入
                    <b className={`font-mono ${(marketFlow.total_main_flow ?? 0) >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                      {safeNetInflow(marketFlow.total_main_flow)}
                    </b>
                  </span>
                  <span className="text-muted-foreground">成交额 <b className="font-mono">{safeNum(marketFlow.total_amount) !== null ? `${safeFixed(marketFlow.total_amount, 0)}亿` : '--'}</b></span>
                  <span className="text-muted-foreground">涨 <b className="text-stock-up font-mono">{marketFlow.up_count ?? '--'}</b>
                    <span className="mx-1">/</span>跌 <b className="text-stock-down font-mono">{marketFlow.down_count ?? '--'}</b></span>
                  <span className="text-muted-foreground">沪 <b className="font-mono">{safeNum(marketFlow.sh_flow) !== null ? `${safeFixed(marketFlow.sh_flow, 1)}亿` : '--'}</b>
                    <span className="mx-1">/</span>深 <b className="font-mono">{safeNum(marketFlow.sz_flow) !== null ? `${safeFixed(marketFlow.sz_flow, 1)}亿` : '--'}</b></span>
                </div>
              </div>

              {/* 板块资金明细: 流入榜 / 流出榜 */}
              {(marketFlow.inflow_boards?.length || marketFlow.outflow_boards?.length) ? (
                <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-3">
                  {marketFlow.inflow_boards?.length ? (
                    <div className="border-t border-border/40 pt-2">
                      <div className="text-[11px] font-semibold text-stock-up mb-1 flex items-center gap-1"><Flame className="w-3 h-3" />资金流入板块</div>
                      <div className="space-y-0.5">
                        {marketFlow.inflow_boards.map(b => (
                          <div key={b.name} className="flex justify-between text-[11px]">
                            <span className="text-muted-foreground truncate">{b.name}</span>
                            <span className="font-mono text-stock-up">{safeNum(b.net_inflow) !== null ? `+${safeFixed(b.net_inflow, 1)}亿` : '--'}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}
                  {marketFlow.outflow_boards?.length ? (
                    <div className="border-t border-border/40 pt-2">
                      <div className="text-[11px] font-semibold text-stock-down mb-1 flex items-center gap-1"><Droplets className="w-3 h-3" />资金流出板块</div>
                      <div className="space-y-0.5">
                        {marketFlow.outflow_boards.map(b => (
                          <div key={b.name} className="flex justify-between text-[11px]">
                            <span className="text-muted-foreground truncate">{b.name}</span>
                            <span className="font-mono text-stock-down">{safeNum(b.net_inflow) !== null ? `${safeFixed(b.net_inflow, 1)}亿` : '--'}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          )}

          {/* 成交额趋势 */}
          <div className="space-y-1">
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
