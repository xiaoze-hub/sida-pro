/**
 * 钉子: API 客户端路径**不许自带 `/api` 前缀**。
 *
 * 由来(2026-09-18 生产实测踩到): 档位页写成 `fetchAPI('/api/tiers')`, 而 `fetchAPI` 的 baseURL
 * 已经是 `…/api` → 实际请求 `/api/api/tiers` → **404**, 页面一直显示"档位信息暂时取不到"。
 * 后端测试查不出这类问题(接口本身是好的), 只有浏览器请求路径能暴露 —— 所以在这里钉死。
 */
import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const apiSrc = resolve(__dirname, '../../packages/api/src')

describe('API 客户端路径前缀', () => {
  it("fetchAPI 的第一个参数不能以 /api 开头(baseURL 已含 /api)", () => {
    const files = readdirSync(apiSrc).filter((f) => f.endsWith('.ts'))
    const bad: string[] = []
    for (const f of files) {
      const src = readFileSync(resolve(apiSrc, f), 'utf-8')
      for (const m of src.matchAll(/fetchAPI<[^>]*>\(\s*'(\/api\/[^']*)'/g)) {
        bad.push(`${f}: ${m[1]}`)
      }
      for (const m of src.matchAll(/fetchAPI\(\s*'(\/api\/[^']*)'/g)) {
        bad.push(`${f}: ${m[1]}`)
      }
    }
    expect(bad).toEqual([])
  })

  it('baseURL 确实以 /api 结尾(这条不成立的话上面那条就是错的)', () => {
    const client = readFileSync(resolve(apiSrc, 'client.ts'), 'utf-8')
    expect(client).toMatch(/API_BASE|BASE_URL/)
  })

  it('档位客户端用 /tiers 与 /pro/... 这种相对路径', () => {
    const src = readFileSync(resolve(apiSrc, 'tiers.ts'), 'utf-8')
    expect(src).toContain("fetchAPI<TiersResp>('/tiers')")
    expect(src).toContain("'/pro/apply'")
    expect(src).toContain("'/pro/apply/status'")
  })
})
