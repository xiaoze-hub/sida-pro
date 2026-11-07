import { useEffect, useSyncExternalStore } from 'react'
import { fetchAPI } from '@sida/api'
import { AUTH_CHANGED_EVENT, createRequestDeadline, currentIdentity } from '@/hooks/useApiQuery'

const EVENT = 'sida:avatar-changed'
/**
 * 个人头像走 `/api/profile`(users.avatar, 按用户隔离), **不是** `/settings/avatar`
 * —— 后者落的是全局单行 `ui_avatar`, 任何账号读到的都是同一个人(多账号串头像)。
 * 2026-11-07 收敛: 前端所有头像读写只认个人资料接口。
 */
const PROFILE_PATH = '/profile'
/**
 * 头像读取的显式超时(2026-11-07): 默认 15s 之外再收紧一点 —— 卡住的请求会一直占着
 * `inflight`, 期间不能重试; 到点失败后用户重新进页面即可重取(见 useAvatar 挂载重试)。
 */
const AVATAR_TIMEOUT_MS = 10_000

interface AvatarSnapshot {
  /** 该 value 属于哪个登录身份; 与当前身份不符时**一律不对外可见**。 */
  identity: string
  value: string
  /** 已从后端拿到确定结果(含"确实没有头像")。 */
  loaded: boolean
  /** 上次读取失败; 允许重试, 但失败期间同样只显示空(绝不回退旧账号)。 */
  failed: boolean
}

let snapshot: AvatarSnapshot = { identity: '', value: '', loaded: false, failed: false }
let inflight: { identity: string; controller: AbortController } | null = null
/**
 * 读写顺序保护(2026-11-07): 同一账号里"读"和"存"会并发 —— 早发的读如果晚于保存落地,
 * 就会拿旧头像盖掉刚存的新头像。规则:
 *  - 每次保存开始 + 落地各 writeEpoch +1; 读请求记下发起时的 epoch, 回来时 epoch 变了就丢弃
 *    (开始那次作废保存前的读, 落地那次作废保存途中发起的读 —— 它们读到的同样是旧值);
 *  - "头像已改"的无值事件同样 +1 并 abort 在途读: 事件只说明值变了, 不知道新值, 事件前发出的读
 *    晚到会把新头像盖回旧的;
 *  - 并发保存只认最后一次(seq), 先发后到的旧保存结果不写缓存也不广播。
 */
let writeEpoch = 0
let saveSeq = 0
/** 在途保存数: 保存期间不重启读(否则那次读拿到的是保存前的值, 还会白跑一趟)。 */
let savingWrites = 0
const listeners = new Set<() => void>()

function notify() {
  // 复制一份再遍历: 监听器在回调里退订/挂载新组件时不影响本轮分发
  for (const fn of Array.from(listeners)) fn()
}

/**
 * 当前登录身份(账号切换的唯一判据)。未登录返回 ''。
 *
 * 与 `useApiQuery.currentIdentity` 是**同一个**判据 —— 由 `@sida/api` 的 `getAuthScope`
 * 统一给出(`u:<sub>@<凭证代次>`): sub 优先于 username(用户名可重名/可改, 只有 sub 稳定),
 * 且同 sub 换发 token(撤销会话后重登)会换代次, 快照随之作废重取。
 * 不再各存一份 token 缓存: 两份缓存迟早分叉, 一处认 B 一处认 A 时读写就会串号。
 */
export function currentAvatarIdentity(): string {
  return currentIdentity()
}

