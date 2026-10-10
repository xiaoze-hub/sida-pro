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
// 仅缓存 GET(带 token 的请求也可缓存, 因为 key 含 path, token 变化影响小)。
const _RESP_CACHE = new Map<string, { ts: number; data: unknown }>()
const _CACHE_TTL_DEFAULT = 30_000 // 30s

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
 *   - 非 GET(ckey 为 null)。
 */
const _INFLIGHT = new Map<string, Promise<unknown>>()

function _cacheKey(path: string, options?: ApiRequestOptions): string | null {
  if (!options || options.method === undefined || options.method === 'GET' || options.method === null) {
    return `${getToken()?.slice(0, 8) || 'anon'}:${path}`
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

  const token = getToken()
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

  const p = _fetchNow<T>(path, options, headers, ckey)
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
 * 单次 fetch 尝试(带本次尝试自己的超时计时器), 失败抛**类型化**错误。
 *
 *  - 超时(本函数注入的 AbortController 触发) → `ApiTimeoutError`(kind='TIMEOUT');
 *  - 调用方自带 `signal` 且已 abort → 原样抛该错误(取消权归调用方, 不误报超时/不重试);
 *  - 其它网络层失败 → **保留原错误对象**(带原始 message), 打上 `kind='NETWORK'` 标记后再抛。
 */
async function _attemptFetch(
  path: string,
  options: ApiRequestOptions | undefined,
  headers: Record<string, string>,
  timeoutMs: number,
): Promise<Response> {
  const callerSignal = options?.signal ?? null
  const timeoutController = callerSignal ? null : new AbortController()
  const timeoutId = timeoutController
    ? window.setTimeout(() => timeoutController.abort(), timeoutMs)
    : null

  try {
    const { timeoutMs: _timeoutMs, cacheMode: _cacheMode, ...requestOptions } = options || {}
    return await fetch(`${API_BASE}${path}`, {
      // P0(2026-09-18): 始终携带 Cookie(httpOnly sida_token + csrf_token)
      credentials: 'include',
      ...requestOptions,
      headers: {
        ...headers,
        ...(requestOptions.headers as Record<string, string> | undefined),
      },
      signal: requestOptions.signal || timeoutController?.signal,
    })
  } catch (error: any) {
    if (error?.name === 'AbortError') {
      if (callerSignal?.aborted) throw error // 调用方取消: 原样抛
      throw new ApiTimeoutError(timeoutMs)   // 我们的超时计时器触发
    }
    const netErr: Error = error instanceof Error ? error : new Error(String(error))
    ;(netErr as ApiError).kind = 'NETWORK'
    throw netErr
  } finally {
    if (timeoutId !== null) {
      window.clearTimeout(timeoutId)
    }
  }
}

/** 解析响应: 401 登出 / 业务码判定 / GET 写缓存。 */
async function _parseResponse<T>(
  res: Response,
  path: string,
  method: string,
  options: ApiRequestOptions | undefined,
  ckey: string | null,
): Promise<T> {
  if (res.status === 401) {
    logout()
    throw new Error('登录已过期')
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
    // 类型化(2026-10-10): 5xx/4xx 归类, 页面错误态不再靠 message 里的数字猜。
    err.kind = res.status >= 500 ? 'HTTP_5xx' : res.status >= 400 ? 'HTTP_4xx' : 'UNKNOWN'
    reportFailure({ path, method, status: res.status, message: err.message })
    throw err
  }
  // 2026-08-12: GET 成功后写缓存
  if (ckey && options?.cacheMode !== false) {
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
    let res: Response
    try {
      res = await _attemptFetch(path, options, headers, timeoutMs)
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

    // 5xx 视为可重试(服务端瞬时故障); 4xx 是确定性错误, 重试无意义。
    if (res.status >= 500 && canRetryThisRound) {
      lastError = Object.assign(new Error(`HTTP ${res.status}`), { status: res.status, kind: 'HTTP_5xx' })
      continue
    }
    return await _parseResponse<T>(res, path, method, options, ckey)
  }

  // 理论不可达(循环内必定 return 或 throw); 兜底抛最后一次错误。
  throw lastError ?? new Error('请求失败')
}

/** 2026-08-12: 清空前端响应缓存(登出/手动刷新时调用)
 *  2026-10-02: 一并清掉在途合并表 —— 登出/显式刷新语义上是「别再复用这一轮的结果」,
 *  在途项被移除后, 新调用会重新发起真实请求(在途的旧 promise 仍会 resolve 给原调用方)。 */
export function clearResponseCache() {
  _RESP_CACHE.clear()
  _INFLIGHT.clear()
}

export const apiClient = {
  request: fetchAPI,
}
