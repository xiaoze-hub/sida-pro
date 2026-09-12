import { useEffect, useMemo, useRef, useState } from 'react'
import { boardTag, fmtAmount, fmtISODate, fmtPct, prevBoardsLabel, scrollToLatest } from '@panwatch/biz-ui/lib/ladder-format'
import DayCandle, { type DayCandleOHLC } from './DayCandle'

export interface LadderStock {
  symbol: string
  name: string
  candle: DayCandleOHLC | null
  pct?: number | null
  first_time?: string | null
  amount?: number | null
  tag?: string | null
  seal_tag?: string | null
  dive?: boolean | null
  boards_vendor?: number | null
  plate_type?: string | null
  open_count?: number | null
  last_sealed?: string | null
  seal_amount?: number | null
  seal_ratio?: number | null
}
export interface LadderRow { boards: number; codes: string[]; names: string[]; tag: string | null; stocks: LadderStock[] }
export interface LadderMark extends LadderStock { prev_boards: number | null }
export interface LadderDay {
  date: string
  rows: LadderRow[]
  blown: LadderMark[]
  broken: LadderMark[]
  charging?: LadderMark[]
  provisional?: boolean
}
export interface LadderStats {
  prev_candidates: number
  first: number
  promoted: number
  blown: number
  broken: number
  charging: number
}

function pctClass(p: number | null | undefined): string {
  if (p == null) return 'text-muted-foreground'
  return p >= 0 ? 'text-[--stock-up]' : 'text-[--stock-down]'
}

/** 每股 tooltip: 状态/板型/开板/尾封/封单/封成比/额, 缺则不显示(不编)。 */
function stockTitle(s: LadderStock): string {
  const parts = [s.name || s.symbol]
  if (s.tag) parts.push(s.tag)
  if (s.plate_type) parts.push(s.plate_type)
  parts.push(fmtPct(s.pct))
  if (s.first_time) parts.push(`首封 ${s.first_time}`)
  if (s.last_sealed) parts.push(`尾封 ${s.last_sealed}`)
  if (s.open_count) parts.push(`开板 ${s.open_count}次`)
  if (s.seal_tag) parts.push(`盘口 ${s.seal_tag}`)
  if (s.dive) parts.push('跳水')
  if (s.boards_vendor != null) parts.push(`vendor连板 ${s.boards_vendor}`)
  if (s.seal_amount != null) parts.push(`封单 ${fmtAmount(s.seal_amount)}`)
  if (s.seal_ratio != null) parts.push(`封成比 ${fmtPct(s.seal_ratio * 100)}`)
  if (s.amount != null) parts.push(`额 ${fmtAmount(s.amount)}`)
  return parts.join(' · ')
}

function StockChip({ s, basis = 'qfq' }: { s: LadderStock; basis?: 'qfq' | 'raw' }) {
  return (
    <span
      title={stockTitle(s)}
      className="flex w-[52px] flex-col items-center gap-0.5 rounded border border-border/30 px-0.5 py-0.5"
    >
      <DayCandle candle={s.candle} basis={basis} />
      <span className="w-full truncate text-center text-[10px] text-foreground/80">{s.name}</span>
      <span className={`text-[9px] ${pctClass(s.pct)}`}>{fmtPct(s.pct)}</span>
      {s.tag ? (
        <span className="rounded bg-accent/50 px-0.5 text-[8px] text-muted-foreground">{s.tag}</span>
      ) : null}
      {s.seal_tag && s.seal_tag !== s.tag ? (
        <span className="rounded bg-accent/40 px-0.5 text-[8px] text-muted-foreground">{s.seal_tag}</span>
      ) : null}
      {s.dive ? (
        <span className="rounded bg-[--stock-down]/20 px-0.5 text-[8px] text-[--stock-down]">跳水</span>
      ) : null}
    </span>
  )
}

