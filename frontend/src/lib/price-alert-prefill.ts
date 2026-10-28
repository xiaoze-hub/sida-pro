/**
 * 价格提醒「预填意图」解析（2026-10-24，Lane B）
 *
 * 背景 bug：`/notifications?tab=alerts` 这类**只带页签参数**的地址曾被当成预填意图，
 * 进入提醒页就自动弹出「新建提醒规则」，并预选第一只自选股（实测为中粮糖业）。
 * 规则：只有出现个股意图（`symbol` 或 `stock_id`）才允许自动预填并弹窗；
 * 其余 query（tab / 任何筛选参数）一律不触发，新建入口只保留用户显式点击。
 */

export interface AlertPrefill {
  /** `stock_id` query，无则 0 */
  stockId: number
  /** 规范化后的代码（大写去空格），无则空串 */
  symbol: string
  /** 市场，默认 CN */
  market: string
  /** 展示名，可能为空 */
  name: string
  /** 明确的支撑破位条件；普通提醒入口不带此字段。 */
  price?: number
  direction?: '<'
}

/**
 * 从 location.search 解析预填意图。
 * @returns 无个股意图时返回 `null`（调用方据此保持弹窗关闭）
 */
export function parseAlertPrefill(search: string | null | undefined): AlertPrefill | null {
  const params = new URLSearchParams(search || '')
  const rawStockId = Number(params.get('stock_id') || 0)
  const stockId = Number.isFinite(rawStockId) && rawStockId > 0 ? rawStockId : 0
  const symbol = String(params.get('symbol') || '').trim().toUpperCase()
  if (stockId === 0 && !symbol) return null
  const isSupportBreak = params.get('intent') === 'support_break'
  const price = Number(params.get('price'))
  if (isSupportBreak && (!symbol || !Number.isFinite(price) || price <= 0)) return null
  return {
    stockId,
    symbol,
    market: String(params.get('market') || '').trim().toUpperCase() || 'CN',
    name: String(params.get('name') || '').trim(),
    ...(isSupportBreak ? { price, direction: '<' as const } : {}),
  }
}
