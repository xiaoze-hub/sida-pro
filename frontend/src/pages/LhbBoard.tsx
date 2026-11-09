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
 * 两条 2026-11-09 复审补上的口径:
 *  ④ **质量定性来自后端, 前端只转述**: 后端 `quality` 给缺口/早写风险/两表交叉提示;
 *     拿不到就是未知(不猜完整), 即使后端声称 complete 但未与官方核对, 页面也不采信;
 *  ⑤ **分析此股龙虎榜**: 带着页面上真实显示过的代码与交易日进数智参谋(只带入口生效的
 *     symbol + q, 不塞无龙虎榜工具的 agent、不塞被入口忽略的 date), 问题正文写明日期,
 *     不预置任何结论 —— 没选股票就不渲染入口; 入口不认的代码不传 symbol(传了只会静默忽略),
 *     文案只说"没被识别为股票锚点", **不猜证券类型**(北交所 920xxx 也曾被旧文案误称非股票),
 *     并明说只是预填、需手动发送。
 *
 * 第二轮复审补上的口径:
 *  ⑥ **席位家数只在身份可核实时才说** —— 「机构专用」是多家机构共用的称号,
 *     按名称去重会把它们算成"1 家"; 核实不了就只报源记录条数 + 身份未知(seatCountText)。
 *
 * 竞态: 切日期 / 切股票都会发新请求, 慢响应的旧请求必须被丢弃(序号守卫),
 * 否则页面会出现"日期=11-05 但表格是 11-06"这种自相矛盾的状态。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertCircle, Loader2, RefreshCw, Sparkles } from 'lucide-react'
import { archiveApi, type ArchiveDragonTigerItem, type ArchiveLhbSeatItem } from '@sida/api'
import PageHeader from '@/components/PageHeader'
import { Button } from '@sida/base-ui/components/ui/button'
import { StatusChip } from '@sida/base-ui/components/ui/status-chip'
import { safeInt, safeMoney, safeMoneyUnsigned, safePercent, safePrice } from '@/lib/format'
import { cn } from '@/lib/utils'
import {
  DEPT_FALLBACK,
  LHB_DAY_LIMIT,
  SEAT_IDENTITY_DISCLAIMER,
  SEAT_IDENTITY_UNKNOWN_NOTE,
  archiveNoteText,
  formatTradeDate,
  groupLhbBySymbol,
  latestDragonTigerDate,
  lhbCopilotHandoff,
  lhbCoverage,
  lhbQualityView,
  reasonLabel,
  seatCountText,
  seatRatioPercentPoints,
  seatSummary,
  splitSeats,
  toApiDate,
  toDateInputValue,
  type LhbSeatRow,
  type LhbQualityView,
  type SeatSummary,
} from '@/lib/lhb'

type LoadStatus = 'idle' | 'loading' | 'ready' | 'error'

interface DayState {
  /** 这份数据对应的交易日(YYYYMMDD) —— 页面"数据日期"只认它, 不认输入框 */
  date: string
  items: ArchiveDragonTigerItem[]
  note: string
  /** 后端质量定性(缺口/风险); 缺失=未知, 页面不得自行推断完整 */
  quality: unknown
}

/** 参谋交接(锚点/预填), 页面上显示的每句话都以它为准 */
interface HandoffState {
  url: string
  anchor: boolean
  /** 无法设锚点时的中性说明(不猜证券类型); 有锚点为空串 */
  anchorNote: string
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
  // 库内占比是 0~1 小数(0.1981 = 19.81%), safePercent 收百分点 ⇒ 先换算一次。
  const ratioPct = seatRatioPercentPoints(ratio)
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
        <span>占{side === 'buy' ? '买入' : '卖出'}总额 {safePercent(ratioPct, 2, false)}</span>
        {seat.rise_probability_3d !== null && seat.rise_probability_3d !== undefined && (
          <span title="源字段原值，未回测">3日上涨概率 {safePercent(seat.rise_probability_3d, 2, false)}</span>
        )}
      </div>
      {seat.reason && <div className="text-xs text-muted-foreground">上榜原因：{seat.reason}</div>}
    </li>
  )
}

