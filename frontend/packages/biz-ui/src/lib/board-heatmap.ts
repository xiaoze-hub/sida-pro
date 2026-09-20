/**
 * 板块热力图纯逻辑 (P1-1, 2026-09-10 借鉴 OpenTerminal 板块 treemap):
 * 色阶映射(A股红涨绿跌, ±3% 夹紧, 平盘归灰) + treemap 数据变换。
 *
 * 色值一律由调用方从 CSS 变量读取传入(见 stock-colors.ts), 本模块不硬编码主题色;
 * 图表消费方(ECharts)无法吃 Tailwind 类, 走本模块返回的 rgba/hsla 字符串。
 */

import { safeFixed, safeNum } from '@/lib/format'
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
  /** 深色文字(浅底用) */
  labelDark: string
  /** 深色块上的浅色字 */
  /** 浅色文字(深底用) */
  labelLight: string
  /** 图表实际底色(用于第一遍估算; 实测像素优先) */
  surface?: string
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
  label: { color: string; show?: boolean }
  /** 标签档位(布局后由真实像素尺寸决定, 见 labelTierFor): 0=不显示 1=只名称 2=名称+涨跌幅 */
  labelTier?: LabelTier
}

const DEFAULT_CLAMP = 3
const DEFAULT_MIN_ALPHA = 0.12
const DEFAULT_MAX_ALPHA = 0.9
const DEFAULT_MIN_SHARE = 0.02
/** 全部板块量能都缺失/为 0 时的保底面积: 固定 1(必须 > 0, ECharts treemap 对 0 面积整块不画) */
/**
 * 色块标签档位(2026-09-20 修缺陷 —— 用户报"有的板块名和涨跌幅不显示, 切到面积等权**都不显示**")。
 *
 * 老规则: "**面积占比** ≥ 0.8% 才画文字"。这是个**代理指标**, 在等权模式下必然失效 ——
 * 等权时每块占比 = 1/N, N=128 个板块时恒为 0.0078 < 0.008 ⇒ **整张图一个标签都没有**;
 * 量能模式下也只有少数大块够阈值("有的不显示")。
 *
 * 真判据是**色块的实际像素尺寸**(46×26 才放得下两行 11px 文字), 而尺寸**布局后**才知道
 * ⇒ 由 BoardHeatmap 的**二遍布局**按真实 rect 决定档位(布局前先全显示, 避免闪烁)。
 * 注意: 这里只控制**文字显示**, 不改面积口径(面积仍 = 量能/等权, 见文件头口径说明)。
 */
export type LabelTier = 0 | 1 | 2
/** 两行(名称 + 涨跌幅)所需最小像素尺寸: 11px 两行 + 内边距 */
export const LABEL_MIN_W_2LINE = 46
export const LABEL_MIN_H_2LINE = 26
/** 一行(仅名称)所需最小像素尺寸 */
export const LABEL_MIN_W_1LINE = 30
export const LABEL_MIN_H_1LINE = 14
/** 由色块真实尺寸定档位。尺寸读不到(NaN)时**按能显示处理** —— 宁可多显示, 不要静默丢标签。 */
export function labelTierFor(w: number, h: number): LabelTier {
  if (!Number.isFinite(w) || !Number.isFinite(h)) return 2
  if (w >= LABEL_MIN_W_2LINE && h >= LABEL_MIN_H_2LINE) return 2
  if (w >= LABEL_MIN_W_1LINE && h >= LABEL_MIN_H_1LINE) return 1
  return 0
}
const EMPTY_FLOOR = 1
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

/**
 * |pct|/clamp 归一 0~1; null/NaN → null(走 neutral)。
 *
 * 口径(2026-09-14 定): **只把「空」当无数据** —— null/undefined/NaN/Infinity。
 * 不按量级猜哨兵值: 后端把缺失写成 -100 时前端仍按"真跌 -100%"上色, 因为
 * 「-100 是哨兵还是真值」只有数据源知道, 按量级猜会把真实深跌误染成灰。
 * 缺数据必须由后端返回 null(`change_pct: null`), 前端这才染 neutral 灰。
 */
function heatRatio(pct: number | null | undefined, clampPct: number): number | null {
  if (pct === null || pct === undefined || !isFinite(pct)) return null
  return Math.min(Math.abs(pct) / clampPct, 1)
}

function alphaOf(ratio: number): number {
  const raw = DEFAULT_MIN_ALPHA + (DEFAULT_MAX_ALPHA - DEFAULT_MIN_ALPHA) * ratio
  return Math.round(raw * 1000) / 1000
}