/** 对外可见值: 身份不符(切号瞬间/已登出) 时恒为空, 不回退上一个账号的头像。 */
function visibleValue(): string {
  const identity = currentAvatarIdentity()
  return snapshot.identity === identity ? snapshot.value : ''
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/**
 * 确保当前身份的头像已加载(幂等; 可每帧调用)。
 * - 切号: 立刻清空可见值 + abort 旧请求, 旧响应回来也不会写进新账号;
 * - 未登录: 不发请求(否则 401 会触发 logout 跳走)。
 */
function ensureLoaded(options?: { retryFailed?: boolean }): void {
  const identity = currentAvatarIdentity()

  if (!identity) {
    if (snapshot.identity !== '' || snapshot.value !== '' || snapshot.loaded) {
      snapshot = { identity: '', value: '', loaded: true, failed: false }
      notify()
    }
    return
  }

  if (snapshot.identity !== identity) {
    // 之前那个账号的数据/在途请求与当前账号无关: 丢弃 + 取消
    inflight?.controller.abort()
    inflight = null
    snapshot = { identity, value: '', loaded: false, failed: false }
    notify()
  }

  if (options?.retryFailed && snapshot.failed) {
    // 重新挂载(进页面/换回本账号)才允许重试失败过的读取 —— 期间不会每帧重试
    snapshot = { identity, value: snapshot.value, loaded: false, failed: false }
    notify()
  }
  if (snapshot.loaded) return
  // 失败不自动重试(否则每帧一次请求风暴); 由重新挂载(retryFailed)或"头像已改"事件触发
  if (snapshot.failed) return
  if (inflight && inflight.identity === identity) return

  const controller = new AbortController()
  inflight = { identity, controller }
  const startedEpoch = writeEpoch
  // 取消 + 超时合并信号(2026-11-07): 不再用 AbortSignal.any/timeout(老 Safari/内嵌 WebView
  // 没有, 一调用就 TypeError)。fetchAPI 收到 signal 后不自建超时, 所以计时器在 fetchAPI
  // settle 之后才 dispose —— 窗口覆盖响应体, 体挂住也会落到下面的失败分支(可重挂重试)。
  const deadline = createRequestDeadline(controller.signal, AVATAR_TIMEOUT_MS)
  fetchAPI<{ avatar?: string }>(PROFILE_PATH, {
    cacheMode: 'reload',
    signal: deadline.signal,
    timeoutMs: AVATAR_TIMEOUT_MS,
  })
    .then(res => {
      // 乱序保护: 响应回来时可能已经切到别的账号 ⇒ 丢弃, 不写缓存不广播
      if (currentAvatarIdentity() !== identity) return
      // 同账号读写乱序: 这期间有过保存 ⇒ 本次读已是旧值, 丢弃并让缓存的保存值生效
      if (startedEpoch !== writeEpoch) {
        if (!savingWrites && !snapshot.loaded && !snapshot.failed) ensureLoaded()
        return
      }
      snapshot = {
        identity,
        value: typeof res?.avatar === 'string' ? res.avatar : '',
        loaded: true,
        failed: false,
      }
      notify()
    })
    .catch(() => {
      if (currentAvatarIdentity() !== identity) return
      if (startedEpoch !== writeEpoch) {
        // 保存已把结果写进缓存(或保存失败, 需重新取): 不要用这次过期读的失败覆盖
        if (!savingWrites && !snapshot.loaded && !snapshot.failed) ensureLoaded()
        return
      }
      snapshot = { identity, value: '', loaded: false, failed: true }
      notify()
    })
    .finally(() => {
      deadline.dispose()
      if (inflight?.controller === controller) inflight = null
    })
}

/**
 * 作废在途读取并重取(收到"头像已改"的无值广播时用)。
 *
 * 无值广播只知道"变了", 不知道新值。事件**之前**发出的读拿的是旧值, 如果放任它落地就会把
 * 刚存的新头像盖回去(个人中心保存后正好是这条路径: PUT 成功 → 广播 → 旧读才回来)。
 * 所以: epoch +1 作废它 + abort 掉(省一次无意义往返, 也免它占着 inflight 让重取被跳过),
 * 再从头取一次。
 */
function reload(): void {
  const identity = currentAvatarIdentity()
  if (!identity) return
  writeEpoch += 1
  inflight?.controller.abort()
  inflight = null
  snapshot = {
    identity,
    // 同账号保留当前可见值, 避免重取期间头像闪成空白; 换了号则一律清空(绝不回退旧账号)
    value: snapshot.identity === identity ? snapshot.value : '',
    loaded: false,
    failed: false,
  }
  notify()
  ensureLoaded()
}

/**
 * 上一次处理过的事件对象(2026-11-07): 每个挂载的 useAvatar 实例都注册了自己的监听器,
 * 同一次 `dispatchEvent` 会被投递给所有实例 —— 同源事件只处理一次, 否则"无值事件 ⇒ 重取"
 * 会被执行 N 次(每次都 abort 掉上一次刚发起的读, 白跑 N 个请求)。
 */
let lastHandledEvent: Event | null = null

function onAvatarEvent(event: Event): void {
  if (event === lastHandledEvent) return
  lastHandledEvent = event

  const identity = currentAvatarIdentity()
  if (!identity) return
  const detail = (event as CustomEvent<unknown>).detail
  if (typeof detail === 'string') {
    // 本会话保存成功后带值广播(服务端已确认): 直接采纳, 省一次往返。
    // 同时作废在途读: 它们读到的是保存前的值, 晚到会把这里的新值盖回去。
    writeEpoch += 1
    inflight?.controller.abort()
    inflight = null
    snapshot = { identity, value: detail, loaded: true, failed: false }
    notify()
    return
  }
  // 无值广播(如个人中心保存后)只表示"变了", 不能当成清空 —— 去后端重取
  reload()
}

/**
 * 保存头像(传空字符串=清空)。写个人资料接口 `/api/profile`(按用户隔离);
 * 成功后仅当身份未变时才写缓存/广播, 避免切号后把旧账号结果喂给新账号。
 */
export async function saveAvatar(value: string): Promise<void> {
  const identity = currentAvatarIdentity()
  if (!identity) throw new Error('未登录, 无法保存头像')
  const seq = ++saveSeq
  // 保存一开始就作废在途的旧读: 它读到的是保存前的头像, 晚到会把新头像盖回去
  writeEpoch += 1
  savingWrites += 1
  inflight?.controller.abort()
  inflight = null
  try {
    const res = await fetchAPI<{ avatar?: string }>(PROFILE_PATH, {
      method: 'PUT',
      body: JSON.stringify({ avatar: value }),
      timeoutMs: AVATAR_TIMEOUT_MS,
    })
    // 并发保存: 只认最后一次, 先发后到的旧结果不写缓存不广播
    if (seq !== saveSeq) return
    if (currentAvatarIdentity() !== identity) return
    // 落地再作废一次: 保存途中发起的读读到的同样是旧值(它可能比 PUT 先提交), 晚到会盖掉新头像
    writeEpoch += 1
    const saved = typeof res?.avatar === 'string' ? res.avatar : value
    snapshot = { identity, value: saved, loaded: true, failed: false }
    notify()
    window.dispatchEvent(new CustomEvent<string>(EVENT, { detail: saved }))
  } catch (error) {
    // 保存失败: 被我们 abort 的那次读不会自己回来 —— 重取一次拿真实值(仍走身份校验)。
    // 被后一次保存取代的旧保存不管: 让最新那次自己收尾。
    if (seq === saveSeq && currentAvatarIdentity() === identity && !snapshot.loaded) ensureLoaded()
    throw error
  } finally {
    savingWrites = Math.max(0, savingWrites - 1)
  }
}

/** 清空内存缓存(登出/切号兜底; 不触发请求)。 */
export function resetAvatarCache(): void {
  // 作废在途读: 登出再登录成同一个账号时, 登出前发出的读仍可能落地(身份字符串一样,
  // 单靠身份校验拦不住), epoch 变化是唯一可靠的判据。
  writeEpoch += 1
  inflight?.controller.abort()
  inflight = null
  snapshot = { identity: '', value: '', loaded: false, failed: false }
  notify()
}

/**
 * 当前头像(data URL 或图片地址)。来源为个人资料(按账号隔离), 跨组件即时同步。
 * 未加载/加载失败/刚切号 ⇒ 空串(调用方回退到首字母/图标), 不会短暂显示他人头像。
 */
export function useAvatar(): string {
  const value = useSyncExternalStore(subscribe, visibleValue, () => '')
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      // 别的标签页换号/登出 → 本标签页身份已变, 立刻重判(不能再挂旧账号头像)
      if (event.key === null || event.key === 'token' || event.key === 'user') ensureLoaded()
    }
    // 同标签页登录/登出/换号/撤销会话会广播统一事件(无 storage 事件): 立刻重判身份并重取,
    // 否则头像会一直挂在"上一个人"上直到别的原因触发重渲染。
    // 登出/撤销(无身份)必须连内存快照一起清: 只"不显示"不足以让旧值彻底失联。
    const onAuthChanged = () => {
      if (!currentAvatarIdentity()) resetAvatarCache()
      ensureLoaded()
    }
    window.addEventListener('storage', onStorage)
    window.addEventListener(AUTH_CHANGED_EVENT, onAuthChanged)
    window.addEventListener(EVENT, onAvatarEvent)
    // 重新挂载 = 用户重新进页面: 上一次失败(超时/网络)可以重试一次, 不靠刷新浏览器
    ensureLoaded({ retryFailed: true })
    return () => {
      window.removeEventListener('storage', onStorage)
      window.removeEventListener(AUTH_CHANGED_EVENT, onAuthChanged)
      window.removeEventListener(EVENT, onAvatarEvent)
    }
  }, [])
  // 无依赖: 同页切号(登录页 navigate 回 SPA, 组件不重挂)时, 任意一次重渲染都能跟上身份
  useEffect(() => {
    ensureLoaded()
  })
  return value
}

/**
 * 把上传的图片文件压缩为 size×size 的方形 JPEG data URL(居中裁剪),
 * 控制体积(约 10-20KB),避免大 base64 撑爆 DB 存储。
 */
export function fileToAvatarDataUrl(file: File, size = 128): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('读取文件失败'))
    reader.onload = () => {
      const img = new Image()
      img.onerror = () => reject(new Error('图片解析失败'))
      img.onload = () => {
        const canvas = document.createElement('canvas')
        canvas.width = size
        canvas.height = size
        const ctx = canvas.getContext('2d')
        if (!ctx) {
          reject(new Error('canvas 不可用'))
          return
        }
        const scale = Math.max(size / img.width, size / img.height)
        const w = img.width * scale
        const h = img.height * scale
        ctx.drawImage(img, (size - w) / 2, (size - h) / 2, w, h)
        resolve(canvas.toDataURL('image/jpeg', 0.85))
      }
      img.src = reader.result as string
    }
    reader.readAsDataURL(file)
  })
}
