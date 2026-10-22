/**
 * 情景关键位图层(2026-10-22)—— **纯逻辑**: 后端情景矩阵 → K 线主图水平线。
 *
 * 数据源: `GET /klines/{symbol}/scenario`(后端 `src/core/scenario_engine.py::build_matrix`)。
 * 该端点与 chat 工具 `get_scenario_matrix` 同一套计算 —— 前端只**画后端算好的关键位**,
 * 不在前端重算方向(铁律: 不预测方向, 只给条件-反应; 前端不得出现第二套口径)。
 *
 * 图上元素(共 2~4 条水平线, 不做卡片、不挡 K 线):
 *   - 阻力   (levels.resist)          → rose  涨红
 *   - 支撑   (levels.support)         → emerald 跌绿
 *   - 走强目标(走强情景的 target)      → rose  涨红
 *   - 走弱目标(走弱情景的 target)      → emerald 跌绿
 *
 * 颜色语义: **中国习惯买红卖绿** —— 阻力/走强目标=红系(涨), 支撑/走弱目标=绿系(跌)。
 * `color` 存的是**语义色名**('rose'/'emerald'), DOM 图例走 Tailwind 类, 画布(canvas 不认
 * Tailwind 类)走 `readScenarioLineColors()` 读 CSS 变量 ⇒ 主题切换自动跟随。
 *
 * 清洗规则(脏数据不进图, 铁律"缺数据不编造"):
 *   1. 非法值: NaN / Infinity / <= 0 / 目标位是"下方无明确支撑"这类非数值 → 丢弃;
 *   2. 极值: 与现价偏离 > 50%(SCENARIO_MAX_DEVIATION) → 视为脏数据丢弃;
 *   3. 去重: 同价位(2 位小数口径)只留一条 —— 优先级 阻力 > 支撑 > 走强目标 > 走弱目标
 *      (结构性关键位优先于情景目标位);
 *   4. 排序: 价格从高到低(图上从上往下读)。
 *
 * 本模块不 import 图表库、不碰 DOM ⇒ 可独立单测(vitest 纯函数用例)。
 */

import type { ScenarioMatrixResponse, ScenarioItem } from '@panwatch/api'

/** 关键位种类(4 种, 与后端 levels/scenarios 一一对应)。 */
export type ScenarioLineKind = 'resist' | 'support' | 'target_strong' | 'target_weak'

/** 语义色名(涨红/跌绿)。DOM 走 Tailwind 类, 画布走 readScenarioLineColors()。 */
export type ScenarioLineTone = 'rose' | 'emerald'

export interface ScenarioLine {
  /** 价位(元) */
  price: number
  kind: ScenarioLineKind
  /** 图上 title 文案(阻力/支撑/走强目标/走弱目标) */
  label: string
  /** 语义色名 */
  color: ScenarioLineTone
}

/** 图上 title 文案 —— 与图例、悬停说明共用一套, 不各写一份。 */
export const SCENARIO_LINE_LABEL: Record<ScenarioLineKind, string> = {
  resist: '阻力',
  support: '支撑',
  target_strong: '走强目标',
  target_weak: '走弱目标',
}

/** 颜色语义映射: 阻力/走强目标=红(涨), 支撑/走弱目标=绿(跌)—— 买红卖绿。 */
export const SCENARIO_LINE_TONE: Record<ScenarioLineKind, ScenarioLineTone> = {
  resist: 'rose',
  support: 'emerald',
  target_strong: 'rose',
  target_weak: 'emerald',
}

/** 与现价偏离超过该比例的价位视为脏数据(丢弃, 不进图)。 */
export const SCENARIO_MAX_DEVIATION = 0.5

/**
 * 去重优先级: 同价位时保留**先出现**的那条 —— 结构性关键位(阻力/支撑)优先于情景目标位。
 * 候选按这个顺序构造, 所以只要"先加入"即可实现优先级。
 */
const KIND_PRIORITY: readonly ScenarioLineKind[] = ['resist', 'support', 'target_strong', 'target_weak']

/**
 * 返回数组, 同时挂两个**非枚举**属性(不干扰 `toEqual([...])` 断言):
 *  - `dropped`: 本次丢弃的脏值个数(诊断用);
 *  - `lines`: 自引用(方便 `const { lines, dropped } = buildScenarioLines(resp)` 这种解构写法)。
 */
export type ScenarioLines = ScenarioLine[] & { dropped: number; lines: ScenarioLine[] }

/** 有限正数(字符串数字也吃, 但空串/NaN/<=0 一律判非法)。 */
function finitePositive(v: unknown): number | null {
  let n: number
  if (typeof v === 'number') n = v
  else if (typeof v === 'string') {
    const t = v.trim()
    if (!t) return null
    n = Number(t)
  } else return null
  if (!Number.isFinite(n) || n <= 0) return null
  return n
}

