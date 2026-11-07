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
import { fetchAPI, type ApiRequestOptions } from '@sida/api'

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
// (同标签页切换没有 storage 事件, 靠 useIdentityScope 的轮询兜底)。
const IDENTITY_POLL_MS = 1000

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

function subscribeIdentity(fn: () => void): () => void {
  _idListeners.add(fn)
  if (_idListeners.size === 1) {
    _idLastEmitted = currentIdentity()
    if (typeof window !== 'undefined') {
      window.addEventListener('storage', onStorageIdentity)
      _idTimer = setInterval(pollIdentity, IDENTITY_POLL_MS)
    }
  }
  return () => {
    _idListeners.delete(fn)
    if (_idListeners.size === 0) {
      if (typeof window !== 'undefined') window.removeEventListener('storage', onStorageIdentity)
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

export interface UseApiQueryOptions {
  /** 传给 fetchAPI 的额外选项(timeoutMs 等) */
  requestOptions?: ApiRequestOptions
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
      // signal 透传(2026-11-07): 保存个人资料前会 cancelQueries 掐掉在途旧读 ——
      // 不接 signal 的话旧读照样落库, 迟到的旧值会盖掉刚保存的新头像/新昵称。
      return fetchAPI<T>(path, { cacheMode: 'reload', ...options?.requestOptions, signal: options?.requestOptions?.signal ?? signal })
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
