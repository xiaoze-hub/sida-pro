/**
 * 首页 Dashboard 私有纯函数层单测(home-audit-ui, 2026-11-08)。
 *
 * 组件测试只能覆盖"能点出来的路径"; 这里的边界(越界序号 / 快照变化 / 缺字段 / 两个账户 /
 * 说明点名别人)才是串股事故发生的地方, 必须逐条钉住。
 */
import { describe, expect, it } from 'vitest'
import {
  agendaSnapshotKey,
  buildAgendaCandidates,
  buildAgendaFeed,
  dateOrNull,
  flowSourceLabel,
  flowSourceParts,
  formatSealRate,
  mainlineLeaderName,
  opportunityBaselineDate,
  opportunityPlanIssue,
  sealRatePct,
  snapshotClock,
  splitPremarketPlans,
  whyMentionsOther,
  type AgendaCandidate,
} from '../../src/lib/dashboard-home-audit'

const c = (over: Partial<AgendaCandidate>): AgendaCandidate => ({ type: 'alert', ...over })

describe('候选整理: 去重 / 去机会重复 / 缺标识闭合', () => {
  it('机会条目让位给机会精选, 机会本身不被删(只是不重复展示)', () => {
    const { candidates, drops } = buildAgendaCandidates(
      [c({ type: 'opportunity', symbol: '300750', name: '宁德时代' })],
      new Set(['CN:300750']),
    )
    expect(candidates).toHaveLength(0)
    expect(drops[0].reason).toBe('duplicate-of-opportunity')
  })

  it('自选与机会精选同一标的 → 只留机会精选那份', () => {
    const { candidates, drops } = buildAgendaCandidates(
      [c({ type: 'watch', symbol: '300750', name: '宁德时代' })],
      new Set(['CN:300750']),
    )
    expect(candidates).toHaveLength(0)
    expect(drops[0].reason).toBe('duplicate-of-opportunity')
  })

  it('持仓也在机会精选 → 持仓行保留(告警语义不同, 不能一起删)', () => {
    const { candidates } = buildAgendaCandidates(
      [c({ type: 'holding', symbol: '300750', name: '宁德时代', hasPosition: true } as Partial<AgendaCandidate>)],
      new Set(['CN:300750']),
    )
    expect(candidates).toHaveLength(1)
  })

  it('缺标识(无代码)的非风险条目 → 丢弃, 不送 AI 猜', () => {
    const { candidates, drops } = buildAgendaCandidates([c({ type: 'watch', name: '' })])
    expect(candidates).toHaveLength(0)
    expect(drops[0].reason).toBe('missing-identity')
  })

  it('组合风险允许无代码', () => {
    const { candidates } = buildAgendaCandidates([c({ type: 'risk', name: '组合风险', signal: '单仓 62%' })])
    expect(candidates).toHaveLength(1)
  })

  it('两个账户持有同一只票(重复行) → 合并为一行并标歧义', () => {
    const { candidates, drops } = buildAgendaCandidates([
      c({ type: 'holding', symbol: '600519', name: '贵州茅台', market: 'CN', signal: '账户A 浮盈 12%' }),
      c({ type: 'holding', symbol: '600519', name: '贵州茅台', market: 'CN', signal: '账户B 浮盈 3%' }),
    ])
    expect(candidates).toHaveLength(1)
    expect(candidates[0].ambiguous).toBe(true)
    expect(drops[0].reason).toBe('duplicate-symbol')
  })

  it('同一代码不同市场不合并', () => {
    const { candidates } = buildAgendaCandidates([
      c({ type: 'holding', symbol: '600519', market: 'CN' }),
      c({ type: 'holding', symbol: '600519', market: 'HK' }),
    ])
    expect(candidates).toHaveLength(2)
  })

  it('持仓 + 触发的提醒(跨来源)合成一行, 信息不被去重吃掉', () => {
    const { candidates } = buildAgendaCandidates([
      c({ type: 'alert', symbol: '600519', name: '贵州茅台', market: 'CN', signal: '触发提醒 放量突破' }),
      c({ type: 'holding', symbol: '600519', name: '贵州茅台', market: 'CN', signal: '持仓浮盈 12%' }),
    ])
    expect(candidates).toHaveLength(1)
    expect(candidates[0].type).toBe('holding') // 类型取更重要的
    expect(candidates[0].signal).toContain('触发提醒 放量突破')
    expect(candidates[0].signal).toContain('持仓浮盈 12%')
    expect(candidates[0].ambiguous).toBeUndefined() // 跨来源不是"两账户歧义"
  })
})

