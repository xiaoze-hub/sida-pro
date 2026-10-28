// @vitest-environment node
/**
 * 证据齐套度纯函数回归（2026-10-23 起，2026-10-28 P0 真实性修订）。
 *
 * 钉住五件事：
 *  ① stage 标签解析：带 `/N`、不带 `/N`、任意前缀/半全角冒号都要认，散文必须返回 null；
 *  ② 工具名映射：已知走人话标签 + 维度，**未知原样返回工具名**（不猜中文、不硬塞维度）；
 *  ③ 失败折入：`error` 与 `ok:false` 两种形状都算 failed，且 failed 粘性；
 *  ④ 计数诚实：total 只来自标签的分母，没有就是 null（不许补 1 或 items.length）；
 *  ⑤ **状态诚实**：done(ok) 只能来自「完成」证据。开跑帧、下一条开跑、流结束都不算完成证据 ——
 *     未确认写 unknown、失败写 failed，两者都不许被表述成成功。
 */
import { describe, expect, it } from 'vitest'

import {
  emptyReadiness,
  foldFailureNotices,
  foldStageEvent,
  foldStageEvents,
  normalizeStageEvent,
  parseStageLabel,
  readinessFromLabels,
  readinessProgressLabel,
  settleReadiness,
  statusText,
  toolGroup,
  toolLabel,
} from '@/lib/copilot/evidence-readiness'

describe('parseStageLabel', () => {
  it('解析标准格式：正在取数 3/10：get_technical_analysis', () => {
    expect(parseStageLabel('正在取数 3/10：get_technical_analysis')).toEqual({
      index: 3,
      total: 10,
      tool: 'get_technical_analysis',
    })
  })

  it('容忍前后空白、半角冒号、无空格：取数中 12/12:get_capital_flow', () => {
    expect(parseStageLabel('  取数中 12/12:get_capital_flow  ')).toEqual({
      index: 12,
      total: 12,
      tool: 'get_capital_flow',
    })
  })

  it('没有 /N 时 index 与 total 都是 null（不编分母）', () => {
    expect(parseStageLabel('正在取数：get_chip_accumulation')).toEqual({
      index: null,
      total: null,
      tool: 'get_chip_accumulation',
    })
  })

  it('只有工具名也认（裸名）', () => {
    expect(parseStageLabel('get_stock_quote')).toEqual({ index: null, total: null, tool: 'get_stock_quote' })
  })

  it('中文散文不认（否则会把一句提示当成工具）', () => {
    expect(parseStageLabel('正在准备上下文…')).toBeNull()
    expect(parseStageLabel('正在获取技术面分析...')).toBeNull()
    expect(parseStageLabel('')).toBeNull()
    expect(parseStageLabel(null)).toBeNull()
  })

  it('纯数字不当工具名', () => {
    expect(parseStageLabel('正在取数 3/10：12345')).toBeNull()
  })
})

describe('工具名 → 人话 + 维度', () => {
  it('已知工具走映射', () => {
    expect(toolLabel('get_technical_analysis')).toBe('技术面')
    expect(toolGroup('get_technical_analysis')).toBe('走势')
    expect(toolLabel('get_chip_accumulation')).toBe('底仓筹码')
    expect(toolGroup('get_chip_accumulation')).toBe('筹码')
    expect(toolGroup('get_cancel_order_intent')).toBe('筹码')
    expect(toolGroup('get_delta_series')).toBe('主力')
    expect(toolGroup('get_market_news')).toBe('消息')
    expect(toolGroup('get_forecast')).toBe('情景')
  })

  it('未知工具：原样显示工具名，分组落「其他」（绝不猜中文名/维度）', () => {
    expect(toolLabel('get_mystery_thing')).toBe('get_mystery_thing')
    expect(toolGroup('get_mystery_thing')).toBe('其他')
    expect(toolLabel('')).toBe('--')
  })
})

