// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { DecisionCompassCard } from '@/components/copilot/DecisionCompassCard'
import { MultiDimensionEvidencePanel } from '@/components/copilot/MultiAgentDebatePanel'
import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import { ChartOverlayService } from '@/lib/copilot/chartOverlayService'

beforeEach(() => localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: 'test-user' }))}.signature`))

afterEach(() => {
  localStorage.removeItem('token')
  cleanup()
  sessionStorage.clear()
})

describe('DecisionCompassCard 决策罗盘', () => {
  it('纯真实数据驱动：支持支撑位与阻力位渲染，标注多维依据综合裁决，无持仓时不显示假成本与盈亏', () => {
    const onDraw = vi.fn()
    render(
      <DecisionCompassCard
        ticker="600111"
        supportPrice={32.8}
        resistancePrice={36.5}
        biasLabel="看多偏强"
        onDrawOnChart={onDraw}
      />,
    )

    expect(screen.getByTestId('copilot-decision-compass')).toBeTruthy()
    expect(screen.getByText('看多偏强')).toBeTruthy()
    expect(screen.getByText('多维依据综合裁决')).toBeTruthy()
    expect(screen.queryByText(/多智能体/)).toBeNull()
    expect(screen.getByText(/支撑 32.80/)).toBeTruthy()
    expect(screen.getByText(/阻力 36.50/)).toBeTruthy()
    // 杜绝虚构成本价与持仓盈亏
    expect(screen.queryByText(/持仓盈亏/)).toBeNull()
    expect(screen.queryByText(/36.52/)).toBeNull()

    const drawBtn = screen.getByText('在 K 线叠加画线')
    fireEvent.click(drawBtn)
    expect(onDraw).toHaveBeenCalledTimes(1)
  })

  it('显式传入持仓成本与现价时，安全计算盈亏与保本点', () => {
    render(
      <DecisionCompassCard
        ticker="600111"
        currentPrice={34.0}
        costPrice={35.0}
        supportPrice={32.0}
        resistancePrice={38.0}
      />,
    )

    expect(screen.getByText(/持仓盈亏: -2.86%/)).toBeTruthy()
    expect(screen.getByText(/成本 35.00/)).toBeTruthy()
    expect(screen.getByText(/现价 34.00/)).toBeTruthy()
  })
})

describe('MultiDimensionEvidencePanel 多维依据透视（如实反映事实，杜绝虚构智能体）', () => {
  it('无真实视角数据时安全返回 null，绝不展示虚构默认值', () => {
    const { container: c1 } = render(<MultiDimensionEvidencePanel />)
    expect(c1.firstChild).toBeNull()

    const { container: c2 } = render(<MultiDimensionEvidencePanel perspectives={[]} />)
    expect(c2.firstChild).toBeNull()
  })

  it('传入真实多维依据时结构化拆解呈现，诚实标注为多维依据透视而非伪造集群', () => {
    render(
      <MultiDimensionEvidencePanel
        perspectives={[
          {
            role: 'technical',
            agentName: '技术面分析',
            status: '中性震荡',
            conclusion: '缩量回踩未破位',
          },
          {
            role: 'funds',
            agentName: '资金与筹码',
            status: '主力净流',
            conclusion: '早盘大单承接良好',
          },
        ]}
      />,
    )

    expect(screen.getByTestId('copilot-multi-dimension-evidence')).toBeTruthy()
    expect(screen.getByText('多维依据透视 (2 维度)')).toBeTruthy()
    expect(screen.getByText('维度研判分解')).toBeTruthy()
    expect(screen.queryByText(/TradingAgents 集群/)).toBeNull()
    expect(screen.queryByText(/多智能体会诊/)).toBeNull()
    expect(screen.getByText('技术面分析')).toBeTruthy()
    expect(screen.getByText('资金与筹码')).toBeTruthy()
  })
})

describe('ChartOverlayService 真实图表画线验证与跨页面持久化', () => {
  it('真实调用 series.createPriceLine 在图表系列上绘制支撑与阻力两条线', () => {
    const createdLines: any[] = []
    const mockSeries = {
      createPriceLine: vi.fn((config) => {
        createdLines.push(config)
        return config
      }),
      removePriceLine: vi.fn(),
    }

    ChartOverlayService.injectDecisionLevels(mockSeries, [
      { price: 32.8, title: '支撑 32.8', kind: 'support' },
      { price: 36.5, title: '阻力 36.5', kind: 'pressure' },
    ])

    // 严密断言：K 线上确实调用了两次 createPriceLine，支撑与阻力两条线真实存在
    expect(mockSeries.createPriceLine).toHaveBeenCalledTimes(2)
    expect(createdLines[0]).toMatchObject({
      price: 32.8,
      title: '支撑 32.8',
    })
    expect(createdLines[1]).toMatchObject({
      price: 36.5,
      title: '阻力 36.5',
    })

    // 测试清除画线
    ChartOverlayService.clearOverlays(mockSeries)
    expect(mockSeries.removePriceLine).toHaveBeenCalledTimes(2)
  })

  it('跨页面画线持久化：saveOverlay 写入会话存储且可通过 getStoredOverlay 跨生命周期还原', () => {
    ChartOverlayService.saveOverlay('600111', [
      { price: 32.8, title: '支撑 32.8', kind: 'support' },
      { price: 36.5, title: '阻力 36.5', kind: 'pressure' },
    ])

    const stored = ChartOverlayService.getStoredOverlay('600111')
    expect(stored).toEqual([
      { price: 32.8, kind: 'support', label: '支撑 32.8' },
      { price: 36.5, kind: 'pressure', label: '阻力 36.5' },
    ])

    ChartOverlayService.clearOverlay('600111')
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
  })
})

describe('AnswerCard 罗盘与多维依据联动', () => {
  it('当回答包含真实支撑与阻力位时自动接入罗盘，并调用持久化画线与锚点', () => {
    const RAW = `【结论】突破箱体震荡向上，置信度 8/10
