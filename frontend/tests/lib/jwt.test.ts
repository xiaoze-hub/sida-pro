// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest'
import { getJwtRole, getJwtUsername, isDemoUser, isGuestUser, safeJwtPayload } from '@/lib/jwt'

const jwt = (payload: unknown) =>
  ['e30', btoa(JSON.stringify(payload)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''), 'sig'].join('.')

beforeEach(() => localStorage.clear())

describe('safeJwtPayload 容错(M-2 口径随抽取保留)', () => {
  it('空/两段/非法 base64/数组 payload → null', () => {
    expect(safeJwtPayload('')).toBeNull()
    expect(safeJwtPayload('a.b')).toBeNull()
    expect(safeJwtPayload('e30.!!!.sig')).toBeNull()
    expect(safeJwtPayload(jwt([1, 2]))).toBeNull()
  })
  it('正常 token 解出 claims', () => {
    expect(safeJwtPayload(jwt({ role: 'owner' }))).toEqual({ role: 'owner' })
  })
})

describe('角色读取', () => {
  it('无 token → role/username 皆 null, 非 demo 非 guest', () => {
    expect(getJwtRole()).toBeNull()
    expect(getJwtUsername()).toBeNull()
    expect(isDemoUser()).toBe(false)
    expect(isGuestUser()).toBe(false)
  })
  it('owner / member / demo 三种口径', () => {
    localStorage.setItem('token', jwt({ username: 'a', role: 'owner' }))
    expect(getJwtRole()).toBe('owner')
    expect(isGuestUser()).toBe(false)
    localStorage.setItem('token', jwt({ username: 'demo', role: 'member' }))
    expect(isDemoUser()).toBe(true)
    expect(isGuestUser()).toBe(true)   // demo 账号按 guest 处理(后端口径一致)
    localStorage.setItem('token', jwt({ username: 'b', role: 'guest' }))
    expect(isGuestUser()).toBe(true)
  })
})
