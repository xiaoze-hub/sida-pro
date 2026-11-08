// @vitest-environment jsdom
/**
 * 跨标签页换号: TanStack 服务端状态缓存必须整体作废 —— 2026-11-08。
 *
 * `registerQueryClient` 原来只挂 `onAuthIdentityChanged`(同标签页登录/登出/撤销)。
 * 另一个标签页换号走的是 **storage 事件**, 不经过那条链路 ⇒ 本标签页的 query 缓存
 * (键大多不含身份, 如 `['portfolio']`)会把上一个账号的数据直接喂给新身份 ——
 * 即使页面被重建, 重建后的第一帧仍是旧用户的缓存数据(stale-while-revalidate)。
 *
 * 判据: 凭证类 key(token / token_expires / user / clear)变化 ⇒ 缓存清空;
 *       无关 key 变化 ⇒ 缓存不动(不能把主题切换当成换号)。
 */
import { QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { registerQueryClient } from '@/hooks/useApiQuery'

const A_TOKEN = `e30.${btoa(JSON.stringify({ sub: 'user-a', username: 'alice' }))}.sig`
const B_TOKEN = `e30.${btoa(JSON.stringify({ sub: 'user-b', username: 'bob' }))}.sig`

function fireStorage(key: string | null) {
  window.dispatchEvent(new StorageEvent('storage', { key, newValue: B_TOKEN }))
}

let client: QueryClient

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('token', A_TOKEN)
  client = new QueryClient()
  registerQueryClient(client)
  client.setQueryData(['portfolio'], { owner: 'alice' })
  client.setQueryData(['profile', 'u:user-a@1'], { name: 'alice' })
})

afterEach(() => {
  client.clear()
  localStorage.clear()
})

describe('跨标签页凭证变化 → query 缓存整体作废', () => {
  it('另一个标签页写入新 token: 旧账号的缓存不得留给新身份', () => {
    localStorage.setItem('token', B_TOKEN)
    fireStorage('token')
    expect(client.getQueryData(['portfolio'])).toBeUndefined()
    expect(client.getQueryData(['profile', 'u:user-a@1'])).toBeUndefined()
  })

  it('另一个标签页登出(removeItem → key 为 token, newValue=null): 同样清空', () => {
    localStorage.removeItem('token')
    fireStorage('token')
    expect(client.getQueryData(['portfolio'])).toBeUndefined()
  })

  it('storage.clear()(key=null) 也按凭证变化处理', () => {
    localStorage.clear()
    fireStorage(null)
    expect(client.getQueryData(['portfolio'])).toBeUndefined()
  })

  it('无关 key(主题/侧栏折叠)不触发清缓存', () => {
    fireStorage('sida_theme')
    expect(client.getQueryData(['portfolio'])).toEqual({ owner: 'alice' })
  })
})
