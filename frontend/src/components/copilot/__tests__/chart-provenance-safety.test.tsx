// @vitest-environment jsdom
/**
 * 图表价位「来源」与「确认」的硬约束（2026-10-28 P2 四收口重写）。
 *
 * 演进（这条线每次返工都落在"别再把客户端声明当凭证"上）：
 *  ① 只校验调用方构造的 `{source,actionId,status}` → 原地可伪造；
 *  ② 全关画线 → 不伪造，但确认后也上不了图；
 *  ③ 只认服务端行 + 会话信封 + `WeakSet` 标记 → 复审再次驳回：信封与标记都是客户端自证；
 *  ④ 本轮：**服务端把身份与目标回显出来**（user_id/action_id/symbol/market/period），
 *     前端逐字核对；apply 响应用 `annotation_id` 精确关联；本地价位没有任何入口。
 *
 * 本文件钉住 ④ 的判定矩阵（纯函数级，快而全；真实图表探针见
 * `tests/components/copilot-annotation-channel.test.tsx`）：
 *  1. 解析 fail-closed：非数组=失败；缺目标/属主回显的行按行丢弃并计数；
 *  2. 逐行核对：全局行、他人账号、无确认动作、标的/市场/周期不符、非法价位 → 都画不了；
 *  3. 同向同价重复行合并（图上不允许两条重合虚线）；
 *  4. apply 响应缺 `annotation_id` / 状态不对 / 明细 id 不一致 → 拒绝（不做 label+price 反查）；
 *  5. 用 id 对上之后，label/price/ts/动作不一致 → 视为契约破坏，拒画；
 *  6. AnswerCard 仍没有画线入口（正文价位只作文本/锚点）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import {
  annotationsUrl,
  linesForTarget,
  parseAnnotationsResponse,
  parseApplyResponse,
  targetRefusal,
  verifyAppliedRow,
  type AnnotationTarget,
} from '@/lib/copilot/serverAnnotations'

const OWNER = 'test-user'
const TARGET: AnnotationTarget = {
  owner: OWNER,
  instrument: 'stock',
  symbol: '600111',
  market: 'CN',
  period: '1d',
}

/** 服务端行的真实形状（`get_copilot_annotations` 2026-10-28 起的新响应）。 */
function row(overrides: Record<string, unknown> = {}) {
  return {
    id: 91,
    symbol: '600111',
    market: 'CN',
    period: '1d',
    user_id: OWNER,
    action_id: 7,
    price: 32.8,
    ts: null,
    label: '支撑位',
    note: '情景矩阵支撑位',
    color: 'up',
    source: 'ai',
    created_at: '2026-10-28 10:00:00',
    ...overrides,
  }
}

