/**
 * K 线形态标注(图层)的**纯逻辑** —— 白名单清洗 + 记号映射。
 *
 * 数据源: `GET /klines/{symbol}/patterns`(后端 `src/core/kline_patterns.py` 的严格规则识别)。
 * 由 `KlineChart` 自取并消费; 本模块不 import 图表库, 可独立单测(纯函数)。
 *
 * 铁律:
 *  - **买红卖绿**: 看涨=红(arrowUp, 标在 K 线下方), 看跌=绿(arrowDown, 标在上方);
 *  - **证据非建议**: 只客观标注形态, 文案不含"建议买入/卖出";
 *  - 缺日期(定位不到 K 线)则不画, 不给假定位; 脏点不进图(白名单过滤)。
 */

export type PatternDirection = 'bullish' | 'bearish' | 'neutral'

export interface PatternMark {
  name: string
  direction: PatternDirection
  /** 形态类别(后端给"反转"/"持续") */
  category?: string
  /** 确认根在序列中的绝对索引 */
  index: number
  date?: string | null
  position?: string | null
  /** 置信依据(满足的严格条件) */
  basis?: string[]
  definition?: string
  span?: [number, number]
}

export const PATTERN_DIRECTION_LABEL: Record<PatternDirection, string> = {
  bullish: '看涨',
  bearish: '看跌',
  neutral: '中性',
}

const DIRECTIONS: readonly PatternDirection[] = ['bullish', 'bearish', 'neutral']

const asString = (v: unknown): string => (typeof v === 'string' ? v : '')

/**
 * 白名单清洗: 脏点不喂图(与 `normalizeKlineEvents` 同思路)。
 * 必填 name(非空) + direction(合法枚举) + index(有限数); 其余字段可缺。
 */
export function normalizePatterns(raw: unknown): PatternMark[] {
  if (!Array.isArray(raw)) return []
  const out: PatternMark[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const it = item as Record<string, unknown>
    const name = asString(it.name).trim()
    const direction = it.direction as PatternDirection
    const index = it.index
    if (!name || !DIRECTIONS.includes(direction)) continue
    if (typeof index !== 'number' || !Number.isFinite(index)) continue
    const basis = Array.isArray(it.basis)
      ? it.basis.filter((b): b is string => typeof b === 'string')
      : []
    const span =
      Array.isArray(it.span) &&
      it.span.length === 2 &&
      it.span.every((n) => typeof n === 'number' && Number.isFinite(n))
        ? ([it.span[0], it.span[1]] as [number, number])
        : undefined
    out.push({
      name,
      direction,
      category: asString(it.category) || undefined,
      index,
      date: typeof it.date === 'string' ? it.date : null,
      position: asString(it.position) || null,
      basis,
      definition: asString(it.definition) || undefined,
      span,
    })
  }
  return out
}

export interface PatternColors {
  up: string
  down: string
  neutral: string
}

export interface PatternMarkerSpec<T> {
  time: T
  position: 'aboveBar' | 'belowBar'
  color: string
  shape: 'arrowUp' | 'arrowDown'
  text: string
}

/** 'YYYY-MM-DD'(前 10 位须是合法日)才可定位到 K 线。 */
function usableDate(date: string | null | undefined): string | null {
  const d = (date || '').slice(0, 10)
  return /^\d{4}-\d{2}-\d{2}$/.test(d) ? d : null
}

/**
 * 形态 → 图表 marker。
 *
 * 按 (交易日 + 方向) 合并: 同一根 K 线上同向的多个形态合成**一个**记号, 文本用 '/' 连接 ——
 * 避免多个 marker 完全重合(与 L4 事件聚合同策略)。
 * 方向映射: 看涨 → 红/下方/arrowUp; 看跌 → 绿/上方/arrowDown; 中性 → 灰/下方/arrowUp。
 * 无合法日期的形态**不画**(不给假定位)。输出按时间升序(图表库要求)。
 */
export function buildPatternMarkers<T>(
  marks: PatternMark[],
  toTime: (date: string) => T,
  colors: PatternColors,
): PatternMarkerSpec<T>[] {
  const grouped = new Map<string, { time: T; direction: PatternDirection; names: string[] }>()
  for (const m of marks) {
    const date = usableDate(m.date)
    if (!date) continue
    const key = `${date}|${m.direction}`
    const g = grouped.get(key)
    if (g) g.names.push(m.name)
    else grouped.set(key, { time: toTime(date), direction: m.direction, names: [m.name] })
  }
  const out: PatternMarkerSpec<T>[] = []
  for (const g of grouped.values()) {
    const bear = g.direction === 'bearish'
    const bull = g.direction === 'bullish'
    out.push({
      time: g.time,
      position: bear ? 'aboveBar' : 'belowBar',
      color: bear ? colors.down : bull ? colors.up : colors.neutral,
      shape: bear ? 'arrowDown' : 'arrowUp',
      text: g.names.join('/'),
    })
  }
  // 图表库要求 marker 按时间升序; 数值时间(s)按数值排, 字符串(ISO)按字典序排
  out.sort((a, b) => {
    const ta: unknown = a.time
    const tb: unknown = b.time
    if (typeof ta === 'number' && typeof tb === 'number') return ta - tb
    const sa = String(ta)
    const sb = String(tb)
    return sa < sb ? -1 : sa > sb ? 1 : 0
  })
  return out
}

/** 悬停读数用: 某交易日命中的形态文案(如 ['红三兵(看涨)']); 证据描述, 无买卖建议。 */
export function patternHoverLabels(marks: PatternMark[], date: string | null): string[] {
  if (!date) return []
  const d = date.slice(0, 10)
  return marks
    .filter((m) => usableDate(m.date) === d)
    .map((m) => `${m.name}(${PATTERN_DIRECTION_LABEL[m.direction]})`)
}