function SeatSection({
  title,
  seats,
  side,
  summary,
}: {
  title: string
  seats: LhbSeatRow[]
  side: 'buy' | 'sell'
  summary: SeatSummary
}) {
  return (
    <section className="space-y-1" data-testid={`lhb-seats-${side}`}>
      <h3 className="flex flex-wrap items-center gap-2 text-[14px] font-medium text-foreground">
        <span className={cn('h-2 w-2 rounded-full', side === 'buy' ? 'bg-gs-go' : 'bg-gs-stop')} aria-hidden />
        {title}
        {/*
          条数 ≠ 独立席位家数: 同一营业部可因多条上榜原因各占一行。
          而且家数只在身份可核实时才存在: 「机构专用」是多家机构共用的称号, 按名称去重
          会把它们误称"1 家"。核实不了就只报源记录条数 + 身份未知(seatCountText 统一拼)。
        */}
        <span className="font-num text-xs font-normal text-muted-foreground" data-testid={`lhb-seat-count-${side}`}>
          {seatCountText(summary)}
        </span>
      </h3>
      {seats.length === 0 ? (
        <p className="py-2 text-xs text-muted-foreground">本侧无披露席位</p>
      ) : (
        <ul className="divide-y divide-border">
          {seats.map((seat, i) => (
            /*
              key 带序号兜底: row_uid 是后端内容哈希, 正常都有; 但若某行缺失, 只用 `side-row_uid`
              会让两行拿到同一个 key —— React 复用节点, 营业部名与金额就会串到相邻行。
            */
            <SeatRow key={`${side}-${seat.row_uid || 'norow'}-${i}`} seat={seat} side={side} />
          ))}
        </ul>
      )}
    </section>
  )
}

