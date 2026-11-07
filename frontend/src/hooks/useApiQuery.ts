// W3.7/D7: TanStack Query 统一服务端状态层。
//
// 设计:
// - useApiQuery 是 fetchAPI 的薄壳: query 管理缓存/失效/重试/去重, fetchAPI 只做传输。
// - queryFn 默认 cacheMode:'reload' —— 绕开 fetchAPI 内置的 30s _RESP_CACHE,
//   否则 mutation 后 refetch 会拿到缓存里的旧值(TanStack Query 才是缓存所有者)。
// - path 传 null/空表示"条件查询"(enabled=false), 与 TanStack 惯例一致。
// - 页签 keep-alive(2026-10-15): 隐藏页签**停轮询** —— refetchInterval 只在该页签可见时生效,
//   回到前台立即补一次刷新(见 useTabActive)。缓存仍在, 切回不闪空。
import { useEffect, useRef, useSyncExternalStore } from 'react'
import { useMutation, useQuery, useQueryClient, type QueryClient, type QueryKey } from '@tanstack/react-query'

import { useTabActive } from '@sida/base-ui'
import { ApiTimeoutError, fetchAPI, type ApiRequestOptions } from '@sida/api'

import { safeJwtPayload } from '@/lib/jwt'

let _queryClient: QueryClient | null = null

/** main.tsx 装配时注册, 供非组件代码(登出流程)清缓存。 */
export function registerQueryClient(client: QueryClient) {
  _queryClient = client
}

// ── 登录身份: 个人数据按账号隔离的唯一判据(2026-11-07) ──
//
// 故障背景: 登录页是 `navigate('/')`(同标签页 SPA 换号, 不刷新), TanStack 缓存里
// `['profile']` / `['profile','stats']` 之类的 key **不含身份** ⇒ B 登录后个人中心
// 直接读到 A 的昵称/头像/统计, 在 B 的新数据回来前一直挂着别人的资料。
// 修法: 个人页查询 key 必须带身份后缀(`scopedKey`), 且身份变化要能主动触发重渲染
// (统一事件 `sida:auth-changed` 立即重判, 1s 轮询只作兜底)。
const IDENTITY_POLL_MS = 1000

/**
 * 统一的登录身份变更事件(2026-11-07)。
 *
 * 登录/登出/同页换号(登录页 `navigate('/')`, 不刷新、没有 storage 事件)完成后, 由登录流程
 * dispatch 一次。身份判据的消费方(`useIdentityScope` / 头像缓存)收到后**立刻**重判:
 * 只靠 1s 轮询的话, 换号后最长 1s 内页面还挂在旧身份上 —— 这段时间里看到的资料/头像/可见按钮
 * 都是上一个人的, 而且用户可能就在这一秒里点了保存(写进新账号, 见 Profile 的提交前核对)。
 * 轮询保留下来当兜底: 事件漏发(其它入口改 token)时行为不退化成"永远不刷新"。
 */
export const AUTH_CHANGED_EVENT = 'sida:auth-changed'

/** 登录/登出/换号后广播身份已变(幂等; 由登录流程调用)。 */
export function notifyAuthChanged(): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new Event(AUTH_CHANGED_EVENT))
}

let _idToken: string | null = null
let _idValue = ''

/** 当前登录身份; 未登录返回 ''。用 JWT 的 username/sub, 不用 token 前缀(HS256 头人人相同)。 */
export function currentIdentity(): string {
  let token = ''
  try {
    token = localStorage.getItem('token') || ''
  } catch {
    token = ''
  }
  if (token === _idToken) return _idValue
  _idToken = token
  if (!token) {
    _idValue = ''
    return _idValue
  }
  const payload = safeJwtPayload(token)
  const claim = payload?.username ?? payload?.sub
  _idValue = typeof claim === 'string' && claim ? `u:${claim}` : `t:${token}`
  return _idValue
}

let _idListeners = new Set<() => void>()
let _idTimer: ReturnType<typeof setInterval> | null = null
let _idLastEmitted = ''

function emitIdentityChange() {
  for (const fn of Array.from(_idListeners)) fn()
}

