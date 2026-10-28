/**
 * 参谋结论 → K 线价位线：**唯一的服务端口径入口**。
 *
 * ## 语义（2026-10-28 P2 三次收口，前两轮都是"疑似核验"）
 *
 * 第一轮：调用方自己传 `{source,actionId,status,symbol}` 就画 —— 那是**自证清白**。
 * 第二轮：全关掉（`LINK_READY=false`），不再伪造，但确认后也确实上不了图。
 * 本轮：只认 `GET /copilot/annotations` 的**服务端行**（解析见 `./serverAnnotations`）。
 * 本地数字（回答正文正则提价、罗盘跳转带过来的价位、sessionStorage 里的历史行）
 * **一律不画**，也不再把它们当"凭证"。
 *
 * 具体三件事：
 *
 *  1. {@link ChartOverlayService.saveOverlay} 保留旧签名但**恒拒**（`client_authored_levels`）：
 *     入参价位由调用方书写，不是服务端给的。老调用点得到的是明确失败，不会静默"升级"成能画线。
 *  2. {@link ChartOverlayService.acceptServerLines} 是唯一写入口：要求数组由
 *     `serverAnnotations` 从服务端响应构造、每行标的/周期与当前 K 线一致、价位合法。
 *  3. 会话存储只存**服务端标注 id**（不存价位、不存 actionId）：刷新后拿 id 回服务端重读，
 *     读不到就一条不画。伪造 id 最多只能"指定看哪条服务端行"，改变不了画出来的价位
 *     （价位逐字来自服务端行），也改变不了服务端的作用域过滤。
 */

import { safeJwtPayload } from '@/lib/jwt'
import {
  isServerVerifiedLines,
  normSymbol,
  type ServerAnnotationLine,
} from './serverAnnotations'

/** 旧接口的入参形状（罗盘/正文价位）；**不是**可画线的凭证。 */
export interface PriceLevelOverlay {
  price: number
  title: string
  color?: string
  kind?: 'support' | 'pressure'
  lineStyle?: number
}

/** 兼容形状：调用方声明"这是服务端标注"，声明本身不参与任何判断。 */
export interface OverlayProvenance {
  source: 'server_annotation'
  actionId: number
  status: 'applied'
  symbol: string
}

export type OverlayRejectReason =
  | 'no_symbol'
  | 'no_identity'
  /** 入参价位由客户端书写（旧 saveOverlay 路径） */
  | 'client_authored_levels'
  /** 数组不是从服务端响应解析出来的 */
  | 'unverified_lines'
  /** 标注标的/周期与当前 K 线不一致 */
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
    case 'client_authored_levels':
      return '未画线：价位必须来自服务端标注记录，回答正文或本地构造的数字不作数。'
    case 'unverified_lines':
      return '未画线：这组价位不是从服务端标注响应解析出来的（本地声明不构成凭证）。'
    case 'symbol_mismatch':
      return '未画线：标注的标的或周期与当前 K 线不一致。'
    case 'invalid_levels':
      return '未画线：价位数据非法（需要正的有限价格与支撑/压力类型）。'
    case 'storage_unavailable':
      return '画线未保存：浏览器不允许会话存储。'
    default:
      return '未画线：来源未确认。'
  }
}

/** 本会话"我确认过"的服务端标注 id 信封（只有 id，没有价位/动作号）。 */
export interface ConfirmedAnnotationEnvelope {
  symbol: string
  period: string
  annotationIds: number[]
}

/** 全局叠加事件（画线/清除）。detail.lines 必须通过 `isServerVerifiedLines`。 */
export const OVERLAY_EVENT = 'sida-kline-overlay'
/** 标注链路状态播报（确认成功/失败/撤销不可用），detail.message 可直接展示。 */
export const ANNOTATION_STATUS_EVENT = 'sida-kline-annotation-status'

/** 新口径存储键：只放服务端标注 id（旧的 `sida_kline_overlay:v2:*` 不再读取）。 */
const STORAGE_PREFIX = 'sida_kline_annotation_ids:v1:'

/** 从 JWT 取账号（`sub`/`user_id`）。取不到 = 未登录，一律不写不读。 */
function currentAccountId(): string | null {
  try {
    if (typeof localStorage === 'undefined') return null
    const claims = safeJwtPayload(localStorage.getItem('token') || '')
    const id = claims?.sub ?? claims?.user_id
    if ((typeof id !== 'string' && typeof id !== 'number') || !String(id).trim()) return null
    return String(id)
  } catch {
    return null
  }
}

/**
 * 本地是否有身份（未登录时**不要**发服务端标注读取：后端 401 会触发全局登出，
 * 匿名访客看一眼 K 线不该被踢出去）。只是"有没有 token"，不是授权判断。
 */
export function hasLocalIdentity(): boolean {
  return currentAccountId() !== null
}

function storageKey(symbol: string, period: string): string | null {
  if (typeof sessionStorage === 'undefined') return null
  const account = currentAccountId()
  const sym = normSymbol(symbol)
  if (!account || !sym) return null
  return `${STORAGE_PREFIX}${encodeURIComponent(account)}:${encodeURIComponent(sym)}:${encodeURIComponent(
    (period || '1d').trim(),
  )}`
}

