import { useEffect, useRef } from 'react'
import { boardTag, fmtISODate, prevBoardsLabel, scrollToLatest } from '@panwatch/biz-ui/lib/ladder-format'
import DayCandle, { type DayCandleOHLC } from './DayCandle'

export interface LadderStock {
  symbol: string
  name: string
  candle: DayCandleOHLC | null
  pct?: number | null
  first_time?: string | null
}
export interface LadderRow { boards: number; codes: string[]; names: string[]; tag: string | null; stocks: LadderStock[] }
export interface LadderMark { symbol: string; name: string | null; prev_boards: number | null }
export interface LadderDay {
  date: string
  rows: LadderRow[]
  blown: LadderMark[]
  broken: LadderMark[]
  provisional?: boolean
}

function MarkGroup({ label, marks, tone }: { label: string; marks: LadderMark[]; tone: 'blown' | 'broken' }) {
  if (marks.length === 0) return null
  const cls = tone === 'blown'
    ? 'border-[--stock-up]/40 text-[--stock-up]'
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
    <div className="w-[150px] shrink-0 rounded border border-border/40 p-1.5">
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
              {r.stocks.map((s) => (
                <div key={s.symbol} className="flex w-[46px] flex-col items-center gap-0.5">
                  <DayCandle candle={s.candle} basis={basis} />
                  <span className="w-full truncate text-center text-[10px] text-foreground/80">{s.name}</span>
                  <span className="text-[9px] text-muted-foreground">{s.first_time ?? ''}</span>
                </div>
              ))}
            </div>
          </div>
        ))
      )}
      <MarkGroup label="炸板" marks={day.blown} tone="blown" />
      <MarkGroup label="断板" marks={day.broken} tone="broken" />
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
}) {
  const { ladder, liveDay, mode, stale, lastOk, noteClosing } = props
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => { scrollToLatest(ref.current) }, [ladder, liveDay])
  const cols = [...ladder, ...(liveDay ? [liveDay] : [])]
  return (
    <div className="rounded border border-border/60 p-3">
      <div className="mb-2 flex items-center gap-2">
        <span className="text-[13px] font-semibold">连板梯队</span>
        <span className="text-[10px] text-muted-foreground">
          {mode === 'live' ? '盘中实时(60s)' : '收盘定型'}
        </span>
        {noteClosing ? (
          <span className="text-[10px] text-[--stock-up]">{noteClosing}</span>
        ) : null}
        {stale ? (
          <span className="text-[10px] text-[--stock-up]">实时源中断 {lastOk ?? ''}</span>
        ) : null}
      </div>
      <div ref={ref} className="overflow-x-auto pb-1">
        <div className="flex w-max gap-1">
          {cols.map((d) => (
            <DayColumn
              key={`${d.date}-${d.provisional ? 'live' : 'fin'}`}
              day={d}
              basis={d.provisional ? 'raw' : 'qfq'}
            />
          ))}
        </div>
      </div>
    </div>
  )
}