function pollIdentity() {
  const now = currentIdentity()
  if (now === _idLastEmitted) return
  _idLastEmitted = now
  emitIdentityChange()
}

function onStorageIdentity(event: StorageEvent) {
  // 别的标签页登录/登出/换号: 立刻重判, 不等轮询
  if (event.key === null || event.key === 'token' || event.key === 'user') pollIdentity()
}

/** 同标签页登录/登出/换号: 登录流程广播统一事件, 立刻重判(不再等 1s 轮询)。 */
function onAuthChangedIdentity() {
  pollIdentity()
}

function subscribeIdentity(fn: () => void): () => void {
  _idListeners.add(fn)
  if (_idListeners.size === 1) {
    _idLastEmitted = currentIdentity()
    if (typeof window !== 'undefined') {
      window.addEventListener('storage', onStorageIdentity)
      window.addEventListener(AUTH_CHANGED_EVENT, onAuthChangedIdentity)
      // 兜底: 有入口改了 token 却没广播事件时, 最迟 1s 内也能跟上(不再作为唯一发现手段)
      _idTimer = setInterval(pollIdentity, IDENTITY_POLL_MS)
    }
  }
  return () => {
    _idListeners.delete(fn)
    if (_idListeners.size === 0) {
      if (typeof window !== 'undefined') {
        window.removeEventListener('storage', onStorageIdentity)
        window.removeEventListener(AUTH_CHANGED_EVENT, onAuthChangedIdentity)
      }
      if (_idTimer != null) clearInterval(_idTimer)
      _idTimer = null
    }
  }
}

/**
 * 当前登录身份(响应式): 同标签页切号/别的标签页换号/登出都会更新。
 * 个人页用它给 query key 加身份后缀, 给本地编辑态做"换人就重来"。
 */
export function useIdentityScope(): string {
  return useSyncExternalStore(subscribeIdentity, currentIdentity, () => '')
}

/** 给个人数据 query key 加身份后缀: `['profile']` → `['profile', 'u:alice']`。 */
export function scopedKey(base: QueryKey, identity: string = currentIdentity()): QueryKey {
  return [...base, identity || 'anon']
}

/** fetchAPI 的默认请求超时(与 `packages/api/src/client.ts` 的 DEFAULT_TIMEOUT_MS 一致)。 */
const DEFAULT_REQUEST_TIMEOUT_MS = 15_000

export interface RequestDeadline {
  /** 传给 fetchAPI 的合并信号: 上游取消或超时到点都会 abort 它。 */
  signal: AbortSignal
  /** 计时器是否已到点(到点 ⇒ 是我们的超时, 不是调用方取消)。 */
  timedOut: () => boolean
  /** 请求 settle 之后必须调用: 清计时器 + 摘掉上游监听。 */
  dispose: () => void
}

/**
 * 取消 + 超时合并信号(2026-11-07 复审)。
 *
 * 手写而**不用** `AbortSignal.any` / `AbortSignal.timeout`: 后者在 Safari < 15.4 与部分
 * 内嵌 WebView 里根本不存在, 一调用就抛 TypeError; 而这是查询的必经路径 ⇒ 不是"超时不好使",
 * 是整站取数一起报错。兼容性必须自己在代码里兜住, 不能指望浏览器版本。
 *
 * 超时窗口必须覆盖**响应体**: fetchAPI 收到调用方 `signal` 后就不再自建超时(见
 * `client.ts::_attemptFetch`), 所以计时器的生命周期归调用方 —— 由外层在 fetchAPI settle 后
 * 才 `dispose()`。头到了就清计时器会把"体挂住"变成永不 settle。
 */
export function createRequestDeadline(upstream: AbortSignal | undefined, timeoutMs: number): RequestDeadline {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | null = null
  let fired = false

  const abortNow = () => {
    if (!controller.signal.aborted) controller.abort()
  }
  const onUpstreamAbort = () => abortNow()

  if (upstream) {
    if (upstream.aborted) abortNow()
    else upstream.addEventListener('abort', onUpstreamAbort)
  }
  if (!controller.signal.aborted && timeoutMs > 0) {
    timer = setTimeout(() => {
      fired = true
      abortNow()
    }, timeoutMs)
  }

  return {
    signal: controller.signal,
    timedOut: () => fired,
    dispose: () => {
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
      upstream?.removeEventListener('abort', onUpstreamAbort)
    },
  }
}

