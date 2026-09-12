import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { fetchAPI } from '@panwatch/api'
import InteractiveKline from '@panwatch/biz-ui/components/InteractiveKline'
import DecisionPioneerCard from '@panwatch/biz-ui/components/DecisionPioneerCard'
import ResonanceVerdictPanel from '@panwatch/biz-ui/components/ResonanceVerdictPanel'
import { fmtAmount, fmtPct } from '@panwatch/biz-ui/lib/ladder-format'

/**
 * 个股整页工作台(v0.5.90 第①步, 老板否掉"卡片堆叠逐层点击")。
 * 布局: 主图区(大K线) + 右栏平铺卡(数智决策三指标/共振判定/盘口L2), 不嵌套不逐层点击。
 * 盘口L2 用 GET /api/stocks/{symbol}/l2(通达信 snapshot+more_info), 30s 轮询; 缺值显 '--' 不编。
 */

interface L2Snap {
  now?: number | null
  last_close?: number | null
  open?: number | null
  high?: number | null
  low?: number | null
  amount?: number | null
  before5min?: number | null
  buyp?: number[]
  buyv?: number[]
  sellp?: number[]
  sellv?: number[]
}
interface L2More {
  zt_price?: number | null
  fcamo?: number | null
  ever_zt_count?: number | null
  l2_tic?: number | null
  l2_order?: number | null
  zjl_hb?: number | null
}
interface L2Resp {
  symbol: string
  as_of?: string | null
  note?: string | null
  snapshot?: L2Snap
  more?: L2More
}

function L2Card({ symbol }: { symbol: string }) {
  const [l2, setL2] = useState<L2Resp | null>(null)
  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const r = await fetchAPI<L2Resp>(`/stocks/${encodeURIComponent(symbol)}/l2`)
        if (alive) setL2(r)
      } catch {
        /* 保留旧值 */
      }
    }
    void load()
    const t = window.setInterval(() => void load(), 30000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [symbol])
  const s = l2?.snapshot ?? {}
  const m = l2?.more ?? {}
  const row = (label: string, val: string) => (
    <div className="flex justify-between text-[11px]">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono">{val}</span>
    </div>
  )
  return (
    <div className="rounded border border-border/60 p-2">
      <div className="mb-1 text-[12px] font-semibold">盘口 / L2</div>
      {l2?.note ? (
        <div className="mb-1 text-[10px] text-muted-foreground">{l2.note}</div>
      ) : null}
      {row('现价', s.now != null ? String(s.now) : '--')}
      {row('涨停价', m.zt_price != null ? String(m.zt_price) : '--')}
      {row('封单', m.fcamo != null ? fmtAmount(m.fcamo) : '--')}
      {row('主力净流入', m.zjl_hb != null ? fmtAmount(m.zjl_hb) : '--')}
      {row('逐笔成交/委托', m.l2_tic != null && m.l2_order != null ? `${m.l2_tic}/${m.l2_order}` : '--')}
      {row('连板(vendor)', m.ever_zt_count != null ? String(m.ever_zt_count) : '--')}
      <div className="mt-1 grid grid-cols-5 gap-0.5 text-[9px]">
        {(s.buyp ?? []).slice(0, 5).map((p, i) => (
          <div key={`b${i}`} className="truncate text-center text-[--stock-up]">
            {p > 0 ? p : '--'}
          </div>
        ))}
      </div>
      <div className="grid grid-cols-5 gap-0.5 text-[9px]">
        {(s.buyv ?? []).slice(0, 5).map((v, i) => (
          <div key={`bv${i}`} className="truncate text-center text-muted-foreground">
            {v > 0 ? v : '--'}
          </div>
        ))}
      </div>
      <div className="mt-0.5 grid grid-cols-5 gap-0.5 text-[9px]">
        {(s.sellp ?? []).slice(0, 5).map((p, i) => (
          <div key={`s${i}`} className="truncate text-center text-[--stock-down]">
            {p > 0 ? p : '--'}
          </div>
        ))}
      </div>
      <div className="grid grid-cols-5 gap-0.5 text-[9px]">
        {(s.sellv ?? []).slice(0, 5).map((v, i) => (
          <div key={`sv${i}`} className="truncate text-center text-muted-foreground">
            {v > 0 ? v : '--'}
          </div>
        ))}
      </div>
      {s.before5min != null && s.now != null ? (
        <div className="mt-1 text-[10px] text-muted-foreground">
          5分钟前 {s.before5min} · 额 {fmtAmount(s.amount ?? null)} · {fmtPct(s.last_close ? ((s.now - s.last_close) / s.last_close) * 100 : null)}
        </div>
      ) : null}
    </div>
  )
}