/** 服务端行信封校验：只认标注 id 列表，其余一律丢弃。 */
function parseEnvelope(raw: string): number[] | null {
  try {
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    const ids = (parsed as { annotationIds?: unknown }).annotationIds
    if (!Array.isArray(ids)) return null
    return ids.filter(
      (id): id is number => typeof id === 'number' && Number.isInteger(id) && id > 0,
    )
  } catch {
    return null
  }
}

export class ChartOverlayService {
  private static linesMap: Map<string, any> = new Map()

  /**
   * **旧路径：恒拒。**
   *
   * 入参 `levels` 是调用方书写的数字（正文正则提价、罗盘跳转传来的价位），
   * 不对应任何服务端标注行。保留这个方法只为让老调用点得到**明确失败**，
   * 而不是悄悄降级成"能画线"。真正的入口是 {@link acceptServerLines}。
   */
  public static saveOverlay(
    symbol: string | undefined,
    _levels: PriceLevelOverlay[],
    _provenance?: OverlayProvenance | null,
  ): SaveOverlayResult {
    if (!normSymbol(symbol)) {
      return { saved: false, reason: 'no_symbol', message: overlayRejectMessage('no_symbol') }
    }
    if (!currentAccountId()) {
      return { saved: false, reason: 'no_identity', message: overlayRejectMessage('no_identity') }
    }
    // 显式忽略 _levels/_provenance：都是客户端对象，任何"看起来合法"的字段都不改变结论。
    return {
      saved: false,
      reason: 'client_authored_levels',
      message: overlayRejectMessage('client_authored_levels'),
    }
  }

  /**
   * 接受**服务端响应解析出的**价位线（唯一写入口）。
   *
   * 校验四件事，任一不过整批拒（不写半截）：
   *  ① 数组必须由 `serverAnnotations` 从服务端响应构造；
   *  ② 每行标的是当前 K 线；③ 每行周期与当前 K 线一致；
   *  ④ 价位为正的有限数、label 非空、annotationId 合法。
   */
  public static acceptServerLines(
    symbol: string,
    period: string,
    lines: unknown,
  ): SaveOverlayResult {
    const sym = normSymbol(symbol)
    const per = (period || '1d').trim()
    if (!sym) {
      return { saved: false, reason: 'no_symbol', message: overlayRejectMessage('no_symbol') }
    }
    if (!currentAccountId()) {
      return { saved: false, reason: 'no_identity', message: overlayRejectMessage('no_identity') }
    }
    if (!isServerVerifiedLines(lines)) {
      return {
        saved: false,
        reason: 'unverified_lines',
        message: overlayRejectMessage('unverified_lines'),
      }
    }
    const valid =
      lines.length > 0 &&
      lines.every(
        (line) =>
          normSymbol(line.symbol) === sym &&
          (line.period || '1d').trim() === per &&
          typeof line.price === 'number' &&
          Number.isFinite(line.price) &&
          line.price > 0 &&
          (line.kind === 'support' || line.kind === 'pressure') &&
          typeof line.label === 'string' &&
          line.label.trim().length > 0 &&
          Number.isInteger(line.annotationId) &&
          line.annotationId > 0,
      )
    if (!valid) {
      const reason: OverlayRejectReason =
        lines.length === 0 ? 'invalid_levels' : 'symbol_mismatch'
      return { saved: false, reason, message: overlayRejectMessage(reason) }
    }

    const key = storageKey(sym, per)
    if (!key) {
      return {
        saved: false,
        reason: 'storage_unavailable',
        message: overlayRejectMessage('storage_unavailable'),
      }
    }
    /**
     * 与既有信封**合并**而不是覆盖：用户可能先后确认支撑、阻力两条（两次 apply 各回读一条），
     * 覆盖会把先确认的那条从"我确认过"里抹掉，刷新时凭空少一条线。
     * 合并的仍然只有服务端标注 id，脏 id 会被服务端读取结果过滤掉（`pickConfirmedRows`）。
     */
    const previous = ChartOverlayService.getRequestedAnnotationIds(sym, per) ?? []
    const envelope: ConfirmedAnnotationEnvelope = {
      symbol: sym,
      period: per,
      annotationIds: Array.from(
        new Set([...previous, ...lines.map((line) => line.annotationId)]),
      ).sort((a, b) => a - b),
    }
    try {
      sessionStorage.setItem(key, JSON.stringify(envelope))
    } catch {
      return {
        saved: false,
        reason: 'storage_unavailable',
        message: overlayRejectMessage('storage_unavailable'),
      }
    }

    ChartOverlayService.broadcast(sym, per, lines)
    return { saved: true, message: `已按服务端标注画线 ${lines.length} 条。` }
  }

