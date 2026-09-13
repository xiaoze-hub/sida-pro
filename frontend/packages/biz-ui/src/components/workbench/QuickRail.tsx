import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { fmtAmount } from '@panwatch/biz-ui/lib/ladder-format'
import { safeFixed, safeNum, safePrice } from '@/lib/format'
import DecisionCard from './DecisionCard'

/**
 * 右栏速览卡容器(工作台 v2 三合一, spec §4.2 / 布局图 §1.2 右栏 320px)。
 *
 * 竖排四张「一眼看过」的卡(顺序即 spec §1.2 的 ①②③④):
 *   ① 数智决策   —— Task 4 的合并卡(三指标读数 + 共振判定, 全工作台**只此一处**);
 *   ② 盘口速览   —— `GET /stocks/{s}/l2`(通达信 snapshot + more_info), **30s 轮询**;
 *   ③ 基本面/股本 —— `GET /stocks/{s}/fundamental`(股本/次新) + `GET /stocks/{s}/l2` 的 `more.pe_ttm/pb`;
 *   ④ 题材/板块   —— `GET /stocks/{s}/blocks` chips。
 *
 * 由 `StockWorkbench.tsx` 内联的 `L2Card/FundamentalCard/BlocksCard` **搬迁精简**而来
 * (逐段挪, 不重写取数逻辑; 轮到 Task 6 重写该页时替换成引用本组件)。
 *
 * 宽度: 本组件**不设宽**, 根节点只有 `flex flex-col gap-2` —— 320px 由页面外壳
 * (`w-[320px] shrink-0`)决定, 便于别处复用(指数/板块页不渲染本卡, 见 spec §1.3)。
 *
 * 真数据纪律: 任一字段缺失/脏值(PG DECIMAL 字符串、空串、NaN)→ `--`, 绝不编造、绝不渲染 NaN;
 * 请求失败 **保留旧值**(stale-on-error), 不把失败伪装成 0。
 * 本文件零裸 toFixed 调用(R6), 格式化全走 `@/lib/format` safe* 与 `fmtAmount`; 配色只用设计令牌。
 */

/** `/stocks/{symbol}/l2` 的 `snapshot` 段(src/core/stock_l2.py::fetch_snapshot)。 */
interface L2Snapshot {
  /** 现价 — Now */
  now?: number | null
  /** 成交额(元) — Amount */
  amount?: number | null
  /** 五档买价 — Buyp */
  buyp?: number[]
  /** 五档买量 — Buyv */
  buyv?: number[]
  /** 五档卖价 — Sellp */
  sellp?: number[]
  /** 五档卖量 — Sellv */
  sellv?: number[]
}

/** `/stocks/{symbol}/l2` 的 `more` 段(src/core/stock_l2.py::fetch_more)。 */
interface L2More {
  /** 涨停价(元) — ZTPrice */
  zt_price?: number | null
  /** 封单额(元, 后端已把 FCAmo 从万元换算成元) — FCAmo */
  fcamo?: number | null
  /** 主力净流入(元) — Zjl_HB */
  zjl_hb?: number | null
  /** PE(TTM) — StaticPE_TTM */
  pe_ttm?: number | null
  /** PB(市净率) — PB_MRQ */
  pb?: number | null
}

interface L2Resp {
  symbol?: string
  /** 本次取数时刻(后端 datetime.now(Asia/Shanghai) ISO 串, 源不可用为 null) */
  as_of?: string | null
  /** 源不可用时的降级说明(不编数据) */
  note?: string | null
  snapshot?: L2Snapshot | null
  more?: L2More | null
}

/** `GET /stocks/{symbol}/fundamental` 响应(src/web/api/stocks.py::get_stock_fundamental)。 */
interface FundamentalResp {
  /** 最近一条股本 {date, ltgb(流通股本, 股), zgb(总股本, 股)} */
  gb?: { date?: string; ltgb?: number | null; zgb?: number | null } | null
  listing?: { name?: string; listing_date?: string } | null
  /** 上市 < 365 天 = 次新; null = 判不出(不猜) */
  sub_new?: boolean | null
  note?: string | null
}

interface BlocksResp {
  blocks: { code: string; name: string; type: string }[]
  note?: string | null
}

/** 盘口速览轮询间隔(与旧 `L2Card` 一致)。 */
const L2_POLL_MS = 30000

/**
 * CN-only 数据面闸门: 本文件三个接口(`/stocks/{s}/l2`、`/fundamental`、`/blocks`)全是
 * 通达信 CN 源(非 CN 标的取不到, 硬发只会把 CN 口径画到别的标的上)—— 仅 `market === 'CN'` 才发。
 * 与 `HeaderBand.tsx::cnStockDataEnabled` 同一条纪律(那边因还要判 type 故未直接复用)。
 */
function cnDataEnabled(market: string): boolean {
  return market === 'CN'
}

/** 卡片共用行(左标签 / 右等宽数值)。 */
function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between text-[11px]">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono">{value}</span>
    </div>
  )
}