// ── 字色必须按**实测对比度**选(2026-09-20 修缺陷) ──────────────────────────────
// 老规则 `alphaOf(r) > 0.45 ? 白字 : labelDark` 有两个致命假设:
//   ① 用 **alpha 当亮度代理** —— 填充色偏亮时 alpha 大 ≠ 底色深, 白字压上去等于看不见;
//   ② 假设 labelDark 一定是深色 —— 深色主题里 `--foreground` 是**近白**(240 15% 90%),
//      于是深浅两个候选**都是浅色** ⇒ 浅色块上的文字必然隐形(用户报的"有的不显示")。
// 真判据只能是**实际画出来的底色亮度**, 所以这里提供对比度工具, 由调用方拿实测像素定字色。
export interface Rgb { r: number; g: number; b: number; a: number }
/** WCAG 相对亮度 */
export function relativeLuminance(c: Rgb): number {
  const f = (v: number) => {
    const s = v / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b)
}
/** WCAG 对比度(1~21) */
export function contrastRatio(a: Rgb, b: Rgb): number {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  const hi = Math.max(la, lb)
  const lo = Math.min(la, lb)
  return (hi + 0.05) / (lo + 0.05)
}
/** 解析 #rgb/#rrggbb/rgb()/rgba()/hsl()/hsla() → Rgb; 解析不了返回 null(不猜)。 */
export function parseColorToRgb(input: string): Rgb | null {
  const str = (input || '').trim()
  const hex = str.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i)
  if (hex) {
    const h = hex[1]
    const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
    return { r: parseInt(full.slice(0, 2), 16), g: parseInt(full.slice(2, 4), 16), b: parseInt(full.slice(4, 6), 16), a: 1 }
  }
  const num = (v: string) => parseFloat(v)
  const rgb = str.match(/^rgba?\(([^)]+)\)$/i)
  if (rgb) {
    const parts = rgb[1].split(/[,\s/]+/).filter(Boolean)
    if (parts.length >= 3) return { r: num(parts[0]), g: num(parts[1]), b: num(parts[2]), a: parts[3] === undefined ? 1 : num(parts[3]) }
    return null
  }
  const hsl = str.match(/^hsla?\(([^)]+)\)$/i)
  if (hsl) {
    const parts = hsl[1].split(/[,\s/]+/).filter(Boolean)
    if (parts.length < 3) return null
    const h = ((num(parts[0]) % 360) + 360) % 360
    const sat = Math.min(Math.max(num(parts[1].replace('%', '')) / 100, 0), 1)
    const light = Math.min(Math.max(num(parts[2].replace('%', '')) / 100, 0), 1)
    const a = parts[3] === undefined ? 1 : num(parts[3])
    const c = (1 - Math.abs(2 * light - 1)) * sat
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1))
    const m = light - c / 2
    const seg: [number, number, number] =
      h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
      : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x]
    return { r: Math.round((seg[0] + m) * 255), g: Math.round((seg[1] + m) * 255), b: Math.round((seg[2] + m) * 255), a }
  }
  return null
}
/** 半透明前景叠到不透明背景上 = 眼睛实际看到的颜色 */
export function compositeOver(fg: Rgb, bg: Rgb): Rgb {
  const a = Math.min(Math.max(fg.a, 0), 1)
  return { r: Math.round(fg.r * a + bg.r * (1 - a)), g: Math.round(fg.g * a + bg.g * (1 - a)), b: Math.round(fg.b * a + bg.b * (1 - a)), a: 1 }
}
/**
 * 字色亮度阈值: 实测底色亮度 ≥ 该值 → 深字, 否则浅字。
 *
 * 0.30 是**两端都保底 3:1** 的取值(推导): 底色亮度 L 时
 *   浅字对比度 = 1.05/(L+0.05), 深字对比度 = (L+0.05)/0.0604(深字亮度取 0.0104)。
 * 两式相等(L≈0.20)时是理论最优, 但那里饱和红块(亮度≈0.23)会判成深字 —— 热力图惯例是
 * "红块白字"。取 0.30 兼顾: 最坏一端(恰好 0.30)浅字 3.0 / 深字 5.8, 两端都不低于 3:1。
 */
export const LABEL_LUMINANCE_THRESHOLD = 0.3
/**
 * 在**实测底色**上选字色: 底色偏亮用深字, 偏暗用浅字, 并回传实际对比度供巡检断言。
 * 为什么不用"两个候选里挑对比度高的": 饱和红(231,77,73)的亮度只有 0.23, 数学上深字对比度
 * 略高(4.6 vs 3.8), 但热力图惯例是"红块白字" —— 按亮度阈值选既保住惯例, 又保证两端都够看。
 */
