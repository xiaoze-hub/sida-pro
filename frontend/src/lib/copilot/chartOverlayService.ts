/**
 * 参谋结论与 TradingView / Lightweight Charts 图元同步画线服务
 *
 * ## 2026-10-28 P2：画线必须带「服务端来源凭证」
 *
 * 之前这里只认 `symbol + 价位数组`，于是 `AnswerCard` 用正则从回答**散文**里提价，
 * 直接落 sessionStorage，个股工作台再读出来画成真 K 线 —— 等于把模型随手写的数字
 * 冒充「已核验图表标注」，而用户从没确认过任何东西。
 *
 * 服务端的真实链路是：
 *
 *     pipeline.propose_levels → propose_scenario_annotations()   （只写 pending 提议）
 *       → GET  /copilot/actions            （本人 pending 列表）
 *       → POST /copilot/actions/{id}/apply （用户点确认 → 写 KlineAnnotation）
 *       → GET  /copilot/annotations        （本人 + 全局标注，图表按 symbol/period 取）
 *
 * 所以前端画线只认最后一步的产物：**apply 成功后带上 actionId / status=applied / 标的**
 * 的凭证。没有凭证一律拒画（`unverified_source`），既不写存储也不广播事件 ——
 * 宁可图上少两条线，也不能让用户以为那是核验过的关键位。
 */

import { safeJwtPayload } from '@/lib/jwt'

export interface PriceLevelOverlay {
  price: number
  title: string
  color?: string
  kind?: 'support' | 'pressure'
  lineStyle?: number // 0: solid, 1: dotted, 2: dashed
}

export interface KlinePriceLinePayload {
  price: number
  kind: 'support' | 'pressure'
  label: string
  /** 来源标记：只有服务端确认过的标注才会出现在存储里 */
  source?: 'server_annotation'
  /** 服务端 CopilotAction id（用户点过确认的那一条） */
  actionId?: number
}

/** 唯一可信来源：服务端提议 + 用户确认（apply）成功。 */
export const OVERLAY_PROVENANCE_SOURCE = 'server_annotation' as const

/**
 * 画线凭证：证明这条线来自服务端动作，并且用户已经确认过。
 *
 * 由调用方（读 `/copilot/actions` → apply 返回体，或 `/copilot/annotations` 的已确认标注）
 * 构造；缺失、`status !== 'applied'`、`actionId` 非正整数、标的与画线标的不一致 → 拒画。
 */
export interface OverlayProvenance {
  source: typeof OVERLAY_PROVENANCE_SOURCE
  actionId: number
  status: 'applied'
  /** 服务端记录的标的，必须与本次画线标的一致（跨股票隔离） */
  symbol: string
}

export type OverlayRejectReason =
  | 'no_symbol'
  | 'no_identity'
  | 'unverified_source'
  | 'symbol_mismatch'
  | 'invalid_levels'
  | 'storage_unavailable'

export interface SaveOverlayResult {
  saved: boolean
  reason?: OverlayRejectReason
  /** 直接给用户看的一句话（失败时说明"未画线"及原因） */
  message: string
}

/** 拒绝原因 → 用户可见文案（单一事实来源，界面不另攒一套措辞）。 */
export function overlayRejectMessage(reason: OverlayRejectReason): string {
  switch (reason) {
    case 'no_symbol':
      return '未画线：缺少股票代码。'
    case 'no_identity':
      return '未画线：请先登录（画线按账号隔离保存）。'
    case 'unverified_source':
      return '未画线：该价位来自回答正文的文本识别，未经服务端核验与确认，不能作为图表标注。'
    case 'symbol_mismatch':
      return '未画线：标注所属标的与当前图表不一致。'
    case 'invalid_levels':
      return '未画线：价位数据非法（需要正的有限价格与支撑/压力类型）。'
    case 'storage_unavailable':
      return '画线未保存：浏览器不允许会话存储。'
    default:
      return '未画线：来源未确认。'
  }
}

const STORAGE_PREFIX = 'sida_kline_overlay:v2:'

