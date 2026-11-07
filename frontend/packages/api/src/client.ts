export const API_BASE = '/api'
// 2026-10-10 前端加载韧性: 默认超时 20s → 15s。
// 后端偶发 502 闪断/慢时, 无超时的 fetch 会让组件**永久 loading**(用户只能刷新浏览器)。
// 收紧到 15s 后由下面的 AbortController 抛类型化超时错误, 页面转显式错误态+重试;
// 调用方仍可用 `timeoutMs` 覆盖(重 POST/慢接口各自语义不变, 不受此默认值影响)。
const DEFAULT_TIMEOUT_MS = 15000

/** GET 幂等请求失败自动重试次数(2026-10-10): 超时/网络错误/5xx 各重试一次; POST 永不重试。 */
const MAX_GET_RETRIES = 1

interface ApiResponse<T> {
  code: number
  success?: boolean
  data: T
  message: string
}

/** 错误类型标记(2026-10-10): 页面/错误态据此分类, 不靠 message 文案猜。 */
export type ClientErrorKind = 'TIMEOUT' | 'NETWORK' | 'HTTP_5xx' | 'HTTP_4xx' | 'UNKNOWN'

/** fetchAPI 抛出的错误(2026-09-18): 带状态码与"权限类拒绝"的结构化标记 */
export interface ApiError extends Error {
  status?: number
  code?: number
  /** 类型化标记(2026-10-10): TIMEOUT / NETWORK / HTTP_5xx / HTTP_4xx。 */
  kind?: ClientErrorKind
  /** 超时错误带上本次实际超时毫秒(排查用)。 */
  timeoutMs?: number
  /**
   * 权限类拒绝标记 —— 后端标记 pro 专属/需升级时带上, 页面据此弹升级引导,
   * 不用去猜错误文案(文案会改, 标记不会)。
   */
  proGate?: { proOnly: boolean; feature?: string }
  /** 429 限流时后端 `Retry-After` 头换算的等待毫秒(可能缺失) —— 供轮询层做退避。 */
  retryAfterMs?: number
  /**
   * 迟到的旧会话 401(2026-11-07): 这个 401 属于**发起时**的凭证, 而当前凭证已经换人/换代。
   * 调用方必须把它当成"这一发请求过期"处理, 不能据此登出当前账号。
   */
  staleAuth?: boolean
}

/**
 * 请求超时(类型化, 2026-10-10)。
 * 与网络错误的区别: 超时 = 后端在响应但太慢/没响应; 网络错误 = 连不上。
 * 页面错误态(`ErrorState.inferErrorType`)认 `kind`, 不靠文案猜。
 */
export class ApiTimeoutError extends Error implements ApiError {
  readonly name = 'ApiTimeoutError'
  readonly kind: ClientErrorKind = 'TIMEOUT'
  readonly timeoutMs: number
  constructor(timeoutMs: number, message = '请求超时，请稍后重试') {
    super(message)
    this.timeoutMs = timeoutMs
  }
}

/**
 * P0(2026-09-18) JWT httpOnly Cookie 迁移:
 * - 后端登录时同时下发 HttpOnly Cookie `sida_token`(JS 读不到) + 响应体 token
 * - fetchAPI 统一 credentials:'include' → 浏览器自动携带 Cookie
 * - getToken() 仍从 localStorage 读, 用于 Authorization header(旧路径兼容)
 * - 后端 get_current_user: **显式 Bearer 优先, Cookie 兜底**(2026-09-18 老板拍板) ——
 *   双轨并存, 旧 token 有效; 但浏览器里残留的旧账号 Cookie **不会**再盖掉显式 Bearer 身份。
 */
export function getToken(): string | null {
  // localStorage fallback(兼容旧会话 + 非浏览器客户端)
  // httpOnly Cookie 无法用 document.cookie 读取, 依赖 credentials:'include' 自动携带
  return localStorage.getItem('token')
}

