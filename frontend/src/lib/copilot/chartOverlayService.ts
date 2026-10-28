/**
 * 参谋结论与 TradingView / Lightweight Charts 图元同步画线服务
 *
 * ## 2026-10-28 P2（二次收口）：服务端标注链路未接通前，本地凭证一律不可信
 *
 * 上一版止血要求调用方传 `{source:'server_annotation', actionId, status:'applied', symbol}`
 * 才允许画线，但那只是一个**普通 JS 对象**：客户端代码、控制台、扩展脚本都能原地构造，
 * 它不携带任何服务端签名、不绑定价格、也不对应 `CopilotAction` 表里的真实记录。
 * 后端虽然已有 `GET /copilot/annotations`（本人+全局），但**前端从未调用它**，
 * 也没有任何服务端校验会核对这份客户端声明 —— 所以把它当"可信凭证"本身就是假的核验。
 *
 * 本次收口：引入唯一开关 {@link SERVER_ANNOTATION_LINK_READY}，在真实服务端标注读取
 * （JWT 作用域内 `GET /copilot/annotations`）接通之前恒为 `false`：
 *
 *   - `saveOverlay` 无论收到什么 provenance（含完全伪造的"合法"对象）都拒画，
 *     不写 sessionStorage、不广播事件；
 *   - `getStoredOverlay` 不再读取会话存储 —— 伪造的 source/actionId 行无法"恢复"上图；
 *   - `injectDecisionLevels` 同样拒绝，本地调用方拿不到图元级旁路。
 *
 * 接通那条链路时唯一合法的数据来源是**服务端响应**（按本人+全局、symbol/period 过滤），
 * 不得再把调用方声明当凭证。
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
  /** 仅供未来服务端读回的数据打标；当前不会由本地写入产生 */
  source?: 'server_annotation'
  /** 服务端 CopilotAction id（未来由服务端响应提供，本地值不可信） */
  actionId?: number
}

/**
 * 服务端标注通道是否已接通（唯一事实来源）。
 *
 * `false` 时：本地对象与 sessionStorage **都不是**可信来源，任何声称"已确认"的
 * payload 都会被拒。只有 `GET /copilot/annotations` 的真实读取接线完成、
 * 并由服务端响应驱动画线后，才允许改为 `true`（届时 `saveOverlay` 的入参需换成
 * 服务端记录，而不是调用方声明的对象）。
 */
export const SERVER_ANNOTATION_LINK_READY: boolean = false

/** 历史常量：仅作标记名保留；当前没有任何本地数据能凭它获得信任。 */
export const OVERLAY_PROVENANCE_SOURCE = 'server_annotation' as const

/**
 * 调用方声明的来源描述 —— **不是凭证**。
 *
 * 保留该类型只为兼容既有调用签名与日志；内容完全由客户端构造，不参与任何信任判断。
 * 真实核验必须发生在服务端：客户端只能消费 `/copilot/annotations` 的响应。
 */
export interface OverlayProvenance {
  source: typeof OVERLAY_PROVENANCE_SOURCE
  actionId: number
  status: 'applied'
  /** 调用方声称的标的，同样不可信 */
  symbol: string
}

export type OverlayRejectReason =
  | 'no_symbol'
  | 'no_identity'
  | 'server_link_unavailable'
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
    case 'server_link_unavailable':
      return '未画线：服务端标注通道尚未接通，本地凭证无法核验；确认服务端提议后，价位暂也不会自动画到 K 线上。'
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

/** 通道未接通时的统一拒画结果（保存 / 绘制共用一处文案）。 */
function linkUnavailable(): SaveOverlayResult {
  return {
    saved: false,
    reason: 'server_link_unavailable',
    message: overlayRejectMessage('server_link_unavailable'),
  }
}

export class ChartOverlayService {
  private static linesMap: Map<string, any> = new Map()

