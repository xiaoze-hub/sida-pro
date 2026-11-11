import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchAPI } from '@sida/api'
import { ANOMALY_LABEL, formatHeatPct } from '@sida/biz-ui/lib/board-heatmap'
import type { AnomalyKind } from '@sida/biz-ui/lib/board-heatmap'
import { safeFixed, safeNum } from '@/lib/format'

/**
 * 板块异动记录 (2026-11-11): 热力图下方的**带时间的历史记录**。
 *
 * 与顶部 `heatmap-anomalies` 芯片的分工(两者都在, 不互相替代):
 * - 芯片 = 当前快照的瞬时命中(`detectHeatAnomaly` 现算), **没有时间轴**, 收盘后归零;
 * - 本面板 = 后端采样的**历史序列** `GET /boards/anomalies`, 有 ts/窗口/当时的涨幅,
 *   盘中错过一眼还能回看「10:32 半导体急拉」。
 *
 * 口径纪律(不许拿当前快照伪造历史):
 * - 只渲染后端返回的 items; 没有就如实说没有, 不用热力图 items 拼一行假记录。
 * - 覆盖区间/采样次数来自响应 `coverage`, samples===0 时明说「暂无采样」。
 * - 接口失败只让**本面板**降级成一行说明, 不阻断热力图渲染(组件自己取数, 与图表解耦)。
 */

export type BoardAnomalyBoardType = 'industry' | 'concept' | 'all'

export interface BoardAnomalyItem {
  ts: string
  block_code: string
  name: string
  board_type?: string
  kind: AnomalyKind
  /** 后端给的中文短标签(可省, 缺省用 ANOMALY_LABEL) */
  label?: string | null
  /** 涨速 %(surge/dump) 或量比倍数(heavy_volume); 缺失为 null */
  value: number | null
  /** 涨速的观察窗口(分钟), 仅 surge/dump 有意义 */
  window_min?: number | null
  change_pct: number | null
  fund_net?: number | null
}

export interface BoardAnomalyCoverage {
  samples: number
  first_ts: string | null
  last_ts: string | null
  boards?: number
  interval_s?: number
}

export interface BoardAnomalyResp {
  available: boolean
  trade_date: string | null
  source?: string
  coverage?: BoardAnomalyCoverage | null
  count: number
  items: BoardAnomalyItem[]
  note?: string | null
}

export interface BoardAnomalyLogProps {
  boardType: BoardAnomalyBoardType
  onOpenBoard: (blockCode: string, name: string) => void
  /**
   * 刷新信号(热力图传 `dataSeq` —— 图表**成功取数**的序号): 变化即重取一次。
   * 面板不自建 60s 定时器: 图表每次成功取数(含 60s 轮询/手动刷新)都会带动这里,
   * 两边节拍同一份; 图表失败时也不会多打一次后端。挂载时先自己取一次, 不等图表。
   */
  refreshKey?: number
  className?: string
}

/** 一次拉取条数(后端契约默认 50) */
const LIMIT = 50
/** UI 行数上限: 50 条全铺会把页面撑长, 超出明说「还有 N 条」 */
const MAX_ROWS = 40

const LABEL_TONE: Record<AnomalyKind, string> = {
  // A 股红涨绿跌: 急拉=红, 急跌=绿; 放量是中性警示 → amber(不与涨跌抢夺语义)
  surge: 'text-stock-up',
  dump: 'text-stock-down',
  heavy_volume: 'text-amber-500',
}

