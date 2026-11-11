import { Fragment, useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { ChevronDown, ChevronRight, Loader2 } from 'lucide-react'
import { decisionsApi, type PremarketItem, type PremarketLedgerResponse, type PremarketRow } from '@sida/api'
import { Button } from '@sida/base-ui/components/ui/button'
import { safeFixed, safeInt, safeNum, safePercent, safePrice } from '@/lib/format'

/**
 * 决策账本 · 盘前分析区块(2026-11-11)。
 *
 * 回答「盘前报告的潜伏表说了什么, 后来到底怎样」: 把盘前报告里
 * 「事件驱动前瞻(双轨制)·潜伏表」按交易日分块列出, 挂**真实**信号(股性因子/收盘位置/连板)
 * 与**事后**表现(当日 / T+1 / T+3, 收盘价口径)。
 *
 * 四条诚实边界(与后端 `GET /api/decisions/premarket` 一致, 前端只透传):
 *  ① `available=false` → 明说「暂无盘前分析记录」, **不用空表冒充**;
 *  ② `parsed=false` → 明说「该期报告未解析到潜伏表」, 不补标的;
 *  ③ 信号/表现缺失 → `signals.demon`/`signals.price` 显「无数据」, 收益显 `--`, **不补 0**;
 *  ④ 止损只在写了**具体价位**时判「已破/未破」(收盘价口径), 条件式止损显原文 + 「条件式未判定」。
 *
 * 加载/失败独立降级: 本区块自己取数、自己兜底, 失败只在本区块显示一行 + 重试,
 * 不带垮页面其他区块(因此这里**不用** role=alert, 页面级错误态仍归 ErrorState)。
 */

const DAYS = 5
const MAX_ROWS = 30

/** A 股红涨绿跌; 缺失值走中性色(不许把 null 当 0 上「涨红」)。 */
function pctClass(v: unknown): string {
  const n = safeNum(v)
  if (n === null) return 'text-muted-foreground'
  if (n > 0) return 'text-stock-up'
  if (n < 0) return 'text-stock-down'
  return 'text-muted-foreground'
}

/** 一格涨幅: 数字走 safePercent(null → '--'), 颜色走 A 股口径。 */
function Pct({ value }: { value: number | null | undefined }) {
  return <span className={`font-mono ${pctClass(value)}`}>{safePercent(value, 2)}</span>
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[12px] text-muted-foreground">{label}</dt>
      <dd className="text-[12px] leading-relaxed break-words">{children}</dd>
    </div>
  )
}

/** 「无数据」= 后端该项为 null(取不到), 绝不补 0。 */
function NoData() {
  return <span className="text-[12px] text-muted-foreground">无数据</span>
}

/** 股性因子(封板/连板/参与度) —— null 一律「无数据」。 */
function DemonDetail({ demon }: { demon: PremarketRow['signals']['demon'] }) {
  if (!demon) return <NoData />
  const seen = [demon.score, demon.grade, demon.n_sealed, demon.max_streak, demon.participation]
  if (seen.every((v) => v === null || v === undefined || v === '')) return <NoData />
  return (
    <span className="font-mono">
      {demon.grade ? <span className="mr-1">{demon.grade}</span> : null}
      {safeFixed(demon.score, 1)}
      <span className="ml-2 font-sans text-muted-foreground">
        封板 {safeInt(demon.n_sealed)} 次 · 最高 {safeInt(demon.max_streak)} 连板
      </span>
      {demon.participation ? <span className="ml-2 font-sans text-muted-foreground">{demon.participation}</span> : null}
    </span>
  )
}

/** 价格位置(收盘/当日/连板/20 日) —— 缺 close 且无 note 即「无数据」。 */
function PriceDetail({ price }: { price: PremarketRow['signals']['price'] }) {
  if (!price) return <NoData />
  if (safeNum(price.close) === null) {
    return price.note ? <span className="text-[12px] text-muted-foreground">{price.note}</span> : <NoData />
  }
  const streak = safeNum(price.limit_up_streak)
  return (
    <span className="font-mono">
      收盘 {safePrice(price.close)}
      <span className="ml-2">
        <Pct value={price.pct} />
      </span>
      <span className="ml-2 font-sans text-muted-foreground">
        {streak === null ? '连板 --' : streak > 0 ? `连板 ${safeInt(streak)}` : '非涨停'}
      </span>
      <span className="ml-2 font-sans text-muted-foreground">
        20日 <Pct value={price.pct_20d} /> · 距20日高 <Pct value={price.from_20d_high} />
      </span>
      {price.note ? <span className="ml-2 font-sans text-muted-foreground">{price.note}</span> : null}
    </span>
  )
}