/**
 * 请求要带出去的 Bearer 凭证(2026-11-07 归一化)。
 *
 * 与 `getToken()` 的区别只有一个: **坏值一律当没有凭证**。空串 / 纯空白 / 字符串 `'null'`
 * 与 `'undefined'`(历史上被写坏的存储值)都不算凭证 —— 后端 `_bearer_from_header` 只在
 * Bearer 非空时采纳它, 而 `Bearer null` 会被当成**真凭证**去验签, 验不过就 401:
 * httpOnly Cookie 明明有效, 用户却被判"登录已过期"。
 *
 * 返回 `null` 时调用方不发 Authorization 头, 让 Cookie 兜底(后端口径: 有 header 才是权威身份)。
 */
export function getRequestToken(): string | null {
  let raw: string | null = null
  try {
    raw = localStorage.getItem('token')
  } catch {
    return null
  }
  if (typeof raw !== 'string') return null
  const token = raw.trim()
  if (!token || token === 'null' || token === 'undefined') return null
  return token
}

/** 身份切换事件名(2026-11-07): 同一标签页登录/登出/换号后同步广播, 供上层清/重建缓存。 */
export const AUTH_IDENTITY_EVENT = 'sida:auth-changed'

/** 身份变化原因。`revoke` = 用户主动下线当前会话, `delete` = 账号注销。 */
export type AuthIdentityChangeReason = 'login' | 'logout' | 'manual' | 'revoke' | 'delete'

/**
 * 从 JWT payload 解出**可信的账号身份**(2026-11-07), 解不出返回 null。
 *
 * 为什么不校验签名: 前端本来就只做展示/分区, 授权始终由后端验签决定; 这里只需要一个
 * "同一浏览器内区分 A/B 账号"的稳定标识。解不开(**失败即关闭**)时调用方必须关闭共享缓存。
 */
function _claimsOf(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split('.')
    if (parts.length < 2) return null
    let b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    while (b64.length % 4) b64 += '='
    const obj = JSON.parse(atob(b64))
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? (obj as Record<string, unknown>) : null
  } catch {
    return null
  }
}

let _identityToken: string | null = null
let _identityValue: string | null = null

/**
 * 缓存代次(2026-11-07): 身份 / 凭证换代、显式清理时 +1。
 *
 * 只把身份写进 key 还不够 —— 两种情况都会让"旧世界"的数据被当成当前数据:
 *   ① 登出前发出的请求**晚到**时, 会带着旧 key 写回表里(同一个人重新登录后又读得到它);
 *   ② **同一个 sub 换发 token**(撤销会话后重登 / 静默续期): 身份字符串一模一样, 旧缓存与
 *      在途请求会被继续复用 —— 被撤销的会话时代取到的私有数据还挂在新会话上。
 * 代次让"请求发起时的世界"与"响应落地时的世界"可比: 不一致即丢弃写入, key 也不再命中。
 *
 * 代次只在本模块内与 token 比较,**token 本身绝不进 key / 日志 / 事件**。
 */
let _cacheEpoch = 0
/** 当前代次对应的 token(仅内存比较用; 不导出、不进任何 key)。 */
let _epochToken: string | null = null

/**
 * 同步凭证代次(2026-11-07): token 变了(换号 / 同账号换发 / 登出)就推进代次并清表。
 *
 * 调用点覆盖每个会用到身份的地方(取身份、算缓存 key), 所以**静默替换 token**(没有广播事件、
 * 没有 storage 事件的入口)也一定会被下一次请求发现, 而不是等上层记得通知。
 */
function _syncTokenGeneration(): void {
  let token: string | null = null
  try {
    token = localStorage.getItem('token')
  } catch {
    token = null
  }
  if (token === _epochToken) return
  _epochToken = token
  _cacheEpoch += 1
  _RESP_CACHE.clear()
  _INFLIGHT.clear()
}

/**
 * 当前请求的缓存身份(2026-11-07)。**没有可信身份时返回 null** = 禁用共享缓存。
 *
 * 故障背景: 旧 `_cacheKey` 用 `token.slice(0, 8)` 当身份 —— HS256 的 JWT 头部
 * (`{"alg":"HS256","typ":"JWT"}`)对**所有用户完全相同**, 前 8 字符恒为 `eyJhbGci`。
 * 于是同一标签页里 A 登录后写下的响应/在途请求, B 登录后被当成"自己的"直接命中,
 * 个人中心/头像/统计串号。
 *
 * 现在的判据是 payload 的 `sub`(后端 user.id)优先, 退化到 `username`。
 * - 无 localStorage token(Cookie-only 会话): 身份由 httpOnly `sida_token` 决定, JS 读不到,
 *   无法区分 A/B ⇒ 返回 null, 禁用响应缓存与在途合并(宁可多打一次请求, 不可串号)。
 * - token 解不出 sub/username(非 JWT / 残缺)⇒ 同样 null, fail-closed。
 */