describe('AI 说明归属: 一律 fail closed', () => {
  const snap = [
    c({ type: 'alert', symbol: '600519', name: '贵州茅台', market: 'CN', signal: '触发提醒 放量突破' }),
    c({ type: 'alert', symbol: '300750', name: '宁德时代', market: 'CN', signal: '触发提醒 放量涨停' }),
  ]
  const key = agendaSnapshotKey(snap)

  it('快照一致 + 序号合法 → 采用 AI 顺序与说明', () => {
    const feed = buildAgendaFeed({
      snapshot: snap,
      curated: [
        { index: 1, importance: 90, why: '宁德时代放量涨停' },
        { index: 0, importance: 50, why: '贵州茅台突破' },
      ],
      responseForSnapshot: key,
      currentSnapshot: key,
    })
    expect(feed.mode).toBe('ai')
    expect(feed.rows.map((r) => r.symbol)).toEqual(['300750', '600519'])
    expect(feed.rows[0].whyFromAI).toBe(true)
  })

  it('候选变了(旧快照响应) → 整份作废, 原始顺序原样保留', () => {
    const changed = agendaSnapshotKey([snap[0]])
    const feed = buildAgendaFeed({
      snapshot: snap,
      curated: [{ index: 0, importance: 90, why: '旧说明' }],
      responseForSnapshot: changed,
      currentSnapshot: key,
    })
    expect(feed.mode).toBe('raw')
    expect(feed.reason).toBe('stale-snapshot')
    expect(feed.rows).toHaveLength(2)
    expect(feed.rows[0].why).toBe('触发提醒 放量突破')
    expect(feed.rows.some((r) => r.why === '旧说明')).toBe(false)
  })

  it('序号越界 / 重复 → 整份作废', () => {
    for (const curated of [
      [{ index: 5, importance: 1, why: 'x' }],
      [{ index: -1, importance: 1, why: 'x' }],
      [{ index: 0.5, importance: 1, why: 'x' }],
      [{ index: 0, importance: 1, why: 'x' }, { index: 0, importance: 2, why: 'y' }],
    ]) {
      const feed = buildAgendaFeed({ snapshot: snap, curated, responseForSnapshot: key, currentSnapshot: key })
      expect(feed.mode).toBe('raw')
      expect(feed.reason).toBe('bad-index')
    }
  })

  it('缺 why(空串/非字符串) → 整份作废', () => {
    const feed = buildAgendaFeed({
      snapshot: snap,
      curated: [{ index: 0, importance: 10, why: '   ' }],
      responseForSnapshot: key,
      currentSnapshot: key,
    })
    expect(feed.reason).toBe('missing-why')
  })

  it('说明点名了另一只候选 → 整份作废(串股)', () => {
    const feed = buildAgendaFeed({
      snapshot: snap,
      curated: [
        { index: 0, importance: 90, why: '宁德时代今日放量涨停' },
        { index: 1, importance: 80, why: '宁德时代放量涨停' },
      ],
      responseForSnapshot: key,
      currentSnapshot: key,
    })
    expect(feed.mode).toBe('raw')
    expect(feed.reason).toBe('why-mismatch')
    expect(feed.rows[0].why).toBe('触发提醒 放量突破')
  })

  it('说明里出现别人的 6 位代码 → 也算串股', () => {
    expect(whyMentionsOther('300750 放量涨停', snap[0], snap)).toBe(true)
    expect(whyMentionsOther('600519 放量突破', snap[0], snap)).toBe(false)
  })

  it('组合风险行的说明可以点名具体股票(无标的 → 不会串股)', () => {
    const withRisk = [
      c({ type: 'risk', name: '组合风险', market: '', signal: '单仓偏高' }),
      c({ type: 'alert', symbol: '600519', name: '贵州茅台', market: 'CN', signal: '触发提醒 放量突破' }),
    ]
    const k = agendaSnapshotKey(withRisk)
    const feed = buildAgendaFeed({
      snapshot: withRisk,
      curated: [
        { index: 0, importance: 90, why: '贵州茅台单仓占比 40%, 集中度过高' },
        { index: 1, importance: 10, why: '贵州茅台放量突破' },
      ],
      responseForSnapshot: k,
      currentSnapshot: k,
    })
    expect(feed.mode).toBe('ai')
  })

  it('说明提到自己 + 顺带提到同板块他人 → 允许(在讲自己)', () => {
    expect(whyMentionsOther('贵州茅台领涨白酒, 宁德时代跟涨', snap[0], snap)).toBe(false)
  })

  it('什么都没点名 → 允许(通用说明)', () => {
    expect(whyMentionsOther('持仓放量异动, 注意回撤', snap[0], snap)).toBe(false)
  })

  it('请求失败/无响应 → 原始候选一条不丢', () => {
    const feed = buildAgendaFeed({ snapshot: snap, curated: null, responseForSnapshot: null, currentSnapshot: key })
    expect(feed.mode).toBe('raw')
    expect(feed.reason).toBe('no-response')
    expect(feed.rows).toHaveLength(2)
  })

  it('多账户歧义行即便 AI 给了说明也只用原始信号', () => {
    const amb = [
      c({ type: 'holding', symbol: '600519', name: '贵州茅台', signal: '账户A 浮盈 12%', ambiguous: true }),
    ]
    const k = agendaSnapshotKey(amb)
    const feed = buildAgendaFeed({
      snapshot: amb,
      curated: [{ index: 0, importance: 90, why: '贵州茅台浮盈 3%, 减仓' }],
      responseForSnapshot: k,
      currentSnapshot: k,
    })
    expect(feed.rows[0].why).toBe('账户A 浮盈 12%')
    expect(feed.rows[0].whyFromAI).toBe(false)
  })
})