describe('foldStageEvent / 计数', () => {
  it('三条开跑事件后：全部 pending，done=0/total=3（开跑 ≠ 取到）', () => {
    const state = foldStageEvents([
      '正在取数 1/3：get_stock_quote',
      '正在取数 2/3：get_chip_accumulation',
      '正在取数 3/3：get_delta_series',
    ])
    expect(state.items.map((i) => i.label)).toEqual(['行情', '底仓筹码', '逐笔'])
    expect(state.items.map((i) => i.status)).toEqual(['pending', 'pending', 'pending'])
    expect(state.done).toBe(0)
    expect(state.unknown).toBe(0)
    expect(state.total).toBe(3)
    expect(state.failed).toBe(0)
  })

  it('失败折入：error 字段 → failed（并计入 failed 计数）', () => {
    const state = foldStageEvents([
      '正在取数 1/2：get_stock_quote',
      { stage: '正在取数 2/2：get_delta_series', error: 'timeout' },
    ])
    expect(state.items[1].status).toBe('failed')
    expect(state.failed).toBe(1)
    // 下一条开跑不能把前一条「结算」成 ok —— 没有任何完成证据
    expect(state.items[0].status).toBe('pending')
    expect(state.done).toBe(0)
  })

  it('失败折入：ok:false 形状同样算 failed', () => {
    const state = foldStageEvents([{ label: '正在取数 1/1：get_forecast', ok: false }])
    expect(state.items[0].status).toBe('failed')
    expect(state.failed).toBe(1)
  })

  it('失败文案（取数失败/超时）也折算成 failed', () => {
    const state = foldStageEvents(['正在取数 4/10：get_main_intent 失败'])
    expect(state.items[0].status).toBe('failed')
  })

  it('failed 是粘性的：同一工具后续再来一条普通开跑事件也不会被洗白', () => {
    let s = foldStageEvents([{ stage: '正在取数 1/2：get_orderbook', error: 'HTTP 500' }])
    s = foldStageEvent(s, '正在取数 2/2：get_orderbook')
    expect(s.items).toHaveLength(1) // 同一工具只占一条，不重复铺 pill
    expect(s.items[0].status).toBe('failed')
    expect(s.failed).toBe(1)
    expect(s.done).toBe(0)
  })

  it('同一工具重复出现不新增条目（重试不铺两条，也不虚增 done）', () => {
    const s = foldStageEvents(['正在取数 1/2：get_stock_quote', '正在取数 2/2：get_stock_quote'])
    expect(s.items).toHaveLength(1)
    expect(s.items[0].status).toBe('pending')
    expect(s.done).toBe(0)
  })

  it('unknown 工具在 state 里保留原名，不翻译', () => {
    const s = foldStageEvents(['正在取数 1/2：get_mystery_thing'])
    expect(s.items[0]).toMatchObject({ key: 'get_mystery_thing', label: 'get_mystery_thing', group: '其他' })
  })

  it('没有 /N 时 total=null，进度文案只说已完成条数', () => {
    const s = readinessFromLabels([
      '正在取数：get_stock_quote',
      '正在取数：get_chip_accumulation',
      '正在取数：get_delta_series',
    ])
    expect(s.total).toBeNull()
    // 只有开跑标签 → 一条 ok 都没有，三条都归 unknown（未确认），不是「已取 3 项」
    expect(s.done).toBe(0)
    expect(s.unknown).toBe(3)
    expect(readinessProgressLabel(s)).toBe('已取 0 项')
    expect(readinessProgressLabel(s)).not.toContain('/')
  })

  it('有分母时进度文案给分数（done 只数真正确认的）', () => {
    const s = foldStageEvents(['正在取数 2/10：get_stock_quote', '正在取数 3/10：get_forecast'])
    expect(readinessProgressLabel(s)).toBe('已取 0/10')
    expect(readinessProgressLabel({ done: 7, total: 10 })).toBe('已取 7/10')
  })

  it('settleReadiness：流结束后 pending 转「未确认」，绝不转 ok；failed 不动', () => {
    const mid = foldStageEvents(['正在取数 1/3：get_stock_quote', '正在取数 2/3：get_delta_series'])
    expect(mid.items.map((i) => i.status)).toEqual(['pending', 'pending'])
    expect(mid.done).toBe(0)
    const done = settleReadiness(mid)
    expect(done.items.map((i) => i.status)).toEqual(['unknown', 'unknown'])
    expect(done).toMatchObject({ done: 0, failed: 0, unknown: 2, total: 3 })

    const withFail = settleReadiness(
      foldStageEvents([
        '正在取数 1/3：get_stock_quote',
        { stage: '正在取数 2/3：get_forecast', error: 'boom' },
        '正在取数 3/3：get_delta_series',
      ]),
    )
    expect(withFail.items.map((i) => i.status)).toEqual(['unknown', 'failed', 'unknown'])
    expect(withFail).toMatchObject({ done: 0, failed: 1, unknown: 2, total: 3 })
  })

  it('空输入不产生条目（空 ≠ 0/0）', () => {
    expect(foldStageEvents([])).toEqual(emptyReadiness())
    expect(settleReadiness(emptyReadiness())).toEqual(emptyReadiness())
    expect(foldStageEvents(['正在准备上下文…']).items).toEqual([])
  })

  it('statusText：ok=✓ / pending=⏳ / failed=未取到 / unknown=未确认', () => {
    expect(statusText('ok')).toBe('✓')
    expect(statusText('pending')).toBe('⏳')
    expect(statusText('failed')).toBe('未取到')
    expect(statusText('unknown')).toBe('未确认')
  })

  it('normalizeStageEvent 认不出工具就返回 null（不污染齐套度）', () => {
    expect(normalizeStageEvent('正在准备上下文…')).toBeNull()
    expect(normalizeStageEvent({ error: 'x' })).toBeNull()
    expect(normalizeStageEvent(null)).toBeNull()
  })
})

