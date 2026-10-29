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
 *  3. {@link revokeAnnotation} —— 用户在状态条撤销一条本人标注（2026-10-29）：服务端删除
 *     成功并**读回**之后才从图上移除，失败则回到服务端事实（详见该函数注释）。
 *
 * ⚠️ 两套"市场"不能混用（2026-10-28 复审阻断 ①）：
 *    - **服务端落库市场**（`SH`/`SZ`/`BJ`，来自 apply 明细）只用于精确回读与逐字核对；
 *    - **页面视图市场**（`?market=CN`）才是账本键 —— 图表/状态条只读当前视图键。
 *    `targetKey` 含 market，所以拿服务端键写账本 = 界面永远读不到的线（也不会有任何提示）。
 *    确认写账本一律走 {@link sameViewTarget} 校验过的**视图目标**。
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
  parseRevokeResponse,
  revokeUrl,
  targetKey,
  targetOfApplied,
  targetRefusal,
  verifyAppliedRow,
  type AnnotationTarget,
  type ReadOptions,
  type RevokedAnnotation,
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
      revoking: [],
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
      revoking: [],
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
      revoking: [],
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
    // 两个目标要分清（复审阻断 ①）：
    //  - `target`：**服务端落库目标**（market 是交易所 SH/SZ/BJ）—— 只用于按 id 精确回读与逐字核对；
    //  - `view`：**页面当前视图目标**（`?market=CN`）—— 账本键/图表/状态条读的都是它。
    // 旧实现拿 `target` 当账本键写：SH 落库的行被写进 SH 键，而页面订阅 CN 键 ⇒
    // 「确认成功」既不上图也没有任何提示，服务端键里还多一条界面永远读不到的线。
    const target = targetOfApplied(applied, owner)
    const view = AnnotationLedger.currentTarget()

    const refusal = targetRefusal(target)
    if (refusal) {
      // 落在不可画的目标上（未登录/指数/同号代码）：挂在当前视图上说清楚
      if (view) AnnotationLedger.notice(view, `已确认标注 #${applied.annotationId}，但${refusal}`, 'warn')
      return view ? AnnotationLedger.snapshot(view) : null
    }

    if (!view) return null // 没打开个股 K 线：确认成功即可，不凭空造目标
    const viewKey = targetKey(view)
    // 目标是否"同一张图"必须按**可证明的同一标的**判定，不能拿字符串 key 比：
    // 落库市场是交易所（SH），页面只有 CN —— 键不同但确实是同一只股票。
    // 旧实现用 targetKey 比对，SH 行在 CN 页会走进"不一致"分支，然后被当成
    // **周期**不符播报（"落库在 1d 周期，当前 K 线是 1d"）—— 复审驳回 ①。
    const sameSymbol = normSymbol(view.symbol) === normSymbol(target.symbol)
    const sameMarket = marketMatches(view.market, target.market, target.symbol, target.instrument)
    const samePeriod = (view.period || '1d').trim() === (target.period || '1d').trim()
    if (view.instrument !== target.instrument || !sameSymbol || !sameMarket || !samePeriod) {
      // 逐项说清到底哪一项不同：标的 → 市场 → 周期，互不冒充。
      const reason =
        view.instrument !== target.instrument
          ? `已确认的是个股标注，当前是${view.instrument === 'index' ? '指数' : '个股'}图`
          : !sameSymbol
            ? `已确认的标注属于 ${target.symbol}，当前 K 线是 ${view.symbol}`
            : !sameMarket
              ? `已确认的标注市场是 ${target.market}，当前 K 线市场是 ${view.market}`
                + '（同一代码在不同交易所能指不同标的，无法证明是这一只）'
              : `已确认的标注落库在 ${target.period} 周期，当前 K 线是 ${view.period}`
      AnnotationLedger.notice(
        view,
        `${reason}，本次未上图（确认本身已成功，切到对应标的/周期即可看到这条线）。`,
        'warn',
      )
      return AnnotationLedger.snapshot(view)
    }

    const res = await fetchRows(target, { ids: [applied.annotationId] })
    // 等网络期间页面可能已经切走（换标的/周期/账号）：账本键是**视图**键，
    // 旧键写进去就是"界面读不到的线"，因此只在视图仍是同一张图时写账本。
    const stillView = sameViewTarget(viewKey)
    if (!stillView) {
      const now = AnnotationLedger.currentTarget()
      if (!now) return null
      AnnotationLedger.notice(
        now,
        `已确认标注 #${applied.annotationId}（${target.symbol} ${target.period}），` +
          `但期间切换了图表视图（当前 ${normSymbol(now.symbol)}），本次未在当前图上显示。`,
        'warn',
      )
      return AnnotationLedger.snapshot(now)
    }
    if (!res.ok) {
      AnnotationLedger.notice(stillView, `已确认，但读取服务端标注失败：${res.message}。本次未画线。`, 'error')
      return AnnotationLedger.snapshot(stillView)
    }
    const verified = verifyAppliedRow(applied, res.rows, target)
    if (!verified.ok) {
      AnnotationLedger.notice(stillView, verified.message, 'warn')
      return AnnotationLedger.snapshot(stillView)
    }

    // 合并的基线也必须是**视图键**的快照：服务端键里没有页面已经在画的那几条线，
    // 拿它当基线会把同标的既有标注整批丢掉（复审阻断 ①）。
    const merged = mergeSnapshotLines(AnnotationLedger.snapshot(stillView).lines, [verified.value])
    // 市场写法不同（视图 CN / 落库 SH）时说明一句，避免用户以为"画错市场了"。
    const marketNote =
      (stillView.market || '').toUpperCase() !== (target.market || '').toUpperCase()
        ? `（服务端落库市场 ${target.market}，当前视图 ${stillView.market}，可证明是同一只标的）`
        : ''
    AnnotationLedger.replaceLines(
      stillView,
      merged,
      `已确认并上图：${merged.map((l) => `${l.label} ${l.price}`).join('、')}（标注 #${merged
        .map((l) => l.annotationId)
        .join('、')}，动作 #${merged.map((l) => l.actionId).join('、')}）${marketNote}`,
    )
    return AnnotationLedger.snapshot(stillView)
  } catch (err) {
    // 兜底：接线层自己的 bug 不能污染"确认成功"的结论。
    const current = AnnotationLedger.currentTarget()
    if (current) AnnotationLedger.notice(current, `图表标注接线异常：${errorText(err)}`, 'warn')
    return current ? AnnotationLedger.snapshot(current) : null
  }
}

