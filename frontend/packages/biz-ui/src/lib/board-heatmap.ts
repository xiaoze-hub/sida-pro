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
  /** 实时量比(仅 live 命中项; 日线回落时为 null) */
  volume_ratio?: number | null
  /** 板块涨速=近5分钟涨幅%(仅 live) */
  speed?: number | null
  /** 是否实时数据(live 模式覆盖) */
  live?: boolean
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
  /** 异动警示环色(可选; 缺省 amber-500 —— 语义警示色, 非涨跌主题色) */
  ring?: string
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
  volumeRatio?: number | null
  speed?: number | null
  /** 异动标注(无 → null); 命中时 itemStyle 带警示环 */
  anomaly: HeatAnomaly | null
  itemStyle: { color: string; borderColor?: string; borderWidth?: number }
  label: { color: string }
}

const DEFAULT_CLAMP = 3
const DEFAULT_MIN_ALPHA = 0.12
const DEFAULT_MAX_ALPHA = 0.9
const LABEL_LIGHT_ALPHA = 0.45
const DEFAULT_MIN_SHARE = 0.02
/** 与 safePercent 展示口径一致: 四舍五入到 0.00 的幅度视为平盘 */
const FLAT_EPSILON = 0.005

// ── 板块异动规则(2026-09-10 盘中实时化; 仅高亮不推送) ─────────────────────────
// 阈值常量: 先在实盘观察几天, 噪声大就上调(规则本身与推送解耦, 后续接通知可直接复用)。
/** 板块涨速(近5分钟涨幅, %)急拉/急跌阈值 */
export const ANOMALY_SPEED_PCT = 0.5
/** 量比放量阈值 */
export const ANOMALY_VOLUME_RATIO = 2.0
/** 缺省警示环色: amber-500(语义警示色, 与红涨绿跌主题色区分) */
const DEFAULT_ANOMALY_RING = '#f59e0b'

export type AnomalyKind = 'surge' | 'dump' | 'heavy_volume'

export interface HeatAnomaly {
  kinds: AnomalyKind[]
  /** 中文短标签, 如 "急拉 · 放量" */
  label: string
}

export const ANOMALY_LABEL: Record<AnomalyKind, string> = {
  surge: '急拉',
  dump: '急跌',
  heavy_volume: '放量',
}

/**
 * 板块异动判定(纯函数): 涨速 ±阈值 → 急拉/急跌; 量比 ≥阈值 → 放量; 可叠加。
 * 数据缺(日线回落/未开盘)一律返回 null —— 不猜不误报。
 */
export function detectHeatAnomaly(
  item: Pick<BoardHeatItem, 'speed' | 'volume_ratio'>,
): HeatAnomaly | null {
  const kinds: AnomalyKind[] = []
  const speed = item.speed
  if (speed != null && isFinite(speed)) {
    if (speed >= ANOMALY_SPEED_PCT) kinds.push('surge')
    else if (speed <= -ANOMALY_SPEED_PCT) kinds.push('dump')
  }
  const vr = item.volume_ratio
  if (vr != null && isFinite(vr) && vr >= ANOMALY_VOLUME_RATIO) kinds.push('heavy_volume')
  if (kinds.length === 0) return null
  return { kinds, label: kinds.map((k) => ANOMALY_LABEL[k]).join(' · ') }
}

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
    const anomaly = detectHeatAnomaly(it)
    return {
      name: it.name || it.block_code,
      value: raw === null ? floor : raw,
      blockCode: it.block_code,
      changePct: it.change_pct,
      fundNet: it.fund_net,
      volume: it.volume,
      date: it.date,
      hasDaily: it.has_daily,
      volumeRatio: it.volume_ratio ?? null,
      speed: it.speed ?? null,
      anomaly,
      itemStyle: {
        color: heatCellColor(it.change_pct, palette, clampPct),
        ...(anomaly
          ? { borderColor: palette.ring || DEFAULT_ANOMALY_RING, borderWidth: 2 }
          : {}),
      },
      label: { color: heatLabelColor(it.change_pct, palette, clampPct) },
    }
  })
}
