// @vitest-environment node
/**
 * 证据齐套度纯函数回归（2026-10-23）。
 *
 * 钉住四件事：
 *  ① stage 标签解析：带 `/N`、不带 `/N`、任意前缀/半全角冒号都要认，散文必须返回 null；
 *  ② 工具名映射：已知走人话标签 + 维度，**未知原样返回工具名**（不猜中文、不硬塞维度）；
 *  ③ 失败折入：`error` 与 `ok:false` 两种形状都算 failed，且 failed 粘性；
 *  ④ 计数诚实：total 只来自标签的分母，没有就是 null（不许补 1 或 items.length）。
 */
import { describe, expect, it } from 'vitest'

import {
  emptyReadiness,
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
  it('三条事件后：前两条 ok、最后一条 pending，done=2/total=3', () => {
    const state = foldStageEvents([
      '正在取数 1/3：get_stock_quote',
      '正在取数 2/3：get_chip_accumulation',
      '正在取数 3/3：get_delta_series',
    ])
    expect(state.items.map((i) => i.label)).toEqual(['行情', '底仓筹码', '逐笔'])
    expect(state.items.map((i) => i.status)).toEqual(['ok', 'ok', 'pending'])
    expect(state.done).toBe(2)
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
    // 前一条被下一条开跑「结算」为 ok，不会跟着变红
    expect(state.items[0].status).toBe('ok')
    expect(state.done).toBe(1)
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

  it('failed 是粘性的：同一工具后续再来一条普通事件也不会被洗白', () => {
    let s = foldStageEvents([{ stage: '正在取数 1/2：get_orderbook', error: 'HTTP 500' }])
    s = foldStageEvent(s, '正在取数 2/2：get_orderbook')
    expect(s.items).toHaveLength(1) // 同一工具只占一条，不重复铺 pill
    expect(s.items[0].status).toBe('failed')
  })

  it('同一工具重复出现不新增条目（重试不铺两条）', () => {
    const s = foldStageEvents(['正在取数 1/2：get_stock_quote', '正在取数 2/2：get_stock_quote'])
    expect(s.items).toHaveLength(1)
    expect(s.items[0].status).toBe('pending')
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
    expect(s.done).toBe(3)
    expect(readinessProgressLabel(s)).toBe('已取 3 项')
    expect(readinessProgressLabel(s)).not.toContain('/')
  })

  it('有分母时进度文案给分数', () => {
    const s = foldStageEvents(['正在取数 2/10：get_stock_quote', '正在取数 3/10：get_forecast'])
    expect(readinessProgressLabel(s)).toBe('已取 1/10')
  })

  it('settleReadiness：流结束后 pending 全部转 ok，failed 不动', () => {
    const mid = foldStageEvents(['正在取数 1/3：get_stock_quote', '正在取数 2/3：get_delta_series'])
    expect(mid.items.map((i) => i.status)).toEqual(['ok', 'pending'])
    expect(mid.done).toBe(1)
    const done = settleReadiness(mid)
    expect(done.items.map((i) => i.status)).toEqual(['ok', 'ok'])
    expect(done).toMatchObject({ done: 2, failed: 0, total: 3 })

    const withFail = settleReadiness(
      foldStageEvents([
        '正在取数 1/3：get_stock_quote',
        { stage: '正在取数 2/3：get_forecast', error: 'boom' },
        '正在取数 3/3：get_delta_series',
      ]),
    )
    expect(withFail.items.map((i) => i.status)).toEqual(['ok', 'failed', 'ok'])
    expect(withFail).toMatchObject({ done: 2, failed: 1, total: 3 })
  })

  it('空输入不产生条目（空 ≠ 0/0）', () => {
    expect(foldStageEvents([])).toEqual(emptyReadiness())
    expect(settleReadiness(emptyReadiness())).toEqual(emptyReadiness())
    expect(foldStageEvents(['正在准备上下文…']).items).toEqual([])
  })

  it('statusText：ok=✓ / pending=⏳ / failed=未取到', () => {
    expect(statusText('ok')).toBe('✓')
    expect(statusText('pending')).toBe('⏳')
    expect(statusText('failed')).toBe('未取到')
  })

  it('normalizeStageEvent 认不出工具就返回 null（不污染齐套度）', () => {
    expect(normalizeStageEvent('正在准备上下文…')).toBeNull()
    expect(normalizeStageEvent({ error: 'x' })).toBeNull()
    expect(normalizeStageEvent(null)).toBeNull()
  })
})
