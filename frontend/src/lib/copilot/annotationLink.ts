/**
 * 服务端情景标注 → K 线：**唯一接线处**（2026-10-28 P2 四收口）。
 *
 * 两个入口，事实源都只有服务端响应，最终都写进同一个按目标分键的账本
 * （`annotationLedger`）：
 *
 *  1. {@link requestAnnotations} —— 进页面 / 切标的 / 切周期时按**精确目标**读取
 *     （`market_strict=1&mine_only=1&action_linked=1`）。服务端只回"我的、有确认动作的、
 *     同一标的同一市场同一周期"的行；读不到就是没有。
 *  2. {@link linkConfirmedAnnotation} —— 用户点确认后的 apply 响应进来：用响应的
 *     `annotation_id` **精确回读那一条**，核对属主/标的/市场/周期/动作/内容，全部一致才画。
 *
 * 两条入口都不接受任何本地数字，也不读 sessionStorage：
 * 上一版的"本会话确认过的标注 id 信封"已删除 —— 它是客户端自证，刷新/换标签页就会
 * 出现"有 id 但服务端已经没有"或"服务端有但本地没记"的两头不实。
 */

import { fetchAPI } from '@sida/api'

import { AnnotationLedger, currentAccountId, type AnnotationSnapshot } from './annotationLedger'
import {
  annotationsUrl,
  linesForTarget,
  marketMatches,
  normSymbol,
  parseAnnotationsResponse,
  parseApplyResponse,
  targetKey,
  targetOfApplied,
  targetRefusal,
  verifyAppliedRow,
  type AnnotationTarget,
  type ReadOptions,
  type ServerAnnotationLine,
  type ServerAnnotationRow,
} from './serverAnnotations'

/** 读取超时：标注是增强层，不能拖住主图（主图自己有 20s 超时）。 */
const READ_TIMEOUT_MS = 8000

type FetchRowsResult =
  | { ok: true; rows: ServerAnnotationRow[]; dropped: number }
  | { ok: false; message: string }

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return '网络异常'
}

/**
 * 读服务端标注（JWT 作用域内）。
 *
 * `cacheMode: 'reload'` 是**必须**的：`fetchAPI` 对 GET 有 30s 进程缓存，
 * 用户刚确认完就回读时若命中旧缓存，会读不到刚落库的那一行 —— 那会把
 * "确认成功"误判成"服务端没有对应记录"。
 */
async function fetchRows(
  target: AnnotationTarget,
  options: ReadOptions = {},
): Promise<FetchRowsResult> {
  let raw: unknown
  try {
    raw = await fetchAPI<unknown>(annotationsUrl(target, options), {
      timeoutMs: READ_TIMEOUT_MS,
      cacheMode: 'reload',
    })
  } catch (err) {
    return { ok: false, message: errorText(err) }
  }
  const parsed = parseAnnotationsResponse(raw)
  if (!parsed.ok) return { ok: false, message: parsed.message }
  return { ok: true, rows: parsed.value.rows, dropped: parsed.value.dropped }
}

/**
 * 读取某个精确目标的标注并写进账本。
 *
 * 语义（都不画，且如实说）：
 *  - 目标本身不可画（未登录/指数/同号代码）→ `refused`（连请求都不发）；
 *  - 读取失败 → `error`（可重试）；
 *  - 服务端没有可画的行 → `idle`（干净空态，不报错）；
 *  - 有行但一条都核对不过（无属主/无动作/目标不符）→ `refused` + 逐条原因。
 */
