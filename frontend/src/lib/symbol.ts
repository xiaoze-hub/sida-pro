/**
 * 展示层代码归一化(2026-10-18 P0 审计 F19 / R19)。
 *
 * 内部符号会漏到页面上: thsdk 竞价池给 `USHT603922` / `USZA000006`(US + 交易所码 + 6 位),
 * 直接进「代码」列后复制、跳转、搜索全失效。表格列统一走 `normSymbol()`。
 */

/** `US` + 两位交易所码 + 6 位数字: USZA/USHA/USHT/USBJ/USTM … */
const INTERNAL_PREFIX = /^US[A-Z]{2}(\d{6})$/i
/** `002361.SZ` / `600000.SH` / `830001.BJ` */
const EXCHANGE_SUFFIX = /^(\d{6})\.(SH|SZ|BJ|SS)$/i
/** 腾讯风格前缀 `sh600000` / `sz002361` */
const EXCHANGE_PREFIX = /^(SH|SZ)(\d{6})$/i

/** 只剥内部 `USxx` 前缀(路由参数用: 不碰 `881101.SH` 这类板块代码)。 */
export function stripInternalPrefix(raw: string | null | undefined): string {
  const s = String(raw ?? '').trim().toUpperCase()
  const m = s.match(INTERNAL_PREFIX)
  return m ? m[1] : String(raw ?? '').trim()
}

/** 展示用 6 位代码: 剥内部前缀 / 交易所后缀 / 腾讯前缀; 认不出则原样返回。 */
export function normSymbol(raw: string | null | undefined): string {
  const s = String(raw ?? '').trim().toUpperCase()
  if (!s) return ''
  const prefixed = s.match(INTERNAL_PREFIX)
  if (prefixed) return prefixed[1]
  const suffixed = s.match(EXCHANGE_SUFFIX)
  if (suffixed) return suffixed[1]
  const tx = s.match(EXCHANGE_PREFIX)
  if (tx) return tx[2]
  return s
}

/** 文本里是否还残留内部符号(`USZA000006` 形态) —— 供 UI 规则/单测断言用。 */
export function hasInternalSymbolPrefix(text: string | null | undefined): boolean {
  return /US[A-Z]{2}\d{6}/i.test(String(text ?? ''))
}