/**
 * 带符号金额(元 → 万/亿): 封单/主力净额**可为负**(FCAmo < 0 = 跌停封单, Zjl_HB < 0 = 净流出),
 * 而共享的 `fmtAmount` 只对正值分万/亿档(负值会原样吐出 `-18000000`)—— 本卡是"一眼看数"处,
 * 这里取绝对值走 `fmtAmount` 再补 `-`, 正数与旧卡逐字一致, 缺值仍 `--`(不编)。
 */
function fmtSignedAmount(v: number | null | undefined): string {
  const n = safeNum(v)
  if (n == null) return '--'
  return `${n < 0 ? '-' : ''}${fmtAmount(Math.abs(n))}`
}

/** 五档价/量单元: 缺失或 0 → `--`(通达信没给该档 = 无值, 不画 0)。 */
function lvl(v: unknown): string {
  const n = safeNum(v)
  return n != null && n > 0 ? String(n) : '--'
}

/** `as_of` ISO 串 → `HH:MM:SS`; 缺值/格式不符 → 原样截断不猜。 */
function asOfClock(iso?: string | null): string | null {
  if (!iso || iso.length < 19) return null
  const clock = iso.slice(11, 19)
  return /^\d{2}:\d{2}:\d{2}$/.test(clock) ? clock : null
}

/** 股本(股) → 亿/万紧凑; 缺值 `--`(R6: 走 safeFixed, 不手写裸 toFixed)。 */
function fmtShares(v: unknown): string {
  const n = safeNum(v)
  if (n == null) return '--'
  return Math.abs(n) >= 1e8 ? `${safeFixed(n / 1e8, 2)}亿` : `${safeFixed(n / 1e4, 2)}万`
}

/**
 * ② 盘口速览: **封单 / 主力净额 + 五档买卖价量**, 30s 轮询(失败保留旧值)。
 *
 * **去重(控制器裁定)**: 「现价」(`snapshot.now`)与「涨停价」(`more.zt_price`)归**带1
 * `HeaderBand`**(顶行现价 / `band1.snapshot` 的 `limit_price`, spec 去重表 #9), 本卡
 * **不渲染**这两个数据点 —— 同一数据点全工作台只出现一次; 五档买卖价即本卡的价格上下文。
 * 「主力净额」保留: 去重表 #4 明确允许右栏留一条主力净额速览摘要。
 * `L2Snapshot.now` / `L2More.zt_price` 的声明**保留**(本文件对 wire 形态的说明, 与同样
 * 未渲染的 `amount` 同例); 日后若要在此加价格行, 先回去重表重新裁定, 不要直接加。
 */
