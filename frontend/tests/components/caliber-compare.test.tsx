// @vitest-environment jsdom
//
// 口径对照页(2026-09-18) —— 三口径并排, 消歧不合并。
//
// 钉四件事:
//  ① 三个源各自成列(明盘 L2 / 暗盘逐笔 / 东财四档), 名称与口径说明来自后端(不硬编码);
//  ② **无数据的源显示「无数据」+ 原因**, 绝不画 0 冒充(净额 0 与"没有数据"是两回事);
//  ③ 净流入红 / 净流出绿(沿用 A 股惯例), 单位自动折 亿/万;
//  ④ 非法代码不发请求, 直接给输入错误提示。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CaliberComparePage from '@/pages/CaliberCompare'

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  caliberCompareApi: { get: getMock },
}))

const RESPONSE = {
  symbol: '002361',
  market: 'CN',
  as_of: '2026-09-18T16:30:00+08:00',
  available_count: 2,
  sources: [
    {
      key: 'thsdk_l2',
      name: '明盘 L2 主力净流入（TQ / 同花顺口径）',
      caliber: '按单笔成交金额分档汇总的特大单+大单净额。',
      unit: '元',
      available: false,
      fields: [],
      note: 'TQ 未返回数据（未配正式账户 / 非交易时段）',
    },
    {
      key: 'tencent_dark',
      name: '暗盘资金（腾讯逐笔 v6）',
      caliber: '腾讯逐笔主动成交，含拆单识别。',
      unit: '元',
      available: true,
      fields: [
        { label: '全量主动净额', value: 36400000 },
        { label: '主力净额（≥20万）', value: -12500000 },
        { label: '主力参与度', value: 41.2, unit: '%' },
      ],
      note: '',
    },
    {
      key: 'eastmoney_flow',
      name: '东财四档资金流',
      caliber: '公开 Level-1 衍生，按单金额四档归类。',
      unit: '元',
      available: true,
      fields: [{ label: '主力净流入', value: 8000000 }],
      note: '基准日 2026-09-18（盘中常见 T-1）',
      date: '2026-09-18',
    },
  ],
  differences: [
    { topic: '「主力」定义不同', detail: 'TQ 按单笔金额分档、暗盘含拆单识别、东财按四档归类。' },
    { topic: '正确用法', detail: '不要取平均；先看方向是否一致。' },
  ],
}

beforeEach(() => {
  getMock.mockReset()
  getMock.mockResolvedValue(RESPONSE)
})

afterEach(() => cleanup())

describe('口径对照页', () => {
  it('渲染三列 + 口径说明 + 差异说明', async () => {
    render(<CaliberComparePage />)

    // 默认查 002361
    await waitFor(() => expect(getMock).toHaveBeenCalledWith('002361'))
    expect(await screen.findByText('明盘 L2 主力净流入（TQ / 同花顺口径）')).toBeTruthy()
    expect(screen.getByText('暗盘资金（腾讯逐笔 v6）')).toBeTruthy()
    expect(screen.getByText('东财四档资金流')).toBeTruthy()
    expect(screen.getByText('「主力」定义不同')).toBeTruthy()
    expect(screen.getByText('2/3 个源有数据')).toBeTruthy()
  })

  it('无数据的源显示「无数据」+ 原因, 不画 0', async () => {
    render(<CaliberComparePage />)
    await screen.findByText('明盘 L2 主力净流入（TQ / 同花顺口径）')

    const noData = screen.getAllByText(/无数据/).map((n) => n.textContent).join(' ')
    expect(noData).toContain('TQ 未返回数据')
    // 不可用的源**整列不渲染数值行** —— 没有 `--` 占位行(那会让人以为"有字段但值为空"),
    // 也没有任何 0(净额 0 与"没有数据"是两回事)。
    expect(screen.queryAllByText('--')).toHaveLength(0)
    expect(screen.queryByText('撤买额')).toBeNull() // 该源独有的字段, 无数据时不该出现
  })

  it('净额按 亿/万 折算且涨红跌绿', async () => {
    render(<CaliberComparePage />)
    // 3640万 净流入 / 1250万 净流出
    const inflow = await screen.findByText('+3640.00万')   // safeMoney 正数带 + 号
    const outflow = screen.getByText('-1250.00万')
    expect(inflow.className).toContain('stock-up')
    expect(outflow.className).toContain('stock-down')
  })

  it('非法代码不发请求', async () => {
    render(<CaliberComparePage />)
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByLabelText('股票代码'), { target: { value: 'abc' } })
    fireEvent.click(screen.getByText('查询'))

    expect(await screen.findByText('请输入 6 位 A 股代码')).toBeTruthy()
    expect(getMock).toHaveBeenCalledTimes(1) // 没有第二次请求
  })
})
