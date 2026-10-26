import CaliberBadge from '@panwatch/biz-ui/components/CaliberBadge'
import { safeFixed, safeNum, toAmount } from '@/lib/format'

/**
 * 四档资金结构（exday_daily · tdx_exday4 口径）—— 纯展示组件, 不取数、不重算口径。
 *
 * 为什么单独一个文件: L2Tab 是取数容器（轮询 + stale-on-error）, 而这块所有数字都由
 * 后端 `/api/market/fund-structure/{symbol}` 从 exday_daily 表原样下发（specs/api/fund_structure.md）。
 * 前端**只渲染**, 不本地补算 —— 四档语义(单笔成交金额分档)一旦被前端"顺手"换算就会跨源混算。
 *
 * 数据稀疏契约(2026-10-23 起才落库累积, 历史无法回填):
 *  - 头部恒显「数据自 <data_since> 起累积 · N 个交易日」—— 不许把 2 天画成长期趋势;
 *  - `count==1` 只展示当日结构, **不画趋势序列**(显式说明行);
 *  - `count==0` / `available=false` 显式「无数据」+ 后端原因原文, 绝不补 0;
 *  - 行内缺值 `--`(safeNum), 柱带缺值画 2px 灰点(与 CancelProfilePanel 同规则, 0 是有效读数不与缺值混用)。
 *
 * 口径标签: tdx_exday4 与逐笔(tick)/东财四档(eastmoney4)不可换算 —— 本组件自带徽章+
 * 完整 tooltip; 同屏的 L2 成品资金/主力意图是别的口径, 各带各的标签(L2Tab 已有), 互不混算。
 *
 * 视觉铁律: 去卡片化(hairline 分节)、字号 10/11/12/13、数字 font-num(tabular-nums)、
 * 买红卖绿(text-stock-up/text-stock-down)。
 */

export interface FundStructureDay {
  id?: number | null
  trade_date?: string | null
  symbol?: string | null
  market?: string | null
  amount?: number | null
  volume_lots?: number | null
  active_buy_amt?: number | null
  active_sell_amt?: number | null
  active_ratio?: number | null
  xl_buy_amt?: number | null
  lg_buy_amt?: number | null
  md_buy_amt?: number | null
  sm_buy_amt?: number | null
  xl_sell_amt?: number | null
  lg_sell_amt?: number | null
  md_sell_amt?: number | null
  sm_sell_amt?: number | null
  xl_active_net?: number | null
  lg_active_net?: number | null
  md_active_net?: number | null
  sm_active_net?: number | null
  retail_ratio?: number | null
  xl_share?: number | null
  main_active_net?: number | null
  main_active_net_pct?: number | null
  chip_migration?: number | null
  gran_hiding?: number | null
  order_fill_ratio?: number | null
  avg_order_lots?: number | null
  avg_trade_lots?: number | null
  cancel_buy_rate?: number | null
  cancel_sell_rate?: number | null
  cancel_skew?: number | null
  quality?: string | null
  reason?: string | null
  source?: string | null
  created_at?: string | null
}

export interface FundStructureResp {
  available: boolean
  reason?: string | null
  symbol?: string
  market?: string
  caliber?: string
  direction_semantics?: string | null
  source?: string | null
  tier_thresholds_hint?: string | null
  data_since?: string | null
  count?: number
  items?: FundStructureDay[]
}

/** 四档展示序(特大→小)与标签; 键与表列前缀一致。 */
const TIERS = [
  { key: 'xl', label: '特大单' },
  { key: 'lg', label: '大单' },
  { key: 'md', label: '中单' },
  { key: 'sm', label: '小单' },
] as const

/** YYYYMMDD → 2026-10-23; 非该格式原样返回(不猜)。 */
export function fmtDay(d: unknown): string {
  const s = String(d ?? '')
  return /^\d{8}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s
}