export async function requestAnnotations(
  target: AnnotationTarget | null,
  options: ReadOptions = {},
): Promise<AnnotationSnapshot> {
  if (!target) {
    AnnotationLedger.setCurrent(null)
    return AnnotationLedger.snapshot(null)
  }
  const refusal = targetRefusal(target)
  if (refusal) return AnnotationLedger.setRefused(target, refusal)

  const seq = AnnotationLedger.beginRequest(target)
  const res = await fetchRows(target, options)
  if (!res.ok) {
    AnnotationLedger.setError(target, `读取服务端标注失败：${res.message}（未画线）`)
    return AnnotationLedger.snapshot(target)
  }

  const verdict = linesForTarget(res.rows, target)
  const notes: string[] = []
  if (res.dropped > 0) notes.push(`${res.dropped} 行缺目标/属主字段被丢弃`)
  if (verdict.merged > 0) notes.push(`合并了 ${verdict.merged} 条同价重复行`)

  let snap: AnnotationSnapshot
  if (verdict.lines.length > 0) {
    snap = {
      key: targetKey(target),
      phase: 'ready',
      lines: verdict.lines,
      annotationIds: Array.from(new Set(verdict.lines.map((l) => l.annotationId))).sort((a, b) => a - b),
      actionIds: Array.from(new Set(verdict.lines.map((l) => l.actionId))).sort((a, b) => a - b),
      message: notes.length > 0 ? `已按服务端标注画线 ${verdict.lines.length} 条（${notes.join('；')}）` : '',
      notice: '',
      noticeLevel: 'info',
      retryable: false,
    }
  } else if (verdict.rejected.length > 0) {
    // 服务端有行，但一条都不该画 —— 说清第一条原因，别让用户以为"没数据"
    snap = {
      key: targetKey(target),
      phase: 'refused',
      lines: [],
      annotationIds: [],
      actionIds: [],
      message: `服务端有 ${res.rows.length} 条标注，但没有一条能核对到当前 K 线：${verdict.rejected[0]}`,
      notice: '',
      noticeLevel: 'info',
      retryable: true,
    }
  } else {
    snap = {
      key: targetKey(target),
      phase: 'idle',
      lines: [],
      annotationIds: [],
      actionIds: [],
      message: '',
      notice: '',
      noticeLevel: 'info',
      retryable: false,
    }
  }
  AnnotationLedger.commit(target, seq, snap)
  return AnnotationLedger.snapshot(target)
}

/**
 * 确认后的接线（由 `applyPendingAction` 调用，永不抛错 —— 确认本身已经成功，
 * 图表侧失败不能反过来把确认报成失败）。
 *
 * 顺序：apply 响应（动作 id + annotation_id + 落库明细）→ 按 id 精确回读 →
 * 核对属主/标的/市场/周期/动作/内容 → 一致才把线交给当前目标的账本。
 *
 * 关键诚实点（复审红线 ③）：确认落库的周期与当前 K 线不一致时**不画**，并明确说
 * "落库在 1d、当前是 5m"——后端 propose 不带视图周期，非日周期确认确实落在日线上，
 * 不能假装已经画到眼前这张图。
 */