/**
 * 视图是否仍是同一张图（账本键相同）。
 *
 * 确认链路是「先 await 回读、再写账本」：期间用户可能换了标的/周期/账号。
 * 账本键是**视图**键，往旧键里写就是"界面永远读不到的线"，所以只认当前键。
 */
function sameViewTarget(key: string): AnnotationTarget | null {
  const now = AnnotationLedger.currentTarget()
  if (!now) return null
  return targetKey(now) === key ? now : null
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

// ---------------------------------------------------------------------------
// 撤销（2026-10-29, P2 撤销闭合）
// ---------------------------------------------------------------------------

/** 撤销请求写进审计的默认原因（用户没输入时用它，审计里至少能看出"从界面点掉"）。 */
export const DEFAULT_REVOKE_REASON = '用户在 K 线标注状态条点击撤销'

/**
 * 进行中的撤销：`视图key#标注id` → promise（登记在账本里，见 `AnnotationLedger`）。
 *
 * 这是**连点的唯一闸门**：同一个 id 的第二次调用直接复用第一次的 promise，
 * 一个请求都不会多发（服务端那边是原子认领，第二个请求会 409 —— 但我们不该让它发出来，
 * 用户看到的应该是"撤销中…"而不是一条 409 报错）。
 */

/** `fetchAPI` 的失败 → 用户能看懂的一句话（按 HTTP 状态说清服务端的拒绝语义）。 */
function revokeFailureText(err: unknown): string {
  const status =
    typeof (err as { status?: unknown })?.status === 'number'
      ? ((err as { status: number }).status)
      : null
  const rawMessage = err instanceof Error && err.message ? err.message : ''
  // `fetchAPI` 在响应体没有 message 时会兜底成 "HTTP <code>"，那不是服务端的话
  const serverMessage = rawMessage && !/^HTTP \d+$/.test(rawMessage) ? rawMessage : ''
  const hint =
    status === 404
      ? '服务端说这条标注不存在、已被撤销，或不属于当前账号'
      : status === 409
        ? '服务端拒绝撤销（无主标注/没有确认动作/已被抢先撤销/编号被复用）'
        : status === 401
          ? '登录已失效，请重新登录后再撤销'
          : status !== null && status >= 500
            ? '服务端异常，本次未删除任何标注'
            : ''
  return [status !== null ? `HTTP ${status}` : '网络异常', hint, serverMessage]
    .filter(Boolean)
    .join('：')
}

/** 这条线是否仍然属于当前这张图（撤销前最后一道目标核对）。 */
function lineTargetMismatch(line: ServerAnnotationLine, target: AnnotationTarget): string | null {
  if (normSymbol(line.symbol) !== normSymbol(target.symbol)) {
    return `标注 #${line.annotationId} 属于 ${line.symbol}，与当前 K 线标的 ${normSymbol(target.symbol)} 不一致`
  }
  if (!marketMatches(target.market, line.market, line.symbol, target.instrument)) {
    return (
      `标注 #${line.annotationId} 的市场（${line.market}）与当前 K 线市场（${target.market}）` +
      '无法证明是同一只标的'
    )
  }
  if ((line.period || '1d').trim() !== (target.period || '1d').trim()) {
    return `标注 #${line.annotationId} 落库在 ${line.period} 周期，当前 K 线是 ${target.period}`
  }
  return null
}

/**
 * 撤销一条**本人已确认**的标注，并在服务端读回之后才动图（2026-10-29, P2）。
 *
 * 顺序（每一步失败都**不动图**，只播报）：
 *  1. 本地前置核对：这条线在不在当前图上、属主是不是我、标的/市场/周期还是不是这张图 ——
 *     任何一条不满足都**不发请求**（绝不删除不属于本人的标注，也不拿别的标的的图去删）；
 *  2. `DELETE /copilot/annotations/{id}`（连点复用同一个 promise）；
 *  3. 严格解析响应（`parseRevokeResponse`：必须 `status=revoked`、`action_status=rolled_back`、
 *     回来的 `annotation_id` 就是请求的那一条）；
 *  4. 只有 3 通过才按**服务端读回**重建当前目标的线（`linesForTarget` 同一套属主/标的/
 *     市场/周期核对），图上只少掉真的不在了的那一条；
 *  5. 读回与撤销结论不一致（读回仍返回该行 / 读回失败）时如实说明，不假装已撤回。
 *
 * 失败时**自动重新读取**：撤销被拒往往是"别处已经撤掉了/动作状态已经变了"，
 * 界面必须回到服务端事实，而不是停在旧账本上让用户再点一次。
 */
export function revokeAnnotation(
  annotationId: number,
  options: { reason?: string } = {},
): Promise<AnnotationSnapshot | null> {
  const view = AnnotationLedger.currentTarget()
  if (!view) return Promise.resolve(null)
  const viewKey = targetKey(view)
  const requestKey = `${viewKey}#${annotationId}`
  const inflight = AnnotationLedger.revokeInflightFor(requestKey)
  if (inflight) return inflight

  const reason = (options.reason ?? DEFAULT_REVOKE_REASON).slice(0, 200)
  const run = runRevoke(view, viewKey, annotationId, reason).finally(() => {
    AnnotationLedger.untrackRevoke(requestKey, run)
  })
  AnnotationLedger.trackRevoke(requestKey, run)
  return run
}

async function runRevoke(
  view: AnnotationTarget,
  viewKey: string,
  annotationId: number,
  reason: string,
): Promise<AnnotationSnapshot | null> {
  const line = AnnotationLedger.snapshot(view).lines.find((l) => l.annotationId === annotationId)
  if (!line) {
    // 已经不在图上（撤销成功过 / 已切换标的）：不发请求，也不假装发生了一次撤销。
    AnnotationLedger.notice(
      view,
      `标注 #${annotationId} 不在当前图上（可能已撤销，或已切换标的/周期），未发送撤销请求。`,
      'warn',
    )
    return AnnotationLedger.snapshot(view)
  }
  const owner = currentAccountId() ?? ''
  if (!owner) {
    AnnotationLedger.notice(view, `撤销标注 #${annotationId} 需要登录（标注按账号隔离）。`, 'error')
    return AnnotationLedger.snapshot(view)
  }
  if (line.ownerId !== owner) {
    AnnotationLedger.notice(
      view,
      `标注 #${annotationId} 属于其他账号（${line.ownerId}），不能撤销（服务端也只允许撤销本人标注）。`,
      'error',
    )
    return AnnotationLedger.snapshot(view)
  }
  const mismatch = lineTargetMismatch(line, view)
  if (mismatch) {
    AnnotationLedger.notice(view, `${mismatch}，不发撤销请求。`, 'error')
    return AnnotationLedger.snapshot(view)
  }

  AnnotationLedger.setRevoking(view, annotationId, true)
  try {
    let raw: unknown
    try {
      raw = await fetchAPI<unknown>(revokeUrl(annotationId, reason), {
        method: 'DELETE',
        timeoutMs: READ_TIMEOUT_MS,
      })
    } catch (err) {
      return resyncAfterRevokeFailure(viewKey, line, revokeFailureText(err))
    }

    const parsed = parseRevokeResponse(raw, annotationId)
    if (!parsed.ok) {
      return resyncAfterRevokeFailure(
        viewKey,
        line,
        `撤销请求已发出，但响应无法核对：${parsed.message}`,
      )
    }
    return applyRevoked(viewKey, line, parsed.value)
  } catch (err) {
    // 接线层自己的 bug 不能吞掉：如实播报，图不动
    const current = sameViewTarget(viewKey)
    if (current) {
      AnnotationLedger.notice(current, `撤销标注 #${annotationId} 时接线异常：${errorText(err)}`, 'error')
      return AnnotationLedger.snapshot(current)
    }
    return null
  } finally {
    // 无论成功/失败/切视图，按钮都不能永久"撤销中…"
    AnnotationLedger.setRevoking(view, annotationId, false)
  }
}

/**
 * 撤销失败（或被拒）→ 回到服务端事实。
 *
 * 失败后必须重新读取：常见原因是"这条已经被别处撤销了/动作状态已变"，
 * 界面若继续显示旧账本，用户会以为按钮坏了。读回之后再看这条线还在不在，
 * 文案跟着事实走（"仍保留" vs "已被撤销，图上已同步移除"）。
 */
async function resyncAfterRevokeFailure(
  viewKey: string,
  line: ServerAnnotationLine,
  why: string,
): Promise<AnnotationSnapshot | null> {
  const still = sameViewTarget(viewKey)
  if (!still) {
    const now = AnnotationLedger.currentTarget()
    if (now) AnnotationLedger.notice(now, `${why}（期间切换了图表视图，当前图未改动）`, 'error')
    return now ? AnnotationLedger.snapshot(now) : null
  }
  await requestAnnotations(still)
  const after = AnnotationLedger.snapshot(still)
  const kept = after.lines.some((l) => l.annotationId === line.annotationId)
  AnnotationLedger.notice(
    still,
    `${why}。${kept ? '图上这条线仍保留' : '服务端读回已没有这条线，图上已同步移除'}；` +
      '已重新读取服务端标注核对。',
    'error',
  )
  return AnnotationLedger.snapshot(still)
}

/**
 * 撤销成功 → 服务端读回 → 只按读回结果重建当前目标的线。
 *
 * 三个诚实点（复审关心的正是这类"看起来成功了"）：
 *  - 读回**仍返回**这条标注 ⇒ 不删线，明说"服务端仍返回、图上保留"（可能是读缓存/别处重写）；
 *  - 读回**失败** ⇒ 只移除"服务端已确认删除的这一条"（删除本身有响应为证），其余不动；
 *  - 期间**切了视图** ⇒ 不碰新图，只播报"已在服务端撤销，当前图未改动"。
 */
async function applyRevoked(
  viewKey: string,
  line: ServerAnnotationLine,
  revoked: RevokedAnnotation,
): Promise<AnnotationSnapshot | null> {
  const what = `${line.label} ${line.price}`
  const now = sameViewTarget(viewKey)
  if (!now) {
    const current = AnnotationLedger.currentTarget()
    if (current) {
      AnnotationLedger.notice(
        current,
        `标注 #${line.annotationId}（${what}）已在服务端撤销（确认动作 #${revoked.actionId} 已 rolled_back），` +
          `但期间切换了图表视图（当前 ${normSymbol(current.symbol)}），当前图未改动。`,
        'warn',
      )
      return AnnotationLedger.snapshot(current)
    }
    return null
  }

  // 读回**整个目标**（不只是这一个 id）：图上的线是"服务端此刻还剩哪些"的函数，
  // 顺手把别处新增/删除的标注也对齐，不在本地做增量加减。
  const res = await fetchRows(now)
  if (!res.ok) {
    const remaining = AnnotationLedger.snapshot(now).lines.filter(
      (l) => l.annotationId !== line.annotationId,
    )
    AnnotationLedger.replaceLines(
      now,
      remaining,
      `已撤销标注 #${line.annotationId}（${what}）：服务端已删除该行（确认动作 #${revoked.actionId} 已 rolled_back）。` +
        `但重新读取失败：${res.message}。本次只从图上移除了这一条（其余标注未动，可点「重新读取」核对）。`,
      { retryable: true, level: 'warn' },
    )
    return AnnotationLedger.snapshot(now)
  }

  const verdict = linesForTarget(res.rows, now)
  if (verdict.lines.some((l) => l.annotationId === line.annotationId)) {
    AnnotationLedger.replaceLines(
      now,
      verdict.lines,
      `撤销请求已成功（服务端返回 revoked），但重新读取仍返回标注 #${line.annotationId}（${what}）。` +
        '图上保留这条线，未假装已撤回；可能是读到了旧数据或另一处重新写入，请稍后点「重新读取」核对。',
      { retryable: true, level: 'error' },
    )
    return AnnotationLedger.snapshot(now)
  }

  // 同向同价的另一条标注（重复确认留下的）会让人误以为"没删掉"：图上这个价位确实还有线，
  // 但已经不是被撤销的那一条 —— 说明白，别把同价位的另一条说成撤销失败。
  const sameLevel = verdict.lines.find(
    (l) =>
      l.annotationId !== line.annotationId &&
      l.kind === line.kind &&
      Math.round(l.price * 100) === Math.round(line.price * 100),
  )
  if (sameLevel) {
    AnnotationLedger.replaceLines(
      now,
      verdict.lines,
      `已撤销标注 #${line.annotationId}（${what}）：服务端已删除该行（确认动作 #${revoked.actionId} 已 rolled_back）。` +
        `同价位还有另一条标注 #${sameLevel.annotationId}（${sameLevel.label} ${sameLevel.price}），` +
        '图上这个价位的线仍然保留 —— 要连它一起去掉，请再撤销那一条。',
      { level: 'warn' },
    )
    return AnnotationLedger.snapshot(now)
  }

  AnnotationLedger.replaceLines(
    now,
    verdict.lines,
    `已撤销标注 #${line.annotationId}（${what}）：服务端已删除该行（确认动作 #${revoked.actionId} 已 rolled_back），` +
      '图上已移除这条线。',
  )
  return AnnotationLedger.snapshot(now)
}
