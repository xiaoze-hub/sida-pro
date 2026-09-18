/**
 * P2-1 钉子(前端): 口径对照页要把"差异归因"呈现出来, 且守住诚实口径。
 *
 * 钉什么: ① 三源并排之后要有一块"差异归因"(对比/差值/相对差/比值/判定);
 *        ② 结论徽标按级别给词(预期带内 / 略出预期带 / 远离预期带 / 数据不足);
 *        ③ 缺数一律 `--`(不补 0), 且用 safeFixed/safeMoney 而非裸 toFixed;
 *        ④ 文案不带会被原样渲染的 markdown 星号。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../..')
const page = readFileSync(resolve(root, 'src/pages/CaliberCompare.tsx'), 'utf-8')
const api = readFileSync(resolve(root, 'packages/api/src/caliberCompare.ts'), 'utf-8')

describe('口径对照页: 差异归因', () => {
  it('渲染成对差异与整页结论', () => {
    expect(page).toMatch(/data\.pair_diffs/)
    expect(page).toContain('data-testid="diff-conclusion"')
    expect(page).toContain('差异归因')
    for (const col of ['对比', '差值', '相对差', '比值']) {
      expect(page).toContain(col)
    }
  })

  it('结论按级别给词(不是只有"正常/异常"两档)', () => {
    for (const w of ['差在预期带内', '略出预期带', '远离预期带 / 方向冲突', '数据不足']) {
      expect(page).toContain(w)
    }
  })

  it('缺数走 `--`, 且不做裸 toFixed', () => {
    expect(page).toMatch(/d\.rel_diff == null \? '--'/)
    expect(page).toMatch(/d\.ratio == null \? '--'/)
    expect(page).not.toMatch(/\.toFixed\(/)
    expect(page).toMatch(/safeFixed\(/)
  })

  it('渲染出来的文案不带 markdown 星号(注释里的文档星号不算)', () => {
    // 只查 JSX 文本: 先剥掉注释行(JSDoc/行注释里的 ** 是文档强调, 不会渲染)
    const codeOnly = page
      .split('\n')
      .filter((l) => !/^\s*(\*|\/\*|\/\/)/.test(l))
      .join('\n')
    expect(codeOnly).not.toMatch(/>[^<]{0,80}\*\*/)
  })
})

describe('API 类型: 成对差异', () => {
  it('类型里带 pair_diffs 与 diff_conclusion(且 level 是四档)', () => {
    expect(api).toContain('pair_diffs?')
    expect(api).toContain('diff_conclusion?')
    expect(api).toMatch(/level: 'ok' \| 'warn' \| 'alert' \| 'unknown'/)
    expect(api).toContain('sign_conflict')
    expect(api).toContain('prefer')
  })
})