/** 规模量(元, 不带符号): 买/卖额是"多少"不是"变化", 带 '+' 会被误读成净值。 */
function amtU(v: unknown): string {
  const n = safeNum(v)
  if (n == null) return '--'
  const abs = Math.abs(n)
  if (abs >= 1e8) return `${safeFixed(abs / 1e8, 2)}亿`
  if (abs >= 1e4) return `${safeFixed(abs / 1e4, 2)}万`
  return `${safeFixed(abs, 2)}`
}

/** 净额(元, 带符号): 正=净流入(红), 负=净流出(绿) —— toAmount 自带符号。 */
function amtNet(v: unknown): string {
  const n = safeNum(v)
  return n == null ? '--' : toAmount(n)
}

/** 已是百分数的读数(如 50.4 表示 50.4%); 缺值 '--'。 */
function pctRaw(v: unknown, digits = 1): string {
  const n = safeNum(v)
  return n == null ? '--' : `${safeFixed(n, digits)}%`
}

/** 净额方向色: 正=红(流入/买), 负=绿(流出/卖), 0/缺值中性。 */
function dirClass(v: unknown): string {
  const n = safeNum(v)
  if (n == null || n === 0) return 'text-foreground'
  return n > 0 ? 'text-stock-up' : 'text-stock-down'
}

/** 取数降级文案(与 L2Tab.feedNote 同语义: 失败/加载/后端原文, 不本地编理由)。 */
function noteText(loading: boolean, hasData: boolean, failed: boolean, lastOkAt: string, backendNote?: string | null): string {
  if (loading && !hasData) return '加载中…'
  if (failed) return lastOkAt ? `取数失败（上次成功 ${lastOkAt}）` : '取数失败（暂无成功记录）'
  return backendNote ?? '无数据'
}

/** 单档买卖对比条: 红段=买入额占比, 绿段=卖出额占比(同一档内买 vs 卖)。 */
function TierRow({ label, buy, sell, net }: { label: string; buy: unknown; sell: unknown; net: unknown }) {
  const b = safeNum(buy)
  const s = safeNum(sell)
  const total = b != null && s != null ? b + s : null
  const buyW = total != null && total > 0 && b != null ? (b / total) * 100 : 0
  return (
    <div className="flex items-center gap-2" data-testid="fund-structure-tier-row">
      <span className="w-11 shrink-0 text-[11px] text-muted-foreground">{label}</span>
      <div className="h-2.5 flex-1 overflow-hidden bg-accent/20" title={total != null ? `买 ${amtU(buy)} / 卖 ${amtU(sell)}` : '该档买卖额缺失'}>
        {total != null && total > 0 ? (
          <div className="flex h-2.5">
            <div className="h-2.5 bg-stock-up/80" style={{ width: `${buyW}%` }} />
            <div className="h-2.5 bg-stock-down/80" style={{ width: `${100 - buyW}%` }} />
          </div>
        ) : null}
      </div>
      <span className="w-16 shrink-0 text-right font-num text-[11px] text-stock-up" title="该档买入额(元)">{amtU(buy)}</span>
      <span className="w-16 shrink-0 text-right font-num text-[11px] text-stock-down" title="该档卖出额(元)">{amtU(sell)}</span>
      <span className={`w-16 shrink-0 text-right font-num text-[11px] ${dirClass(net)}`} title="该档主动净额(主动买−主动卖, 元)">{amtNet(net)}</span>
    </div>
  )
}

/** 主动买占比(%·0~100)日序列柱带: 缺值 2px 灰点(不画成 0)。 */
function RatioBand({ items }: { items: FundStructureDay[] }) {
  return (
    <div className="flex h-9 items-end gap-px" data-testid="fund-structure-band-active-ratio">
      {items.map((d, idx) => {
        const v = safeNum(d.active_ratio)
        const day = fmtDay(d.trade_date)
        return (
          <div key={d.trade_date ?? idx} className="flex h-full flex-1 items-end" title={v == null ? `${day} · 主动买占比缺值（不按 0 计）` : `${day} · 主动买占比 ${pctRaw(v)}`}>
            {v == null ? (
              <div className="h-0.5 w-full bg-border" />
            ) : (
              <div className="w-full bg-stock-up/60" style={{ height: `${Math.max(2, Math.min(100, v))}%` }} />
            )}
          </div>
        )
      })}
    </div>
  )
}