function MarkGroup({ label, marks, tone }: { label: string; marks: LadderMark[]; tone: 'blown' | 'broken' | 'charging' }) {
  if (!marks || marks.length === 0) return null
  const cls = tone === 'blown'
    ? 'border-[--stock-up]/40 text-[--stock-up]'
    : tone === 'charging'
      ? 'border-[--stock-up]/30 text-[--stock-up]/80'
      : 'border-border/50 text-muted-foreground'
  return (
    <div className={`mt-1 rounded border px-1 py-0.5 ${cls}`}>
      <div className="text-[10px] font-medium">{label} {marks.length}</div>
      <div
        title={marks.map((m) => `${m.name || m.symbol}(${prevBoardsLabel(m.prev_boards)})`).join('、')}
        className="truncate text-[10px]"
      >
        {marks.slice(0, 3).map((m) => m.name || m.symbol).join('、')}
        {marks.length > 3 ? ` +${marks.length - 3}` : ''}
      </div>
    </div>
  )
}

function DayColumn({ day, basis }: { day: LadderDay; basis: 'qfq' | 'raw' }) {
  const sealed = day.rows.reduce((a, r) => a + r.codes.length, 0)
  return (
    <div className="w-[170px] shrink-0 rounded border border-border/40 p-1.5">
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-[11px] font-medium text-foreground/85">{fmtISODate(day.date)}</span>
        <span className="text-[10px] text-muted-foreground">封 {sealed}</span>
      </div>
      {day.provisional ? (
        <div className="mb-1 inline-block rounded bg-primary/15 px-1 text-[9px] text-primary">盘中</div>
      ) : null}
      {day.rows.length === 0 && day.blown.length === 0 && day.broken.length === 0 ? (
        <div className="py-2 text-center text-[10px] text-muted-foreground">无收盘封板</div>
      ) : (
        day.rows.map((r) => (
          <div key={r.boards} className="mb-1 last:mb-0">
            <div className="text-[10px] font-medium text-primary">
              {r.boards}板
              {r.tag ?? boardTag(r.boards)
                ? <span className="ml-1 rounded bg-primary/15 px-0.5 text-[9px]">{r.tag ?? boardTag(r.boards)}</span>
                : null}
            </div>
            <div className="flex flex-wrap gap-1">
              {r.stocks.map((s) => <StockChip key={s.symbol} s={s} basis={basis} />)}
            </div>
          </div>
        ))
      )}
      <MarkGroup label="炸板" marks={day.blown} tone="blown" />
      <MarkGroup label="断板" marks={day.broken} tone="broken" />
      <MarkGroup label="冲板" marks={day.charging ?? []} tone="charging" />
    </div>
  )
}