describe('封板率单位', () => {
  it('0.62 是比例 → 62%', () => {
    expect(sealRatePct(0.62)).toBe(62)
    expect(formatSealRate(0.62)).toBe('62%')
  })
  it('1 → 100%; 0.005 → 0.5%', () => {
    expect(formatSealRate(1)).toBe('100%')
    expect(formatSealRate(0.005)).toBe('0.5%')
  })
  it('已是百分数(>1)不二次放大', () => {
    expect(sealRatePct(62)).toBe(62)
  })
  it('缺值 → null / --, 不写 0%', () => {
    expect(sealRatePct(null)).toBeNull()
    expect(formatSealRate(undefined)).toBe('--')
    expect(formatSealRate(Number.NaN)).toBe('--')
    expect(formatSealRate(-1)).toBe('--')
  })
})

describe('主线龙头: 真实嵌套结构', () => {
  it('leader.name 嵌套可取', () => {
    expect(mainlineLeaderName({ name: '光模块', leader: { code: '300308', name: '中际旭创' } })).toBe('中际旭创')
  })
  it('leader 为字符串也兼容', () => {
    expect(mainlineLeaderName({ leader: '中际旭创' })).toBe('中际旭创')
  })
  it('旧扁平 leader_name 兜底', () => {
    expect(mainlineLeaderName({ leader_name: '中际旭创' })).toBe('中际旭创')
  })
  it('没有龙头 → null(不编造)', () => {
    expect(mainlineLeaderName({ name: '光模块' })).toBeNull()
    expect(mainlineLeaderName(null)).toBeNull()
  })
})

