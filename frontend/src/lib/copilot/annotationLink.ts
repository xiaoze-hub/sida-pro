/**
 * 服务端情景标注 → K 线：**唯一接线处**（2026-10-28 P2 三次收口）。
 *
 * 两条入口，事实源都只有服务端响应：
 *
 *  1. {@link restoreServerAnnotations} —— 页面进入 / 切标的 / 切周期时，
 *     拿本会话"我确认过"的标注 id 去 `GET /copilot/annotations` 重读，
 *     只画**服务端还在、且 id 对得上**的行。服务端不给的（撤销/过期/跨账号）一条不画。
 *  2. {@link linkConfirmedAnnotation} —— 用户点确认后的 apply 响应进来，
 *     用 apply 响应证明"哪个动作、什么状态、什么价位"，再回读标注列表做核对，
 *     两边对上才画（见 `corroborateWithApply`）。
 *
 * 两者都不接受任何本地数字：价位、标签、标的全部来自服务端响应字段；
 * 客户端能提供的只有"我请求过哪些标注 id"。
 */

import { fetchAPI } from '@sida/api'

import { ChartOverlayService, hasLocalIdentity } from './chartOverlayService'
import {
  annotationLinesFromRows,
  annotationsUrl,
  corroborateWithApply,
  markServerVerified,
  normSymbol,
  parseAnnotationsResponse,
  parseApplyResponse,
  pickConfirmedRows,
  type AnnotationRequest,
  type ServerAnnotationLine,
  type ServerAnnotationRow,
} from './serverAnnotations'

/** 图表侧看到的标注链路状态（文案直接可展示，不在这里再加工）。 */
export type AnnotationLinkState =
  | { kind: 'idle' }
  | { kind: 'ready'; lines: ServerAnnotationLine[]; annotationIds: number[]; actionIds: number[] }
  /** 服务端有记录，但本会话没有可配对的确认凭证 → 不画，说明原因 */
  | { kind: 'unattributed'; serverRows: number; message: string }
  | { kind: 'error'; message: string }

/** 读取超时：标注是增强层，不能拖住主图（主图自己有 20s 超时）。 */
const READ_TIMEOUT_MS = 8000

interface FetchRowsOk {
  ok: true
  rows: ServerAnnotationRow[]
}
interface FetchRowsFail {
  ok: false
  message: string
}

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return '网络异常'
}

/**
 * 读服务端标注（JWT 作用域内，本人 + 全局）。
 *
 * `cacheMode: 'reload'` 是**必须**的：`fetchAPI` 对 GET 有 30s 进程缓存，
 * 用户刚确认完就回读时若命中旧缓存，会读不到刚落库的那一行 —— 那会把
 * "确认成功"误判成"服务端没有对应记录"。
 */
async function fetchRows(
  request: AnnotationRequest,
): Promise<FetchRowsOk | FetchRowsFail> {
  let raw: unknown
  try {
    raw = await fetchAPI<unknown>(annotationsUrl(request), {
      timeoutMs: READ_TIMEOUT_MS,
      cacheMode: 'reload',
    })
  } catch (err) {
    return { ok: false, message: errorText(err) }
  }
  const parsed = parseAnnotationsResponse(raw)
  if (!parsed.ok) return { ok: false, message: parsed.message }
  return { ok: true, rows: parsed.value.rows }
}

/** 状态就绪时的载荷（id 全部取自服务端行）。供事件驱动的调用方复用，避免两处各算一遍。 */
export function annotationReadyState(lines: ServerAnnotationLine[]): AnnotationLinkState {
  return {
    kind: 'ready',
    lines,
    annotationIds: Array.from(new Set(lines.map((line) => line.annotationId))).sort((a, b) => a - b),
    actionIds: Array.from(
      new Set(
        lines
          .map((line) => line.actionId)
          .filter((id): id is number => typeof id === 'number' && Number.isInteger(id) && id > 0),
      ),
    ),
  }
}

/**
 * 进页面/换标的/换周期时的恢复：**只画服务端确认还在的行**。
 *
 * 失败语义（都不画，且如实说）：
 *  - 读取失败（网络/HTTP/响应形状不符）→ `error`，带服务端或接口给的原因；
 *  - 本会话没有确认记录但服务端有记录 → `unattributed`：**不画**。
 *    原因见 `SERVER_ANNOTATION_CONTRACT`：响应不带 action 关联与归属（"我的" vs "全局"），
 *    无法证明确认动作属于本账号，硬画等于把别人的/历史的行为算成本人确认；
 *  - 确认过的 id 在服务端已不存在 → `unattributed` 并清掉本地信封（服务端才是事实源）。
 */