export interface UseApiQueryOptions {
  /** 传给 fetchAPI 的额外选项(timeoutMs 等) */
  requestOptions?: ApiRequestOptions
  /** 保存前需要取消在途请求的页面显式启用；不能改变全站默认超时语义。 */
  cancelOnQueryAbort?: boolean
  /** 条件启用; enabled=false 时不发请求 */
  enabled?: boolean
  /** 覆盖全局 staleTime(毫秒) */
  staleTime?: number
  /** 覆盖全局轮询间隔(毫秒); 0=不轮询 */
  refetchInterval?: number
}

export function useApiQuery<T>(
  key: QueryKey,
  path: string | null | undefined,
  options?: UseApiQueryOptions,
) {
  const enabled = (options?.enabled ?? true) && !!path
  // 隐藏页签停轮询(keep-alive 成本控制)。初次取数不受影响(缓存该有就有), 只掐掉定时重取。
  const tabActive = useTabActive()
  const interval = options?.refetchInterval
  const query = useQuery<T>({
    queryKey: key,
    queryFn: ({ signal }) => {
      if (!path) throw new Error('useApiQuery: path 为空但已启用')
      const request: ApiRequestOptions = { cacheMode: 'reload', ...options?.requestOptions }
      // 默认路径一个字不改: 不传 signal ⇒ fetchAPI 用自己的超时(15s)/GET 单次重试/在途合并。
      // 传 signal 会把这三样全关掉(见 client.ts), 所以只有页面显式要求"保存前取消在途查询"时
      // 才接管 —— 不允许为了个别页面的取消需求把全站超时语义改薄。
      if (!options?.cancelOnQueryAbort || request.signal) return fetchAPI<T>(path, request)
      const timeoutMs =
        typeof request.timeoutMs === 'number' && request.timeoutMs > 0
          ? request.timeoutMs
          : DEFAULT_REQUEST_TIMEOUT_MS
      const deadline = createRequestDeadline(signal, timeoutMs)
      return fetchAPI<T>(path, { ...request, signal: deadline.signal, timeoutMs })
        .catch((error: unknown) => {
          // 上游取消(TanStack cancelQueries / 卸载)原样抛; 我们计时器到点则归类成 TIMEOUT,
          // 页面据此转错误态 + 重试, 而不是把"超时"混成"取消"一直转圈。
          if (deadline.timedOut()) throw new ApiTimeoutError(timeoutMs)
          throw error
        })
        .finally(deadline.dispose)
    },
    enabled,
    staleTime: options?.staleTime,
    refetchInterval: tabActive && interval ? interval : undefined,
  })

  // 回到前台(隐藏→可见)立即补一次刷新 —— 否则最长要等一个 interval 才更新, 读数停在旧值。
  // refetch 的引用每次渲染都会变, 所以放 ref, effect 只依赖可见性本身。
  const refetchRef = useRef(query.refetch)
  refetchRef.current = query.refetch
  const wasActiveRef = useRef(tabActive)
  useEffect(() => {
    if (tabActive && !wasActiveRef.current && enabled) void refetchRef.current()
    wasActiveRef.current = tabActive
  }, [tabActive, enabled])

  return query
}

export interface UseApiMutationOptions<TVars, TData> {
  /** 成功后要失效的 query key 列表(触发相关查询重取) */
  invalidateKeys?: QueryKey[]
  onSuccess?: (data: TData, vars: TVars) => void
}

export function useApiMutation<TData = unknown, TVars = void>(
  mutationFn: (vars: TVars) => Promise<TData>,
  options?: UseApiMutationOptions<TVars, TData>,
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: (data, vars) => {
      for (const key of options?.invalidateKeys ?? []) {
        queryClient.invalidateQueries({ queryKey: key })
      }
      options?.onSuccess?.(data, vars)
    },
  })
}

/** 登出/切换账号时清空全部服务端状态缓存(配合既有 clearResponseCache 调用点)。 */
export function clearQueryCache() {
  _queryClient?.clear()
}
