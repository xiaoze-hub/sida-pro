/**
 * 安全格式化工具集（2026-08-23 新增，对应 aud-20260823-frontend-collectors.md S-5 / M-3~M-8）。
 *
 * 背景：Dashboard 2026-08-21 修复过 `c.price.toFixed is not a function` 这类 TypeError；
 * 真因是后端 PG DECIMAL 经 psycopg2 → JSON 后变成字符串，前端裸 `.toFixed()` 崩。
 * 同模式在 Stocks / IndexDetail / Forecast / Opportunities / AnalysisDetail / PaperTrading /
 * Reports / ShadowAccount 等页全都存在，本工具统一抽出供全项目复用。
 *
 * 重要：本文件只暴露纯函数，不要 import 任何 React 相关模块，
 * 便于在 Dashboard.tsx / Stocks.tsx 等文件顶部直接静态引用，避免循环依赖。
 */

/**
 * 把任意值转 number；不接受 null/undefined、空串、非有限数（NaN/Infinity），
 * 一律返回 null。绝不抛 TypeError，永不返回 NaN。
 *
 * M-8 提示：以前许多地方 `value ?? 0` 看似兜了 null，但字符串 "abc" 仍会变成 NaN。
 * 本函数把 NaN 也走 fallback 路径，从根上避免渲染出 "NaN%"、"NaN亿"。
 */
export function safeNum(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

/**
 * 安全版 toFixed：把值转数字后保留 digits 位小数，无效值返回 fallback（默认 "--"）。
 *
 * @param v        待格式化值（任意）
 * @param digits   小数位数（默认 2）
 * @param fallback 无效值显示文本（默认 "--"）
 */
export function safeFixed(v: unknown, digits = 2, fallback = '--'): string {
  const n = safeNum(v)
  if (n === null) return fallback
  return n.toFixed(digits)
}

/**
 * 安全百分比：返回带 % 的字符串；对 null/NaN 走 fallback。
 *
 * @param v        0~100 区间的百分点数值（如 3.45 表示 3.45%）
 * @param digits   小数位数（默认 2）
 * @param withSign 是否带正负号（默认 true；下调时显示 +、上涨时显示 +）
 * @param fallback 无效值显示文本（默认 "--"）
 */
export function safePercent(
  v: unknown,
  digits = 2,
  withSign = true,
  fallback = '--',
): string {
  const n = safeNum(v)
  if (n === null) return fallback
  const sign = withSign && n > 0 ? '+' : ''
  return `${sign}${n.toFixed(digits)}%`
}

/**
 * 安全金额：A 股资金以"元"为单位（后端实际给的是元，不是万元/亿元）。
 *
 * 规则：
 *  - 绝对值 ≥ 1e8 → 转为亿单位（保留 2 位小数）
 *  - 绝对值 ≥ 1e4 → 转为万单位（保留 2 位小数）
 *  - 否则保留原精度显示
 *
 * 对 null/NaN/字符串数字全走 fallback，避免 "undefined亿"、"NaN万"。
 */
export function safeMoney(v: unknown, fallback = '--'): string {
  const n = safeNum(v)
  if (n === null) return fallback
  const abs = Math.abs(n)
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(n / 1e4).toFixed(2)}万`
  // 小金额按精度自适应：1 元以内保 4 位小数、否则 2 位
  const digits = abs < 1 ? 4 : 2
  // 去掉末尾的 0（如 100.20 → "100.20"、100.00 → "100"）
  return Number(n.toFixed(digits)).toString()
}

/**
 * 安全价格显示（K线/快照价格）：
 *  - 自动按精度回退掉多余 0（30.00 → "30"、30.50 → "30.5"）
 *  - 无效值返回 fallback
 *
 * 对应 Stocks.tsx 原 formatPrice(value) 的安全版本。
 */
export function safePrice(v: unknown, maxDigits = 4, fallback = '--'): string {
  const n = safeNum(v)
  if (n === null) return fallback
  return Number(n.toFixed(maxDigits)).toString()
}

/**
 * 安全整数显示：成交量/股本等。对非整数四舍五入，对无效值返回 fallback。
 */
export function safeInt(v: unknown, fallback = '--'): string {
  const n = safeNum(v)
  if (n === null) return fallback
  return Math.round(n).toLocaleString('zh-CN')
}

/**
 * 安全中文千分位格式化：1234567.89 → "1,234,567.89"。无效值走 fallback。
 */
export function safeThousand(v: unknown, digits = 2, fallback = '--'): string {
  const n = safeNum(v)
  if (n === null) return fallback
  return n.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/**
 * 安全 NetInflow（资金流专用）：所有数值显示带正负号和"亿"单位。
 * 对应 IndexDetail.tsx / Opportunities.tsx 中 `(marketFlow.total_main_flow ?? 0).toFixed(1)`。
 *
 * M-8: 即便用 `?? 0`，字符串 "abc" 也会被 Number() 转成 NaN；本函数走 fallback。
 */
export function safeNetInflow(
  v: unknown,
  digits = 1,
  unit = '亿',
  fallback = '--',
): string {
  const n = safeNum(v)
  if (n === null) return fallback
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(digits)}${unit}`
}
