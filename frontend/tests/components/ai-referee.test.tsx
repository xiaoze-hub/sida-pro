// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'
import { AiRefereePanel, RefereeStatsCard } from '@/components/AiRefereePanel'

/**
 * P0-2「AI 裁判零消费」回归: 裁判结论(verdict/direction/reason)上屏 + 裁判战绩卡片。
 *
 * 守四件事(幻觉敏感红线: 变更必须可见, 缺数据必须显式):
 *  ① 三态徽标: confirm(裁判确认)/adjust(裁判调整)/abstain(裁判弃权); 未知 verdict 原样透出;
 *  ② adjust 覆盖最终方向 ⇒ **显式**标注「最终方向已被裁判调整」+ 理由全文(不截断);
 *  ③ 字段缺失(旧响应无 ai_referee)⇒ 显式「无裁判结论」, 不臆造;
 *  ④ 裁判战绩: 样本不足(total=0 / accuracy=null)显式, **不把 0 当真实命中率**。
 *
 * 另含端到端: 渲真 `Forecast` 页, 预测完成后裁判结论 + 战绩卡片都出现(P0 根因是
 * 前端全库 grep 'ai_referee' 零命中 —— 本条证明消费链路已接通)。全部 mock 网络, 禁真网络。
 */

if (typeof globalThis.ResizeObserver === 'undefined') {
  // Radix(base-ui) 组件在 jsdom 下需要; 只做空实现, 不替代任何断言。
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  getToken: () => 'test-token',
  clearToken: () => {},
  fetchAPI: (...a: unknown[]) => api.fetchAPI(...a),
  stocksApi: { list: async () => [] },
}))

// 预测锥图自带 klines 取数 + ECharts(与本 P0-2 无关); 换轻量替身隔离, 断言只盯裁判消费链路。
vi.mock('@/components/ForecastConeChart', () => ({
  default: () => <div data-testid="forecast-cone-stub" />,
}))

import ForecastPage from '@/pages/Forecast'

const PREDICT = {
  symbol: '600519',
  stock_name: '贵州茅台',
  last_close: 100,
  last_date: '2026-10-09',
  pred_days: 3,
  prediction: [101, 102, 103],
  direction: 'down',
  expected_pct: 3,
  models: {
    kronos: { median: [101, 102, 103], p5: [99, 100, 101], p95: [103, 104, 105], n_samples: 30 },
    chronos: null,
    xgboost: [101, 102, 103],
    linreg: [101, 102, 103],
  },
  recommendation: {
    action: '减仓',
    tone: 'down',
    confidence: '中',
    target_price: 103,
    expected_pct: 3,
    stop_loss: 97,
    summary: '模型分歧转下',
  },
  ai_referee: {
    verdict: 'adjust',
    direction: 'down',
    reason: '主力资金连续三日净流出, 高位放量滞涨, 模型上调目标价缺乏盘面支撑',
  },
  elapsed_ms: 1234,
}