export default function LhbBoardPage() {
  const today = useMemo(() => todayCst(), [])
  const navigate = useNavigate()
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
      // 显式带 limit: 只有知道自己要了多少条, 才能判断"刚好取满 = 可能被截断"。
      // market=CN: 让后端质量定性的行数与页面展示的行数同口径(否则定性可能只覆盖一个市场)。
      .dragonTiger({ date: apiDate, limit: LHB_DAY_LIMIT, market: 'CN' })
      .then((res) => {
        if (seq !== daySeq.current) return
        setDay({ date: apiDate, items: res?.items ?? [], note: res?.note ?? '', quality: res?.quality ?? null })
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
  const buySummary = useMemo(() => seatSummary(seatSplit.buy), [seatSplit])
  const sellSummary = useMemo(() => seatSummary(seatSplit.sell), [seatSplit])
  const seatsHaveMultipleReasons = buySummary.multipleReasons || sellSummary.multipleReasons
  const selectedName = seatTarget?.name || seatTarget?.symbol || ''
  const dataDate = day?.date ?? toApiDate(dateInput)

  /**
   * 覆盖口径只看"这一份数据实际是什么": 已完成加载就按条数算(0 条 = 归档无记录),
   * 加载中/失败时为 null(不拿空数组冒充"覆盖 0 条", 那会读成"当日没上榜")。
   * 质量定性由后端 quality 决定; 后端没给就是未知, 不自行补"完整"。
   */
  const coverage = useMemo(
    () => (dayStatus === 'ready' ? lhbCoverage(day?.items.length ?? 0, LHB_DAY_LIMIT, day?.quality) : null),
    [dayStatus, day],
  )

  /**
   * 质量定性只认"这一份已加载完的数据": 切日期时 `day` 还留着上一日的数据,
   * 若直接读它, 加载中会显示上一个交易日的早写风险/写入窗口 —— 那是串日期。未 ready 一律未知。
   */
  const quality: LhbQualityView = useMemo(
    () => lhbQualityView(dayStatus === 'ready' ? day?.quality : null),
    [dayStatus, day],
  )

  /**
   * 参谋交接: 锚点只对入口认的 A 股代码成立; 其它代码不传 symbol(入口会忽略), 只把代码写进问题正文。
   * 两种情况的文案都写明"预填、不自动发送", 不让用户以为已经替他把问题发出去了。
   */
  const handoff: HandoffState = useMemo(
    () =>
      seatTarget
        ? lhbCopilotHandoff({ symbol: seatTarget.symbol, name: seatTarget.name, date: seatTarget.date })
        : { url: '', anchor: false, anchorNote: '' },
    [seatTarget],
  )

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
            {/*
              质量定性常驻: 由后端 quality 决定措辞; 后端没给就是"覆盖未核实"(未知),
              不因有行就改成"完整"。这是页面最容易骗人的地方 —— 有数据时用户更会以为看的是全榜。
            */}
            <StatusChip tone={quality.tone} data-testid="lhb-quality-chip">
              {quality.label}
            </StatusChip>
            <StatusChip tone="info" data-testid="lhb-unit-note">
              金额单位：元（万/亿为换算显示）
            </StatusChip>
            {dayStatus === 'ready' && (
              <span className="text-xs text-muted-foreground">
                上榜 {safeInt(groups.length)} 只 / {safeInt(day?.items.length)} 条原因行
                {latestKnown && latestDate && dataDate === latestDate ? ' · 库内最新交易日' : ''}
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

      {/*
        质量说明放在榜单上方(而不是塞进 chip): 句子较长, 但它是这个页面最重要的诚实标记 ——
        条数、是否可能被截断、后端定性(缺口/风险)三件事必须能被读到。没有 quality 就写"未知"。
      */}
      {coverage && (
        <p
          data-testid="lhb-quality-note"
          className={cn(
            'rounded-lg px-2.5 py-2 text-xs',
            coverage.tone === 'danger'
              ? 'bg-destructive/10 text-destructive'
              : 'bg-role-opp/10 text-role-opp',
          )}
        >
          {coverage.text}
        </p>
      )}

      {/* 后端给的风险提示(早写风险等): 逐条展示, 不合并成一句含糊的"可能有问题" */}
      {quality.risks.length > 0 && (
        <ul data-testid="lhb-quality-risks" className="space-y-1 rounded-lg bg-muted px-2.5 py-2 text-xs text-muted-foreground">
          {quality.risks.map((risk) => (
            <li key={risk}>风险提示：{risk}</li>
          ))}
        </ul>
      )}

      {/*
        交叉提示只说明两张表对不上, 不据此判定任何一方完整 —— 后端 note 原话已经写明,
        这里原样转述, 不改写成"发现问题/数据缺失"。
      */}
      {quality.crossCheck && (
        <p data-testid="lhb-quality-crosscheck" className="rounded-lg bg-s2 px-2.5 py-2 text-xs text-muted-foreground">
          {quality.crossCheck}
        </p>
      )}

      {quality.writeWindow && (
        <p data-testid="lhb-quality-write-window" className="rounded-lg bg-s2 px-2.5 py-2 text-xs text-muted-foreground">
          {quality.writeWindow}
        </p>
      )}

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
                {/*
                  后端 note 里那句"完整榜单(含 ETF/可转债 6 位码)"是无凭据的完整性承诺, 不能替它转述;
                  这里只保留可核对的分句, 空不空的原因仍以后端给的原话为准。
                */}
                {archiveNoteText(day?.note) || '后端未给出空榜原因'}。归档表没有该日记录时不会用其它日期数据顶替。
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
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-muted-foreground">
                  来源日期 {formatTradeDate(seatTarget.date)}
                </span>
                {/*
                  「分析此股龙虎榜」= 带着**已在页面上看到的**代码与交易日去参谋继续追问。
                  不选股就不渲染(没有标的/日期时不造上下文), 地址由 lhbCopilotHandoff 统一拼:
                  只带参谋入口真实生效的 symbol + q; **ETF/可转债不传 symbol**(入口只认 A 股代码,
                  传了会静默忽略, 文案却会承诺一个不存在的锚点)。q 只是预填, 需用户自己点发送。
                */}
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  data-testid="lhb-analyze-copilot"
                  data-anchor={handoff.anchor ? 'true' : 'false'}
                  disabled={!handoff.url}
                  title="打开数智参谋并预填问题（不会自动发送，需你确认后手动发送）"
                  onClick={() => {
                    if (handoff.url) navigate(handoff.url)
                  }}
                >
                  <Sparkles className="h-3.5 w-3.5" /> 分析此股龙虎榜
                </Button>
              </div>
            )}
          </div>

          <p className="rounded-lg bg-muted px-2 py-1.5 text-xs text-muted-foreground" data-testid="lhb-seat-disclaimer">
            {SEAT_IDENTITY_DISCLAIMER}
          </p>

          {seatTarget && (
            <p className="text-xs text-muted-foreground" data-testid="lhb-analyze-hint">
              {handoff.anchor
                ? `将打开数智参谋并预填问题（不会自动发送）：锚点 ${seatTarget.symbol}，日期 ${formatTradeDate(
                    seatTarget.date,
                  )} 写在问题正文里，需你确认后手动发送。本页只展示原始读数，不预置结论。`
                : `将打开数智参谋并预填问题（不会自动发送）。${handoff.anchorNote}代码与日期只写在问题正文里，需你确认后手动发送；本页不预置结论。`}
            </p>
          )}

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
              <SeatSection title="买方席位" seats={seatSplit.buy} side="buy" summary={buySummary} />
              <SeatSection title="卖方席位" seats={seatSplit.sell} side="sell" summary={sellSummary} />
              {seatsHaveMultipleReasons && (
                <p className="rounded-lg bg-muted px-2 py-1.5 text-xs text-muted-foreground" data-testid="lhb-seat-rows-note">
                  同一营业部可能因多条上榜原因重复出现：上方「条记录」是席位明细行数；
                  记录条数不等于独立席位家数，只有身份可核实的席位才按家数统计。
                </p>
              )}
              {(!buySummary.identityVerified || !sellSummary.identityVerified) && (
                <p className="rounded-lg bg-muted px-2 py-1.5 text-xs text-muted-foreground" data-testid="lhb-seat-identity-note">
                  {SEAT_IDENTITY_UNKNOWN_NOTE}
                </p>
              )}
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
