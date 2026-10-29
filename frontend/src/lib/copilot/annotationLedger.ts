/**
 * 服务端标注的**唯一内存账本**（2026-10-28 P2 四收口）。
 *
 * 复审红线里两条跟"状态"有关：
 *  ② 切股票/周期必须**立刻**清掉旧价线与通知，并防异步旧响应污染；
 *  ④ 不得靠可伪造的客户端 `WeakSet` 当安全边界。
 *
 * 这个账本同时解决两件事：
 *
 *  1. **按精确目标分键**（`账号|标的类型|代码|市场|周期`）。图表只读"当前 key"的快照，
 *     于是换标的/换周期的那一刻拿到的就是空快照 —— 不需要等新请求回来，也永远不会
 *     把上一只票的线留在图上。旧实现把线存在一个不区分标的的全局 Map 里，这是重复/串线的来源。
 *  2. **发布序号**。同一 key 的每次请求取一个递增序号；响应回来时序号不是最新 → 直接丢弃。
 *     这样 A→B→A 快速切换、"慢响应后到"都不会覆盖新状态。
 *
 * 2026-10-29 撤销闭合再加两样（都不改变上面两条）：
 *  - `revoking`：当前正在撤销的标注 id —— 界面据此禁用按钮，且**线在请求在途时不动**
 *    （服务端还没删，本地先删就是撒谎）；
 *  - `revokeInflight`：按 `key#标注id` 登记在途撤销，连点复用同一个 promise。
 *
 * 账本里**不存价位**：只存 `ServerAnnotationLine`（每个字段都能对回服务端行，由
 * `serverAnnotations` 的核对路径产生）。它也不是"安全边界" —— 边界在服务端：JWT 过滤 +
 * 精确读回。客户端能做的最多是把一个自造对象塞进账本，而界面上画的是账本里那些行的
 * `price/label/id`，没有一处从正文/本地存储取数。
 */

import { safeJwtPayload } from '@/lib/jwt'

import {
  targetKey,
  type AnnotationTarget,
  type ServerAnnotationLine,
} from './serverAnnotations'

/** 账本状态：idle=没有可画的（干净空态）/ loading / ready / refused / error。 */
export type AnnotationPhase = 'idle' | 'loading' | 'ready' | 'refused' | 'error'

export interface AnnotationSnapshot {
  /** 快照对应的精确目标 key；空串 = 没有目标 */
  key: string
  phase: AnnotationPhase
  /** 可画线（只含当前 key 的目标） */
  lines: ServerAnnotationLine[]
  annotationIds: number[]
  actionIds: number[]
  /** 状态说明（refused/error 时是"为什么没上图"） */
  message: string
  /** 一次性事件提示（确认成功但周期不同等），下次请求时清空 */
  notice: string
  /** notice 的级别 */
  noticeLevel: 'info' | 'warn' | 'error'
  /** 当前状态是否值得给一个"重新读取"按钮 */
  retryable: boolean
  /**
   * 正在撤销中的标注 id（2026-10-29 撤销闭合）。
   * 界面据此把这条线的撤销按钮置成"撤销中…"并禁用 —— 连点不会发出第二个 DELETE；
   * 请求结束（成功/失败）都必须清掉，否则按钮会永久卡在撤销中。
   */
  revoking: number[]
}