/** 情景 target 可能是数字, 也可能是 "32.5" / "下方无明确支撑(需重新评估)" 这类字符串。 */
function targetNumber(item: ScenarioItem): number | null {
  const t = item?.target
  if (typeof t === 'number') return finitePositive(t)
  if (typeof t === 'string') {
    const m = /-?\d+(?:\.\d+)?/.exec(t)
    if (!m) return null
    return finitePositive(Number(m[0]))
  }
  return null
}

/**
 * 情景档位识别 —— 后端两种字段形态都吃:
 *  - 结构化形态: `{id: 'strong'|'flat'|'weak'}`;
 *  - 既有形态:   `{name: '走强'|'震荡'|'走弱'}`(前端示例里的 `label` 同义)。
 */
function scenarioRole(item: ScenarioItem): 'strong' | 'weak' | null {
  const id = String(item?.id ?? '').trim().toLowerCase()
  if (id === 'strong' || id === 'weak') return id
  const name = String(item?.name || item?.label || '').trim()
  if (name === '走强') return 'strong'
  if (name === '走弱') return 'weak'
  return null
}

export interface ScenarioLineResult {
  lines: ScenarioLine[]
  /** 被丢弃的脏值个数(非法价/极值/不可解析的目标位) */
  dropped: number
}

/**
 * 核心实现(返回 lines + dropped 两个字段)。
 *
 * `available !== true` → 空数组(静默不画, 不弹错、不编造关键位)。
 */
export function buildScenarioLineResult(resp: ScenarioMatrixResponse | null | undefined): ScenarioLineResult {
  if (!resp || resp.available !== true) return { lines: [], dropped: 0 }

  const levels = (resp.levels || {}) as Record<string, unknown>
  const scenarios = Array.isArray(resp.scenarios) ? resp.scenarios : []

  // 候选: 结构性关键位 + 两档目标位。目标位只在后端真给了该档情景时才算候选
  // (没给 = 不是"脏值", 不计入 dropped)。
  const raw: Array<{ kind: ScenarioLineKind; price: number | null }> = []
  raw.push({ kind: 'resist', price: finitePositive(levels.resist) })
  raw.push({ kind: 'support', price: finitePositive(levels.support) })

  let strong: number | null = null
  let weak: number | null = null
  let strongSeen = false
  let weakSeen = false
  for (const item of scenarios) {
    const role = scenarioRole(item)
    if (role === 'strong' && !strongSeen) {
      strongSeen = true
      strong = targetNumber(item)
    } else if (role === 'weak' && !weakSeen) {
      weakSeen = true
      weak = targetNumber(item)
    }
  }
  if (strongSeen) raw.push({ kind: 'target_strong', price: strong })
  if (weakSeen) raw.push({ kind: 'target_weak', price: weak })

  // 现价(用于极值判定; 取不到 = 不做偏离判定, 只过 NaN/<=0 那关)
  const current = finitePositive(resp.price ?? levels.price)

  let dropped = 0
  const kept: ScenarioLine[] = []
  for (const cand of raw) {
    const p = cand.price
    if (p == null) {
      dropped++
      continue
    }
    if (current != null && Math.abs(p - current) / current > SCENARIO_MAX_DEVIATION) {
      dropped++ // 极值脏数据
      continue
    }
    kept.push({
      price: p,
      kind: cand.kind,
      label: SCENARIO_LINE_LABEL[cand.kind],
      color: SCENARIO_LINE_TONE[cand.kind],
    })
  }

  // 去重(同价位只留一条) + 排序(高→低)
  const seen = new Set<number>()
  const lines: ScenarioLine[] = []
  for (const l of kept) {
    const key = Math.round(l.price * 100)
    if (seen.has(key)) continue
    seen.add(key)
    lines.push(l)
  }
  lines.sort((a, b) => b.price - a.price)
  return { lines, dropped }
}

/**
 * 情景关键位 → 图上水平线(主入口)。
 *
 * 返回**数组**(长度 = 要画的线数, 0~4), 另挂 `dropped`(丢弃的脏值个数)与 `lines`(自引用)。
 * `available:false` → 空数组(静默不画)。
 */
export function buildScenarioLines(resp: ScenarioMatrixResponse | null | undefined): ScenarioLines {
  const { lines, dropped } = buildScenarioLineResult(resp)
  const out = lines as ScenarioLines
  // 非枚举属性: 不进 JSON 序列化、不干扰 `toEqual([...])` 断言, 但可直接读。
  Object.defineProperty(out, 'dropped', { value: dropped, enumerable: false, configurable: true })
  Object.defineProperty(out, 'lines', { value: out, enumerable: false, configurable: true })
  return out
}

/** 图例/悬停文案用: 一行说清这条线是什么(如 "阻力 33.92")。 */
export function scenarioLineText(line: ScenarioLine): string {
  return `${line.label} ${line.price}`
}

export { KIND_PRIORITY as SCENARIO_LINE_PRIORITY }