/** 主力(特大+大)主动净流入占比(%)日序列: 中线为 0, 上红下绿, 高度按窗口最大 |v| 归一。 */
function NetBand({ items }: { items: FundStructureDay[] }) {
  const maxAbs = Math.max(
    0.01,
    ...items.map((d) => Math.abs(safeNum(d.main_active_net_pct) ?? 0)),
  )
  return (
    <div className="relative h-9" data-testid="fund-structure-band-main-net">
      <div className="absolute inset-x-0 top-1/2 h-px bg-border/60" />
      <div className="flex h-full items-stretch gap-px">
        {items.map((d, idx) => {
          const v = safeNum(d.main_active_net_pct)
          const day = fmtDay(d.trade_date)
          const h = v == null ? null : Math.min(50, (Math.abs(v) / maxAbs) * 50)
          return (
            <div key={d.trade_date ?? idx} className="relative flex-1" title={v == null ? `${day} · 主力净流入占比缺值（不按 0 计）` : `${day} · 主力(特大+大)主动净流入占比 ${pctRaw(v, 3)}`}>
              {h == null || v == null ? (
                <div className="absolute inset-x-0 top-1/2 h-0.5 -translate-y-1/2 bg-border" />
              ) : v >= 0 ? (
                <div className="absolute inset-x-0 bottom-1/2 bg-stock-up/70" style={{ height: `${h}%` }} />
              ) : (
                <div className="absolute inset-x-0 top-1/2 bg-stock-down/70" style={{ height: `${h}%` }} />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default function FundStructurePanel({
  data,
  loading,
  failed,
  lastOkAt,
}: {
  data: FundStructureResp | null
  loading: boolean
  failed: boolean
  lastOkAt: string
}) {
  const items = data?.items ?? []
  const count = safeNum(data?.count) ?? items.length
  const hasResp = data != null && data.available === true
  const latest = count > 0 ? items[count - 1] : null
  const quality = latest?.quality ?? null

  if (!hasResp || count === 0 || !latest) {
    // 无数据显式态: 后端 reason 原文优先(不本地编理由), 累积起点照标 —— 该股没落库 ≠ 表没数据
    return (
      <div className="text-[12px] text-muted-foreground" data-testid="fund-structure-no-data">
        {noteText(loading, hasResp, failed, lastOkAt, data?.available === false ? data?.reason : '该股暂无落库记录')}
        <div className="mt-0.5 text-[10px] text-muted-foreground">
          {data?.data_since ? `数据自 ${fmtDay(data.data_since)} 起累积` : '数据自 2026-10-23 起累积'}（每交易日 15:45 落库）
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-2" data-testid="fund-structure-panel">
      {/* 口径行: 徽章 + 阈值提示 + 稀疏标注(spec EXDAY-003/005) */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-muted-foreground">
        {/* 2026-10-24 合并去重: 上游 CaliberBadge 已支持 tdx_exday4(main 的 08e39c1),
            不再自实现第二套徽章 —— 否则同一口径在全站会有两种样式。
            data-testid 留在外层, 既有用例继续有效。 */}
        <span data-testid="fund-structure-caliber">
          <CaliberBadge
            caliber="tdx_exday4"
            className="text-[10px]"
            title={[
            `口径 ${data?.caliber ?? 'tdx_exday4'}(通达信 TQ get_exday_data): ${data?.direction_semantics ?? '按单笔成交金额分四档, 与逐笔/东财四档不可换算'}`,
            data?.tier_thresholds_hint ? `档位阈值(推断): ${data.tier_thresholds_hint}` : '',
            ].filter(Boolean).join('\n')}
          />
        </span>
        <span>按单笔成交金额分档 · 与逐笔/东财四档不可换算</span>
        <span data-testid="fund-structure-since" className="text-foreground">
          数据自 {fmtDay(data?.data_since)} 起累积 · {count} 个交易日
        </span>
      </div>
      {quality && quality !== 'ok' ? (
        <div className="text-[10px] text-amber-600 dark:text-amber-400" data-testid="fund-structure-quality">
          ⚠ 数据待核：{latest.reason || quality}
        </div>
      ) : null}

      {/* 当日结构: 四档买卖对比(买红卖绿) */}
      <div className="space-y-1 pt-0.5" data-testid="fund-structure-tiers">
        <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
          <span className="w-11 shrink-0">{fmtDay(latest.trade_date)}</span>
          <span className="flex-1" />
          <span className="w-16 shrink-0 text-right text-stock-up">买入</span>
          <span className="w-16 shrink-0 text-right text-stock-down">卖出</span>
          <span className="w-16 shrink-0 text-right">主动净</span>
        </div>
        {TIERS.map((t) => (
          <TierRow
            key={t.key}
            label={t.label}
            buy={latest[`${t.key}_buy_amt` as keyof FundStructureDay]}
            sell={latest[`${t.key}_sell_amt` as keyof FundStructureDay]}
            net={latest[`${t.key}_active_net` as keyof FundStructureDay]}
          />
        ))}
      </div>

      {/* 当日主动买卖 + 结构因子 */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 border-t border-border/30 pt-1.5 text-[11px] md:grid-cols-4">
        <div>
          <div className="text-[10px] text-muted-foreground">主动买入</div>
          <div className="font-num text-stock-up">{amtU(latest.active_buy_amt)}</div>
        </div>
        <div>
          <div className="text-[10px] text-muted-foreground">主动卖出</div>
          <div className="font-num text-stock-down">{amtU(latest.active_sell_amt)}</div>
        </div>
        <div>
          <div className="text-[10px] text-muted-foreground">主动买占比</div>
          <div className="font-num">{pctRaw(latest.active_ratio)}</div>
        </div>
        <div>
          <div className="text-[10px] text-muted-foreground">主力净流入占比</div>
          <div className={`font-num ${dirClass(latest.main_active_net_pct)}`} title="主力(特大+大)主动净额 / 成交额(%)">
            {pctRaw(latest.main_active_net_pct, 3)}
          </div>
        </div>
        <div>
          <div className="text-[10px] text-muted-foreground">散户化率</div>
          <div className="font-num" title="小单买入额占成交额(%)">{pctRaw(latest.retail_ratio)}</div>
        </div>
        <div>
          <div className="text-[10px] text-muted-foreground">特大单占比</div>
          <div className="font-num">{pctRaw(latest.xl_share)}</div>
        </div>
        <div>
          <div className="text-[10px] text-muted-foreground">筹码迁移</div>
          <div className={`font-num ${dirClass(latest.chip_migration)}`} title="大单主动净 − 小单主动净(元); >0 筹码向大资金集中">
            {amtNet(latest.chip_migration)}
          </div>
        </div>
        <div>
          <div className="text-[10px] text-muted-foreground">当日成交额</div>
          <div className="font-num">{amtU(latest.amount)}</div>
        </div>
      </div>

      {/* 日期序列: ≥2 个交易日才画(spec EXDAY-005); 单日显式说明不画 */}
      {count >= 2 ? (
        <div className="space-y-2 border-t border-border/30 pt-1.5" data-testid="fund-structure-series">
          <div>
            <div className="mb-0.5 text-[10px] text-muted-foreground">主动买占比 · 近 {count} 个交易日(%)</div>
            <RatioBand items={items} />
          </div>
          <div>
            <div className="mb-0.5 text-[10px] text-muted-foreground">主力(特大+大)主动净流入占比 · 红=净流入 绿=净流出(%)</div>
            <NetBand items={items} />
          </div>
        </div>
      ) : (
        <div className="border-t border-border/30 pt-1.5 text-[10px] text-muted-foreground" data-testid="fund-structure-single-day">
          仅 {count} 个交易日 —— 展示当日结构, 不画趋势（本表 2026-10-23 起累积, 历史无法回填）
        </div>
      )}
    </div>
  )
}