function FundamentalCard({ symbol }: { symbol: string }) {
  const [fund, setFund] = useState<{
    gb?: { date?: string; ltgb?: number | null; zgb?: number | null } | null
    listing?: { name?: string; listing_date?: string } | null
    sub_new?: boolean | null
    note?: string | null
  } | null>(null)
  const [val, setVal] = useState<{ pe_dynamic?: number | null; pe_ttm?: number | null; pb?: number | null; dividend_yield?: number | null } | null>(null)
  useEffect(() => {
    let alive = true
    fetchAPI<typeof fund>(`/stocks/${encodeURIComponent(symbol)}/fundamental`)
      .then((r) => { if (alive) setFund(r ?? null) })
      .catch(() => { /* 保留旧值 */ })
    fetchAPI<{ more?: typeof val }>(`/stocks/${encodeURIComponent(symbol)}/l2`)
      .then((r) => { if (alive) setVal(r?.more ?? null) })
      .catch(() => { /* 保留旧值 */ })
    return () => { alive = false }
  }, [symbol])
  const row = (label: string, v: string) => (
    <div className="flex justify-between text-[11px]">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono">{v}</span>
    </div>
  )
  const fmtNum = (v: number | null | undefined, unit = '') =>
    v == null ? '--' : `${v}${unit}`
  return (
    <div className="rounded border border-border/60 p-2">
      <div className="mb-1 flex items-center gap-1 text-[12px] font-semibold">
        基本面 / 股本
        {fund?.sub_new ? (
          <span className="rounded bg-[--stock-up]/20 px-1 text-[9px] text-[--stock-up]">次新</span>
        ) : null}
      </div>
      {fund?.note ? <div className="mb-1 text-[10px] text-muted-foreground">{fund.note}</div> : null}
      {row('PE(动)', fmtNum(val?.pe_dynamic))}
      {row('PE(TTM)', fmtNum(val?.pe_ttm))}
      {row('PB', fmtNum(val?.pb))}
      {row('股息率', val?.dividend_yield != null ? `${val.dividend_yield}%` : '--')}
      {row('流通股本', fund?.gb?.ltgb != null ? fmtNum(Math.round(fund.gb.ltgb / 1e4), '万') : '--')}
      {row('总股本', fund?.gb?.zgb != null ? fmtNum(Math.round(fund.gb.zgb / 1e4), '万') : '--')}
      {row('上市', fund?.listing?.listing_date ? fund.listing.listing_date : '--')}
    </div>
  )
}

function BlocksCard({ symbol }: { symbol: string }) {
  const [data, setData] = useState<{ blocks: { code: string; name: string; type: string }[]; note?: string | null } | null>(null)
  useEffect(() => {
    let alive = true
    fetchAPI<{ blocks: { code: string; name: string; type: string }[]; note?: string | null }>(
      `/stocks/${encodeURIComponent(symbol)}/blocks`,
    )
      .then((r) => { if (alive) setData(r) })
      .catch(() => { /* 保留旧值 */ })
    return () => { alive = false }
  }, [symbol])
  const blocks = data?.blocks ?? []
  return (
    <div className="rounded border border-border/60 p-2">
      <div className="mb-1 text-[12px] font-semibold">题材 / 板块</div>
      {data?.note ? <div className="text-[10px] text-muted-foreground">{data.note}</div> : null}
      {blocks.length === 0 && !data?.note ? (
        <div className="text-[10px] text-muted-foreground">--</div>
      ) : null}
      <div className="flex flex-wrap gap-1">
        {blocks.map((b) => (
          <span key={b.code} className="rounded bg-accent/50 px-1 py-0.5 text-[10px] text-foreground/80">
            {b.name}
            <span className="ml-0.5 text-[9px] text-muted-foreground">{b.type}</span>
          </span>
        ))}
      </div>
    </div>
  )
}

export default function StockWorkbench() {
  const { symbol = '' } = useParams()
  if (!symbol) return <div className="p-4 text-[12px] text-muted-foreground">缺少股票代码</div>
  return (
    <div className="mx-auto flex max-w-[1500px] gap-3 p-3">
      <div className="min-w-0 flex-1">
        <div className="mb-1 text-[14px] font-semibold">{symbol}</div>
        <div className="rounded border border-border/60 p-2">
          <InteractiveKline symbol={symbol} market="CN" />
        </div>
      </div>
      <div className="flex w-[320px] shrink-0 flex-col gap-2">
        <DecisionPioneerCard symbol={symbol} market="CN" />
        <ResonanceVerdictPanel symbol={symbol} />
        <FundamentalCard symbol={symbol} />
        <BlocksCard symbol={symbol} />
        <L2Card symbol={symbol} />
      </div>
    </div>
  )
}