export async function restoreServerAnnotations(
  request: AnnotationRequest,
): Promise<AnnotationLinkState> {
  const symbol = normSymbol(request.symbol)
  const period = (request.period || '1d').trim()
  if (!symbol) return { kind: 'idle' }
  // 未登录不发请求：后端 401 会触发全局登出，匿名看 K 线不该被踢。
  if (!hasLocalIdentity()) return { kind: 'idle' }

  const ids = ChartOverlayService.getRequestedAnnotationIds(symbol, period)
  const res = await fetchRows({ symbol, market: request.market, period })
  if (!res.ok) {
    return { kind: 'error', message: `读取服务端标注失败：${res.message}（未画线）` }
  }

  if (!ids) {
    if (res.rows.length === 0) return { kind: 'idle' }
    return {
      kind: 'unattributed',
      serverRows: res.rows.length,
      message: `服务端有 ${res.rows.length} 条该标的标注记录，但本会话没有对应的确认凭证（接口不返回动作溯源与归属，无法判断是否本账号确认过），本次未上图。`,
    }
  }

  const picked = pickConfirmedRows(res.rows, ids)
  const lines = annotationLinesFromRows(picked, { symbol, market: request.market, period })
  if (lines.length === 0) {
    // 服务端已经没有这些行（撤销/过期/换账号）→ 本地信封作废，绝不"凭历史画线"。
    ChartOverlayService.pruneRequestedAnnotationIds(symbol, period, [])
    return {
      kind: 'unattributed',
      serverRows: res.rows.length,
      message: '之前确认过的标注在服务端已不存在（可能已撤销或有效期过了），未画线。',
    }
  }

  const accepted = ChartOverlayService.acceptServerLines(symbol, period, markServerVerified(lines))
  if (!accepted.saved) return { kind: 'error', message: accepted.message }
  ChartOverlayService.pruneRequestedAnnotationIds(
    symbol,
    period,
    lines.map((line) => line.annotationId),
  )
  return annotationReadyState(lines)
}

/**
 * 确认后的接线（由 `applyPendingAction` 调用，永不抛错 —— 确认本身已经成功，
 * 图表侧失败不能反过来把确认报成失败）。
 *
 * 顺序：apply 响应（证明动作/状态/价位）→ 回读标注列表（证明服务端真的落了这条线）→
 * 两边对得上才画。任何一步不成立都不画，并把原因播报给界面。
 */
export async function linkConfirmedAnnotation(
  applyRaw: unknown,
): Promise<AnnotationLinkState | null> {
  try {
    const parsedApply = parseApplyResponse(applyRaw)
    if (!parsedApply.ok) {
      // 加自选/盯盘提醒不是图表标注 → 静默。标注动作形状不对才提示。
      if (parsedApply.reason !== 'not_annotation') {
        ChartOverlayService.reportAnnotationStatus({
          level: 'warn',
          message: `动作已确认，但无法核对图表标注：${parsedApply.message}`,
        })
      }
      return null
    }

    const applied = parsedApply.value
    const request: AnnotationRequest = {
      symbol: applied.symbol,
      market: applied.market,
      period: applied.period,
    }
    const res = await fetchRows(request)
    if (!res.ok) {
      ChartOverlayService.reportAnnotationStatus({
        symbol: applied.symbol,
        level: 'warn',
        message: `已确认，但读取服务端标注失败：${res.message}。本次未画线。`,
      })
      return { kind: 'error', message: `读取服务端标注失败：${res.message}（未画线）` }
    }

    const corroborated = corroborateWithApply(applied, res.rows, request)
    if (!corroborated.ok) {
      ChartOverlayService.reportAnnotationStatus({
        symbol: applied.symbol,
        level: 'warn',
        message: corroborated.message,
      })
      return null
    }

    const lines = markServerVerified(corroborated.value)
    const accepted = ChartOverlayService.acceptServerLines(
      applied.symbol,
      applied.period,
      lines,
    )
    if (!accepted.saved) {
      ChartOverlayService.reportAnnotationStatus({
        symbol: applied.symbol,
        level: 'error',
        message: accepted.message,
      })
      return { kind: 'error', message: accepted.message }
    }

    ChartOverlayService.reportAnnotationStatus({
      symbol: applied.symbol,
      level: 'info',
      message: `已确认并上图：${lines
        .map((line) => `${line.label} ${line.price}`)
        .join('、')}（服务端标注 #${lines.map((line) => line.annotationId).join('、')}，动作 #${applied.actionId}）`,
    })
    return annotationReadyState(lines)
  } catch (err) {
    // 兜底：接线层自己的 bug 不能污染"确认成功"的结论。
    ChartOverlayService.reportAnnotationStatus({
      level: 'warn',
      message: `图表标注接线异常：${errorText(err)}`,
    })
    return null
  }
}
