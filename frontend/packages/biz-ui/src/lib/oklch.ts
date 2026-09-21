/**
 * OKLCH → sRGB(2026-09-20 设计系统落地: 涨跌强度阶改 OKLCH 均匀发散)。
 *
 * 为什么不用 alpha 线性插值: 老实现把 up/down 色按 alpha 0.12→0.9 叠底, 那是 **sRGB 线性**插值,
 * 感知上不均匀 —— 弱档全挤在一起(看不清 0.3% 和 0.8% 的差别), 强档又几乎一样。
 * OKLCH 的 L 通道是感知均匀的, 等步长 = 等观感差异。
 *
 * 为什么要转 sRGB: ECharts/lightweight-charts 画在 **canvas** 上, canvas 的 fillStyle 虽在
 * 新 Chrome 里认 oklch(), 但 tooltip/导出/老浏览器不认 ⇒ 统一转成 #rrggbb 最稳。
 */
export interface Rgb255 {
  r: number
  g: number
  b: number
}

function gamma(x: number): number {
  return x <= 0.0031308 ? 12.92 * x : 1.055 * Math.pow(x, 1 / 2.4) - 0.055
}

/** OKLCH(L 0–1, C 0–0.4, H 角度) → sRGB 0–255。超出色域的值按线性裁剪(不报错, 不 NaN)。 */
export function oklchToRgb(L: number, C: number, H: number): Rgb255 {
  const hRad = (H * Math.PI) / 180
  const a = C * Math.cos(hRad)
  const b = C * Math.sin(hRad)

  // OKLab → LMS
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b
  const s_ = L - 0.0894841775 * a - 1.291485548 * b

  const l = l_ * l_ * l_
  const m = m_ * m_ * m_
  const s = s_ * s_ * s_

  // LMS → 线性 sRGB
  const rLin = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
  const gLin = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
  const bLin = -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s

  const to255 = (v: number) => {
    const c = Math.min(Math.max(gamma(v), 0), 1)
    return Math.round(c * 255)
  }
  return { r: to255(rLin), g: to255(gLin), b: to255(bLin) }
}

export function rgbToHex({ r, g, b }: Rgb255): string {
  const h = (v: number) => Math.min(Math.max(Math.round(v), 0), 255).toString(16).padStart(2, '0')
  return `#${h(r)}${h(g)}${h(b)}`
}

/** `oklch(L C H)` / `oklch(L% C H)` → 分量; 解析不了返回 null(不猜)。 */
export function parseOklch(input: string): { L: number; C: number; H: number } | null {
  const m = (input || '').trim().match(/^oklch\(\s*([\d.]+%?)\s+([\d.]+%?)\s+([\d.]+)(?:deg)?\s*(?:\/[^)]*)?\)$/i)
  if (!m) return null
  const num = (v: string, scale: number) => (v.endsWith('%') ? (parseFloat(v) / 100) * scale : parseFloat(v))
  const L = num(m[1], 1)
  const C = num(m[2], 0.4)
  const H = parseFloat(m[3])
  if (!Number.isFinite(L) || !Number.isFinite(C) || !Number.isFinite(H)) return null
  return { L, C, H }
}

/** `oklch(...)` 字符串 → #rrggbb; 解析不了返回 null。 */
export function oklchStringToHex(input: string): string | null {
  const parsed = parseOklch(input)
  if (!parsed) return null
  return rgbToHex(oklchToRgb(parsed.L, parsed.C, parsed.H))
}
