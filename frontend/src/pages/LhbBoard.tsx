/**
 * 龙虎榜独立工作区(2026-11-09)。
 *
 * 目的: 把"今天谁上榜、因为什么、谁在买、谁在卖"做成一个可独立打开的页面, 而不是塞在
 * 个股基本面面板里当一行明细。
 *
 * 数据契约(来自代码, 不是猜 — 详见 packages/api/src/archive.ts 头注释):
 *  · `GET /api/archive/dragon-tiger?date=YYYYMMDD` → `{count, note, items}`
 *    `dragon_tiger_events` 唯一键 `(trade_date, symbol, reason)` ⇒ **同股可多原因多行**;
 *  · `GET /api/archive/lhb-seats?date=&symbol=` → `{count, note, items}`
 *    `lhb_seat_details` 按 `side`(buy/sell) 给营业部买卖明细, `row_uid` 为内容哈希主键;
 *  · `GET /api/archive/overview` → 各表 `latest_date`, 用来把默认日期落在**有数据的那天**
 *    (而不是"今天" — 周末/节假日/未采集时"今天"一定是空榜, 用户会以为功能坏了)。
 *
 * 三条不可退让的口径:
 *  ① **多上榜原因不做金额加总**: 每条原因是独立原始行, 相加会把同一笔成交重复计;
 *  ② **营业部席位 ≠ 已验证游资**: 席位只代表成交归属, 页面常驻声明(SEAT_IDENTITY_DISCLAIMER);
 *  ③ **空/失败/加载如实显示**: 空榜透出后端 note, 失败给可重试错误态, 不伪造任何一行。
 *
 * 竞态: 切日期 / 切股票都会发新请求, 慢响应的旧请求必须被丢弃(序号守卫),
 * 否则页面会出现"日期=11-05 但表格是 11-06"这种自相矛盾的状态。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertCircle, Loader2, RefreshCw } from 'lucide-react'
import { archiveApi, type ArchiveDragonTigerItem, type ArchiveLhbSeatItem } from '@sida/api'
import PageHeader from '@/components/PageHeader'
import { Button } from '@sida/base-ui/components/ui/button'
import { StatusChip } from '@sida/base-ui/components/ui/status-chip'
import { safeInt, safeMoney, safeMoneyUnsigned, safePercent, safePrice } from '@/lib/format'
import { cn } from '@/lib/utils'
import {
  DEPT_FALLBACK,
  SEAT_IDENTITY_DISCLAIMER,
  formatTradeDate,
  groupLhbBySymbol,
  latestDragonTigerDate,
  reasonLabel,
  splitSeats,
  toApiDate,
  toDateInputValue,
  type LhbSeatRow,
} from '@/lib/lhb'

type LoadStatus = 'idle' | 'loading' | 'ready' | 'error'

interface DayState {
  /** 这份数据对应的交易日(YYYYMMDD) —— 页面"数据日期"只认它, 不认输入框 */
  date: string
  items: ArchiveDragonTigerItem[]
  note: string
}

interface SeatTarget {
  date: string
  symbol: string
  name: string
}

function todayCst(): string {
  // en-CA 的日期格式就是 YYYY-MM-DD; 时区固定 Asia/Shanghai(与后端 _today 同口径)。
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
}

function errText(e: unknown): string {
  if (e instanceof Error && e.message) return e.message
  return '请求失败（未收到有效响应）'
}

function pctClass(v: number | null): string {
  if (v === null) return 'text-muted-foreground'
  if (v > 0) return 'text-stock-up'
  if (v < 0) return 'text-stock-down'
  return 'text-muted-foreground'
}

function SeatRow({ seat, side }: { seat: LhbSeatRow; side: 'buy' | 'sell' }) {
  const ratio = side === 'buy' ? seat.total_buy_ratio : seat.total_sell_ratio
  return (
    <li className="flex flex-col gap-1 py-2 first:pt-0 last:pb-0" data-testid={`lhb-seat-${side}-${seat.row_uid}`}>
      <div className="flex items-start justify-between gap-2">
        <span className="min-w-0 break-words text-[14px] text-foreground">{seat.deptName}</span>
        <span
          className={cn(
            'shrink-0 font-num text-[14px] tabular-nums',
            seat.netAmt === null ? 'text-muted-foreground' : seat.netAmt >= 0 ? 'text-stock-up' : 'text-stock-down',
          )}
        >
          {safeMoney(seat.netAmt)}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span>买 {safeMoneyUnsigned(seat.buyAmt)}</span>
        <span>卖 {safeMoneyUnsigned(seat.sellAmt)}</span>
        <span>占{side === 'buy' ? '买入' : '卖出'}总额 {safePercent(ratio, 2, false)}</span>
        {seat.rise_probability_3d !== null && seat.rise_probability_3d !== undefined && (
          <span title="源字段原值，未回测">3日上涨概率 {safePercent(seat.rise_probability_3d, 2, false)}</span>
        )}
      </div>
      {seat.reason && <div className="text-xs text-muted-foreground">上榜原因：{seat.reason}</div>}
    </li>
  )
}