/** 止损列: 有具体价位 → 已破/未破(收盘价口径); 条件式 → 原文 + 「条件式未判定」。 */
function StopCell({ row }: { row: PremarketRow }) {
  const price = safeNum(row.stop_price)
  if (price !== null) {
    const breached = row.outcome?.stop_breached
    return (
      <span className="flex flex-wrap items-baseline gap-1">
        <span className="font-mono">{safePrice(price)}</span>
        {breached === true ? (
          <span className="text-destructive" title="收盘价跌破止损价(盘中路径无数据)">
            已破
          </span>
        ) : breached === false ? (
          <span className="text-muted-foreground" title="收盘价未跌破止损价(盘中路径无数据)">
            未破
          </span>
        ) : (
          <span className="text-muted-foreground">未判定</span>
        )}
      </span>
    )
  }
  if (!row.stop) return <span className="text-muted-foreground">--</span>
  return (
    <span className="flex flex-wrap items-baseline gap-1">
      <span className="break-words">{row.stop}</span>
      <span className="text-muted-foreground">条件式未判定</span>
    </span>
  )
}

/** 展开行: 受益逻辑/事件/板块 + 止损原文 + 目标 + 证伪条件 + 真实信号明细。 */
function RowDetail({ row }: { row: PremarketRow }) {
  return (
    <div className="space-y-2 px-2 py-2" data-testid="premarket-row-detail">
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
        <Field label="事件">{row.event || '--'}</Field>
        <Field label="受益板块">{row.sector || '--'}</Field>
        <Field label="受益逻辑">{row.logic || '--'}</Field>
        <Field label="止损原文">
          {row.stop || '--'}
          {safeNum(row.stop_price) === null ? (
            <span className="ml-1 text-muted-foreground">(条件式, 无法按收盘价判定)</span>
          ) : (
            <span className="ml-1 font-mono text-muted-foreground">({safePrice(row.stop_price)})</span>
          )}
        </Field>
        <Field label="目标">{row.target || '--'}</Field>
        <Field label="证伪条件">{row.falsify || '--'}</Field>
        <Field label="批次">{row.batch || '--'}</Field>
        <Field label="事后表现(收盘价口径)">
          <span className="font-mono">
            当日 <Pct value={row.outcome?.t0} /> · T+1 <Pct value={row.outcome?.t1} /> · T+3{' '}
            <Pct value={row.outcome?.t3} />
          </span>
          {row.outcome?.note ? <span className="ml-2 text-muted-foreground">{row.outcome.note}</span> : null}
        </Field>
        <Field label="股性因子">
          <DemonDetail demon={row.signals?.demon ?? null} />
        </Field>
        <Field label="价格位置">
          <PriceDetail price={row.signals?.price ?? null} />
        </Field>
      </dl>
    </div>
  )
}