/** 矩阵视图(借鉴 quicktiny 多日天梯): 行=板高, 列=日期, 格=该(日,板高)个股。 */
function MatrixView({ cols, collapsed, onlyBoard, onToggleRow, onOnlyBoard }: {
  cols: LadderDay[]
  collapsed: Set<number>
  onlyBoard: number | null
  onToggleRow: (b: number) => void
  onOnlyBoard: (b: number | null) => void
}) {
  const boards = useMemo(() => {
    const set = new Set<number>()
    cols.forEach((d) => d.rows.forEach((r) => set.add(r.boards)))
    return [...set].sort((a, b) => b - a)
  }, [cols])
  const shown = onlyBoard != null ? boards.filter((b) => b === onlyBoard) : boards
  return (
    <div className="overflow-x-auto pb-1">
      <div className="flex w-max gap-1">
        <div className="w-[64px] shrink-0">
          <div className="mb-1 text-[10px] text-muted-foreground">层级</div>
          {shown.map((b) => (
            <div key={b} className="mb-1 flex h-[64px] flex-col justify-center">
              <button
                type="button"
                onClick={() => onOnlyBoard(onlyBoard === b ? null : b)}
                className={`rounded px-1 text-[10px] font-medium ${
                  onlyBoard === b ? 'bg-primary text-primary-foreground' : 'bg-accent/50 text-foreground/80'
                }`}
                title={onlyBoard === b ? '取消只看本板' : '只看本板'}
              >
                {b}板
              </button>
              <button
                type="button"
                onClick={() => onToggleRow(b)}
                className="mt-0.5 text-[9px] text-muted-foreground"
              >
                {collapsed.has(b) ? '展开' : '收起'}
              </button>
            </div>
          ))}
        </div>
        {cols.map((d) => (
          <div key={d.date} className="w-[170px] shrink-0">
            <div className="mb-1 truncate text-[10px] font-medium text-foreground/85">
              {fmtISODate(d.date)}
              {d.provisional ? <span className="ml-1 text-primary">盘中</span> : null}
            </div>
            {shown.map((b) => {
              const row = d.rows.find((r) => r.boards === b)
              return (
                <div key={b} className="mb-1 flex h-[64px] flex-wrap content-start gap-1 overflow-hidden rounded border border-border/30 p-0.5">
                  {!collapsed.has(b) && row
                    ? row.stocks.map((s) => (
                      <StockChip key={s.symbol} s={s} basis={d.provisional ? 'raw' : 'qfq'} />
                    ))
                    : null}
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}

export default function LadderBoard(props: {
  ladder: LadderDay[]
  liveDay: LadderDay | null
  mode: string
  stale: boolean
  lastOk: string | null
  noteClosing?: string | null
  stats?: LadderStats | null
  asOf?: string | null
}) {
  const { ladder, liveDay, mode, stale, lastOk, noteClosing, stats, asOf } = props
  const ref = useRef<HTMLDivElement>(null)
  const [view, setView] = useState<'cols' | 'matrix'>('cols')
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set())
  const [onlyBoard, setOnlyBoard] = useState<number | null>(null)
  useEffect(() => { scrollToLatest(ref.current) }, [ladder, liveDay, view])
  const cols = [...ladder, ...(liveDay ? [liveDay] : [])]
  const toggleRow = (b: number) => setCollapsed((prev) => {
    const next = new Set(prev)
    if (next.has(b)) next.delete(b)
    else next.add(b)
    return next
  })
  return (
    <div className="rounded border border-border/60 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-semibold">连板梯队</span>
        <span className="text-[10px] text-muted-foreground">
          {mode === 'live' ? '盘中实时(60s)' : '收盘定型'}
        </span>
        {stats ? (
          <span className="text-[10px] text-muted-foreground">
            昨候选 {stats.prev_candidates} · 首板 {stats.first} · 晋级 {stats.promoted} ·
            炸板 {stats.blown} · 断板 {stats.broken} · 冲板 {stats.charging}
          </span>
        ) : null}
        <span className="ml-auto flex items-center gap-2">
          {asOf ? <span className="text-[9px] text-muted-foreground">更新 {asOf.slice(11, 19)}</span> : null}
          <button
            type="button"
            onClick={() => setView(view === 'cols' ? 'matrix' : 'cols')}
            className="rounded border border-border/50 px-1.5 py-0.5 text-[10px] text-muted-foreground"
          >
            {view === 'cols' ? '矩阵视图' : '按日列视图'}
          </button>
        </span>
      </div>
      {noteClosing ? (
        <div className="mb-1 text-[10px] text-[--stock-up]">{noteClosing}</div>
      ) : null}
      {stale ? (
        <div className="mb-1 text-[10px] text-[--stock-up]">实时源中断 {lastOk ?? ''}</div>
      ) : null}
      <div ref={ref}>
        {view === 'cols' ? (
          <div className="overflow-x-auto pb-1">
            <div className="flex w-max gap-1">
              {cols.map((d) => (
                <DayColumn key={`${d.date}-${d.provisional ? 'live' : 'fin'}`} day={d} basis={d.provisional ? 'raw' : 'qfq'} />
              ))}
            </div>
          </div>
        ) : (
          <MatrixView cols={cols} collapsed={collapsed} onlyBoard={onlyBoard}
            onToggleRow={toggleRow} onOnlyBoard={setOnlyBoard} />
        )}
      </div>
    </div>
  )
}