function SeatSection({ title, seats, side }: { title: string; seats: LhbSeatRow[]; side: 'buy' | 'sell' }) {
  return (
    <section className="space-y-1" data-testid={`lhb-seats-${side}`}>
      <h3 className="flex items-center gap-2 text-[14px] font-medium text-foreground">
        <span className={cn('h-2 w-2 rounded-full', side === 'buy' ? 'bg-gs-go' : 'bg-gs-stop')} aria-hidden />
        {title}
        <span className="font-num text-xs font-normal text-muted-foreground">{seats.length} 席</span>
      </h3>
      {seats.length === 0 ? (
        <p className="py-2 text-xs text-muted-foreground">本侧无披露席位</p>
      ) : (
        <ul className="divide-y divide-border">
          {seats.map((seat) => (
            <SeatRow key={`${side}-${seat.row_uid}`} seat={seat} side={side} />
          ))}
        </ul>
      )}
    </section>
  )
}

export default function LhbBoardPage() {
  const today = useMemo(() => todayCst(), [])
  const [dateInput, setDateInput] = useState('')

  const [latestDate, setLatestDate] = useState<string | null>(null)
  const [latestKnown, setLatestKnown] = useState(false)
  const [day, setDay] = useState<DayState | null>(null)
  const [dayStatus, setDayStatus] = useState<LoadStatus>('loading')
  const [dayError, setDayError] = useState('')
  const [reloadTick, setReloadTick] = useState(0)

  const [seatTarget, setSeatTarget] = useState<SeatTarget | null>(null)
  const [seats, setSeats] = useState<ArchiveLhbSeatItem[]>([])
  const [seatsStatus, setSeatsStatus] = useState<LoadStatus>('idle')
  const [seatsError, setSeatsError] = useState('')

  const daySeq = useRef(0)
  const seatSeq = useRef(0)

  // 默认日期 = 龙虎榜表最新可用日; 取不到(overview 失败/统计为 None)才退回今天, 并如实标注。
  useEffect(() => {
    let alive = true
    archiveApi
      .overview()
      .then((res) => {
        if (!alive) return
        const latest = latestDragonTigerDate(res?.tables)
        setLatestDate(latest)
        setLatestKnown(true)
        setDateInput((cur) => cur || toDateInputValue(latest) || toDateInputValue(today))
      })
      .catch(() => {
        if (!alive) return
        setLatestDate(null)
        setLatestKnown(true)
        setDateInput((cur) => cur || toDateInputValue(today))
      })
    return () => {
      alive = false
    }
  }, [today])

  // 日榜: 输入日期变化 → 拉全榜。序号守卫丢弃迟到的旧响应。
  useEffect(() => {
    const apiDate = toApiDate(dateInput)
    if (!apiDate) return
    const seq = ++daySeq.current
    setDayStatus('loading')
    setDayError('')
    setSeatTarget(null)
    setSeats([])
    setSeatsStatus('idle')
    setSeatsError('')
    archiveApi
      .dragonTiger({ date: apiDate })
      .then((res) => {
        if (seq !== daySeq.current) return
        setDay({ date: apiDate, items: res?.items ?? [], note: res?.note ?? '' })
        setDayStatus('ready')
      })
      .catch((e) => {
        if (seq !== daySeq.current) return
        setDay(null)
        setDayError(errText(e))
        setDayStatus('error')
      })
  }, [dateInput, reloadTick])

  const loadSeats = useCallback((target: SeatTarget) => {
    const seq = ++seatSeq.current
    setSeatTarget(target)
    setSeats([])
    setSeatsStatus('loading')
    setSeatsError('')
    archiveApi
      .lhbSeats({ date: target.date, symbol: target.symbol })
      .then((res) => {
        if (seq !== seatSeq.current) return
        setSeats(res?.items ?? [])
        setSeatsStatus('ready')
      })
      .catch((e) => {
        if (seq !== seatSeq.current) return
        setSeatsStatus('error')
        setSeatsError(errText(e))
      })
  }, [])

  const groups = useMemo(() => groupLhbBySymbol(day?.items), [day])
  const hasMultipleReasons = groups.some((g) => g.multipleReasons)
  const seatSplit = useMemo(() => splitSeats(seats), [seats])
  const selectedName = seatTarget?.name || seatTarget?.symbol || ''
  const dataDate = day?.date ?? toApiDate(dateInput)

  return (
    <div className="page-container space-y-4" data-testid="lhb-page">
      <PageHeader
        title="龙虎榜"
        description="按交易日查看全榜：先看谁上榜、因何上榜，再点开个股核对买卖席位。金额以元为原始单位，界面换算为万/亿展示。"
        actions={
          <div className="flex flex-wrap items-center gap-2 p-1">
            <label htmlFor="lhb-date" className="text-xs text-muted-foreground">
              榜单日期
            </label>
            <input
              id="lhb-date"
              data-testid="lhb-date"
              type="date"
              value={dateInput}
              max={today}
              onChange={(e) => setDateInput(e.target.value)}
              className="h-9 rounded-lg bg-muted px-2 text-[14px] text-foreground"
            />
            <Button
              type="button"
              variant="ghost"
              size="sm"
              data-testid="lhb-refresh"
              onClick={() => setReloadTick((n) => n + 1)}
              aria-label="刷新榜单"
            >
              <RefreshCw className="h-4 w-4" /> 刷新
            </Button>
          </div>
        }
        metadata={
          <>
            <StatusChip data-testid="lhb-source-date">
              数据日期 {formatTradeDate(dataDate)}
            </StatusChip>
            <StatusChip tone="info" data-testid="lhb-unit-note">
              金额单位：元（万/亿为换算显示）
            </StatusChip>
            {dayStatus === 'ready' && (
              <span className="text-xs text-muted-foreground">
                上榜 {safeInt(groups.length)} 只 / {safeInt(day?.items.length)} 条原因行
                {latestKnown && latestDate && dataDate === latestDate ? ' · 已是最新可用日' : ''}
              </span>
            )}
            {latestKnown && !latestDate && (
              <span className="text-xs text-amber-600 dark:text-amber-500">
                最新可用日期未知（新鲜度统计不可用），已回退到今日，可能为空榜
              </span>
            )}
          </>
        }
      />

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)]">
        {/* ── 左: 当日榜单 ─────────────────────────────────────────────── */}
        <section className="card-surface space-y-3 bg-card p-3" aria-label="当日龙虎榜">
          {hasMultipleReasons && (
            <p className="rounded-lg bg-muted px-2 py-1.5 text-xs text-muted-foreground" data-testid="lhb-multi-reason-note">
              同一股票可能同时命中多条上榜原因：金额按各原因原始行分别展示，不做加总（相加会重复计同一笔成交）。
            </p>
          )}

          {dayStatus === 'loading' && (
            <div className="flex items-center gap-2 py-6 text-[14px] text-muted-foreground" data-testid="lhb-loading">
              <Loader2 className="h-4 w-4 animate-spin" /> 正在读取 {formatTradeDate(dataDate)} 榜单…
            </div>
          )}

          {dayStatus === 'error' && (
            <div className="flex flex-col items-start gap-2 py-4" role="alert" data-testid="lhb-error">
              <p className="flex items-center gap-2 text-[14px] text-foreground">
                <AlertCircle className="h-4 w-4 text-destructive" />
                榜单读取失败：{dayError}
              </p>
              <p className="text-xs text-muted-foreground">
                归档接口只读不回源，失败通常意味着服务暂不可用；下方可重试同一日期。
              </p>
              <Button type="button" size="sm" variant="outline" data-testid="lhb-retry" onClick={() => setReloadTick((n) => n + 1)}>
                重试
              </Button>
            </div>
          )}

          {dayStatus === 'ready' && groups.length === 0 && (
            <div className="py-6" data-testid="lhb-empty">
              <p className="text-[14px] text-foreground">{formatTradeDate(dataDate)} 无龙虎榜数据</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {day?.note?.trim() || '后端未给出空榜原因'}。归档表没有该日记录时不会用其它日期数据顶替。
              </p>
            </div>
          )}

          {dayStatus === 'ready' && groups.length > 0 && (
            <ul className="space-y-2" data-testid="lhb-stock-list">
              {groups.map((g) => {
                const active = seatTarget?.symbol === g.symbol && seatTarget?.date === (day?.date ?? '')
                const openSeats = () => loadSeats({ date: g.tradeDate || (day?.date ?? ''), symbol: g.symbol, name: g.name })
                return (
                  <li key={g.symbol}>
                    {/*
                      整卡可点(原因行也算), 但真正的交互元素只有标题按钮 ——
                      避免 <ul> 被塞进 <button> 内(内容模型非法, 读屏会把原因当按钮名)。
                    */}
                    <div
                      data-testid={`lhb-stock-${g.symbol}`}
                      onClick={openSeats}
                      className={cn(
                        'cursor-pointer rounded-lg p-2.5 transition-colors',
                        active ? 'bg-muted' : 'hover:bg-muted',
                      )}
                    >
                      <button
                        type="button"
                        aria-pressed={active}
                        onClick={(e) => {
                          e.stopPropagation()
                          openSeats()
                        }}
                        className="flex w-full flex-wrap items-baseline justify-between gap-x-2 gap-y-1 text-left"
                      >
                        <span className="flex min-w-0 flex-wrap items-baseline gap-2">
                          <span className="truncate text-[14px] font-medium text-foreground">{g.name || g.symbol}</span>
                          <span className="font-num text-xs text-muted-foreground">{g.symbol}</span>
                          {g.reasons.length > 1 && (
                            <span className="rounded bg-accent px-1.5 py-0.5 text-xs text-accent-foreground">
                              {g.reasons.length} 条原因
                            </span>
                          )}
                        </span>
                        <span className="flex items-baseline gap-2 font-num text-xs tabular-nums">
                          <span className="text-muted-foreground">{safePrice(g.close)}</span>
                          <span className={pctClass(g.changePct)}>{safePercent(g.changePct)}</span>
                        </span>
                      </button>

                      <ul className="mt-1.5 space-y-1">
                        {g.reasons.map((r, i) => (
                          <li
                            key={`${g.symbol}-${i}`}
                            data-testid={`lhb-reason-${g.symbol}-${i}`}
                            className="flex flex-col gap-0.5 rounded bg-background px-2 py-1"
                          >
                            <span className="text-xs text-foreground">{reasonLabel(r.reason)}</span>
                            <span className="flex flex-wrap gap-x-3 gap-y-0.5 font-num text-xs tabular-nums text-muted-foreground">
                              <span>净买 {safeMoney(r.netBuy)}</span>
                              <span>买 {safeMoneyUnsigned(r.buyAmt)}</span>
                              <span>卖 {safeMoneyUnsigned(r.sellAmt)}</span>
                              <span>成交 {safeMoneyUnsigned(r.dealAmt)}</span>
                            </span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </section>

        {/* ── 右: 席位明细 ─────────────────────────────────────────────── */}
        <section className="card-surface space-y-3 bg-card p-3" aria-label="营业部席位" data-testid="lhb-seats-panel">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="text-[14px] font-medium text-foreground">
              {seatTarget ? `${selectedName} 席位` : '营业部席位'}
            </h2>
            {seatTarget && (
              <span className="text-xs text-muted-foreground">
                来源日期 {formatTradeDate(seatTarget.date)}
              </span>
            )}
          </div>

          <p className="rounded-lg bg-muted px-2 py-1.5 text-xs text-muted-foreground" data-testid="lhb-seat-disclaimer">
            {SEAT_IDENTITY_DISCLAIMER}
          </p>

          {!seatTarget && (
            <p className="py-6 text-[14px] text-muted-foreground" data-testid="lhb-seats-idle">
              从左侧选择一只上榜股票，查看当日买方/卖方营业部明细。
            </p>
          )}

          {seatTarget && seatsStatus === 'loading' && (
            <div className="flex items-center gap-2 py-6 text-[14px] text-muted-foreground" data-testid="lhb-seats-loading">
              <Loader2 className="h-4 w-4 animate-spin" /> 正在读取席位…
            </div>
          )}

          {seatTarget && seatsStatus === 'error' && (
            <div className="space-y-2 py-2" role="alert" data-testid="lhb-seats-error">
              <p className="flex items-center gap-2 text-[14px] text-foreground">
                <AlertCircle className="h-4 w-4 text-destructive" />
                席位读取失败：{seatsError}
              </p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                data-testid="lhb-seats-retry"
                onClick={() => loadSeats(seatTarget)}
              >
                重试
              </Button>
            </div>
          )}

          {seatTarget && seatsStatus === 'ready' && seats.length === 0 && (
            <div className="py-4" data-testid="lhb-seats-empty">
              <p className="text-[14px] text-foreground">该股当日无营业部席位明细</p>
              <p className="mt-1 text-xs text-muted-foreground">
                可能原因：该榜原因不含营业部明细披露，或归档尚未覆盖；页面不会用其它日期或其它股票的席位填充。
              </p>
            </div>
          )}

          {seatTarget && seatsStatus === 'ready' && seats.length > 0 && (
            <div className="space-y-3">
              <SeatSection title="买方席位" seats={seatSplit.buy} side="buy" />
              <SeatSection title="卖方席位" seats={seatSplit.sell} side="sell" />
              <p className="text-xs text-muted-foreground">
                排序：按净额降序；净额缺失的行排在最后（缺失不等于 0）。营业部名缺失显示「{DEPT_FALLBACK}」。
              </p>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