export function getAuthIdentity(): string | null {
  _syncTokenGeneration()
  const token = getToken()
  if (!token) {
    _identityToken = null
    _identityValue = null
    return null
  }
  if (token === _identityToken) return _identityValue
  _identityToken = token
  const claims = _claimsOf(token)
  const sub = typeof claims?.sub === 'string' ? claims.sub : ''
  const username = typeof claims?.username === 'string' ? claims.username : ''
  // 身份标识只由 claim 组成 —— **不含 token 本身**, 避免任何路径把它带进日志/错误信息。
  _identityValue = sub || username ? `u:${sub || username}` : null
  return _identityValue
}

/** 当前凭证代次(单调递增; 换号/同账号换发 token/显式清理都会推进)。 */
export function getAuthGeneration(): number {
  _syncTokenGeneration()
  return _cacheEpoch
}

/**
 * 统一身份分桶串: `u:<sub>@<代次>`; 无法确认身份时 null(fail-closed)。
 * 上层(query key / 头像快照)只用它做分区, **不再各自解析 token**, 也不把原 token 写进 key。
 */
export function getAuthScope(): string | null {
  const identity = getAuthIdentity()
  return identity ? `${identity}@${_cacheEpoch}` : null
}

// 修复(M-1 + L-5, 2026-08-23): 多并发 401 同时触发 logout() 会重复跳转,
// 用 _logoutInProgress 标志位保证单飞(single-flight):
//   - 第一个 401 拿到锁, 构造 returnUrl 后跳转 login
//   - 后续 401 直接 no-op, 不再产生重复 window.location.href 赋值
// 服务端目前没有 refresh 机制, 401 只能强制登出; 等后续加上 /auth/refresh 后,
// 此函数仍可继续兼容(只是极少数情况下被触发).
let _logoutInProgress = false
export function logout() {
  if (_logoutInProgress) return
  _logoutInProgress = true
  localStorage.removeItem('token')
  localStorage.removeItem('token_expires')
  // 2026-11-07: `user` 是好几处私有状态的分区依据(自选记忆 / 系统状态快照 / 头像 / 设置页),
  // 登出必须一起清 —— 否则下一个人登录时若响应里没带 user, 页面继续按上一个人的 id 读写。
  // 与注销(DataPrivacySection)保持同一套键, 别让两条收尾路径各删一半。
  localStorage.removeItem('user')
  // 2026-11-07: 身份已不可信 —— 立即清响应缓存/在途表并广播。代次(epoch)随之递增,
  // 登出前发出的在途请求即使晚到, 也写不进"下一个身份"的缓存。
  notifyAuthIdentityChanged('logout')
  // P0(2026-09-18): best-effort 清服务端 httpOnly Cookie(JS 清不掉, 须走 API)
  try {
    void fetch(`${API_BASE}/auth/logout`, {
      method: 'POST',
      credentials: 'include',
      keepalive: true,
    }).catch(() => undefined)
  } catch { /* 忽略 */ }
  // 修复(M-1, 2026-08-23): 在 login 后 returnUrl 回跳, 避免被踢后失去当前页面.
  // 仅在已登录页(非 /login)时记录, 避免自己跳自己时把 returnUrl 写成 /login.
  try {
    const here = window.location.pathname + window.location.search + window.location.hash
    if (here && !here.startsWith('/login')) {
      sessionStorage.setItem('postLoginReturnUrl', here)
    }
  } catch { /* sessionStorage 不可用就略过 */ }
  // 2026-09-16: 退出登录跳落地页(官网), 不再跳 /login
  window.location.href = '/'
}