function QuoteCard({ symbol, market }: { symbol: string; market: string }) {
  const cn = cnDataEnabled(market)
  const [l2, setL2] = useState<L2Resp | null>(null)

  // 换股/换市场先清旧值(否则会把上一只票的盘口画到新标的上); 轮询失败不清, 以保 stale-on-error。
  useEffect(() => {
    setL2(null)
  }, [symbol, market])

  useEffect(() => {
    if (!symbol || !cn) return
    let alive = true
    const load = async () => {
      try {
        const r = await fetchAPI<L2Resp>(`/stocks/${encodeURIComponent(symbol)}/l2`)
        if (alive) setL2(r ?? null)
      } catch {
        /* 保留旧值(stale-on-error) */
      }
    }
    void load()
    const t = window.setInterval(() => void load(), L2_POLL_MS)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [symbol, cn])

  const s = l2?.snapshot ?? {}
  const m = l2?.more ?? {}
  const clock = asOfClock(l2?.as_of)
  return (
    <div className="rounded border border-border/60 p-2">
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-[12px] font-semibold">盘口速览</span>
        {clock ? <span className="font-mono text-[9px] text-muted-foreground">快照 {clock}</span> : null}
      </div>
      {l2?.note ? <div className="mb-1 text-[10px] text-muted-foreground">{l2.note}</div> : null}
      {/* 去重: 现价/涨停价 只在带1 HeaderBand, 此处不渲染(见本组件头注) */}
      <Row label="封单" value={fmtSignedAmount(m.fcamo)} />
      <Row label="主力净额" value={fmtSignedAmount(m.zjl_hb)} />
      <div className="mt-1 grid grid-cols-5 gap-0.5 text-[9px]">
        {(s.buyp ?? []).slice(0, 5).map((p, i) => (
          <div key={`b${i}`} className="truncate text-center text-[--stock-up]">
            {lvl(p)}
          </div>
        ))}
      </div>
      <div className="grid grid-cols-5 gap-0.5 text-[9px]">
        {(s.buyv ?? []).slice(0, 5).map((v, i) => (
          <div key={`bv${i}`} className="truncate text-center text-muted-foreground">
            {lvl(v)}
          </div>
        ))}
      </div>
      <div className="mt-0.5 grid grid-cols-5 gap-0.5 text-[9px]">
        {(s.sellp ?? []).slice(0, 5).map((p, i) => (
          <div key={`s${i}`} className="truncate text-center text-[--stock-down]">
            {lvl(p)}
          </div>
        ))}
      </div>
      <div className="grid grid-cols-5 gap-0.5 text-[9px]">
        {(s.sellv ?? []).slice(0, 5).map((v, i) => (
          <div key={`sv${i}`} className="truncate text-center text-muted-foreground">
            {lvl(v)}
          </div>
        ))}
      </div>
    </div>
  )
}

/** ③ 基本面/股本(精简 3 行: PE(TTM) / PB / 股本) —— PE/PB 走 /l2 的 more, 股本走 /fundamental。 */
function FundamentalCard({ symbol, market }: { symbol: string; market: string }) {
  const cn = cnDataEnabled(market)
  const [fund, setFund] = useState<FundamentalResp | null>(null)
  const [more, setMore] = useState<L2More | null>(null)

  useEffect(() => {
    setFund(null)
    setMore(null)
  }, [symbol, market])

  useEffect(() => {
    if (!symbol || !cn) return
    let alive = true
    fetchAPI<FundamentalResp>(`/stocks/${encodeURIComponent(symbol)}/fundamental`)
      .then((r) => { if (alive) setFund(r ?? null) })
      .catch(() => { /* 保留旧值 */ })
    // PE/PB 复用 /l2 的 more(后端已含, 不新增 RPC —— 见 src/core/tdx_fundamental.py 头注)
    fetchAPI<{ more?: L2More | null }>(`/stocks/${encodeURIComponent(symbol)}/l2`)
      .then((r) => { if (alive) setMore(r?.more ?? null) })
      .catch(() => { /* 保留旧值 */ })
    return () => { alive = false }
  }, [symbol, cn])

  const gb = fund?.gb
  return (
    <div className="rounded border border-border/60 p-2">
      <div className="mb-1 flex items-center gap-1 text-[12px] font-semibold">
        基本面 / 股本
        {fund?.sub_new ? (
          <span className="rounded bg-[--stock-up]/20 px-1 text-[9px] text-[--stock-up]">次新</span>
        ) : null}
      </div>
      {fund?.note ? <div className="mb-1 text-[10px] text-muted-foreground">{fund.note}</div> : null}
      <Row label="PE(TTM)" value={safePrice(safeNum(more?.pe_ttm), 2)} />
      <Row label="PB" value={safePrice(safeNum(more?.pb), 2)} />
      {/* 股本 = 流通/总(单行容纳两个真值, 精简不丢数据); 缺值一侧 '--' */}
      <Row label="股本(流通/总)" value={`${fmtShares(gb?.ltgb)} / ${fmtShares(gb?.zgb)}`} />
    </div>
  )
}

/** ④ 题材/板块 chips。 */
function BlocksCard({ symbol, market }: { symbol: string; market: string }) {
  const cn = cnDataEnabled(market)
  const [data, setData] = useState<BlocksResp | null>(null)

  useEffect(() => {
    setData(null)
  }, [symbol, market])

  useEffect(() => {
    if (!symbol || !cn) return
    let alive = true
    fetchAPI<BlocksResp>(`/stocks/${encodeURIComponent(symbol)}/blocks`)
      .then((r) => { if (alive) setData(r ?? null) })
      .catch(() => { /* 保留旧值 */ })
    return () => { alive = false }
  }, [symbol, cn])

  const blocks = data?.blocks ?? []
  return (
    <div className="rounded border border-border/60 p-2">
      <div className="mb-1 text-[12px] font-semibold">题材 / 板块</div>
      {data?.note ? <div className="text-[10px] text-muted-foreground">{data.note}</div> : null}
      {blocks.length === 0 && !data?.note ? (
        <div className="text-[10px] text-muted-foreground">--</div>
      ) : null}
      <div className="flex flex-wrap gap-1">
        {/* key 用 code+序号: 通达信反查里「概念/指数」类关系的 code 会是 '0'(实测 002636 有 5 条),
            单用 b.code 会重复 key(React 复用错乱) —— 序号在列表整体替换(换股清空)下稳定。 */}
        {blocks.map((b, i) => (
          <span
            key={`${b.code}-${i}`}
            className="rounded bg-accent/50 px-1 py-0.5 text-[10px] text-foreground/80"
          >
            {b.name}
            <span className="ml-0.5 text-[9px] text-muted-foreground">{b.type}</span>
          </span>
        ))}
      </div>
    </div>
  )
}

/**
 * 右栏速览卡(顺序即 spec §1.2): 数智决策 → 盘口速览 → 基本面/股本 → 题材/板块。
 * 数智决策**只在这里渲染一次**, 本文件不重复任何三指标/共振读数。
 */
export default function QuickRail({ symbol, market = 'CN' }: { symbol: string; market: string }) {
  return (
    <div className="flex flex-col gap-2">
      <DecisionCard symbol={symbol} market={market} />
      <QuoteCard symbol={symbol} market={market} />
      <FundamentalCard symbol={symbol} market={market} />
      <BlocksCard symbol={symbol} market={market} />
    </div>
  )
}
