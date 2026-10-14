/**
 * 图表说明(chart-notes)的**纯逻辑** —— 白名单清洗 + 「为什么」文案 + 图下说明条。
 *
 * 数据源: `GET /klines/{symbol}/chart-notes`(后端 `src/core/chart_notes.py`)。
 * 产品口径(2026-10-14): 看图决策 = 提供图表说明供用户和 AI 共同决策 ——
 * 图上每个标记都要能回答「**为什么在那**」(触发条件 / 数据时点 / 失效条件 / 置信度)。
 *
 * 铁律(与后端 chart_notes 单一事实源逐字一致):
 *  - AI 读的 = 图上标的: 本模块只**清洗/展示**后端给的说明, **不重算**任何标记或口径;
 *  - 缺数据显式(「时点缺失」/「无可用触发条件」), **不猜、不补 0、不编**;
 *  - 说明是**证据语言**, 不是买卖建议 —— 文案不含「建议买入/卖出」。
 *
 * 本模块不 import 图表库, 可独立单测(纯函数)。
 */

export interface ChartNote {
  kind: string
  markerType: string
  label: string
  date: string | null
  price: number | null
  side?: string | null
  index?: number | null
  /** 触发条件(哪几个指标/条件成立) —— 确定性来自后端, 不由 LLM 编 */
  trigger: string[]
  asOf: string | null
  asOfIsToday: boolean
  /** 数据时点说明(「数据时点: …(今日/非今日)」或「时点缺失」) */
  asOfNote: string
  /** 失效条件(永不为空; 缺失回兜底并标 defaulted) */
  invalidation: string[]
  invalidationDefaulted: boolean
  confidence: Record<string, unknown>
  id: string
}

export interface ChartNotesPayload {
  notes: ChartNote[]
  zoneNote: ChartNote | null
  count: number
  notesText: string[]
}

const asString = (v: unknown): string => (typeof v === 'string' ? v : '')
const asStrArr = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x.length > 0) : []

/**
 * 白名单清洗: 脏点不进图/不进条。
 * 必填 kind(非空) + trigger(非空数组); 其余字段可缺(缺 → 显式 null/空, 不补假值)。
 * 与后端契约字段一一对应(见 src/core/chart_notes.py::build_note)。
 */
export function normalizeChartNotes(raw: unknown): ChartNote[] {
  if (!Array.isArray(raw)) return []
  const out: ChartNote[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const it = item as Record<string, unknown>
    const kind = asString(it.kind).trim()
    const trigger = asStrArr(it.trigger)
    if (!kind || trigger.length === 0) continue
    const price = it.price
    out.push({
      kind,
      markerType: asString(it.marker_type) || kind,
      label: asString(it.label),
      date: typeof it.date === 'string' ? it.date : null,
      price: typeof price === 'number' && Number.isFinite(price) ? price : null,
      side: typeof it.side === 'string' ? it.side : null,
      index: typeof it.index === 'number' && Number.isFinite(it.index) ? it.index : null,
      trigger,
      asOf: typeof it.as_of === 'string' ? it.as_of : null,
      asOfIsToday: it.as_of_is_today === true,
      asOfNote: asString(it.as_of_note),
      invalidation: asStrArr(it.invalidation),
      invalidationDefaulted: it.invalidation_defaulted === true,
      confidence: (it.confidence && typeof it.confidence === 'object'
        ? (it.confidence as Record<string, unknown>)
        : {}) as Record<string, unknown>,
      id: asString(it.id),
    })
  }
  return out
}

/** 后端整包 → 清洗后的载荷(脏值过滤, 缺则空; 不编造)。 */
export function normalizeChartNotesPayload(raw: unknown): ChartNotesPayload {
  const obj = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const notes = normalizeChartNotes(obj.notes)
  const zoneRaw = obj.zone_note
  const zoneNotes = normalizeChartNotes(zoneRaw ? [zoneRaw] : [])
  const notesText = asStrArr(obj.notes_text)
  return {
    notes,
    zoneNote: zoneNotes[0] ?? null,
    count: typeof obj.count === 'number' ? obj.count : notes.length,
    notesText: notesText.length ? notesText : notes.map(markerWhy),
  }
}

/** 说明 → 一行式「为什么」(悬停浮层 / 图例 title / 图下说明条共用)。 */
export function markerWhy(note: ChartNote): string {
  const trig = note.trigger.length ? note.trigger.join('、') : '触发条件缺失'
  const asOf = note.asOfNote || '数据时点缺失'
  const inval = note.invalidation.length ? note.invalidation.join('、') : '失效条件缺失'
  return `触发: ${trig} · 时点: ${asOf} · 失效: ${inval}`
}

/** 该交易日的说明(供悬停读数「为什么」)。date 取前 10 位(与 K 线日期对齐)。 */
export function notesForDate(notes: ChartNote[], date: string | null | undefined): ChartNote[] {
  const d = (date || '').slice(0, 10)
  if (!d) return []
  return notes.filter((n) => (n.date || '').slice(0, 10) === d)
}

/**
 * 图下说明条(一行式小字, **非卡片**): 取最近 `limit` 条说明拼句。
 * 无说明 → 空串(调用方据此**不渲染**, 不留空壳)。
 */
export function notesBarText(notes: ChartNote[], limit = 3): string {
  const rows = notes.slice(Math.max(0, notes.length - limit))
  return rows
    .map((n) => {
      const head = [n.date, n.markerType, n.label].filter(Boolean).join(' · ')
      return `${head} —— ${markerWhy(n)}`
    })
    .join('；')
}

/** 区(当前 GS 标记)说明 → 一行式小字(与推送/战报同源取句的展示侧)。 */
export function zoneNoteText(note: ChartNote | null): string {
  if (!note) return ''
  return `${note.label} —— ${markerWhy(note)}`
}

/**
 * 按 (类型 + 交易日 + 标签前缀) 找对应说明 —— 供图例逐项挂 `title`(悬停「为什么」)。
 * 找不到 → null(该图例项不挂 title, 不编造)。
 */
export function noteForMarker(
  notes: ChartNote[],
  kind: string,
  date: string | null | undefined,
  labelPrefix?: string,
): ChartNote | null {
  const d = (date || '').slice(0, 10)
  const pref = (labelPrefix || '').trim()
  const hit = notes.find(
    (n) =>
      n.kind === kind &&
      (n.date || '').slice(0, 10) === d &&
      (!pref || n.label.startsWith(pref)),
  )
  return hit ?? null
}