const EMPTY: AnnotationSnapshot = {
  key: '',
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

/** 稳定的空快照（`useSyncExternalStore` 要求 getSnapshot 在无变化时返回同一对象）。 */
export const EMPTY_SNAPSHOT = EMPTY

/**
 * 每个 key 的**稳定**空快照缓存。
 *
 * `useSyncExternalStore` 要求 `getSnapshot()` 在状态未变时返回**同一个引用**，
 * 否则 React 会判定"每次都变了"并反复重渲染（控制台报
 * "The result of getSnapshot should be cached to avoid an infinite loop"）。
 * 空态也不能每次现造对象，所以按 key 缓存一份。
 */
const EMPTY_BY_KEY = new Map<string, AnnotationSnapshot>()

function emptyForKey(key: string): AnnotationSnapshot {
  const hit = EMPTY_BY_KEY.get(key)
  if (hit) return hit
  if (EMPTY_BY_KEY.size >= 64) {
    const oldest = EMPTY_BY_KEY.keys().next().value
    if (typeof oldest === 'string') EMPTY_BY_KEY.delete(oldest)
  }
  const snap: AnnotationSnapshot = { ...EMPTY, key }
  EMPTY_BY_KEY.set(key, snap)
  return snap
}

/** JWT 里的账号 id（`sub`/`user_id`）。取不到 = 未登录。 */
export function currentAccountId(): string | null {
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

/** 只回答"有没有 token 身份"，不是授权判断（授权在服务端）。 */
export function hasIdentity(): boolean {
  return currentAccountId() !== null
}

function readySnapshot(key: string, lines: ServerAnnotationLine[], message: string): AnnotationSnapshot {
  return {
    key,
    phase: lines.length > 0 ? 'ready' : 'idle',
    lines,
    annotationIds: Array.from(new Set(lines.map((l) => l.annotationId))).sort((a, b) => a - b),
    actionIds: Array.from(new Set(lines.map((l) => l.actionId))).sort((a, b) => a - b),
    message,
    notice: '',
    noticeLevel: 'info',
    retryable: false,
    revoking: [],
  }
}

/** 同向同价去重（与 `linesForTarget` 同口径，供"确认一条 + 已有线"合并）。 */
export function mergeLines(
  a: readonly ServerAnnotationLine[],
  b: readonly ServerAnnotationLine[],
): ServerAnnotationLine[] {
  const byLevel = new Map<string, ServerAnnotationLine>()
  for (const line of [...a, ...b]) {
    const key = `${line.kind}:${Math.round(line.price * 100)}`
    const prev = byLevel.get(key)
    if (!prev || line.annotationId > prev.annotationId) byLevel.set(key, line)
  }
  return [...byLevel.values()].sort((x, y) => x.price - y.price)
}

/**
 * 账本本体。只暴露"读快照 / 开一次请求 / 提交结果"这三个动作，
 * 提交时的序号与目标校验都在内部完成 —— 调用方忘不掉。
 */
class AnnotationLedgerImpl {
  /** key → 快照 */
  private snapshots = new Map<string, AnnotationSnapshot>()
  /** key → 最新请求序号 */
  private seq = new Map<string, number>()
  /**
   * 撤销在途登记：`视图key#标注id` → promise（2026-10-29 撤销闭合）。
   *
   * 放在账本里而不是接线层的模块变量里，是为了让它跟着 {@link clearAll} 一起被清掉：
   * 账号切换/登出/测试隔离之后，一个残留的旧 promise 会让"同一条标注"的撤销
   * 永远命中旧在途项 —— 界面点了没反应、也没有任何请求，最难查的那类 bug。
   */
  private revokeInflight = new Map<string, Promise<AnnotationSnapshot | null>>()
  /**
   * key → **上一次已知的线**（2026-10-29 复审补丁）。
   *
   * `beginRequest` 会把 `lines` 清空（loading 相位），但"清空"只是界面上的瞬时态，
   * 不是"这个目标没有线"这个事实。撤销读回若恰好落在 loading 窗口里，拿空数组当
   * 减法基线会把**其余线整批抹掉**（别的标注不再出现，也没有任何提示）。所以单独
   * 留一份"上次落地过的线"，只给 {@link baselineLines} 在 loading 窗口里用。
   */
  private lastLines = new Map<string, ServerAnnotationLine[]>()
  /**
   * key → 待应用增量（2026-10-29 复审补丁）。
   *
   * 撤销读回落在 loading 窗口时，**不能**递增序号把在途的那次重读丢掉（那是用户点的
   * 「重新读取」/后台刷新，丢掉就是静默失效），也不能让它整批覆盖掉刚撤销的那条。
   * 折中：此刻先把结果写上界面，同时把"要减掉的 id / 要加进来的线"记在这里，
   * 让那次重读落地时（{@link commit}）套上增量 —— 快照是新的，撤销结论也还成立。
   */
  private pending = new Map<string, { drop: number[]; add: ServerAnnotationLine[] }>()
  /** 页面当前视图目标（确认接线据此判断"是不是同一张图"） */
  private current: AnnotationTarget | null = null
  private listeners = new Set<() => void>()

  /** 当前页面视图目标（`null` = 页面没打开个股 K 线）。 */
  currentTarget(): AnnotationTarget | null {
    return this.current
  }

  /** 读某个目标的快照；没有就返回稳定空快照。 */
  snapshot(target: AnnotationTarget | null): AnnotationSnapshot {
    if (!target) return EMPTY
    const key = targetKey(target)
    return this.snapshots.get(key) ?? emptyForKey(key)
  }

  /**
   * "此刻这个目标上有哪些线" —— 用于落地前的**减法基线**。
   *
   * 与 `snapshot().lines` 的唯一区别：loading 窗口里 `lines` 被 `beginRequest` 清空了，
   * 这里回落到"上一次已知的线"，避免"读回落在重读窗口 → 其余线整批消失"。
   * 别的相位（ready/idle/error/refused）照抄快照：那才是界面上真正显示的线。
   */
  baselineLines(target: AnnotationTarget): ServerAnnotationLine[] {
    const key = targetKey(target)
    const base = this.snapshots.get(key) ?? emptyForKey(key)
    if (base.phase === 'loading') return this.lastLines.get(key) ?? base.lines
    return base.lines
  }

  /**
   * 开一次请求：记录"当前目标"、递增该 key 的序号、**同步**把快照置成 loading（清空旧线）。
   * 返回的序号必须原样交给 {@link commit}。
   *
   * `revoking` 必须**继承**而不是清空（2026-10-29 撤销竞态补丁）：这次请求不代表别处那条
   * 撤销已经结束 —— 清掉它会让"撤销中…"提前消失、按钮重新可点，用户一点就再发一个
   * DELETE（只剩 409）。正式解除只在撤销自己的 finally 里做。
   */
  beginRequest(target: AnnotationTarget): number {
    this.current = target
    const key = targetKey(target)
    const next = (this.seq.get(key) ?? 0) + 1
    this.seq.set(key, next)
    const base = this.snapshots.get(key) ?? emptyForKey(key)
    this.write(key, {
      key,
      phase: 'loading',
      lines: [],
      annotationIds: [],
      actionIds: [],
      message: '正在核对服务端标注…',
      notice: '',
      noticeLevel: 'info',
      retryable: false,
      revoking: base.revoking,
    })
    return next
  }

  /** 页面切换目标（即使不发请求也要更新"当前视图"，好让确认接线说对周期）。 */
  setCurrent(target: AnnotationTarget | null) {
    this.current = target
    this.emit()
  }

  /**
   * 提交结果。**只有序号仍是最新、且目标仍是当前视图**才写入；
   * 返回是否生效（false = 被丢弃的旧响应，调用方不必再管）。
   */
  commit(target: AnnotationTarget, seq: number, snapshot: AnnotationSnapshot): boolean {
    const key = targetKey(target)
    if (this.seq.get(key) !== seq) return false
    if (!this.current || targetKey(this.current) !== key) return false
    // 一次读取不改变"谁正在撤销中"：并行的另一次撤销必须继续显示"撤销中…"
    const base = this.snapshots.get(key) ?? emptyForKey(key)
    const next = this.applyPending(key, { ...snapshot, key, revoking: base.revoking })
    this.write(key, next)
    this.lastLines.set(key, next.lines)
    return true
  }

  /**
   * 把 loading 窗口里记下的"减掉的 id / 加进来的线"套到刚落地的读回上。
   * 没有增量 = 原样返回（正常路径一行都不改）。
   */
  private applyPending(key: string, snapshot: AnnotationSnapshot): AnnotationSnapshot {
    const delta = this.pending.get(key)
    if (!delta) return snapshot
    this.pending.delete(key)
    const drop = new Set(delta.drop)
    const lines = snapshot.lines.filter((l) => !drop.has(l.annotationId))
    for (const line of delta.add) {
      if (lines.some((l) => l.annotationId === line.annotationId)) continue
      // 同一价位已经有线（同向同价）就别叠一条：与 `mergeLines` 同口径，留新的那条
      const at = lines.findIndex((l) => l.kind === line.kind && Math.round(l.price * 100) === Math.round(line.price * 100))
      if (at >= 0) lines[at] = line
      else lines.push(line)
    }
    lines.sort((x, y) => x.price - y.price)
    return {
      ...snapshot,
      phase: lines.length > 0 ? 'ready' : 'idle',
      lines,
      annotationIds: Array.from(new Set(lines.map((l) => l.annotationId))).sort((a, b) => a - b),
      actionIds: Array.from(new Set(lines.map((l) => l.actionId))).sort((a, b) => a - b),
      message: lines.length > 0 ? snapshot.message : '',
      // 那次读回本来会"清空一次性提示"，但它是在撤销/确认落地**之前**发起的；
      // 撤销结论不能因为一次并发的后台重读就从界面上消失。
      notice: snapshot.notice || this.snapshots.get(key)?.notice || '',
      noticeLevel: snapshot.notice ? snapshot.noticeLevel : this.snapshots.get(key)?.noticeLevel ?? 'info',
      retryable: snapshot.retryable || (this.snapshots.get(key)?.retryable ?? false),
    }
  }

  /** 目标级拒画（未登录/指数/同号代码）：不发请求，直接给出原因。 */
  setRefused(target: AnnotationTarget, message: string): AnnotationSnapshot {
    this.current = target
    const key = targetKey(target)
    // 拒画也要让序号前进，避免"拒画之前发出的请求"回来把线画上去
    this.seq.set(key, (this.seq.get(key) ?? 0) + 1)
    // `revoking` 必须继承：撤销在途时来一次拒画，不能让按钮提前解禁（第二个 DELETE 只剩 409）
    const base = this.snapshots.get(key) ?? emptyForKey(key)
    const snap: AnnotationSnapshot = {
      ...EMPTY,
      key,
      phase: 'refused',
      message,
      revoking: base.revoking,
    }
    // 拒画以后没有任何线可落地：留着"待应用增量"只会让它在很久之后的一次读取上
    // 复活/抹掉不该动的行，索性丢弃（下一次读取就是事实）。
    this.pending.delete(key)
    this.write(key, snap)
    return snap
  }

  /**
   * 读取失败（网络/形状不符）：清空旧线并给出可重试原因。
   *
   * `seq` 可选：调用方把 `beginRequest` 拿到的序号传进来，旧请求的失败就不会覆盖
   * 一次更新的读取（否则新的 loading 会被上一次的 error 顶掉）。`revoking` 同样继承 ——
   * 一次失败不代表那次撤销结束了。
   */
  setError(target: AnnotationTarget, message: string, seq?: number): AnnotationSnapshot {
    const key = targetKey(target)
    if (seq !== undefined && this.seq.get(key) !== seq) return this.snapshot(target)
    const base = this.snapshots.get(key) ?? emptyForKey(key)
    const snap: AnnotationSnapshot = {
      ...EMPTY,
      key,
      phase: 'error',
      message,
      retryable: true,
      revoking: base.revoking,
    }
    // 同 setRefused：错误态没有线，"待应用增量"留到下一次读取上只会过时。
    this.pending.delete(key)
    this.write(key, snap)
    return snap
  }

  /** 一次性提示（确认成功但没画到当前图）：只挂在当前目标上，不动别的 key。 */
  notice(target: AnnotationTarget, message: string, level: AnnotationSnapshot['noticeLevel'] = 'warn') {
    const key = targetKey(target)
    const base = this.snapshots.get(key) ?? emptyForKey(key)
    this.write(key, { ...base, notice: message, noticeLevel: level })
  }

  /**
   * 标记/解除某条标注的"撤销中"（2026-10-29 撤销闭合）。
   *
   * 只动 `revoking`，不碰线、不碰序号：撤销还在路上时图上那条线必须继续显示
   * （服务端还没删；本地先删就是撒谎）。请求结束的 finally 里必须解除，
   * 否则按钮永久"撤销中…"。
   */
  setRevoking(target: AnnotationTarget, annotationId: number, on: boolean) {
    const key = targetKey(target)
    const base = this.snapshots.get(key) ?? emptyForKey(key)
    const next = base.revoking.filter((id) => id !== annotationId)
    if (on) next.push(annotationId)
    this.write(key, { ...base, revoking: next.sort((a, b) => a - b) })
  }

  /**
   * 确认成功 / 撤销完成且目标就是当前视图：用新的线集合替换快照。
   *
   * `opts.retryable` 用于"撤销成功但读回失败/读回仍含该行" —— 用户需要一个
   * 重新读取的入口；`opts.level` 决定 notice 的级别（成功=info，不同步=warn/error）。
   */
  replaceLines(
    target: AnnotationTarget,
    lines: ServerAnnotationLine[],
    message: string,
    opts: { retryable?: boolean; level?: AnnotationSnapshot['noticeLevel'] } = {},
  ) {
    const key = targetKey(target)
    const base = this.snapshots.get(key) ?? emptyForKey(key)
    if (base.phase === 'loading') {
      /*
       * 此刻有一次读取在途（`beginRequest` 已经把 lines 清空，写着"正在核对…"）。
       * 这次落地的结论（撤销/确认）比那次读取更新，但**不能靠递增序号把它丢掉** ——
       * 那是用户点的「重新读取」或后台刷新，丢掉就是静默失效。
       *
       * 所以：界面立刻按这次结论写（用 `baselineLines` 给的"上次已知的线"做基线），
       * 同时把"减掉的 id / 加进来的线"记成待应用增量，让那次读取落地时套上
       * （见 {@link commit} / {@link applyPending}）。读回是新的，撤销结论也还成立。
       */
      const visible = this.lastLines.get(key) ?? []
      const nextIds = new Set(lines.map((l) => l.annotationId))
      const prevIds = new Set(visible.map((l) => l.annotationId))
      const prev = this.pending.get(key) ?? { drop: [], add: [] }
      // 待减掉的 id = 上次已知但这次没了的 + 之前已经记下要减、这次也没加回来的
      const drop = new Set<number>()
      for (const id of prevIds) if (!nextIds.has(id)) drop.add(id)
      for (const id of prev.drop) if (!nextIds.has(id)) drop.add(id)
      // 待加进来的线 = 之前记下且这次仍在的 + 这次新出现的（不在上次已知里）
      const add = [
        ...prev.add.filter((l) => lines.some((n) => n.annotationId === l.annotationId)),
        ...lines.filter((l) => !prevIds.has(l.annotationId)),
      ]
      this.pending.set(key, { drop: [...drop], add })
    } else {
      // 已确认/已撤销的行一定比「此前发出的任何一次读取」都新：把序号推进一格，
      // 让那些还在路上的旧读取回来时被 {@link commit} 丢弃 —— 否则一个早于 apply
      // 发出的读取会把刚确认的线整批覆盖掉（图上闪一下又消失）。
      this.seq.set(key, (this.seq.get(key) ?? 0) + 1)
    }
    // `revoking` 同样不能清空：撤销 A 落地时，撤销 B 可能还在路上（见 beginRequest 的注释）。
    // 清掉只会让 B 的按钮提前解禁；B 自己的 finally 会把它摘掉。
    this.lastLines.set(key, lines)
    this.write(key, {
      ...readySnapshot(key, lines, message),
      notice: message,
      noticeLevel: opts.level ?? 'info',
      retryable: opts.retryable ?? false,
      revoking: base.revoking,
    })
  }

  /** 清空全部（登出/测试隔离）。 */
  clearAll() {
    this.snapshots.clear()
    EMPTY_BY_KEY.clear()
    this.seq.clear()
    this.revokeInflight.clear()
    this.lastLines.clear()
    this.pending.clear()
    this.current = null
    this.emit()
  }

  /** 读某条撤销的在途 promise（`undefined` = 目前没有在途撤销）。 */
  revokeInflightFor(key: string): Promise<AnnotationSnapshot | null> | undefined {
    return this.revokeInflight.get(key)
  }

  /** 登记一条在途撤销（连点的唯一闸门，见 {@link revokeInflight}）。 */
  trackRevoke(key: string, promise: Promise<AnnotationSnapshot | null>) {
    this.revokeInflight.set(key, promise)
  }

  /** 撤销结束：只删自己那一条（别把后来者覆盖掉）。 */
  untrackRevoke(key: string, promise: Promise<AnnotationSnapshot | null>) {
    if (this.revokeInflight.get(key) === promise) this.revokeInflight.delete(key)
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  private write(key: string, snapshot: AnnotationSnapshot) {
    // 只保留最近 12 个目标，避免长时间切换标的把内存堆起来
    if (!this.snapshots.has(key) && this.snapshots.size >= 12) {
      const oldest = this.snapshots.keys().next().value
      if (typeof oldest === 'string') this.snapshots.delete(oldest)
    }
    this.snapshots.set(key, snapshot)
    this.emit()
  }

  private emit() {
    for (const listener of [...this.listeners]) {
      try {
        listener()
      } catch {
        // 订阅者自己的异常不能影响账本
      }
    }
  }
}

export const AnnotationLedger = new AnnotationLedgerImpl()

/** 测试隔离：清空账本（生产代码不要在渲染路径里调用）。 */
export function resetAnnotationLedger() {
  AnnotationLedger.clearAll()
}
