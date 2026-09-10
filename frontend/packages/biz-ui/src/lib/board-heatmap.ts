/**
 * 板块热力图纯逻辑 (P1-1, 2026-09-10 借鉴 OpenTerminal 板块 treemap):
 * 色阶映射(A股红涨绿跌, ±3% 夹紧, 平盘归灰) + treemap 数据变换。
 *
 * 色值一律由调用方从 CSS 变量读取传入(见 stock-colors.ts), 本模块不硬编码主题色;
 * 图表消费方(ECharts)无法吃 Tailwind 类, 走本模块返回的 rgba/hsla 字符串。
 */

import { safeFixed } from '@/lib/format'
import { withAlpha } from './stock-colors'

export interface BoardHeatItem {
  block_code: string
  name: string
  board_type: string
  change_pct: number | null
  fund_net: number | null
  volume: number | null
  date: string | null
  has_daily: boolean
}

export interface HeatPalette {
  /** 涨 = 红 (--stock-up) */
  up: string
  /** 跌 = 绿 (--stock-down) */
  down: string
  /** 无数据/平盘 底 */
  neutral: string
  /** 浅色块上的深色字 */
  labelDark: string
  /** 深色块上的浅色字 */
  labelLight: string
}

export type HeatAreaMetric = 'volume' | 'equal'

export interface TreemapCell {
  name: string
  value: number
  blockCode: string
  changePct: number | null
  fundNet: number | null
  volume: number | null
  date: string | null
  hasDaily: boolean
  itemStyle: { color: string }
  label: { color: string }
}

const DEFAULT_CLAMP = 3
const DEFAULT_MIN_ALPHA = 0.12
const DEFAULT_MAX_ALPHA = 0.9
const LABEL_LIGHT_ALPHA = 0.45
const DEFAULT_MIN_SHARE = 0.02
/** 与 safePercent 展示口径一致: 四舍五入到 0.00 的幅度视为平盘 */
const FLAT_EPSILON = 0.005

/** |pct|/clamp 归一 0~1; null/NaN → null(走 neutral)。 */
function heatRatio(pct: number | null | undefined, clampPct: number): number | null {
  if (pct === null || pct === undefined || !isFinite(pct)) return null
  return Math.min(Math.abs(pct) / clampPct, 1)
}

function alphaOf(ratio: number): number {
  const raw = DEFAULT_MIN_ALPHA + (DEFAULT_MAX_ALPHA - DEFAULT_MIN_ALPHA) * ratio
  return Math.round(raw * 1000) / 1000
}

/** 涨跌幅 → 色块底色。无数据/平盘 → neutral 灰(不编造涨跌色)。 */
export function heatCellColor(
  pct: number | null | undefined,
  palette: HeatPalette,
  clampPct = DEFAULT_CLAMP,
): string {
  const r = heatRatio(pct, clampPct)
  if (r === null) return palette.neutral
  if (Math.abs(pct as number) < FLAT_EPSILON) return palette.neutral
  return withAlpha((pct as number) > 0 ? palette.up : palette.down, alphaOf(r))
}

/** 涨跌幅 → 色块内文字色(深底浅字/浅底深字)。 */
export function heatLabelColor(
  pct: number | null | undefined,
  palette: HeatPalette,
  clampPct = DEFAULT_CLAMP,
): string {
  const r = heatRatio(pct, clampPct)
  if (r === null) return palette.labelDark
  return alphaOf(r) > LABEL_LIGHT_ALPHA ? palette.labelLight : palette.labelDark
}

/** 热力图内百分比文案: 正数带 +, 无数据显式"无数据"(禁止留空猜测)。 */
export function formatHeatPct(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || !isFinite(pct)) return '无数据'
  return `${pct >= 0 ? '+' : ''}${safeFixed(pct, 2)}%`
}

function median(nums: number[]): number {
  const s = [...nums].sort((a, b) => a - b)
  const mid = Math.floor(s.length / 2)
  return s.length % 2 === 1 ? s[mid] : (s[mid - 1] + s[mid]) / 2
}

/**
 * 板块列表 → treemap cells。
 *
 * 面积口径 areaMetric:
 * - 'volume': 成交额(缺失/0 → 取正值中位数的 minShare 保底, 保证"无数据"块可见可点)
 * - 'equal':  等权(涨幅对比较场景)
 */
export function toTreemapCells(
  items: BoardHeatItem[],
  opts: {
    palette: HeatPalette
    areaMetric: HeatAreaMetric
    clampPct?: number
    minShare?: number
  },
): TreemapCell[] {
  const { palette, areaMetric, clampPct = DEFAULT_CLAMP, minShare = DEFAULT_MIN_SHARE } = opts
  const raws: (number | null)[] = items.map((it) => {
    if (areaMetric === 'equal') return 1
    const v = it.volume
    return typeof v === 'number' && isFinite(v) && v > 0 ? v : null
  })
  const positives = raws.filter((v): v is number => v !== null)
  const floor = positives.length > 0 ? median(positives) * minShare : 1

  return items.map((it, i) => {
    const raw = raws[i]
    return {
      name: it.name || it.block_code,
      value: raw === null ? floor : raw,
      blockCode: it.block_code,
      changePct: it.change_pct,
      fundNet: it.fund_net,
      volume: it.volume,
      date: it.date,
      hasDaily: it.has_daily,
      itemStyle: { color: heatCellColor(it.change_pct, palette, clampPct) },
      label: { color: heatLabelColor(it.change_pct, palette, clampPct) },
    }
  })
}
