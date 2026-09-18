/**
 * P2-4 钉子(前端): 档位页是**公开面** —— 未登录可看、不显示价格、字阶只用公开档位那几档。
 *
 * 由来: 档位对比原来是**没有的**; Profile 的 Pro 升级卡片里则**硬编码**了一串能力清单,
 * 权限一改就变假话。这轮把它换成"后端现读现拼 + 页面只排版"。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../..')
const app = readFileSync(resolve(root, 'src/App.tsx'), 'utf-8')
const page = readFileSync(resolve(root, 'src/pages/Tiers.tsx'), 'utf-8')
const profile = readFileSync(resolve(root, 'src/pages/Profile.tsx'), 'utf-8')

/** 去掉注释, 只留会渲染的代码。 */
function stripComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

/** 公开面字阶白名单(与 Landing/Developers 一致, ui-rules R10 公开面 ≤7 档) */
const PUBLIC_SIZES = new Set(['12', '13', '16', '20', '28', '36'])

describe('档位页: 公开可访问', () => {
  it('未登录时独立渲染(与 /terms /developers 同款)', () => {
    expect(app).toMatch(/location\.pathname === '\/tiers' && !isAuthenticated\(\)/)
    expect(app).toContain("<Route path=\"/tiers\" element={<TiersPage />} />")
  })

  it('落地页有入口(转化 ≤2 步: 落地页 → 档位页 → 注册/申请)', () => {
    const landing = readFileSync(resolve(root, 'src/pages/Landing.tsx'), 'utf-8')
    expect(landing).toContain('to="/tiers"')
  })
})

describe('档位页: 不显示价格', () => {
  it('页面里没有任何金额图标/价格字段', () => {
    // 只看**会渲染出来的代码**: 注释里写"不写 ￥0"是说明, 不算违规(否则检查会逼着人不解释)
    const code = stripComments(page)
    for (const bad of ['￥', '¥', 'USD', 'usd', 'price', 'Price', '/月', '/年']) {
      expect(code).not.toContain(bad)
    }
  })

  it('价格区由后端 billing_enabled 决定(数据驱动而不是话术)', () => {
    expect(page).toContain('data.billing_enabled')
    expect(page).toContain('内测期不收费')
  })
})

describe('档位页: 数据来源单一', () => {
  it('清单来自 /api/tiers, 不在前端硬编码能力名', () => {
    expect(page).toContain('fetchTiers')
    // 只允许极少量的"话术"句子, 不允许出现权限点级别的硬编码清单
    for (const perm of ['数智决策', '暗盘资金', 'L2资金', '机会页']) {
      expect(page).not.toContain(perm)
    }
  })

  it('Profile 的 Pro 卡片不再硬编码能力清单, 改为指向档位页', () => {
    expect(profile).toContain('to="/tiers"')
    expect(profile).not.toContain('数智决策三指标、暗盘资金')
  })
})

describe('档位页: 公开面字阶', () => {
  it('只用公开档位的那几档', () => {
    const sizes = [...page.matchAll(/text-\[(\d+)px\]/g)].map((m) => m[1])
    const bad = sizes.filter((s) => !PUBLIC_SIZES.has(s))
    expect(bad).toEqual([])
  })

  it('文案不带 markdown 星号', () => {
    expect(page).not.toMatch(/>[^<]{0,80}\*\*/)
  })
})
