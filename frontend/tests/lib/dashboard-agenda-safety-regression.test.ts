import { describe, expect, it } from 'vitest'
import { agendaSnapshotKey, buildAgendaCandidates, buildAgendaFeed, opportunityBaselineDate, flowSourceParts, splitPremarketPlans } from '@/lib/dashboard-home-audit'

describe('首页事实保真', () => {
  it('相同标的不同提醒都保留且不被机会吞掉', () => {
    const rows = [
      { type: 'alert' as const, symbol: '600000', signal: '跌破10元' },
      { type: 'alert' as const, symbol: '600000', signal: '涨破12元' },
      { type: 'watch' as const, symbol: '600000', signal: '异动' },
    ]
    expect(buildAgendaCandidates(rows, new Set(['CN:600000'])).candidates.map(x => x.signal)).toEqual(rows.map(x => x.signal))
  })
  it('信号或名称变化必须令快照失效', () => {
    const row = {type:'risk' as const, name:'风险', signal:'仓位1%'}
    expect(agendaSnapshotKey([row])).not.toBe(agendaSnapshotKey([{...row, signal:'仓位100%'}]))
  })
  it('AI部分返回不能丢弃其他候选', () => {
    const rows = [{type:'risk' as const, signal:'风险一'}, {type:'risk' as const, signal:'风险二'}]
    const key = agendaSnapshotKey(rows)
    const result = buildAgendaFeed({snapshot:rows, curated:[{index:0,importance:90,why:'风险一'}], responseForSnapshot:key,currentSnapshot:key})
    expect(result.rows.map(x => x.signal)).toContain('风险二')
  })
  it('一项无日期时不能宣称日期完整一致', () => {
    expect(opportunityBaselineDate([{snapshot_date:'2026-11-06'},{}]).kind).not.toBe('ok')
  })
  it('畸形预案不得崩溃，也不得默默丢失原有内容', () => {
    const rows = [{ symbol: 600519, name: '贵州茅台', action: 0 }, { symbol:'000001', action:'' }]
    const result = splitPremarketPlans(rows as never)
    expect(result.valid.length).toBe(2)
  })
  it('成交和涨跌来源缺失不得猜测与主力相同', () => {
    const parts=flowSourceParts({source:'eastmoney_push2delay_cn'})
    expect(parts.amount).toBe('来源未知')
    expect(parts.breadth).toBe('来源未知')
  })
})
