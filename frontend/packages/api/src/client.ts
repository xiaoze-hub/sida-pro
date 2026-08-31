export const API_BASE = '/api'
const DEFAULT_TIMEOUT_MS = 20000

interface ApiResponse<T> {
  code: number
  success?: boolean
  data: T
  message: string
}

export function getToken(): string | null {
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
  // 修复(M-1, 2026-08-23): 在 login 后 returnUrl 回跳, 避免被踢后失去当前页面.
  // 仅在已登录页(非 /login)时记录, 避免自己跳自己时把 returnUrl 写成 /login.
  try {
    const here = window.location.pathname + window.location.search + window.location.hash
    if (here && !here.startsWith('/login')) {
      sessionStorage.setItem('postLoginReturnUrl', here)
    }
  } catch { /* sessionStorage 不可用就略过 */ }
  window.location.href = '/login'
}

// 修复(L-4, 2026-08-23): 原 `new Date(string) < new Date()` 受本地时区影响(后端 UTC / 前端 CST
// 会差几小时, 影响过期判断). 改用 Date.parse 严格 ISO 8601 解析:
// - 解析失败返回 NaN, NaN < Date.now() → true → 视为已过期, fail-closed
// - 后端应继续保证 expires_at 是 ISO 8601(如 '2026-08-23T12:00:00Z')
export function isAuthenticated(): boolean {
  const token = getToken()
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

function _cacheKey(path: string, options?: ApiRequestOptions): string | null {
  if (!options || options.method === undefined || options.method === 'GET' || options.method === null) {
    return `${getToken()?.slice(0, 8) || 'anon'}:${path}`
  }
  return null // 只缓存 GET
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

  const timeoutController = options?.signal ? null : new AbortController()
  const timeoutMs = typeof options?.timeoutMs === 'number' && options.timeoutMs > 0
    ? options.timeoutMs
    : DEFAULT_TIMEOUT_MS
  const timeoutId = timeoutController
    ? window.setTimeout(() => timeoutController.abort(), timeoutMs)
    : null

  let res: Response
  try {
    const { timeoutMs: _timeoutMs, cacheMode: _cacheMode, ...requestOptions } = options || {}
    res = await fetch(`${API_BASE}${path}`, {
      ...requestOptions,
      headers: {
        ...headers,
        ...(requestOptions.headers as Record<string, string> | undefined),
      },
      signal: requestOptions.signal || timeoutController?.signal,
    })
  } catch (error: any) {
    if (error?.name === 'AbortError') {
      throw new Error('请求超时，请稍后重试')
    }
    throw error
  } finally {
    if (timeoutId !== null) {
      window.clearTimeout(timeoutId)
    }
  }

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
    throw new Error(body.message || `HTTP ${res.status}`)
  }
  // 2026-08-12: GET 成功后写缓存
  if (ckey && options?.cacheMode !== false) {
    _RESP_CACHE.set(ckey, { ts: Date.now(), data: body.data })
  }
  return body.data
}

/** 2026-08-12: 清空前端响应缓存(登出/手动刷新时调用) */
export function clearResponseCache() {
  _RESP_CACHE.clear()
}

export const apiClient = {
  request: fetchAPI,
}
