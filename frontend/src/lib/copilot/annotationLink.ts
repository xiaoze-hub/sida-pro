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

import {
  AnnotationLedger,
  currentAccountId,
  mergeMarkers,
  type AnnotationSnapshot,
} from './annotationLedger'
import {
  annotationsUrl,
  marksForTarget,
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
  type ServerAnnotationMarker,
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
    // 带上序号：这次失败若已被更新的一次读取取代（重读/切回来），不许把它的 loading 顶掉
    AnnotationLedger.setError(target, `读取服务端标注失败：${res.message}（未画线）`, seq)
    return AnnotationLedger.snapshot(target)
  }

  // 两条通道同一套核对（属主/确认动作/标的/市场/周期）：有价位的 → 价位线，
  // 没价位但有日粒度日期的 → 该日 K 线 marker。任何一条都不凭空补位。
  const verdict = marksForTarget(res.rows, target)
  const notes: string[] = []
  if (res.dropped > 0) notes.push(`${res.dropped} 行缺目标/属主字段被丢弃`)
  // 文案保留各自通道的原话：同价重复（价位线）/ 同日期同标签重复（时间点），
  // 别把两种"重复"混成一个数字让用户以为看错了图层。
  if (verdict.mergedLines > 0) notes.push(`合并了 ${verdict.mergedLines} 条同价重复行`)
  if (verdict.mergedMarkers > 0) notes.push(`合并了 ${verdict.mergedMarkers} 条同日期重复标注`)
  const total = verdict.lines.length + verdict.markers.length
  const markerRender = AnnotationLedger.snapshot(target).markerRender

  let snap: AnnotationSnapshot
  if (total > 0) {
    // 说法把两条通道分开：几条线、几个时间点标记 —— 不让用户以为"都是线"。
    const parts: string[] = []
    if (verdict.lines.length > 0) parts.push(`${verdict.lines.length} 条价位线`)
    if (verdict.markers.length > 0) parts.push(`${verdict.markers.length} 个时间点标记`)
    snap = {
      key: targetKey(target),
      phase: 'ready',
      lines: verdict.lines,
      markers: verdict.markers,
      markerRender,
      annotationIds: Array.from(
        new Set([
          ...verdict.lines.map((l) => l.annotationId),
          ...verdict.markers.map((m) => m.annotationId),
        ]),
      ).sort((a, b) => a - b),
      actionIds: Array.from(
        new Set([
          ...verdict.lines.map((l) => l.actionId),
          ...verdict.markers.map((m) => m.actionId),
        ]),
      ).sort((a, b) => a - b),
      message:
        notes.length > 0 ? `已按服务端标注上图：${parts.join('、')}（${notes.join('；')}）` : '',
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
      markers: [],
      markerRender: null,
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
      markers: [],
      markerRender: null,
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
    const current = AnnotationLedger.snapshot(stillView)
    // 市场写法不同（视图 CN / 落库 SH）时说明一句，避免用户以为"画错市场了"。
    const marketNote =
      (stillView.market || '').toUpperCase() !== (target.market || '').toUpperCase()
        ? `（服务端落库市场 ${target.market}，当前视图 ${stillView.market}，可证明是同一只标的）`
        : ''

    /*
     * 纯时间点标注（2026-10-29）：只说"已交给图表画在该日"，**不说"已上图"** ——
     * 日期有没有对应 K 线由图表回报（`onAnnotationMarkersDrawn`）决定，状态条据此
     * 区分"已上图"和"该日没有 K 线、没上图"。这里把话说满就是假称已上图。
     */
    if (verified.value.shape === 'marker') {
      const marker = verified.value.marker
      AnnotationLedger.replaceLines(
        stillView,
        current.lines,
        `已确认时间点标注 #${marker.annotationId}（${marker.date} ${marker.label}，` +
          `动作 #${marker.actionId}）：已交给图表画在该日 K 线上；该日没有 K 线时不会上图（状态条会注明）` +
          marketNote,
        { markers: mergeMarkers(current.markers, [marker]) },
      )
      return AnnotationLedger.snapshot(stillView)
    }

    const merged = mergeSnapshotLines(current.lines, [verified.value.line])
    AnnotationLedger.replaceLines(
      stillView,
      merged,
      `已确认并上图：${merged.map((l) => `${l.label} ${l.price}`).join('、')}（标注 #${merged
        .map((l) => l.annotationId)
        .join('、')}，动作 #${merged.map((l) => l.actionId).join('、')}）${marketNote}`,
      { markers: current.markers },
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

/**
 * `fetchAPI` 的失败 → 用户能看懂的一句话（按 HTTP 状态 / 错误类型说清服务端的拒绝语义）。
 *
 * ⚠️ 401 **不带 status**：`fetchAPI` 在 401 时先 `logout()` 再抛 `new Error('登录已过期')`，
 * 既没有 `status` 也没有 `kind`。只按 status 判会把它归成"没有状态码"→ 说成"网络异常"，
 * 让用户去查网络而不是重新登录。所以未授权必须同时按 status 与文案识别。
 *
 * 反过来，**没有证据就不要说是网络**：既没有 status 也没有 NETWORK/TIMEOUT 标记时，
 * 只能说"没有收到服务端确认"，不能说"网络异常"（可能是响应形状坏了）。
 */
function revokeFailure(err: unknown): { text: string; unauthorized: boolean } {
  const box = err as { status?: unknown; kind?: unknown } | null
  const status =
    typeof box?.status === 'number' && Number.isFinite(box.status) ? box.status : null
  const kind = typeof box?.kind === 'string' ? box.kind : ''
  const rawMessage = err instanceof Error && err.message ? err.message : ''
  // `fetchAPI` 在响应体没有 message 时会兜底成 "HTTP <code>"，那不是服务端的话
  const serverMessage = rawMessage && !/^HTTP \d+$/.test(rawMessage) ? rawMessage : ''

  if (status === 401 || /登录已过期|登录已失效|未登录|unauthoriz/i.test(rawMessage)) {
    return {
      text: '登录已失效，请重新登录后再撤销（本次未删除任何标注，图上这条线保留）',
      unauthorized: true,
    }
  }
  if (status !== null) {
    const hint =
      status === 404
        ? '服务端说这条标注不存在、已被撤销，或不属于当前账号'
        : status === 409
          ? '服务端拒绝撤销（无主标注/没有确认动作/已被抢先撤销/编号被复用）'
          : status >= 500
            ? '服务端异常，本次未删除任何标注'
            : ''
    return { text: [`HTTP ${status}`, hint, serverMessage].filter(Boolean).join('：'), unauthorized: false }
  }
  if (kind === 'TIMEOUT') {
    const extra = serverMessage && !/超时/.test(serverMessage) ? `：${serverMessage}` : ''
    return { text: `请求超时，服务端未确认删除（不当作已撤销）${extra}`, unauthorized: false }
  }
  if (kind === 'NETWORK') {
    const extra = serverMessage && !/网络|Failed to fetch|Load failed/i.test(serverMessage)
      ? `：${serverMessage}`
      : ''
    return { text: `网络异常，请求可能没有到达服务端（不当作已撤销）${extra}`, unauthorized: false }
  }
  return {
    text: `撤销请求失败，没有收到服务端的确认（不当作已撤销）${
      serverMessage ? `：${serverMessage}` : ''
    }`,
    unauthorized: false,
  }
}

/** 这条标注是否仍然属于当前这张图（撤销前最后一道目标核对；价位线与时间点共用）。 */
function lineTargetMismatch(
  line: { annotationId: number; symbol: string; market: string; period: string },
  target: AnnotationTarget,
): string | null {
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
 * 撤销对账用的**最小描述**（2026-10-29）：价位线与时间点标注共用一套撤销口径。
 *
 * 为什么抽出来：撤销的动作语义对两条通道完全一样（本人确认过 → DELETE → 严格解析 →
 * 读回对账 → 只减这一条），差别只在"图上是什么形状"。若给 marker 另写一条撤销链路，
 * 迟早会出现"线核对了属主、marker 忘了"的漏洞 —— 这正是本文件一直在防的那类问题。
 */
interface RevokableMark {
  annotationId: number
  /** 人读描述：「支撑位 32.8」/「2026-10-20 提示」 */
  describe: string
  /** 同槽位键：价位线 = `kind:price*100`；时间点 = `date|label|tone` */
  slotKey: string
  /** 同槽位的人话（价位线="同价位"；时间点="同一日期"） */
  slotWord: string
  symbol: string
  market: string
  period: string
  ownerId: string
}

function lineAsMark(line: ServerAnnotationLine): RevokableMark {
  return {
    annotationId: line.annotationId,
    describe: `${line.label} ${line.price}`,
    slotKey: `${line.kind}:${Math.round(line.price * 100)}`,
    slotWord: '同价位',
    symbol: line.symbol,
    market: line.market,
    period: line.period,
    ownerId: line.ownerId,
  }
}

function markerAsMark(marker: ServerAnnotationMarker): RevokableMark {
  return {
    annotationId: marker.annotationId,
    describe: `${marker.date} ${marker.label}`,
    slotKey: `${marker.date}|${marker.label}|${marker.tone}`,
    slotWord: '同一日期',
    symbol: marker.symbol,
    market: marker.market,
    period: marker.period,
    ownerId: marker.ownerId,
  }
}

/**
 * 撤销一条**本人已确认**的标注，并在服务端读回之后才动图（2026-10-29, P2）。
 *
 * 顺序（每一步失败都**不动图**，只播报）：
 *  1. 本地前置核对：这条标注在不在当前图上（价位线或时间点通道）、属主是不是我、
 *     标的/市场/周期还是不是这张图 —— 任何一条不满足都**不发请求**
 *     （绝不删除不属于本人的标注，也不拿别的标的的图去删）；
 *  2. `DELETE /copilot/annotations/{id}`（连点复用同一个 promise）；
 *  3. 严格解析响应（`parseRevokeResponse`：必须 `status=revoked`、`action_status=rolled_back`、
 *     回来的 `annotation_id` 就是请求的那一条）；
 *  4. 只有 3 通过才按**服务端读回**对账当前目标的标注（`marksForTarget` 同一套属主/标的/
 *     市场/周期核对）：只对账"被撤销的这一条 / 同一槽位上的另一条"，别的行一行不动；
 *  5. 读回与撤销结论不一致（读回仍返回该行 / 读回失败）时如实说明，不假装已撤回。
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
  const snap = AnnotationLedger.snapshot(view)
  // 两条通道都找：只在自己看得到的那条上撤。找不到 = 已经不在图上（撤销成功过/切换标的）。
  const lineHit = snap.lines.find((l) => l.annotationId === annotationId)
  const markerHit = lineHit ? undefined : snap.markers.find((m) => m.annotationId === annotationId)
  const mark = lineHit ? lineAsMark(lineHit) : markerHit ? markerAsMark(markerHit) : null
  if (!mark) {
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
  if (mark.ownerId !== owner) {
    AnnotationLedger.notice(
      view,
      `标注 #${annotationId} 属于其他账号（${mark.ownerId}），不能撤销（服务端也只允许撤销本人标注）。`,
      'error',
    )
    return AnnotationLedger.snapshot(view)
  }
  const mismatch = lineTargetMismatch(mark, view)
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
      // 必须 await：`return <promise>` 会让下面的 finally 立刻执行（按钮提前解禁、
      // 还能再点出第二个 DELETE），而且读回阶段抛出的异常会绕过本函数的 catch，
      // 变成调用方没人处理的未处理拒绝 —— 撤销结论既没播报也把页面弄响。
      const failure = revokeFailure(err)
      if (failure.unauthorized) {
        /*
         * 401 已经被 `fetchAPI` 处理过（logout + 抛"登录已过期"）：再发一次读回 GET
         * 只会再 401 一次，还会再触发一次 logout/跳转 —— 对用户毫无信息增量。
         * 这里不发请求，直接按"会话失效、本次未删除"播报，线保持原样（服务端没删）。
         */
        const still = sameViewTarget(viewKey)
        if (still) {
          AnnotationLedger.notice(still, failure.text, 'error')
          return AnnotationLedger.snapshot(still)
        }
        return null
      }
      return await resyncAfterRevokeFailure(viewKey, mark, failure.text)
    }

    const parsed = parseRevokeResponse(raw, annotationId)
    if (!parsed.ok) {
      return await resyncAfterRevokeFailure(
        viewKey,
        mark,
        `撤销请求已发出，但响应无法核对：${parsed.message}`,
      )
    }
    return await applyRevoked(viewKey, mark, parsed.value)
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
 * 界面若继续显示旧账本，用户会以为按钮坏了。读回之后再看这条标注还在不在，
 * 文案跟着事实走（"仍保留" vs "已被撤销，图上已同步移除"）。
 *
 * ⚠️ 读回**只做减法**（2026-10-29 竞态补丁）：这次读的唯一目的是"这条还在不在"。
 * 旧实现用 `requestAnnotations` 整批重建 —— 它先把图清成 loading，再拿这份读回替换全部
 * 线：期间并行的确认标注会被抹掉，且会把账本的"当前视图"回写成这个（可能已经过期的）目标。
 */
async function resyncAfterRevokeFailure(
  viewKey: string,
  mark: RevokableMark,
  why: string,
): Promise<AnnotationSnapshot | null> {
  const still = sameViewTarget(viewKey)
  if (!still) {
    const now = AnnotationLedger.currentTarget()
    if (now) AnnotationLedger.notice(now, `${why}（期间切换了图表视图，当前图未改动）`, 'error')
    return now ? AnnotationLedger.snapshot(now) : null
  }
  const res = await fetchRows(still)
  // 读回期间也可能切走：切走了就不碰任何图，只在当前图上播报
  const after = sameViewTarget(viewKey)
  if (!after) {
    const now = AnnotationLedger.currentTarget()
    if (now) AnnotationLedger.notice(now, `${why}（期间切换了图表视图，当前图未改动）`, 'error')
    return now ? AnnotationLedger.snapshot(now) : null
  }
  const current = AnnotationLedger.baselineLines(after)
  const currentMarkers = AnnotationLedger.baselineMarkers(after)
  const held =
    current.some((l) => l.annotationId === mark.annotationId) ||
    currentMarkers.some((m) => m.annotationId === mark.annotationId)
  if (!res.ok) {
    // 读回失败 ⇒ 没有证据说它已经不在了：线保留，不猜
    AnnotationLedger.notice(
      after,
      `${why}。重新读取服务端标注失败：${res.message}，无法核对${
        held ? '，图上这条标注保留' : ''
      }（图上的标注未改动）。`,
      'error',
    )
    return AnnotationLedger.snapshot(after)
  }
  const verdict = marksForTarget(res.rows, after)
  const kept =
    verdict.lines.some((l) => l.annotationId === mark.annotationId) ||
    verdict.markers.some((m) => m.annotationId === mark.annotationId)
  if (!kept) {
    AnnotationLedger.replaceLines(
      after,
      current.filter((l) => l.annotationId !== mark.annotationId),
      `${why}。服务端读回已没有这条标注，图上已同步移除；已重新读取服务端标注核对。`,
      {
        level: 'error',
        markers: currentMarkers.filter((m) => m.annotationId !== mark.annotationId),
      },
    )
    return AnnotationLedger.snapshot(after)
  }
  AnnotationLedger.notice(
    after,
    `${why}。图上这条标注仍保留；已重新读取服务端标注核对。`,
    'error',
  )
  return AnnotationLedger.snapshot(after)
}

/**
 * 撤销成功 → 服务端读回 → 只按读回结果**对账被撤销的那一条**（两条通道同一套口径）。
 *
 * 三个诚实点：
 *  - 读回**仍返回**这条标注 ⇒ 不删，明说"服务端仍返回、图上保留"；
 *  - 读回**失败** ⇒ 只移除"服务端已确认删除的这一条"（删除本身有响应为证），其余不动；
 *  - 期间**切了视图** ⇒ 不碰新图，只播报"已在服务端撤销，当前图未改动"。
 *
 * ⚠️ 不做整批重建：读回是**快照**，落地永远基于"此刻"账本的**减法** —— 别的行一行不动，
 * 同槽位的另一条要先按 id 再核对一次（核对不过就不画，宁可说"未能同步"也不复活一条
 * 不存在的标注）。
 */
async function applyRevoked(
  viewKey: string,
  mark: RevokableMark,
  revoked: RevokedAnnotation,
): Promise<AnnotationSnapshot | null> {
  const what = mark.describe
  /** 两条通道的"同槽位"人话不同：价位线是同一个价位，时间点标注是同一天同一标签。 */
  const slotWord = mark.slotWord
  const head =
    `已撤销标注 #${mark.annotationId}（${what}）：服务端已删除该行` +
    `（确认动作 #${revoked.actionId} 已 rolled_back）`
  const now = sameViewTarget(viewKey)
  if (!now) {
    const current = AnnotationLedger.currentTarget()
    if (current) {
      AnnotationLedger.notice(
        current,
        `标注 #${mark.annotationId}（${what}）已在服务端撤销（确认动作 #${revoked.actionId} 已 rolled_back），` +
          `但期间切换了图表视图（当前 ${normSymbol(current.symbol)}），当前图未改动。`,
        'warn',
      )
      return AnnotationLedger.snapshot(current)
    }
    return null
  }

  // 读回**整个目标**（不只是这一个 id）：撤销后同一槽位可能还有另一条标注该顶上来，
  // 只看这一个 id 会把那个价位/那个日期从图上"凭空抹掉"。但结果**不整批落地**。
  const res = await fetchRows(now)
  // 读回很慢，这期间用户可能换了标的/周期/账号：只认"仍是同一张图"的账本键
  const after = sameViewTarget(viewKey)
  if (!after) {
    const current = AnnotationLedger.currentTarget()
    if (current) {
      AnnotationLedger.notice(
        current,
        `标注 #${mark.annotationId}（${what}）已在服务端撤销（确认动作 #${revoked.actionId} 已 rolled_back），` +
          `但期间切换了图表视图（当前 ${normSymbol(current.symbol)}），当前图未改动。`,
        'warn',
      )
      return AnnotationLedger.snapshot(current)
    }
    return null
  }

  // 落地的基线必须是**此刻**的账本，不是读回发起时的快照；且 loading 窗口里
  // 集合已被清空 —— 用 baseline* 回落到"上次已知的"，否则其余标注整批消失。
  const currentLines = AnnotationLedger.baselineLines(after)
  const currentMarkers = AnnotationLedger.baselineMarkers(after)
  const restLines = currentLines.filter((l) => l.annotationId !== mark.annotationId)
  const restMarkers = currentMarkers.filter((m) => m.annotationId !== mark.annotationId)
  const held =
    currentLines.some((l) => l.annotationId === mark.annotationId) ||
    currentMarkers.some((m) => m.annotationId === mark.annotationId)

  if (!res.ok) {
    AnnotationLedger.replaceLines(
      after,
      restLines,
      `${head}。但重新读取失败：${res.message}。本次只从图上移除了这一条（其余标注未动，可点「重新读取」核对）。`,
      { retryable: true, level: 'warn', markers: restMarkers },
    )
    return AnnotationLedger.snapshot(after)
  }

  const verdict = marksForTarget(res.rows, after)
  const stillListed =
    verdict.lines.some((l) => l.annotationId === mark.annotationId) ||
    verdict.markers.some((m) => m.annotationId === mark.annotationId)
  if (stillListed) {
    // 服务端仍返回该行：不删；也不把它"再加回来"（只做减法）
    AnnotationLedger.replaceLines(
      after,
      currentLines,
      `撤销请求已成功（服务端返回 revoked），但重新读取仍返回标注 #${mark.annotationId}（${what}）。` +
        (held ? '图上保留这条标注，未假装已撤回' : '图上此前已不在这条标注，本次未重新加回') +
        '；可能是读到了旧数据或另一处重新写入，请稍后点「重新读取」核对。',
      { retryable: true, level: 'error', markers: currentMarkers },
    )
    return AnnotationLedger.snapshot(after)
  }

  // 同槽位的另一条（重复确认留下的）会让人误以为"没删掉"：图上那个位置确实还有标注，
  // 但已经不是被撤销的那一条 —— 说明白，别把同槽位的另一条说成撤销失败。
  const sameSlotLine = verdict.lines.find(
    (l) => l.annotationId !== mark.annotationId && `${l.kind}:${Math.round(l.price * 100)}` === mark.slotKey,
  )
  const sameSlotMarker = verdict.markers.find(
    (m) =>
      m.annotationId !== mark.annotationId &&
      `${m.date}|${m.label}|${m.tone}` === mark.slotKey,
  )
  const sameSlotId = sameSlotLine?.annotationId ?? sameSlotMarker?.annotationId
  const slotOccupiedNow =
    restLines.some((l) => `${l.kind}:${Math.round(l.price * 100)}` === mark.slotKey) ||
    restMarkers.some((m) => `${m.date}|${m.label}|${m.tone}` === mark.slotKey)
  if (sameSlotId !== undefined && !slotOccupiedNow) {
    /*
     * ⚠️ 那条同槽位的行来自那次 GET 的**快照**：别的标签页完全可能在快照之后撤销了它 ——
     * 直接把快照里的行画上去，就是**复活一条已经不存在的标注**（用户会以为"撤了又回来"）。
     * 所以落地前按 id 再精确核对一次；还有它才画，没有/核对失败就不画并如实说明。
     */
    const confirm = await fetchRows(after, { ids: [sameSlotId] })
    const now2 = sameViewTarget(viewKey)
    if (!now2) {
      const current2 = AnnotationLedger.currentTarget()
      if (current2) {
        AnnotationLedger.notice(
          current2,
          `${head}；期间切换了图表视图（当前 ${normSymbol(current2.symbol)}），当前图未改动。`,
          'warn',
        )
        return AnnotationLedger.snapshot(current2)
      }
      return null
    }
    const confirmed = confirm.ok ? marksForTarget(confirm.rows, now2) : null
    const liveLine = confirmed?.lines.find((l) => l.annotationId === sameSlotId)
    const liveMarker = confirmed?.markers.find((m) => m.annotationId === sameSlotId)
    const what2 = liveLine
      ? `${liveLine.label} ${liveLine.price}`
      : liveMarker
        ? `${liveMarker.date} ${liveMarker.label}`
        : sameSlotLine
          ? `${sameSlotLine.label} ${sameSlotLine.price}`
          : `${sameSlotMarker?.date ?? ''} ${sameSlotMarker?.label ?? ''}`.trim()
    if (!liveLine && !liveMarker) {
      AnnotationLedger.replaceLines(
        now2,
        restLines,
        `${head}；读回里${slotWord}还有另一条标注 #${sameSlotId}（${what2}），` +
          (confirmed
            ? '但落地前再次核对时服务端已经没有它（可能已在别处撤销），本次未把它画到图上。'
            : `但落地前再次核对失败（${confirm.ok ? '' : confirm.message}），无法确认它还在，本次未把它画到图上。`),
        { retryable: !confirmed, level: 'warn', markers: restMarkers },
      )
      return AnnotationLedger.snapshot(now2)
    }
    // 只把这个槽位换成本来就在服务端的那一条，其余行一行不动（不引入新槽位）
    AnnotationLedger.replaceLines(
      now2,
      liveLine ? mergeSnapshotLines(restLines, [liveLine]) : restLines,
      `${head}；${slotWord}还有另一条标注 #${sameSlotId}（${what2}），` +
        '图上这个位置的标注仍然保留 —— 要连它一起去掉，请再撤销那一条。',
      {
        level: 'warn',
        markers: liveMarker ? mergeMarkers(restMarkers, [liveMarker]) : restMarkers,
      },
    )
    return AnnotationLedger.snapshot(now2)
  }

  AnnotationLedger.replaceLines(after, restLines, `${head}，图上已移除这条标注。`, {
    markers: restMarkers,
  })
  return AnnotationLedger.snapshot(after)
}