function PeriodBlock({
  item,
  open,
  onToggle,
}: {
  item: PremarketItem
  open: Record<string, boolean>
  onToggle: (key: string) => void
}) {
  const s = item.summary
  return (
    <div className="mt-3 first:mt-2" data-testid="premarket-period">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 border-b border-border pb-1">
        <span className="font-mono text-[14px] font-semibold">{item.date}</span>
        {item.title && (
          <span className="min-w-0 truncate text-[12px] text-muted-foreground" title={item.title}>
            {item.title}
          </span>
        )}
        {item.quality === 'suspect' && (
          <span className="rounded border border-border px-1 text-[12px] text-muted-foreground" title="该期报告正文为空/异常, 解析结果不可信">
            正文异常
          </span>
        )}
        <span className="ml-auto text-[12px] text-muted-foreground">
          潜伏 {safeInt(s?.rows)} 只 · 当日上涨 {safeInt(s?.t0_up)}/{safeInt(s?.t0_scored)} · T+1 上涨{' '}
          {safeInt(s?.t1_up)}/{safeInt(s?.t1_scored)}
        </span>
      </div>

      {!item.parsed || (item.rows ?? []).length === 0 ? (
        <p className="mt-1.5 text-[12px] text-muted-foreground" data-testid="premarket-unparsed">
          该期报告未解析到潜伏表 —— 不补标的, 也不推测当时想买什么。
        </p>
      ) : (
        <div className="mt-1 overflow-x-auto">
          <table className="w-full text-left text-[12px]">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="px-2 py-1 font-medium">标的</th>
                <th className="px-2 py-1 font-medium">强度</th>
                <th className="px-2 py-1 font-medium">埋伏方式</th>
                <th className="px-2 py-1 text-right font-medium">当日</th>
                <th className="px-2 py-1 text-right font-medium">T+1</th>
                <th className="px-2 py-1 text-right font-medium">T+3</th>
                <th className="px-2 py-1 font-medium">止损</th>
                <th className="px-2 py-1 text-right font-medium">明细</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {item.rows.map((row, idx) => {
                const key = `${item.date}-${row.code}-${idx}`
                const expanded = !!open[key]
                return (
                  <Fragment key={key}>
                    <tr
                      className="cursor-pointer align-top hover:bg-s2"
                      onClick={() => onToggle(key)}
                      data-testid="premarket-row"
                    >
                      <td className="px-2 py-1">
                        <div className="flex flex-wrap items-baseline gap-1">
                          <span className="font-mono">{row.code}</span>
                          <span className="max-w-[9rem] truncate" title={row.name}>
                            {row.name || '--'}
                          </span>
                        </div>
                        {(row.tags ?? []).length > 0 && (
                          <div className="mt-0.5 flex flex-wrap gap-1">
                            {(row.tags ?? []).map((t) => (
                              <span
                                key={t}
                                className="rounded border border-border px-1 text-[12px] text-muted-foreground"
                              >
                                {t}
                              </span>
                            ))}
                          </div>
                        )}
                      </td>
                      <td className="px-2 py-1">{row.strength || '--'}</td>
                      <td className="px-2 py-1">{row.method || '--'}</td>
                      <td className="px-2 py-1 text-right">
                        <Pct value={row.outcome?.t0} />
                      </td>
                      <td className="px-2 py-1 text-right">
                        <Pct value={row.outcome?.t1} />
                      </td>
                      <td className="px-2 py-1 text-right">
                        <Pct value={row.outcome?.t3} />
                      </td>
                      <td className="px-2 py-1">
                        <StopCell row={row} />
                      </td>
                      <td className="px-2 py-1 text-right">
                        <button
                          type="button"
                          className="inline-flex items-center gap-0.5 text-[12px] text-muted-foreground hover:text-foreground"
                          aria-expanded={expanded}
                          aria-label={`${row.name || row.code} 明细`}
                          onClick={(e) => {
                            e.stopPropagation()
                            onToggle(key)
                          }}
                        >
                          {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                          明细
                        </button>
                      </td>
                    </tr>
                    {expanded && (
                      <tr>
                        <td colSpan={8} className="bg-s2">
                          <RowDetail row={row} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function PremarketLedgerSection() {
  const [data, setData] = useState<PremarketLedgerResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [open, setOpen] = useState<Record<string, boolean>>({})

  const load = useCallback(async () => {
    setLoading(true)
    setFailed(false)
    try {
      const r = await decisionsApi.premarket(DAYS, MAX_ROWS)
      setData(r)
    } catch {
      // 独立降级: 只标记本区块失败, 页面其他区块照常
      setFailed(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const toggle = useCallback((key: string) => {
    setOpen((prev) => ({ ...prev, [key]: !prev[key] }))
  }, [])

  const items = useMemo(
    () => [...(data?.items ?? [])].sort((a, b) => String(b.date).localeCompare(String(a.date))),
    [data],
  )

  return (
    <section className="analysis-section" data-testid="premarket-ledger">
      <h2 className="text-[16px] font-semibold">盘前分析</h2>
      <p className="mt-0.5 text-[12px] text-muted-foreground">
        盘前报告里潜伏表(事件驱动前瞻 · 双轨制)的标的, 用真实行情对账: 当日 / T+1 / T+3 按收盘价算,
        止损只在写了具体价位时判是否跌破; 潜伏标的由 AI 撰写, 本页只解析、不补造。
      </p>

      {loading && !data && (
        <div className="mt-2 flex items-center gap-2 text-[12px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          盘前分析读取中…
        </div>
      )}

      {failed && !data && (
        <div className="mt-2 flex items-center gap-2 text-[12px] text-muted-foreground" data-testid="premarket-error">
          <span>盘前分析加载失败</span>
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
            重试
          </Button>
        </div>
      )}

      {data && (!data.available || (data.items ?? []).length === 0) && (
        <p className="mt-2 text-[12px] text-muted-foreground" data-testid="premarket-empty">
          暂无盘前分析记录{data.error ? `(${data.error})` : ''} —— 盘前报告生成并解析出潜伏表后才会入账, 空即空。
        </p>
      )}

      {data?.available &&
        items.map((item) => <PeriodBlock key={item.date} item={item} open={open} onToggle={toggle} />)}

      {data?.note && <p className="mt-2 text-[12px] text-muted-foreground">{data.note}</p>}
    </section>
  )
}