/**
 * 401 的统一裁决(2026-11-07): 这一发请求带出去的凭证**还是不是**当前凭证。
 *
 * `fetchAPI` 与两个自己 `fetch` 的流式入口(health / chat)都必须走这里, 否则同一条
 * "A 的在途请求在 B 登录后才 401" 会在其中一条路径上把 B 踢下线。
 *
 * 判据是"发起时带出去的凭证"与"现在的凭证"是否相同:
 *   - 相同 ⇒ 当前会话真的失效 → 登出(清本地身份 + 广播);
 *   - 不同(换号 / 登出 / 同账号换发) ⇒ 这个 401 属于旧世界, 只让本次请求失败(staleAuth),
 *     不动当前会话与缓存。
 *
 * 两种凭证都取不到(Cookie-only 会话 / 匿名)时属于"不可区分": 按当前会话处理(登出是保守方向,
 * 反正本地没有可保护的凭证), 与既有行为一致。
 */
export function authErrorFor401(credentialToken: string | null): ApiError {
  const stale = getRequestToken() !== credentialToken
  if (!stale) logout()
  const err = new Error(stale ? '本次请求的登录状态已失效' : '登录已过期') as ApiError
  err.status = 401
  err.kind = 'HTTP_4xx'
  if (stale) err.staleAuth = true
  return err
}

// 修复(L-4, 2026-08-23): 原 `new Date(string) < new Date()` 受本地时区影响(后端 UTC / 前端 CST
// 会差几小时, 影响过期判断). 改用 Date.parse 严格 ISO 8601 解析:
// - 解析失败返回 NaN, NaN < Date.now() → true → 视为已过期, fail-closed
// - 后端应继续保证 expires_at 是 ISO 8601(如 '2026-08-23T12:00:00Z')
export function isAuthenticated(): boolean {
  const token = getToken()
  // UI 门禁仍看 localStorage(与既有路由守卫兼容); httpOnly Cookie 是 API 鉴权双轨,
  // 不单独作为"已登录"UI 依据(无 localStorage 时走登录页重新建立双写)。
  if (!token) return false

  const expires = localStorage.getItem('token_expires')
  if (expires) {
    const ts = Date.parse(expires)
    if (Number.isNaN(ts) || ts < Date.now()) {
      logout()
      return false
    }
  }
  return true
}

export interface ApiRequestOptions extends RequestInit {
  timeoutMs?: number
  /** 2026-08-12 前端缓存: 'reload'=强制刷新(跳过缓存) / 数字=该请求TTL秒(覆盖默认) / false=禁用 */
  cacheMode?: 'reload' | number | false
}

// 2026-08-12 前端状态缓存: GET 响应内存缓存(TTL 30s, 与行情刷新周期一致)。
// 解决"切换页面/重开弹窗每次都重新请求"——同会话内二次打开直接命中, 零请求。
// 仅缓存 GET, 且**必须**按认证身份分区(见 getAuthIdentity): A/B 账号的响应绝不能互相命中。
const _RESP_CACHE = new Map<string, { ts: number; data: unknown }>()
const _CACHE_TTL_DEFAULT = 30_000 // 30s

// 缓存代次 `_cacheEpoch` 的声明与其语义见上方 `_syncTokenGeneration`(身份分区 + 凭证换代)。

/**
 * 2026-10-02 首屏并发化: 同一 GET 的**在途**请求合并(single-flight)。
 *
 * 背景(B1 首页冷态走查代码定位): 首页「市场温度」被 3 个组件各打一次 `/market/phase`
 * (`usePhaseLabel` / `MarketPhaseCard` / `PhaseGaugeCard`), 「主线 Top1」被 2 个组件各打一次
 * `/market/mainline` —— 冷启动时它们与首屏其余 15+ 个请求一起争抢浏览器**同域连接池**
 * (HTTP/1.1 默认 6 条), 关键画布(`/market/phase`)实际排在自己的重复请求后面。
 * 与既有 30s 响应缓存同一取舍(GET 幂等): 并发同键**共享一次真实请求**, 响应回来后各调用方
 * 拿到同一份 data; 在途合并只在“上游还没回”这段窗口内生效, 不改变缓存 TTL 语义。
 *
 * 跳过条件(任一):
 *   - `cacheMode:'reload' | false` —— 调用方显式要新数据(如实时端点/手动刷新);
 *   - 自带 `signal` —— 取消权归调用方, 共享会让一方 abort 影响另一方;
 *   - 非 GET(ckey 为 null);
 *   - **身份不可信**(ckey 为 null, 见 getAuthIdentity) —— Cookie-only / 匿名会话里
 *     JS 看不到真实身份, 合并等于赌"两个调用方是同一个账号"。
 */