  /**
   * 把价位线持久化到会话存储并广播全局事件。
   *
   * **当前恒拒**：服务端标注读取未接通，`provenance` 只是客户端声明，不构成凭证。
   * 因此无论传入何种对象（含完全伪造的"已确认"payload）都不写存储、不发事件，
   * 只返回可展示的原因，避免把本地数据冒充已核验图表标注。
   */
  public static saveOverlay(
    symbol: string | undefined,
    levels: PriceLevelOverlay[],
    _provenance?: OverlayProvenance | null,
  ): SaveOverlayResult {
    const sym = normSymbol(symbol)
    if (!sym) {
      return { saved: false, reason: 'no_symbol', message: overlayRejectMessage('no_symbol') }
    }
    if (!storageKey(sym)) {
      return { saved: false, reason: 'no_identity', message: overlayRejectMessage('no_identity') }
    }
    if (!SERVER_ANNOTATION_LINK_READY) {
      // 显式忽略 _provenance：它不是凭证，任何"看起来合法"的字段都不改变结论。
      return linkUnavailable()
    }

    // 通道接通后才走这里：入参必须是服务端 `/copilot/annotations` 返回的记录，
    // 不得由调用方声明。价位仍照常做合法性校验。
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
    try {
      sessionStorage.setItem(key, JSON.stringify(lines))
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
          detail: { symbol: sym, lines },
        }),
      )
    }
    return { saved: true, message: '画线已保存。' }
  }

  /**
   * 从会话存储读取某标的的持久化画线。
   *
   * 通道未接通时**恒返回 `null`**：会话存储里的内容（包括带 `source`/`actionId`
   * 的伪造行）没有任何服务端校验可对照，一律不还原上图。
   */
  public static getStoredOverlay(symbol: string): KlinePriceLinePayload[] | null {
    if (!SERVER_ANNOTATION_LINK_READY) return null
    const key = storageKey(symbol)
    if (!key || typeof sessionStorage === 'undefined') return null
    try {
      const raw = sessionStorage.getItem(key)
      if (!raw) return null
      const parsed = JSON.parse(raw)
      if (!Array.isArray(parsed)) return null
      const lines = parsed.filter((line): line is KlinePriceLinePayload => {
        if (!line || typeof line !== 'object') return false
        if (typeof line.price !== 'number' || !Number.isFinite(line.price) || line.price <= 0) {
          return false
        }
        return line.kind === 'support' || line.kind === 'pressure'
      })
      return lines.length > 0 ? lines : null
    } catch {
      return null
    }
  }

  /**
   * 清除某标的的画线并广播清除事件。
   *
   * 存储键按账号隔离：登出（无 token）时不触碰任何账号的数据。
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
   * 将价位线画到图表系列上（图元级原语）。
   *
   * 通道未接通时**拒画**并清空既有线：这个方法过去没有任何来源校验，是绕过
   * `saveOverlay` 直接把本地数字画上真 K 线的旁路。返回是否真的画了。
   */
  public static injectDecisionLevels(
    series: any,
    levels: PriceLevelOverlay[],
    _provenance?: OverlayProvenance | null,
  ): boolean {
    if (!series || typeof series.createPriceLine !== 'function') {
      return false
    }

    // 先清除已有的旧画线
    this.clearOverlays(series)

    if (!SERVER_ANNOTATION_LINK_READY) return false

    const lines = normalizeLevels(levels)
    if (!lines) return false

    lines.forEach((lvl) => {
      try {
        const priceLine = series.createPriceLine({
          price: lvl.price,
          color: lvl.kind === 'support' ? '#22c55e' : '#ef4444',
          lineWidth: 2,
          lineStyle: 2,
          axisLabelVisible: true,
          title: lvl.label,
        })
        this.linesMap.set(`${lvl.label}_${lvl.price}`, priceLine)
      } catch {
        // 捕获可能已销毁的 series 引用
      }
    })
    return true
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