export function pickLabelColor(surface: Rgb, dark: string, light: string): { color: string; contrast: number } {
  const useDark = relativeLuminance(surface) >= LABEL_LUMINANCE_THRESHOLD
  const color = useDark ? dark : light
  const rgb = parseColorToRgb(color) ?? { r: 0, g: 0, b: 0, a: 1 }
  return { color, contrast: contrastRatio(surface, rgb) }
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

/**
 * 涨跌幅 → 色块内文字色(**第一遍估算**: 把半透明填充叠到 `palette.surface` 上再按对比度选)。
 * 真正的字色由 BoardHeatmap 的**二遍**用画布实测像素覆盖 —— 这里只是避免第一帧闪一下。
 */
export function heatLabelColor(
  pct: number | null | undefined,
  palette: HeatPalette,
  clampPct = DEFAULT_CLAMP,
): string {
  const fill = parseColorToRgb(heatCellColor(pct, palette, clampPct))
  const surface = parseColorToRgb(palette.surface ?? '') ?? { r: 255, g: 255, b: 255, a: 1 }
  if (!fill) return palette.labelDark
  // 必须**先合成再判**: 半透明填充的 RGB 不是眼睛看到的颜色(无数据灰块 0.18 alpha 叠白底
  // 是浅灰 → 深字; 直接拿填充 RGB 判会判成深底 → 白字 → 隐形)。
  return pickLabelColor(compositeOver(fill, surface), palette.labelDark, palette.labelLight).color
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
 * 单板块可用成交额: **正有限数**才算(0/负/NaN/Infinity/空串/非数值字符串 → null)。
 * 走 safeNum 兼容 PG DECIMAL 经 JSON 变字符串的脏数, 不裸 Number()。
 */
function usableVolume(v: unknown): number | null {
  const n = safeNum(v)
  return n !== null && n > 0 ? n : null
}

/**
 * 有几个板块的成交额是可用的(面积:量能视图的信息量来源)。
 * 0 ⇒ 该视图的面积不携带任何信息, 只能靠保底面积铺满 —— 调用方必须显式说明, 不许静默画。
 */
export function usableVolumeCount(items: BoardHeatItem[]): number {
  let n = 0
  for (const it of items) if (usableVolume(it.volume) !== null) n++
  return n
}

/** 面积:量能视图是否有可用量能(至少一个板块成交额为正有限数)。 */
export function hasUsableVolume(items: BoardHeatItem[]): boolean {
  return usableVolumeCount(items) > 0
}

/**
 * cells 能否被 ECharts treemap 画出来: 非空 **且** 至少一个 value 是正有限数。
 *
 * 为什么必须查: ECharts treemap 对全 0/NaN 的 value 会**整块不画**(实测 echarts 6.1:
 * 128 个 value=0 的节点渲染出的 svg 只剩背景, 路径数 2), 页面表现为一个卡宽的空白灰框。
 * 「画不出来」必须由调用方渲染显式空态, 不允许把空白当结果。
 */
export function hasDrawableArea(cells: TreemapCell[] | null | undefined): boolean {
  if (!cells || cells.length === 0) return false
  return cells.some((c) => Number.isFinite(c.value) && c.value > 0)
}

/**
 * 板块列表 → treemap cells。
 *
 * 面积口径 areaMetric:
 * - 'volume': 成交额(缺失/0/脏数 → 取正值中位数的 minShare 保底, 保证"无数据"块可见可点;
 *   **全部板块都缺失/为 0 时** 退回固定保底面积 EMPTY_FLOOR>0 —— 面积为 0 会让 ECharts
 *   整块不画, 那种"静默空白"是缺陷本体。此时面积已不代表量能, 调用方应显式说明
 *   (见 BoardHeatmap 的 hasUsableVolume 空态), 不得让用户把保底面积误读成量能)
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
    return usableVolume(it.volume)
  })
  const positives = raws.filter((v): v is number => v !== null)
  // 保底面积必须恒 > 0: ECharts treemap 对 0 面积整块不画(空白灰框缺陷的根因)。
  const measured = positives.length > 0 ? median(positives) * minShare : EMPTY_FLOOR
  const floor = Number.isFinite(measured) && measured > 0 ? measured : EMPTY_FLOOR

  // 面积口径不变(raw volume)。**标签显不显示不再由占比决定**(占比不是像素尺寸, 等权模式下恒相等
  // ⇒ 老规则会整张图无标签)。这里第一遍先全显示, 布局算好后由 BoardHeatmap 按真实 rect 降档。

  return items.map((it, i) => {
    const raw = raws[i]
    const anomaly = detectHeatAnomaly(it)
    const value = raw === null ? floor : raw
    return {
      name: it.name || it.block_code,
      value,
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
      label: {
        color: heatLabelColor(it.change_pct, palette, clampPct),
        // 第一遍全显示(避免"先空后显"的闪烁); 真实档位在二遍布局里按 rect 覆盖。
        show: true,
      },
      labelTier: 2 as LabelTier,
    }
  })
}