const _INFLIGHT = new Map<string, Promise<unknown>>()

function _cacheKey(path: string, options?: ApiRequestOptions): string | null {
  if (!options || options.method === undefined || options.method === 'GET' || options.method === null) {
    // 2026-11-07: 身份必须来自 JWT claim(sub/username)+ 凭证代次, **绝不能用 token 前缀** ——
    // HS256 头部对所有用户相同(恒为 `eyJhbGci`), 前缀分区等于不分區; 同 sub 换发的新 token
    // 也必须换一个代次, 否则被撤销会话时代的响应还挂在新会话上。
    // 没有可信身份(Cookie-only / 匿名 / 解不开)时返回 null: 不读缓存、不写缓存、不合并
    // 在途请求 —— 宁可多打一次请求, 也不能让两个身份共享同一份响应。
    const scope = getAuthScope()
    if (!scope) return null
    return `${scope}:${path}`
  }
  return null // 只缓存 GET
}

/**
 * API 失败监听(2026-09-22): 让上层把"这次请求失败了"汇进会话消息流(右栏 AlertLog)。
 * 用**注册回调**而不是直接 import UI 包 —— `packages/api` 是底层包, 不该反向依赖 biz-ui。
 * 上层在启动时注册一次即可(App.tsx)。
 */
type ApiFailure = { path: string; method: string; status: number; message: string }
let failureListener: ((f: ApiFailure) => void) | null = null

export function onApiFailure(fn: ((f: ApiFailure) => void) | null): void {
  failureListener = fn
}

function reportFailure(f: ApiFailure) {
  try {
    failureListener?.(f)
  } catch {
    /* 监听方出错绝不能影响主流程 */
  }
}

export async function fetchAPI<T>(path: string, options?: ApiRequestOptions): Promise<T> {
  const headers: Record<string, string> = {}

  // 2026-11-07: 用归一化后的凭证(见 getRequestToken) —— 头里带出去的那个值同时就是 401 时
  // 判断"这一发是否属于旧世界"的依据, 两处必须是同一个值, 不能在别处再读一次存储。
  const token = getRequestToken()
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  if (options?.body && !(options.body instanceof FormData)) {
    // 2026-08-15: FormData 不设 Content-Type —— 浏览器自动带 multipart/form-data; boundary=xxx,
    // 否则 FastAPI 收不到 UploadFile 字段(422 missing file)
    headers['Content-Type'] = 'application/json'
  }

  // 2026-08-12: GET 缓存命中直接返回(除非 cacheMode:'reload')
  const _CACHE_ENABLED = true
  const ckey = _cacheKey(path, options)
  if (_CACHE_ENABLED && ckey && options?.cacheMode !== 'reload' && options?.cacheMode !== false) {
    const hit = _RESP_CACHE.get(ckey)
    if (hit && Date.now() - hit.ts < _CACHE_TTL_DEFAULT) {
      return hit.data as T
    }
  }

  // 2026-10-02: 在途同键合并(见 _INFLIGHT 注释)。仅对“可缓存 GET”生效。
  const dedupable =
    !!ckey && options?.cacheMode !== 'reload' && options?.cacheMode !== false && !options?.signal
  if (dedupable) {
    const inflight = _INFLIGHT.get(ckey as string)
    if (inflight) return inflight as Promise<T>
  }

  const p = _fetchNow<T>(path, options, headers, ckey, token)
  if (dedupable) {
    const key = ckey as string
    _INFLIGHT.set(key, p as Promise<unknown>)
    const cleanup = () => {
      if (_INFLIGHT.get(key) === (p as Promise<unknown>)) _INFLIGHT.delete(key)
    }
    // .then(cleanup, cleanup) 而非 .finally: finally 派生的 promise 在拒绝时无人处理
    // 会冒 unhandled rejection; then 双分支返回的 promise 一定 resolved。
    void (p as Promise<unknown>).then(cleanup, cleanup)
  }
  return p
}