const REFEREE_STATS = {
  total: 12,
  symbol: '600519',
  confirm_count: 10,
  adjust_count: 2,
  baseline_accuracy: 50,
  referee_accuracy: 66.7,
  delta_accuracy_pct: 16.7,
  confirm_accuracy: 60,
  adjust_accuracy: null,
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('AiRefereePanel 三态徽标', () => {
  it('confirm/adjust/abstain 各渲染对应徽标; 未知 verdict 原样透出不归类', () => {
    const { rerender } = render(
      <AiRefereePanel referee={{ verdict: 'confirm', direction: 'up', reason: '盘面确认' }} />,
    )
    expect(screen.getByTestId('ai-referee-badge').textContent).toContain('裁判确认')
    expect(screen.getByTestId('ai-referee-panel').getAttribute('data-verdict')).toBe('confirm')
    // confirm 绝不出现 adjust 覆盖标注
    expect(screen.queryByTestId('ai-referee-adjust-override')).toBeNull()

    rerender(<AiRefereePanel referee={{ verdict: 'adjust', direction: 'down', reason: '主力派发' }} />)
    expect(screen.getByTestId('ai-referee-badge').textContent).toContain('裁判调整')

    rerender(<AiRefereePanel referee={{ verdict: 'abstain', direction: null, reason: '裁判不可用' }} />)
    expect(screen.getByTestId('ai-referee-badge').textContent).toContain('裁判弃权')
    expect(screen.getByTestId('ai-referee-panel').getAttribute('data-verdict')).toBe('abstain')
    expect(screen.getByTestId('ai-referee-abstain-note').textContent).toContain('维持上方模型方向')

    rerender(<AiRefereePanel referee={{ verdict: 'mystery', direction: null, reason: '?' }} />)
    expect(screen.getByTestId('ai-referee-badge').textContent).toContain('未知结论')
    expect(screen.getByTestId('ai-referee-unknown-note')).toBeTruthy()
  })
})

describe('AiRefereePanel adjust 覆盖标注(幻觉敏感红线: 变更必须可见)', () => {
  it('adjust 且给出方向 ⇒ 显式「最终方向已被裁判调整」 + 理由全文', () => {
    const reason = '主力资金连续三日净流出, 且高位放量滞涨, 模型上调目标价缺乏盘面支撑, 故方向下修'
    render(<AiRefereePanel referee={{ verdict: 'adjust', direction: 'down', reason }} />)

    const note = screen.getByTestId('ai-referee-adjust-override')
    expect(note.textContent).toContain('最终方向已被裁判调整')
    expect(note.textContent).toContain('↓ 看空')
    // 理由全文(不截断)
    expect(screen.getByTestId('ai-referee-panel').textContent).toContain(reason)
  })

  it('adjust 但未给出有效方向 ⇒ 不作为方向覆盖, 不出现覆盖标注', () => {
    render(<AiRefereePanel referee={{ verdict: 'adjust', direction: null, reason: '只质疑未定向' }} />)
    expect(screen.getByTestId('ai-referee-badge').textContent).toContain('裁判调整')
    expect(screen.queryByTestId('ai-referee-adjust-override')).toBeNull()
  })
})

describe('AiRefereePanel 字段缺失兼容态(旧响应)', () => {
  it('无 ai_referee ⇒ 显式「无裁判结论」, 不臆造 verdict/reason', () => {
    render(<AiRefereePanel referee={undefined} />)
    const panel = screen.getByTestId('ai-referee-panel')
    expect(panel.getAttribute('data-verdict')).toBe('none')
    expect(panel.textContent).toContain('无裁判结论')
    expect(screen.queryByTestId('ai-referee-badge')).toBeNull()
  })

  it('referee 对象存在但 reason 为空 ⇒ 显式「裁判未给出理由」', () => {
    render(<AiRefereePanel referee={{ verdict: 'confirm', direction: 'up', reason: '' }} />)
    expect(screen.getByTestId('ai-referee-panel').textContent).toContain('裁判未给出理由')
  })
})

describe('RefereeStatsCard 裁判战绩', () => {
  it('无记录/样本不足 ⇒ 显式标注, 不把 0 当命中率', () => {
    render(
      <RefereeStatsCard stats={{ total: 0, symbol: 'all', message: '暂无裁判记录（裁判层尚未介入过预测）' }} />,
    )
    const card = screen.getByTestId('referee-stats-card')
    expect(card.getAttribute('data-empty')).toBe('true')
    expect(card.textContent).toContain('暂无裁判记录')
    expect(card.textContent).toContain('样本不足')
    // 不展示任何伪造命中率数字
    expect(card.textContent).not.toContain('%')
  })

  it('stats 为 null ⇒ 显式空态而非崩/空白', () => {
    render(<RefereeStatsCard stats={null} />)
    const card = screen.getByTestId('referee-stats-card')
    expect(card.getAttribute('data-empty')).toBe('true')
    expect(card.textContent).toContain('样本不足')
  })

  it('有样本 ⇒ 展示命中率/基线/增减; adjust 样本为 0 显式「样本不足」', () => {
    render(<RefereeStatsCard stats={REFEREE_STATS} />)
    const card = screen.getByTestId('referee-stats-card')
    expect(card.getAttribute('data-empty')).toBe('false')
    expect(card.textContent).toContain('66.7%') // 裁判命中率
    expect(card.textContent).toContain('50%') // 模型基线
    expect(card.textContent).toContain('+16.7pp') // 增减
    expect(card.textContent).toContain('样本不足') // adjust_accuracy=null
  })
})

describe('Forecast 页消费 ai_referee + 裁判战绩(P0-2 端到端)', () => {
  beforeEach(() => {
    api.fetchAPI.mockReset()
    api.fetchAPI.mockImplementation((url: string) => {
      if (url.startsWith('/forecast/predict?')) return Promise.resolve(PREDICT)
      if (url.startsWith('/forecast/predict/status')) return Promise.resolve({ status: 'done', result: PREDICT, logs: [] })
      if (url.startsWith('/forecast/referee-stats')) return Promise.resolve(REFEREE_STATS)
      if (url.startsWith('/forecast/weights'))
        return Promise.resolve({ weights: { kronos: 0.4, chronos: 0.3, xgboost: 0.15, linreg: 0.15 }, source: 'history' })
      if (url.startsWith('/forecast/health')) return Promise.resolve({ status: 'ok' })
      if (url.startsWith('/forecast/history')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
  })

  it('预测完成后渲染裁判结论 + adjust 覆盖标注 + 消费 referee-stats', async () => {
    render(
      <ToastProvider>
        <ForecastPage />
      </ToastProvider>,
    )
    fireEvent.change(screen.getByPlaceholderText(/输入名称或代码/), { target: { value: '600519' } })
    fireEvent.click(screen.getByRole('button', { name: /开始预测/ }))

    const panel = await screen.findByTestId('ai-referee-panel')
    expect(panel.getAttribute('data-verdict')).toBe('adjust')
    expect(screen.getByTestId('ai-referee-adjust-override').textContent).toContain('最终方向已被裁判调整')
    // 主方向限定词改为「裁判调整后」——不把裁判改过的方向当纯模型输出
    expect(screen.getByText('裁判调整后')).toBeTruthy()

    // 裁判战绩卡片消费 /forecast/referee-stats
    await waitFor(() => {
      expect(screen.getByTestId('referee-stats-card').getAttribute('data-empty')).toBe('false')
    })
    expect(api.fetchAPI.mock.calls.map((c) => String(c[0])).some((u) => u.includes('/forecast/referee-stats?symbol=600519'))).toBe(true)
  })
})