function storageKey(symbol: string | undefined): string | null {
  if (!symbol || typeof localStorage === 'undefined') return null
  try {
    const claims = safeJwtPayload(localStorage.getItem('token') || '')
    const id = claims?.sub ?? claims?.user_id
    if ((typeof id !== 'string' && typeof id !== 'number') || !String(id).trim()) return null
    return `${STORAGE_PREFIX}${encodeURIComponent(String(id))}:${encodeURIComponent(symbol.toUpperCase())}`
  } catch {
    return null
  }
}

function normSymbol(symbol: string | undefined | null): string {
  return typeof symbol === 'string' ? symbol.trim().toUpperCase() : ''
}

/** 凭证是否真的存在且已确认（任意一项不满足 → 不足以画线）。 */
function provenanceIsVerified(
  provenance: OverlayProvenance | null | undefined,
): provenance is OverlayProvenance {
  if (!provenance || typeof provenance !== 'object') return false
  if (provenance.source !== OVERLAY_PROVENANCE_SOURCE) return false
  if (provenance.status !== 'applied') return false
  const id = provenance.actionId
  return typeof id === 'number' && Number.isInteger(id) && id > 0
}

/**
 * 价位归一化：非正/非有限价格、未知 kind、空标题一律判非法（整批拒绝，不写半截）。
 */
function normalizeLevels(levels: PriceLevelOverlay[]): KlinePriceLinePayload[] | null {
  if (!Array.isArray(levels) || levels.length === 0) return null
  const out: KlinePriceLinePayload[] = []
  const seen = new Set<string>()
  for (const lvl of levels) {
    if (!lvl || typeof lvl !== 'object') return null
    const price = lvl.price
    if (typeof price !== 'number' || !Number.isFinite(price) || price <= 0) return null
    const kind = lvl.kind
    if (kind !== 'support' && kind !== 'pressure') return null
    const label = typeof lvl.title === 'string' ? lvl.title.trim() : ''
    if (!label) return null
    const dedupeKey = `${kind}:${price}`
    if (seen.has(dedupeKey)) continue
    seen.add(dedupeKey)
    out.push({ price, kind, label })
  }
  return out.length > 0 ? out : null
}

export class ChartOverlayService {
  private static linesMap: Map<string, any> = new Map()