function parseRows(raw: unknown[]) {
  const parsed = parseAnnotationsResponse(raw)
  if (!parsed.ok) throw new Error(parsed.message)
  return parsed.value
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('1. 解析 fail-closed', () => {
  it('顶层不是数组 → 失败（空 ≠ 错，不把对象当空列表）', () => {
    const parsed = parseAnnotationsResponse({ rows: [] })
    expect(parsed.ok).toBe(false)
    if (!parsed.ok) expect(parsed.reason).toBe('shape')
  })

  it('缺目标/属主回显的行按行丢弃并计数（客户端无法核对，画不了）', () => {
    const { rows, dropped } = parseRows([
      row(),
      // 旧服务端形状：没有 symbol/market/period/user_id/action_id
      { id: 92, price: 36.5, ts: null, label: '阻力位', color: 'down', source: 'ai', created_at: '' },
      row({ id: 93, price: -1 }), // 非法价位
      row({ id: 91 }), // 重复 id
    ])
    expect(rows.map((r) => r.id)).toEqual([91])
    expect(dropped).toBe(3)
  })

  it('user_id=null（全局行）与 action_id=null（无确认动作）都能解析，但核对时才拒', () => {
    const { rows } = parseRows([row({ user_id: null }), row({ id: 92, action_id: null })])
    expect(rows).toHaveLength(2)
    expect(rows[0].ownerId).toBeNull()
    expect(rows[1].actionId).toBeNull()
  })
})

describe('2. 逐行核对（身份 → 目标 → 内容）', () => {
  const verdictFor = (overrides: Record<string, unknown>) => linesForTarget(parseRows([row(overrides)]).rows, TARGET)

  it('全对 → 可画，且每个字段都能对回服务端行', () => {
    const { lines } = verdictFor({})
    expect(lines).toHaveLength(1)
    expect(lines[0]).toMatchObject({
      annotationId: 91,
      actionId: 7,
      price: 32.8,
      kind: 'support',
      symbol: '600111',
      market: 'CN',
      period: '1d',
      ownerId: OWNER,
    })
  })

  it('全局行 / 他人账号 / 无确认动作 / 非 AI 标注 → 分别拒绝', () => {
    expect(verdictFor({ user_id: null }).rejected[0]).toContain('全局行')
    expect(verdictFor({ user_id: 'someone-else' }).rejected[0]).toContain('其他账号')
    expect(verdictFor({ action_id: null }).rejected[0]).toContain('没有对应的确认动作')
    expect(verdictFor({ source: 'user' }).rejected[0]).toContain('不是 AI 标注')
  })

  it('标的 / 市场 / 周期任一不符 → 拒绝（严格等值，不做 A 股族归并）', () => {
    expect(verdictFor({ symbol: '002636' }).rejected[0]).toContain('不一致')
    expect(verdictFor({ market: 'SH' }).rejected[0]).toContain('不一致')
    expect(verdictFor({ period: '5m' }).rejected[0]).toContain('不一致')
  })

  it('时间点标注（没有价位）不上图；方向不可判也不画', () => {
    expect(verdictFor({ price: null, ts: '2026-10-28 10:00' }).rejected[0]).toContain('没有价位')
    const ambiguous = linesForTarget(
      parseRows([row({ label: '关注', color: 'other', note: null })]).rows,
      TARGET,
    )
    expect(ambiguous.lines).toHaveLength(0)
    expect(ambiguous.rejected[0]).toContain('无法判断支撑/阻力')
  })
})

describe('3. 同向同价重复行合并', () => {
  it('两条同价同向（连点/历史重复）只画一条，保留较新确认并计数', () => {
    const { rows } = parseRows([row({ id: 91 }), row({ id: 95 })])
    const { lines, merged } = linesForTarget(rows, TARGET)
    expect(lines).toHaveLength(1)
    expect(lines[0].annotationId).toBe(95) // id 大 = 较新
    expect(merged).toBe(1)
  })

  it('同价不同向（支撑=阻力）不合并 —— 两个语义都要在图上', () => {
    const { rows } = parseRows([
      row({ id: 91, price: 35.0, label: '支撑位', color: 'up' }),
      row({ id: 92, price: 35.0, label: '阻力位', color: 'down' }),
    ])
    expect(linesForTarget(rows, TARGET).lines).toHaveLength(2)
  })
})

describe('4. apply 响应：只认 annotation_id', () => {
  const applyBody = (overrides: Record<string, unknown> = {}) => ({
    id: 7,
    kind: 'annotation',
    status: 'applied',
    payload: { symbol: '600111', label: '支撑位', price: 32.8 },
    annotation_id: 91,
    annotation: {
      id: 91,
      symbol: '600111',
      market: 'CN',
      period: '1d',
      price: 32.8,
      ts: null,
      label: '支撑位',
      color: 'up',
      source: 'ai',
    },
    ...overrides,
  })

  it('正常响应 → 动作 id / 标注 id / 落库明细都能取到', () => {
    const parsed = parseApplyResponse(applyBody())
    expect(parsed.ok).toBe(true)
    if (parsed.ok) {
      expect(parsed.value).toMatchObject({ actionId: 7, annotationId: 91, symbol: '600111', period: '1d' })
    }
  })

  it('没有 annotation_id → 明确失败（绝不用 label+price 反查兜底）', () => {
    const body = applyBody()
    delete (body as Record<string, unknown>).annotation_id
    const parsed = parseApplyResponse(body)
    expect(parsed.ok).toBe(false)
    if (!parsed.ok) {
      expect(parsed.reason).toBe('no_annotation_id')
      expect(parsed.message).toContain('annotation_id')
    }
  })

  it('未 applied / 非标注动作 / 明细 id 不一致 → 拒绝', () => {
    expect(parseApplyResponse(applyBody({ status: 'pending' })).ok).toBe(false)
    expect(parseApplyResponse(applyBody({ kind: 'watchlist' })).ok).toBe(false)
    expect(parseApplyResponse(applyBody({ annotation: { id: 92, symbol: '600111' } })).ok).toBe(false)
  })
})

describe('5. 用 id 对上之后的交叉核对', () => {
  const applied = {
    actionId: 7,
    annotationId: 91,
    symbol: '600111',
    market: 'CN',
    period: '1d',
    label: '支撑位',
    price: 32.8,
    ts: null,
  }

  it('读回里没有这个 id → 拒（哪怕有同 label 同价的别的行）', () => {
    const rows = parseRows([row({ id: 92 })]).rows
    const verdict = verifyAppliedRow(applied, rows, TARGET)
    expect(verdict.ok).toBe(false)
    if (!verdict.ok) expect(verdict.message).toContain('#91')
  })

  it('关联的动作不是刚才那个动作 → 拒', () => {
    const rows = parseRows([row({ action_id: 8 })]).rows
    const verdict = verifyAppliedRow(applied, rows, TARGET)
    expect(verdict.ok).toBe(false)
    if (!verdict.ok) expect(verdict.message).toContain('动作 #8')
  })

  it('读回价位/标签/时间点与确认响应矛盾 → 拒（契约破坏，不猜哪个对）', () => {
    expect(verifyAppliedRow(applied, parseRows([row({ price: 33.3 })]).rows, TARGET).ok).toBe(false)
    expect(verifyAppliedRow(applied, parseRows([row({ label: '阻力位', color: 'down' })]).rows, TARGET).ok).toBe(false)
    expect(
      verifyAppliedRow(applied, parseRows([row({ ts: '2026-10-28' })]).rows, TARGET).ok,
    ).toBe(false)
  })

  it('全部一致 → 通过（同一链路必须真的能画）', () => {
    const verdict = verifyAppliedRow(applied, parseRows([row()]).rows, TARGET)
    expect(verdict.ok).toBe(true)
  })
})

describe('6. 目标级拒画与读取 URL', () => {
  it('未登录 / 指数 / 000001 同号代码 → 目标级拒绝，不发请求', () => {
    expect(targetRefusal({ ...TARGET, owner: '' }) ?? '').toContain('请先登录')
    expect(targetRefusal({ ...TARGET, instrument: 'index' }) ?? '').toContain('指数')
    expect(targetRefusal({ ...TARGET, symbol: '000001' }) ?? '').toContain('标的类型')
    expect(targetRefusal({ ...TARGET, symbol: '399001' }) ?? '').toContain('标的类型')
    // 普通个股不拦
    expect(targetRefusal(TARGET)).toBeNull()
  })

  it('读取 URL 默认严格模式；ids 只在精确读取时出现', () => {
    const url = annotationsUrl(TARGET)
    expect(url).toContain('market_strict=1')
    expect(url).toContain('mine_only=1')
    expect(url).toContain('action_linked=1')
    expect(url).not.toContain('ids=')
    expect(annotationsUrl(TARGET, { ids: [91] })).toContain('ids=91')
  })
})

describe('7. AnswerCard 不提供本地画线入口', () => {
  it('正文价位只作文本/锚点，并明确说明未画到 K 线', () => {
    localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: OWNER }))}.signature`)
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    const parsed = parseAnswer(
      '【结论】观察\n【依据】\n- 支撑位 32.80 元\n- 阻力位 36.50 元\n【预案】观望\n【口径】行情',
    )
    render(<AnswerCard parsed={parsed} symbol="600111" />)
    expect(screen.queryByText('在 K 线叠加画线')).toBeNull()
    expect(screen.getByTestId('copilot-level-provenance-note').textContent).toContain('未画到 K 线')
    expect(alert).not.toHaveBeenCalled()
  })
})