// ── 真实性：事件契约 / 完成证据 / 失败证据 ─────────────────────────────────
/**
 * 契约实测（2026-10-28，基线 a6eb3c98，只读核对后端源码）：
 *  - `src/harness/pipeline.py:425` 只在工具**开跑**时 `yield ("stage", "正在取数 i/N：tool")`；
 *    `src/harness/pipeline_market.py:355` 同形状，标签是 step.label。
 *  - 流式端点把 stage 包成 `{"message": payload}`（`src/web/api/chat.py:2260`），即前端拿到的是
 *    **一条字符串**，没有任何 `ok`/`status` 字段。
 *  - 每个工具的成功/失败只体现在收尾的 `("runs", (results, errors))` 里，errors 是
 *    `"{label}:{err}"` 形式的字符串（`pipeline_market.py:311`），当前**没有**下发给前端。
 *
 * 结论：字符串 stage 帧不携带任何完成证据。把它当「上一条已完成」是推断，不是事实；
 * 把流结束当「全部成功」更糟。以下用例钉住这个结论，防止再退回伪成功。
 */
describe('证据齐套真实性（P0）', () => {
  it('契约：SSE 的 stage 消息形状（{message} 字符串）能认，散文仍不认', () => {
    expect(normalizeStageEvent({ message: '正在取数 2/4：get_capital_flow' })).toMatchObject({
      tool: 'get_capital_flow',
      index: 2,
      total: 4,
      failed: false,
    })
    expect(normalizeStageEvent({ message: '正在准备上下文...' })).toBeNull()
  })

  it('契约：开跑帧无论到没到 N/N，都不产生 ok（done 要有证据，不是时间流逝）', () => {
    const s = foldStageEvents([
      '正在取数 1/2：get_stock_quote',
      '正在取数 2/2：get_stock_quote',
      '正在取数 2/2：get_chip_accumulation',
    ])
    expect(s.items.every((i) => i.status !== 'ok')).toBe(true)
    expect(s.done).toBe(0)
  })

  it('无运行：空事件流 settle 后仍是空，不显示 0/0、不造条目', () => {
    const s = settleReadiness(foldStageEvents([]))
    expect(s.items).toHaveLength(0)
    expect(s).toEqual(emptyReadiness())
    expect(s.total).toBeNull()
    expect(s.done).toBe(0)
    expect(s.failed).toBe(0)
    expect(s.unknown).toBe(0)
  })

  it('部分完成：只有显式完成证据的条目算 ok，其余保持未确认', () => {
    let s = foldStageEvents([
      '正在取数 1/3：get_stock_quote',
      { stage: '正在取数 2/3：get_forecast', ok: true },
      { stage: '正在取数 3/3：get_delta_series', error: 'HTTP 500' },
    ])
    s = settleReadiness(s)
    expect(s.items.map((i) => i.status)).toEqual(['unknown', 'ok', 'failed'])
    expect(s).toMatchObject({ done: 1, failed: 1, unknown: 1, total: 3 })
    // 阶段总数与步骤条数不矛盾：条目数不超过分母，三类之和等于条目数
    expect(s.items.length).toBeLessThanOrEqual(s.total as number)
    expect(s.done + s.failed + s.unknown).toBe(s.items.length)
  })

  it('真实成功：status/ok 显式证据才落 ok，且 settle 不回退', () => {
    const s = settleReadiness(
      foldStageEvents([
        { tool: 'get_forecast', status: 'ok' },
        { tool: 'get_stock_quote', ok: true },
        { tool: 'get_delta_series', status: 'done' },
      ]),
    )
    expect(s.items.map((i) => i.status)).toEqual(['ok', 'ok', 'ok'])
    expect(s).toMatchObject({ done: 3, failed: 0, unknown: 0 })
  })

  it('真实成功：显式 ok 之后再来一条开跑帧（重试开跑），不退化成 pending/unknown', () => {
    let s = foldStageEvents([{ tool: 'get_forecast', ok: true }])
    s = settleReadiness(foldStageEvent(s, '正在取数 2/2：get_forecast'))
    expect(s.items[0].status).toBe('ok')
    expect(s.done).toBe(1)
  })

  it('失败：后端 errors 的 `{label}:{err}` 形状命中唯一维度才标 failed，猜不到就不动', () => {
    const base = foldStageEvents([
      '正在取数 1/3：get_stock_quote',
      '正在取数 2/3：get_technical_analysis',
      '正在取数 3/3：get_forecast',
    ])
    const hit = foldFailureNotices(base, ['技术面:timeout', '行情:empty'])
    expect(hit.items.map((i) => i.status)).toEqual(['failed', 'failed', 'pending'])
    expect(hit).toMatchObject({ done: 0, failed: 2 })

    // 认不出的通知 → 不猜、不造条目、不把任何东西变 ok
    const miss = foldFailureNotices(base, ['维度X:boom', '', '技术面分析失败'])
    expect(miss.items.map((i) => i.status)).toEqual(['pending', 'pending', 'pending'])
    expect(miss.items).toHaveLength(3)
    expect(miss.done).toBe(0)
  })

  it('失败：同一 label 对应多条时不猜（ambiguous → 全部保持原状）', () => {
    const base = foldStageEvents([
      '正在取数 1/2：get_stock_quote',
      '正在取数 2/2：get_mystery_thing',
    ])
    // 两条 label 文本相同 → 通知无法唯一归属，保持原状
    const dup = { ...base, items: base.items.map((it) => ({ ...it, label: '同名' })) }
    const out = foldFailureNotices(dup, ['同名:timeout'])
    expect(out.items.map((i) => i.status)).toEqual(['pending', 'pending'])
    expect(out.failed).toBe(0)
  })

  it('全部失败（管线回落）：settle 后 done 仍为 0，不许出现 ✓', () => {
    const s = settleReadiness(
      foldStageEvents([
        { stage: '正在取数 1/2：get_stock_quote', error: 'timeout' },
        { stage: '正在取数 2/2：get_delta_series', ok: false },
      ]),
    )
    expect(s.items.map((i) => i.status)).toEqual(['failed', 'failed'])
    expect(s).toMatchObject({ done: 0, failed: 2, unknown: 0, total: 2 })
    expect(s.items.map((i) => statusText(i.status))).toEqual(['未取到', '未取到'])
  })

  it('阶段与步骤矛盾：标签 index 超过 total 时不改 total、不补条目', () => {
    const s = foldStageEvents(['正在取数 5/3：get_stock_quote', '正在取数 6/3：get_forecast'])
    expect(s.total).toBe(3)
    expect(s.items).toHaveLength(2)
    expect(s.items.length).toBeLessThanOrEqual(s.total as number)
    expect(s.done).toBe(0)
  })

  it('进度文案永不把未确认/失败算进「已取」', () => {
    const s = settleReadiness(
      foldStageEvents([
        '正在取数 1/4：get_stock_quote',
        { stage: '正在取数 2/4：get_forecast', ok: true },
        { stage: '正在取数 3/4：get_delta_series', error: 'boom' },
      ]),
    )
    expect(s).toMatchObject({ done: 1, failed: 1, unknown: 1, total: 4 })
    expect(readinessProgressLabel(s)).toBe('已取 1/4')
  })
})