  /**
   * 把**服务端已确认**的价位线持久化到会话存储并广播全局事件。
   *
   * 没有 `provenance`（或凭证不完整 / 标的对不上）时**拒绝**：不写存储、不发事件，
   * 只返回可展示的原因。这样散文提价永远到不了真 K 线。
   */
  public static saveOverlay(
    symbol: string | undefined,
    levels: PriceLevelOverlay[],
    provenance?: OverlayProvenance | null,
  ): SaveOverlayResult {
    const sym = normSymbol(symbol)
    if (!sym) {
      return { saved: false, reason: 'no_symbol', message: overlayRejectMessage('no_symbol') }
    }
    if (!storageKey(sym)) {
      return { saved: false, reason: 'no_identity', message: overlayRejectMessage('no_identity') }
    }
    if (!provenanceIsVerified(provenance)) {
      return {
        saved: false,
        reason: 'unverified_source',
        message: overlayRejectMessage('unverified_source'),
      }
    }
    if (normSymbol(provenance.symbol) !== sym) {
      return {
        saved: false,
        reason: 'symbol_mismatch',
        message: overlayRejectMessage('symbol_mismatch'),
      }
    }
    const lines = normalizeLevels(levels)
    if (!lines) {
      return {
        saved: false,
        reason: 'invalid_levels',
        message: overlayRejectMessage('invalid_levels'),
      }
    }
    if (typeof sessionStorage === 'undefined') {
      return {
        saved: false,
        reason: 'storage_unavailable',
        message: overlayRejectMessage('storage_unavailable'),
      }
    }

    const key = storageKey(sym) as string
    const tagged: KlinePriceLinePayload[] = lines.map((line) => ({
      ...line,
      source: OVERLAY_PROVENANCE_SOURCE,
      actionId: provenance.actionId,
    }))
    try {
      sessionStorage.setItem(key, JSON.stringify(tagged))
    } catch {
      return {
        saved: false,
        reason: 'storage_unavailable',
        message: overlayRejectMessage('storage_unavailable'),
      }
    }

    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent('sida-kline-overlay', {
          detail: {
            symbol: sym,
            lines: tagged,
            provenance: {
              source: provenance.source,
              actionId: provenance.actionId,
            },
          },
        }),
      )
    }
    return { saved: true, message: '画线已保存。' }
  }

  /**
   * 从会话存储读取某标的的持久化画线。
   *
   * 只还原**带来源标记**的线：历史上由散文正则写进去的无凭证数据（没有
   * `source`/`actionId`）一律丢弃，避免旧的不安全数据继续上真图。
   */
  public static getStoredOverlay(symbol: string): KlinePriceLinePayload[] | null {
    const key = storageKey(symbol)
    if (!key || typeof sessionStorage === 'undefined') return null
    try {
      const raw = sessionStorage.getItem(key)
      if (!raw) return null
      const parsed = JSON.parse(raw)
      if (!Array.isArray(parsed)) return null
      const verifiedLines = parsed.filter((line): line is KlinePriceLinePayload => {
        if (!line || typeof line !== 'object') return false
        if (line.source !== OVERLAY_PROVENANCE_SOURCE) return false
        const id = line.actionId
        if (typeof id !== 'number' || !Number.isInteger(id) || id <= 0) return false
        if (typeof line.price !== 'number' || !Number.isFinite(line.price) || line.price <= 0) {
          return false
        }
        return line.kind === 'support' || line.kind === 'pressure'
      })
      return verifiedLines.length > 0 ? verifiedLines : null
    } catch {
      return null
    }
  }

  /**
   * 清除某标的的画线并广播清除事件
   */
  public static clearOverlay(symbol: string) {
    const key = storageKey(symbol)
    if (!key) return
    if (typeof sessionStorage !== 'undefined') {
      try {
        sessionStorage.removeItem(key)
      } catch {
        // ignore
      }
    }
    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent('sida-kline-overlay', {
          detail: {
            symbol,
            lines: [],
          },
        }),
      )
    }
  }

  /**
   * 派发全局 K 线画线叠加事件（兼容方法）
   */
  public static emitOverlay(
    symbol: string | undefined,
    levels: PriceLevelOverlay[],
    provenance?: OverlayProvenance | null,
  ): SaveOverlayResult {
    return this.saveOverlay(symbol, levels, provenance)
  }

  /**
   * 将**已确认**的价位线画到图表系列上。
   *
   * 注意：这是图元级原语，不做来源校验 —— 调用方必须已经拿到
   * `saveOverlay` 所需的同一份 `OverlayProvenance`。当前仓库里没有任何
   * 路径会用散文价位调用它（见 AnswerCard 的"未核验"提示）。
   */
  public static injectDecisionLevels(series: any, levels: PriceLevelOverlay[]) {
    if (!series || typeof series.createPriceLine !== 'function') {
      return
    }

    // 先清除已有的旧画线
    this.clearOverlays(series)

    levels.forEach((lvl) => {
      try {
        const priceLine = series.createPriceLine({
          price: lvl.price,
          color: lvl.color ?? (lvl.kind === 'support' ? '#22c55e' : '#ef4444'),
          lineWidth: 2,
          lineStyle: lvl.lineStyle ?? 2,
          axisLabelVisible: true,
          title: lvl.title,
        })
        this.linesMap.set(`${lvl.title}_${lvl.price}`, priceLine)
      } catch {
        // 捕获可能已销毁的 series 引用
      }
    })
  }

  public static clearOverlays(series: any) {
    if (!series || typeof series.removePriceLine !== 'function') {
      this.linesMap.clear()
      return
    }

    this.linesMap.forEach((line) => {
      try {
        series.removePriceLine(line)
      } catch {
        // ignore
      }
    })
    this.linesMap.clear()
  }
}
