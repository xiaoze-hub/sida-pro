// W3.7/D7: TanStack Query 统一服务端状态层。
//
// 设计:
// - useApiQuery 是 fetchAPI 的薄壳: query 管理缓存/失效/重试/去重, fetchAPI 只做传输。
// - queryFn 默认 cacheMode:'reload' —— 绕开 fetchAPI 内置的 30s _RESP_CACHE,
//   否则 mutation 后 refetch 会拿到缓存里的旧值(TanStack Query 才是缓存所有者)。
// - path 传 null/空表示"条件查询"(enabled=false), 与 TanStack 惯例一致。
// - 页签 keep-alive(2026-10-15): 隐藏页签**停轮询** —— refetchInterval 只在该页签可见时生效,
//   回到前台立即补一次刷新(见 useTabActive)。缓存仍在, 切回不闪空。
import { useEffect, useRef } from 'react'
import { useMutation, useQuery, useQueryClient, type QueryClient, type QueryKey } from '@tanstack/react-query'

import { useTabActive } from '@sida/base-ui'
import { fetchAPI, type ApiRequestOptions } from '@sida/api'

let _queryClient: QueryClient | null = null

/** main.tsx 装配时注册, 供非组件代码(登出流程)清缓存。 */
export function registerQueryClient(client: QueryClient) {
  _queryClient = client
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
    queryFn: () => {
      if (!path) throw new Error('useApiQuery: path 为空但已启用')
      return fetchAPI<T>(path, { cacheMode: 'reload', ...options?.requestOptions })
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