/** UTC/东八区 ISO → 记录页上的 HH:MM */
function fmtHm(iso: string | null | undefined): string {
  if (!iso) return ''
  // 时间戳带 +08:00 偏移, 直接取 ISO 里的钟点, **不做时区换算** ——
  // 否则用户不在东八区时记录时间会整体平移, 与行情时段对不上。
  const m = /T(\d{2}:\d{2})/.exec(iso)
  if (m) return m[1]
  const d = new Date(iso)
  if (!isFinite(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}`
}

function todayLocal(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** 类型标签: 后端 label 优先(可含「急拉 · 放量」这类组合), 缺省走本地枚举 */
function kindLabel(item: BoardAnomalyItem): string {
  const lbl = (item.label ?? '').trim()
  if (lbl) return lbl
  return ANOMALY_LABEL[item.kind] ?? '异动'
}

/**
 * 数值口径: 涨速带**方向符号**(方向由 kind 定, 不信 value 的符号 → 负值的 dump 也不会显示成 +);
 * 放量显示倍数。数值缺失一律 '--', 不补 0(补 0 等于编造一次「0.00% 的急拉」)。
 */
function valueText(item: BoardAnomalyItem): string {
  const magnitude = safeNum(item.value)
  if (magnitude === null) return '--'
  const win = safeNum(item.window_min)
  const winText = win !== null && win > 0 ? ` / ${safeFixed(win, 0)}分钟` : ''
  if (item.kind === 'heavy_volume') return `量比 ${safeFixed(Math.abs(magnitude), 1)}x`
  const signed = `${item.kind === 'dump' ? '-' : '+'}${safeFixed(Math.abs(magnitude), 2)}%`
  return `${item.kind === 'dump' ? '跌速' : '涨速'} ${signed}${winText}`
}

/** 响应形状校验: 不合契约(缺 available/items)按失败处理, 不当空列表糊过去。 */
function asResp(raw: unknown): BoardAnomalyResp {
  const r = raw as Partial<BoardAnomalyResp> | null | undefined
  if (!r || typeof r !== 'object' || typeof r.available !== 'boolean' || !Array.isArray(r.items)) {
    throw new Error('板块异动记录响应格式异常')
  }
  return r as BoardAnomalyResp
}

export default function BoardAnomalyLog({
  boardType,
  onOpenBoard,
  refreshKey = 0,
  className,
}: BoardAnomalyLogProps) {
  /** 记录归属哪个分类: 切页签时旧分类的记录不能冒充新分类(与 BoardHeatmap 同一纪律) */
  const [loaded, setLoaded] = useState<{ type: BoardAnomalyBoardType; resp: BoardAnomalyResp } | null>(null)
  const [failedType, setFailedType] = useState<BoardAnomalyBoardType | null>(null)

  const onOpenRef = useRef(onOpenBoard)
  useEffect(() => {
    onOpenRef.current = onOpenBoard
  }, [onOpenBoard])

  const aliveRef = useRef(true)
  const seqRef = useRef(0)
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

  const load = useCallback(async () => {
    const seq = ++seqRef.current
    try {
      const raw = await fetchAPI<BoardAnomalyResp>(`/boards/anomalies?type=${boardType}&limit=${LIMIT}`)
      if (!aliveRef.current || seq !== seqRef.current) return
      setLoaded({ type: boardType, resp: asResp(raw) })
      setFailedType(null)
    } catch {
      if (!aliveRef.current || seq !== seqRef.current) return
      // 只降级本面板: 热力图有它自己的请求与错误态, 不被异动接口连坐。
      setFailedType(boardType)
    }
  }, [boardType])

  // 挂载 / 切分类(boardType 变 → load 变) / 图表拿到新数据(refreshKey 变) 都重取一次;
  // 轮询节奏由 BoardHeatmap 的 60s 闸门驱动, 本组件不另开定时器。
  useEffect(() => {
    void load()
  }, [load, refreshKey])

  const resp = loaded && loaded.type === boardType ? loaded.resp : null
  const error = failedType === boardType

  const coverage = resp?.coverage ?? null
  const samples = safeNum(coverage?.samples)
  const showCoverage = resp?.available === true && samples !== null && samples > 0
  const dayLabel = (() => {
    const d = resp?.trade_date
    if (!d) return ''
    return d === todayLocal() ? '今日' : d
  })()
  const coverageText = (() => {
    if (!showCoverage) return ''
    const first = fmtHm(coverage?.first_ts)
    const last = fmtHm(coverage?.last_ts)
    const span = first && last ? `${first}–${last}` : ''
    const parts = [dayLabel, span].filter(Boolean).join(' ')
    return `${parts ? `${parts} · ` : ''}采样 ${safeFixed(samples, 0)} 次`
  })()

  const items = resp?.available === true ? resp.items : []
  const total = Math.max(safeNum(resp?.count) ?? items.length, items.length)
  const visible = items.slice(0, MAX_ROWS)
  const more = total - visible.length

  const shell = `mt-4 border-t border-border pt-3 ${className ?? ''}`.trim()

  const statusLine = (text: string, extra?: string | null) => (
    <p className="mt-1 text-[12px] text-muted-foreground" data-testid="board-anomaly-log-status">
      {text}
      {extra ? <span className="ml-1">{extra}</span> : null}
    </p>
  )

  return (
    <section aria-label="板块异动记录" data-testid="board-anomaly-log" className={shell}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h3 className="text-[14px] font-semibold">板块异动记录</h3>
        {/* 覆盖说明是这张表可信度的前提: 没有它, 读者无从判断「空」是没异动还是没采样 */}
        {coverageText ? (
          <span className="text-[12px] text-muted-foreground" data-testid="board-anomaly-coverage">
            {coverageText}
          </span>
        ) : null}
        {resp?.available === true && items.length > 0 && total > items.length ? (
          <span className="ml-auto text-[12px] text-muted-foreground">今日共 {total} 条</span>
        ) : null}
      </div>

      {error ? (
        statusLine('异动记录暂不可用')
      ) : resp === null ? (
        statusLine('异动记录加载中…')
      ) : resp.available !== true ? (
        statusLine('异动记录暂不可用', resp.note ?? null)
      ) : items.length === 0 ? (
        statusLine(
          samples === 0
            ? '今日暂无采样（交易时段每 2 分钟采样一次）'
            : '今日暂无板块异动记录',
        )
      ) : (
        <>
          <div className="mt-1.5 divide-y divide-border">
            {/* 表头: 列宽必须与行完全一致, 改一处要两处一起改(否则数值列对不齐) */}
            <div className="flex w-full items-center gap-2 pb-1 text-[12px] text-muted-foreground">
              <span className="w-[42px] shrink-0">时间</span>
              <span className="min-w-0 flex-1">板块</span>
              <span className="w-[52px] shrink-0">类型</span>
              <span className="w-[128px] shrink-0 text-right">数值</span>
              <span className="w-[58px] shrink-0 text-right">当前涨幅</span>
            </div>
            {visible.map((item) => {
              const cp = safeNum(item.change_pct)
              return (
                <button
                  key={`${item.ts}-${item.block_code}-${item.kind}`}
                  type="button"
                  data-testid="board-anomaly-row"
                  title={`${fmtHm(item.ts)} ${item.name} ${kindLabel(item)} ${valueText(item)}`}
                  onClick={() => onOpenRef.current(item.block_code, item.name)}
                  className="flex w-full items-center gap-2 py-1 text-left text-[12px] transition-colors hover:bg-s2"
                >
                  <span className="w-[42px] shrink-0 font-mono text-muted-foreground">{fmtHm(item.ts) || '--'}</span>
                  <span className="min-w-0 flex-1 truncate font-medium" title={item.name}>
                    {item.name}
                  </span>
                  <span className={`w-[52px] shrink-0 ${LABEL_TONE[item.kind] ?? 'text-muted-foreground'}`}>
                    {kindLabel(item)}
                  </span>
                  <span className="w-[128px] shrink-0 text-right font-mono">{valueText(item)}</span>
                  <span
                    className={`w-[58px] shrink-0 text-right font-mono ${
                      cp === null ? 'text-muted-foreground' : cp >= 0 ? 'text-stock-up' : 'text-stock-down'
                    }`}
                  >
                    {formatHeatPct(item.change_pct)}
                  </span>
                </button>
              )
            })}
          </div>
          {more > 0 ? (
            <p className="mt-1 text-[12px] text-muted-foreground" data-testid="board-anomaly-more">
              还有 {more} 条
            </p>
          ) : null}
          {resp.note ? <p className="mt-1 text-[12px] text-muted-foreground">{resp.note}</p> : null}
        </>
      )}
    </section>
  )
}
