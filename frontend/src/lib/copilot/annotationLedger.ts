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
   * 开一次请求：记录"当前目标"、递增该 key 的序号、**同步**把快照置成 loading（清空旧线）。
   * 返回的序号必须原样交给 {@link commit}。
   */
  beginRequest(target: AnnotationTarget): number {
    this.current = target
    const key = targetKey(target)
    const next = (this.seq.get(key) ?? 0) + 1
    this.seq.set(key, next)
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
      revoking: [],
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
    this.write(key, { ...snapshot, key })
    return true
  }

  /** 目标级拒画（未登录/指数/同号代码）：不发请求，直接给出原因。 */
  setRefused(target: AnnotationTarget, message: string): AnnotationSnapshot {
    this.current = target
    const key = targetKey(target)
    // 拒画也要让序号前进，避免"拒画之前发出的请求"回来把线画上去
    this.seq.set(key, (this.seq.get(key) ?? 0) + 1)
    const snap: AnnotationSnapshot = {
      ...EMPTY,
      key,
      phase: 'refused',
      message,
    }
    this.write(key, snap)
    return snap
  }

  /** 读取失败（网络/形状不符）：清空旧线并给出可重试原因。 */
  setError(target: AnnotationTarget, message: string): AnnotationSnapshot {
    const key = targetKey(target)
    const snap: AnnotationSnapshot = {
      ...EMPTY,
      key,
      phase: 'error',
      message,
      retryable: true,
    }
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
    // 已确认的行一定比「此前发出的任何一次读取」都新：把序号推进一格，
    // 让那些还在路上的旧读取回来时被 {@link commit} 丢弃 —— 否则一个早于 apply
    // 发出的读取会把刚确认的线整批覆盖掉（图上闪一下又消失）。
    this.seq.set(key, (this.seq.get(key) ?? 0) + 1)
    this.write(key, {
      ...readySnapshot(key, lines, message),
      notice: message,
      noticeLevel: opts.level ?? 'info',
      retryable: opts.retryable ?? false,
      revoking: [],
    })
  }

  /** 清空全部（登出/测试隔离）。 */
  clearAll() {
    this.snapshots.clear()
    EMPTY_BY_KEY.clear()
    this.seq.clear()
    this.revokeInflight.clear()
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