  /**
   * 本会话里"我确认过"的服务端标注 id（按账号+标的+周期分键）。
   *
   * 只返回 id —— 价位永远回服务端重读。换账号/换标的读不到彼此的信封，
   * 服务端还会再按 user_id 过滤一层。
   */
  public static getRequestedAnnotationIds(symbol: string, period: string): number[] | null {
    const key = storageKey(symbol, period)
    if (!key || typeof sessionStorage === 'undefined') return null
    let raw: string | null = null
    try {
      raw = sessionStorage.getItem(key)
    } catch {
      return null
    }
    if (!raw) return null
    const ids = parseEnvelope(raw)
    return ids && ids.length > 0 ? ids : null
  }

  /** 服务端已不给这些 id 了（撤销/过期）→ 把信封收敛到还活着的那些。 */
  public static pruneRequestedAnnotationIds(
    symbol: string,
    period: string,
    alive: readonly number[],
  ) {
    const key = storageKey(symbol, period)
    if (!key || typeof sessionStorage === 'undefined') return
    const keep = Array.from(new Set(alive.filter((id) => Number.isInteger(id) && id > 0)))
    try {
      if (keep.length === 0) sessionStorage.removeItem(key)
      else
        sessionStorage.setItem(
          key,
          JSON.stringify({
            symbol: normSymbol(symbol),
            period: (period || '1d').trim(),
            annotationIds: keep,
          }),
        )
    } catch {
      // 存不进去不影响本次显示：这次仍按服务端行画，只是刷新后不再自动恢复。
    }
  }

  /**
   * 清除某标的的画线并广播清除事件（登出/换标的时调用）。
   * 存储键按账号隔离：登出（无 token）时不触碰任何账号的数据。
   */
  public static clearOverlay(symbol: string, period?: string) {
    const sym = normSymbol(symbol)
    const per = (period || '1d').trim()
    const key = storageKey(sym, per)
    if (!key) return
    if (typeof sessionStorage !== 'undefined') {
      try {
        sessionStorage.removeItem(key)
      } catch {
        // ignore
      }
    }
    ChartOverlayService.broadcast(sym, per, [])
  }

  /** 读取并校验 `sida-kline-overlay` 事件 detail（外部 detail 同样要过服务端标记）。 */
  public static readOverlayEventDetail(
    detail: unknown,
  ): { symbol: string; period: string; lines: ServerAnnotationLine[] } | null {
    if (!detail || typeof detail !== 'object') return null
    const d = detail as { symbol?: unknown; period?: unknown; lines?: unknown }
    const symbol = normSymbol(typeof d.symbol === 'string' ? d.symbol : '')
    if (!symbol) return null
    const period = typeof d.period === 'string' && d.period.trim() ? d.period.trim() : '1d'
    if (!isServerVerifiedLines(d.lines)) return null
    const lines = d.lines.filter(
      (line) =>
        normSymbol(line.symbol) === symbol && (line.period || '1d').trim() === period,
    )
    return { symbol, period, lines }
  }

  /**
   * 派发叠加事件（内部用）。外部构造不出"合法 detail" —— lines 必须已过服务端标记，
   * 而标记只在 `serverAnnotations` 的解析路径里产生。
   */
  private static broadcast(symbol: string, period: string, lines: ServerAnnotationLine[]) {
    if (typeof window === 'undefined') return
    window.dispatchEvent(new CustomEvent(OVERLAY_EVENT, { detail: { symbol, period, lines } }))
  }

  /** 派发标注链路状态（确认失败/撤销不可用等），detail.message 可直接展示给用户。 */
  public static reportAnnotationStatus(payload: {
    symbol?: string
    level: 'info' | 'warn' | 'error'
    message: string
  }) {
    if (typeof window === 'undefined') return
    window.dispatchEvent(
      new CustomEvent(ANNOTATION_STATUS_EVENT, {
        detail: {
          symbol: normSymbol(payload.symbol),
          level: payload.level,
          message: payload.message,
        },
      }),
    )
  }

  /** 兼容方法：旧调用点走它 → 与 {@link saveOverlay} 同样恒拒。 */
  public static emitOverlay(
    symbol: string | undefined,
    levels: PriceLevelOverlay[],
    provenance?: OverlayProvenance | null,
  ): SaveOverlayResult {
    return this.saveOverlay(symbol, levels, provenance)
  }

  /**
   * 图元级原语：把**服务端标注**价位线画到 series 上。
   *
   * 非服务端构造的数组 → 拒画（并清掉既有线）。返回是否真的画了。
   */
  public static injectDecisionLevels(series: any, lines: unknown): boolean {
    if (!series || typeof series.createPriceLine !== 'function') {
      return false
    }

    // 先清除已有的旧画线
    this.clearOverlays(series)

    if (!isServerVerifiedLines(lines) || lines.length === 0) return false

    let drawn = 0
    lines.forEach((line) => {
      if (typeof line.price !== 'number' || !Number.isFinite(line.price) || line.price <= 0) return
      try {
        const priceLine = series.createPriceLine({
          price: line.price,
          color: line.kind === 'support' ? '#22c55e' : '#ef4444',
          lineWidth: 2,
          lineStyle: 2,
          axisLabelVisible: true,
          title: line.label,
        })
        this.linesMap.set(String(line.annotationId), priceLine)
        drawn += 1
      } catch {
        // 捕获可能已销毁的 series 引用
      }
    })
    return drawn > 0
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