/**
 * 单次 fetch 尝试的句柄（2026-10-29 复审 B1：超时窗口必须**覆盖响应体**）。
 *
 * 旧实现把 `AbortController` 计时器放在一次 `fetch()` 的 try/finally 里 ——
 * `fetch()` 一 resolve（= 只收到响应头）就 `clearTimeout`，随后 `res.json()` 在**没有
 * 任何超时**的情况下读体。真实故障形态（网关先回 200 头、chunked 体卡住）下：
 * `_parseResponse` 无限 pending ⇒ 本次 `fetchAPI` 永不 settle ⇒ `_INFLIGHT` 里的共享
 * promise 永不释放 ⇒ 上游 hook 的 in-flight 闸门也永不释放，行情与市场墙钟一起冻结。
 *
 * 因此把"计时器生命周期"交给调用方：**读完响应体才 dispose**；计时器在体读到一半触发时
 * 同样 abort 掉这次读取，并记下 `timedOut()` 供上层归类成 TIMEOUT（而不是"解析失败"）。
 */
interface FetchAttempt {
  res: Response
  /** 本次尝试的计时器是否已经触发（体读到一半被我们 abort）。 */
  timedOut: () => boolean
  /** 释放计时器（体已读完 / 放弃这次响应）。 */
  dispose: () => void
}

async function _attemptFetch(
  path: string,
  options: ApiRequestOptions | undefined,
  headers: Record<string, string>,
  timeoutMs: number,
): Promise<FetchAttempt> {
  const callerSignal = options?.signal ?? null
  const timeoutController = callerSignal ? null : new AbortController()
  let timerFired = false
  const timeoutId = timeoutController
    ? window.setTimeout(() => {
        timerFired = true
        timeoutController.abort()
      }, timeoutMs)
    : null
  const dispose = () => {
    if (timeoutId !== null) window.clearTimeout(timeoutId)
  }

  try {
    const { timeoutMs: _timeoutMs, cacheMode: _cacheMode, ...requestOptions } = options || {}
    const res = await fetch(`${API_BASE}${path}`, {
      // P0(2026-09-18): 始终携带 Cookie(httpOnly sida_token + csrf_token)
      credentials: 'include',
      ...requestOptions,
      headers: {
        ...headers,
        ...(requestOptions.headers as Record<string, string> | undefined),
      },
      signal: requestOptions.signal || timeoutController?.signal,
    })
    // 头到了**不算完成**：计时器保留到体读完（见 FetchAttempt 注释）。
    return { res, timedOut: () => timerFired, dispose }
  } catch (error: any) {
    dispose()
    if (error?.name === 'AbortError') {
      if (callerSignal?.aborted) throw error // 调用方取消: 原样抛
      throw new ApiTimeoutError(timeoutMs)   // 我们的超时计时器触发
    }
    const netErr: Error = error instanceof Error ? error : new Error(String(error))
    ;(netErr as ApiError).kind = 'NETWORK'
    throw netErr
  }
}