【依据】
- 关键支撑位 32.80 元有大买单防守
- 上方阻力位 36.50 元附近面临套牢盘压力
- 技术面走势形成底背离金叉
【预案】回踩 32.80 不破可加仓，突破 36.50 减仓
【口径】通达信实时 + L2
以上分析不构成投资建议。`

    const parsed = parseAnswer(RAW)
    const onAnchoredPrice = vi.fn()
    const overlaySpy = vi.fn()

    window.addEventListener('sida-kline-overlay', overlaySpy)

    render(
      <AnswerCard
        parsed={parsed}
        symbol="600111"
        onAnchoredPrice={onAnchoredPrice}
      />,
    )

    // 验证罗盘组件已正常接入
    expect(screen.getByTestId('copilot-decision-compass')).toBeTruthy()
    expect(screen.getByText(/支撑 32.80/)).toBeTruthy()
    expect(screen.getByText(/阻力 36.50/)).toBeTruthy()

    // 验证多维依据透视面板正常接入且文案诚实
    expect(screen.getByTestId('copilot-multi-dimension-evidence')).toBeTruthy()
    expect(screen.queryByText(/TradingAgents/)).toBeNull()

    // 点击在 K 线叠加画线
    const drawBtn = screen.getByText('在 K 线叠加画线')
    fireEvent.click(drawBtn)

    expect(onAnchoredPrice).toHaveBeenCalledWith(
      expect.objectContaining({
        value: 32.8,
        kind: 'level',
      }),
    )
    expect(overlaySpy).toHaveBeenCalledTimes(1)

    // 验证会话中已成功保存持久化画线，保证后续工作台还原
    const stored = ChartOverlayService.getStoredOverlay('600111')
    expect(stored).toHaveLength(2)
    expect(stored![0].price).toBe(32.8)
    expect(stored![1].price).toBe(36.5)

    window.removeEventListener('sida-kline-overlay', overlaySpy)
  })

  it('回答不含支撑阻力时不渲染假罗盘', () => {
    const RAW = `【结论】平淡无奇，观望为宜
【依据】
- 今日成交量平淡
【预案】继续观望
【口径】普通行情
以上分析不构成投资建议。`

    const parsed = parseAnswer(RAW)
    render(<AnswerCard parsed={parsed} symbol="600111" />)
    expect(screen.queryByTestId('copilot-decision-compass')).toBeNull()
  })
})