describe('日期/时点: 缺失就写未知, 不从别的源借', () => {
  it('ISO 日期可取, 非法值 null', () => {
    expect(dateOrNull('2026-11-07')).toBe('2026-11-07')
    expect(dateOrNull('2026-11-07T09:30:00Z')).toBe('2026-11-07')
    expect(dateOrNull('')).toBeNull()
    expect(dateOrNull('昨天')).toBeNull()
    expect(dateOrNull(null)).toBeNull()
  })

  it('快照时点 → HH:mm:ss; 缺失/非法 null', () => {
    expect(snapshotClock('2026-11-07T01:31:20Z')).toMatch(/^\d{2}:\d{2}:\d{2}$/)
    expect(snapshotClock(null)).toBeNull()
    expect(snapshotClock('not-a-date')).toBeNull()
  })

  it('数据源代码可读化; 未知代码原样透出; 空 → 来源未知', () => {
    expect(flowSourceLabel('eastmoney_push2delay_cn')).toBe('东财 push2delay')
    expect(flowSourceLabel('db_snapshot')).toBe('库内快照')
    expect(flowSourceLabel('weird_src')).toBe('weird_src')
    expect(flowSourceLabel(null)).toBe('来源未知')
  })

  it('资金流三指标各自回落不同上游时逐个标注, 不合并', () => {
    const parts = flowSourceParts({
      source: 'eastmoney_push2delay_cn',
      amount_source: 'tx_index_amount',
      breadth_source: 'tdx_tq',
    })
    expect(parts.main).toBe('东财 push2delay')
    expect(parts.amount).toBe('tx_index_amount') // 未知代码原样透出, 不编造中文名
    expect(parts.breadth).toBe('tdx_tq')
  })

  it('没给的独立来源沿用主 source', () => {
    expect(flowSourceParts({ source: 'db_snapshot' })).toEqual({ main: '库内快照', amount: '库内快照', breadth: '库内快照' })
    expect(flowSourceParts(null)).toEqual({ main: '来源未知', amount: '来源未知', breadth: '来源未知' })
  })
})

describe('机会基准日与计划有效性', () => {
  it('全一致 → 给日期', () => {
    expect(opportunityBaselineDate([{ snapshot_date: '2026-11-07' }, { snapshot_date: '2026-11-07' }])).toEqual({
      kind: 'ok',
      date: '2026-11-07',
    })
  })
  it('缺失 → missing(不默认今天)', () => {
    expect(opportunityBaselineDate([{ snapshot_date: '' }, {}])).toEqual({ kind: 'missing' })
  })
  it('多日期 → mixed, 不挑一个冒充', () => {
    const r = opportunityBaselineDate([{ snapshot_date: '2026-11-06' }, { snapshot_date: '2026-11-07' }])
    expect(r.kind).toBe('mixed')
  })
  it('plan_valid=false → 给出原因', () => {
    expect(opportunityPlanIssue({ plan_valid: false, plan_invalid_reason: '止损高于建仓价' })).toBe('止损高于建仓价')
    expect(opportunityPlanIssue({ plan_valid: false })).toContain('计划无效')
    expect(opportunityPlanIssue({ plan_valid: true })).toBeNull()
    expect(opportunityPlanIssue({})).toBeNull()
  })
})

describe('盘前预案分拣', () => {
  it('缺标的或缺动作的预案单独计数', () => {
    const { valid, invalid } = splitPremarketPlans([
      { symbol: '600519', name: '贵州茅台', action: 'watch' },
      { name: '无代码', action: 'hold' },
      { symbol: '000001' },
      {},
    ])
    expect(valid).toHaveLength(2)
    expect(invalid).toBe(2)
  })
  it('undefined 不炸', () => {
    expect(splitPremarketPlans(undefined)).toEqual({ valid: [], invalid: 0 })
  })
})