/** 解析响应: 401 登出 / 业务码判定 / GET 写缓存。 */
async function _parseResponse<T>(
  res: Response,
  path: string,
  method: string,
  options: ApiRequestOptions | undefined,
  ckey: string | null,
  credentialToken: string | null,
): Promise<T> {
  if (res.status === 401) {
    /*
     * 2026-11-07 跨账号风险: **迟到的旧会话 401 不能把新账号登出**。
     *
     * 真实序列: A 的页面在途请求正在等响应 → 用户登出/换号, B 登录成功并已写入新 token →
     * A 那个请求此时才收到 401 → 旧实现无条件 `logout()` ⇒ 刚登录好的 B 被踢回登录页,
     * 而且是在 B 的会话完全有效的情况下。
     *
     * 裁决只在 `authErrorFor401` 一处(health/chat 两个流式入口共用同一份), 这里只负责
     * 把"旧世界的失败"排除在告警流之外 —— 否则换号后右栏会凭空出现一条"登录过期"。
     */
    const err = authErrorFor401(credentialToken)
    if (err.staleAuth) throw err
    reportFailure({ path, method, status: 401, message: err.message })
    throw err
  }

  const body: ApiResponse<T> = await res.json().catch(() => ({
    code: res.status,
    data: null as T,
    message: `HTTP ${res.status}`,
  }))
  if (body.code !== 0 || body.success === false) {
    // 2026-09-18: 权限类拒绝带结构化标记(后端 ResponseWrapper 透传) —— 附到 error 上,
    // 页面据此弹"升级 Pro"引导而不是当成普通报错(靠文案猜字符串太脆)。
    const err = new Error(body.message || `HTTP ${res.status}`) as ApiError
    const anyBody = body as any
    if (anyBody.pro_guide || anyBody.pro_only || anyBody.feature) {
      err.proGate = {
        proOnly: !!anyBody.pro_only,
        feature: typeof anyBody.feature === 'string' ? anyBody.feature : undefined,
      }
    }
    err.status = res.status
    err.code = body.code
    // 429: 把 Retry-After 换算成毫秒挂到错误上, 轮询层据此退避(不靠猜固定间隔)。
    if (res.status === 429) {
      const retryAfter = Number(res.headers?.get?.('Retry-After'))
      if (Number.isFinite(retryAfter) && retryAfter > 0) {
        err.retryAfterMs = Math.min(retryAfter, 3600) * 1000
      }
    }
    // 类型化(2026-10-10): 5xx/4xx 归类, 页面错误态不再靠 message 里的数字猜。
    err.kind = res.status >= 500 ? 'HTTP_5xx' : res.status >= 400 ? 'HTTP_4xx' : 'UNKNOWN'
    reportFailure({ path, method, status: res.status, message: err.message })
    throw err
  }
  // 2026-08-12: GET 成功后写缓存
  // 2026-11-07: 写回前重新算一次 key —— 身份切换/登出(→ null)或缓存代次已推进时,
  // 本次响应属于**上一个世界**, 直接丢弃(旧请求迟到不得污染新身份的缓存)。
  if (ckey && options?.cacheMode !== false && _cacheKey(path, options) === ckey) {
    _RESP_CACHE.set(ckey, { ts: Date.now(), data: body.data })
  }
  return body.data
}

/**
 * fetchAPI 的实际取数(与在途合并解耦, 便于同键共享同一份 promise)。
 *
 * 2026-10-10 加载韧性: **GET 幂等请求失败(超时 / 网络错误 / 5xx)自动重试一次**;
 * POST/PUT/PATCH/DELETE 一律不重试(可能已产生副作用)。调用方自带 `signal` 时也不重试
 * (取消权归调用方, 重试会违背其取消意图)。重试在**同一次 fetchAPI 调用内**进行, 对外仍是
 * "一次调用 / 一份 promise" —— 在途合并与 30s 缓存的语义都不受影响。
 */