export async function linkConfirmedAnnotation(
  applyRaw: unknown,
): Promise<AnnotationSnapshot | null> {
  try {
    const parsedApply = parseApplyResponse(applyRaw)
    if (!parsedApply.ok) {
      if (parsedApply.reason === 'not_annotation') return null // 加自选/盯盘提醒不是图表标注
      const current = AnnotationLedger.currentTarget()
      if (current) {
        AnnotationLedger.notice(current, `动作已确认，但无法核对图表标注：${parsedApply.message}`, 'warn')
      }
      return current ? AnnotationLedger.snapshot(current) : null
    }

    const applied = parsedApply.value
    const owner = currentAccountId() ?? ''
    const target = targetOfApplied(applied, owner)
    const current = AnnotationLedger.currentTarget()

    const refusal = targetRefusal(target)
    if (refusal) {
      // 落在不可画的目标上（未登录/指数/同号代码）：挂在当前视图上说清楚
      if (current) AnnotationLedger.notice(current, `已确认标注 #${applied.annotationId}，但${refusal}`, 'warn')
      return current ? AnnotationLedger.snapshot(current) : null
    }

    if (!current) return null // 没打开个股 K 线：确认成功即可，不凭空造目标
    // 目标是否"同一张图"必须按**可证明的同一标的**判定，不能拿字符串 key 比：
    // 落库市场是交易所（SH），页面只有 CN —— 键不同但确实是同一只股票。
    // 旧实现用 targetKey 比对，SH 行在 CN 页会走进"不一致"分支，然后被当成
    // **周期**不符播报（"落库在 1d 周期，当前 K 线是 1d"）—— 复审驳回 ①。
    const sameSymbol = normSymbol(current.symbol) === normSymbol(target.symbol)
    const sameMarket = marketMatches(current.market, target.market, target.symbol)
    const samePeriod = (current.period || '1d').trim() === (target.period || '1d').trim()
    if (current.instrument !== target.instrument || !sameSymbol || !sameMarket || !samePeriod) {
      // 逐项说清到底哪一项不同：标的 → 市场 → 周期，互不冒充。
      const reason =
        current.instrument !== target.instrument
          ? `已确认的是个股标注，当前是${current.instrument === 'index' ? '指数' : '个股'}图`
          : !sameSymbol
            ? `已确认的标注属于 ${target.symbol}，当前 K 线是 ${current.symbol}`
            : !sameMarket
              ? `已确认的标注市场是 ${target.market}，当前 K 线市场是 ${current.market}`
                + '（同一代码在不同交易所能指不同标的，无法证明是这一只）'
              : `已确认的标注落库在 ${target.period} 周期，当前 K 线是 ${current.period}`
      AnnotationLedger.notice(
        current,
        `${reason}，本次未上图（确认本身已成功，切到对应标的/周期即可看到这条线）。`,
        'warn',
      )
      return AnnotationLedger.snapshot(current)
    }

    const res = await fetchRows(target, { ids: [applied.annotationId] })
    if (!res.ok) {
      AnnotationLedger.notice(target, `已确认，但读取服务端标注失败：${res.message}。本次未画线。`, 'error')
      return AnnotationLedger.snapshot(target)
    }
    const verified = verifyAppliedRow(applied, res.rows, target)
    if (!verified.ok) {
      AnnotationLedger.notice(target, verified.message, 'warn')
      return AnnotationLedger.snapshot(target)
    }

    const merged = mergeSnapshotLines(AnnotationLedger.snapshot(target).lines, [verified.value])
    AnnotationLedger.replaceLines(
      target,
      merged,
      `已确认并上图：${merged.map((l) => `${l.label} ${l.price}`).join('、')}（标注 #${merged
        .map((l) => l.annotationId)
        .join('、')}，动作 #${merged.map((l) => l.actionId).join('、')}）`,
    )
    return AnnotationLedger.snapshot(target)
  } catch (err) {
    // 兜底：接线层自己的 bug 不能污染"确认成功"的结论。
    const current = AnnotationLedger.currentTarget()
    if (current) AnnotationLedger.notice(current, `图表标注接线异常：${errorText(err)}`, 'warn')
    return current ? AnnotationLedger.snapshot(current) : null
  }
}

/** 同向同价去重（与 `linesForTarget` 同口径）：「确认一条」要叠加到已有的线上。 */
function mergeSnapshotLines(
  previous: readonly ServerAnnotationLine[],
  next: readonly ServerAnnotationLine[],
): ServerAnnotationLine[] {
  const byLevel = new Map<string, ServerAnnotationLine>()
  for (const line of [...previous, ...next]) {
    const key = `${line.kind}:${Math.round(line.price * 100)}`
    const prev = byLevel.get(key)
    if (!prev || line.annotationId > prev.annotationId) byLevel.set(key, line)
  }
  return [...byLevel.values()].sort((a, b) => a.price - b.price)
}

/** 页面视图变了但还没发请求时，也要让账本知道"当前看的是哪张图"。 */
export function setCurrentAnnotationTarget(target: AnnotationTarget | null) {
  AnnotationLedger.setCurrent(target)
}