async function _fetchNow<T>(
  path: string,
  options: ApiRequestOptions | undefined,
  headers: Record<string, string>,
  ckey: string | null,
  credentialToken: string | null,
): Promise<T> {
  const method = (options?.method ?? 'GET').toUpperCase()
  const callerSignal = options?.signal ?? null
  const canRetry = method === 'GET' && !callerSignal
  const timeoutMs = typeof options?.timeoutMs === 'number' && options.timeoutMs > 0
    ? options.timeoutMs
    : DEFAULT_TIMEOUT_MS

  let lastError: unknown = null
  for (let attempt = 0; attempt <= MAX_GET_RETRIES; attempt++) {
    const canRetryThisRound = canRetry && attempt < MAX_GET_RETRIES
    let handle: FetchAttempt
    try {
      handle = await _attemptFetch(path, options, headers, timeoutMs)
    } catch (error) {
      const kind = error instanceof ApiTimeoutError ? 'TIMEOUT' : (error as ApiError | undefined)?.kind
      const retryable = kind === 'TIMEOUT' || kind === 'NETWORK'
      if (retryable && canRetryThisRound) {
        lastError = error
        continue
      }
      // 终态失败(不可重试 / 重试已用尽): 广播给右栏 AlertLog 后抛类型化错误
      if (retryable) {
        reportFailure({
          path,
          method,
          status: 0,
          message: kind === 'TIMEOUT' ? '请求超时' : ((error as Error)?.message || '网络错误'),
        })
      }
      throw error
    }

    try {
      // 5xx 视为可重试(服务端瞬时故障); 4xx 是确定性错误, 重试无意义。
      if (handle.res.status >= 500 && canRetryThisRound) {
        lastError = Object.assign(new Error(`HTTP ${handle.res.status}`), { status: handle.res.status, kind: 'HTTP_5xx' })
        continue
      }
      // 体在这里读完；计时器在读完之前一直有效（B1：头到了不算完成）。
      return await _parseResponse<T>(handle.res, path, method, options, ckey, credentialToken)
    } catch (error) {
      // 体读到一半被超时计时器 abort：仍归类为 TIMEOUT（不是"响应解析失败"）。
      if (handle.timedOut()) {
        const timeoutError = new ApiTimeoutError(timeoutMs)
        if (canRetryThisRound) {
          lastError = timeoutError
          continue
        }
        reportFailure({ path, method, status: 0, message: '请求超时' })
        throw timeoutError
      }
      throw error
    } finally {
      handle.dispose()
    }
  }

  // 理论不可达(循环内必定 return 或 throw); 兜底抛最后一次错误。
  throw lastError ?? new Error('请求失败')
}

/** 2026-08-12: 清空前端响应缓存(登出/手动刷新时调用)
 *  2026-10-02: 一并清掉在途合并表 —— 登出/显式刷新语义上是「别再复用这一轮的结果」,
 *  在途项被移除后, 新调用会重新发起真实请求(在途的旧 promise 仍会 resolve 给原调用方)。 */
export function clearResponseCache() {
  // 2026-11-07: 代次推进 —— 清理后仍在途的请求回来时, key 已不匹配, 写入被丢弃。
  _cacheEpoch += 1
  _RESP_CACHE.clear()
  _INFLIGHT.clear()
}

/**
 * 身份变更的额外清理回调(2026-11-07): 让上层(React Query 缓存)能挂在**同一个**收尾点上。
 *
 * `packages/api` 是底层包, 不能反向 import 业务层; 但登出/撤销会话必须同时清掉两层缓存,
 * 否则"同标签页重新登录"这类不刷新的路径仍会命中上一个身份/上一代 token 的条目。
 * 上层在启动时注册一次(`registerQueryClient`), 这里只负责通知。
 */
let identityResetListener: ((reason: AuthIdentityChangeReason) => void) | null = null

export function onAuthIdentityChanged(fn: ((reason: AuthIdentityChangeReason) => void) | null): void {
  identityResetListener = fn
}

/**
 * 同步通知"登录身份已切换"(2026-11-07), 登录/登出/换号/撤销会话都必须调用。
 *
 * 做两件事, 且都是**同步**的 —— 不能等下一次身份轮询:
 *   1. 能力层: 清响应缓存 + 在途合并表 + 推进代次(旧响应迟到写不进来);
 *   2. 事件层: 广播 `AUTH_IDENTITY_EVENT`, 让上层(React Query 缓存等)立刻按新身份重建。
 *
 * 事件 detail 只带 reason, **不带 token / 用户标识** —— 避免凭证流向日志或第三方监听者。
 */
export function notifyAuthIdentityChanged(reason: AuthIdentityChangeReason = 'login'): void {
  // 登录成功后必须解除登出单飞锁 —— 否则"登出一次 → 重新登录"之后再遇到真 401 不会登出。
  if (reason === 'login') _logoutInProgress = false
  clearResponseCache()
  try {
    identityResetListener?.(reason)
  } catch {
    /* 上层清理失败不能阻断登录/登出主流程 */
  }
  try {
    if (typeof window !== 'undefined' && typeof CustomEvent !== 'undefined') {
      window.dispatchEvent(new CustomEvent(AUTH_IDENTITY_EVENT, { detail: { reason } }))
    }
  } catch {
    /* 非浏览器环境(SSR / 单测)没有 dispatchEvent 也不该影响登录流程 */
  }
}

export const apiClient = {
  request: fetchAPI,
}
