// @vitest-environment jsdom
/**
 * 因子权重面板 · 「已生效」口径 + 并发范围文案(2026-11-12 复审)。
 *
 * 红线: **建议 ≠ 已生效**。
 *  ① 「已生效: N」必须来自后端返回的 `changed` / `write_back`, 不允许前端写死 0
 *     —— 写死的话, 一旦后端改了写入语义, 界面会继续撒谎且没人发现;
 *  ② 并发保护范围必须如实显示: 只有 `cross_worker_exclusive` 为真才能说「跨 worker」,
 *     Redis 降级时显示「仅本进程」;
 *  ③ 审计页要能认出权重类 action(update_factor_weight / rebalance_strategy_weights),
 *     否则全局权重改动在审计里只是一串英文。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  list: vi.fn(),
  calibrate: vi.fn(),
  update: vi.fn(),
  getToken: vi.fn(() => 'tok-1'),
  getMyPermissions: vi.fn(),
}))

vi.mock('@sida/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@sida/api')>()
  return {
    ...actual,
    factorsApi: {
      ...actual.factorsApi,
      list: (...args: unknown[]) => api.list(...args),
      calibrate: (...args: unknown[]) => api.calibrate(...args),
      update: (...args: unknown[]) => api.update(...args),
    },
    getToken: () => api.getToken(),
    getMyPermissions: (...args: unknown[]) => api.getMyPermissions(...args),
  }
})

import FactorWeightsPanel from '../../src/components/FactorWeightsPanel'
import { ACTION_LABELS } from '../../src/pages/Audit'

const weightRow = {
  factor_code: 'alpha_score',
  market: 'CN',
  weight: 1,
  is_pinned: false,
  auto_calibrate: true,
  last_ic: null,
  last_ir: null,
  last_sample_size: null,
  last_calibrated_at: null,
  last_observation_at: null,
  last_observation_stale: true,
  observation_note: '历史观测(meta 记录), 非当前实时值',
  reason: '',
  updated_at: null,
}

const proposal = {
  factor_code: 'alpha_score',
  market: 'CN',
  current_weight: 1,
  base_weight: 1,
  proposed_weight: 1.1,
  target: 1.1,
  status: 'proposed',
  ic: 0.03,
  ir: 0.4,
  ic_holdout: 0.02,
  sample_size: 40,
}

const calibration = (over: Record<string, unknown> = {}) => ({
  market: 'CN',
  mode: 'readonly',
  ok: true,
  computed_at: '2026-11-12T10:00:00+08:00',
  cached: false,
  cache_age_seconds: 0,
  cache_ttl_seconds: 60,
  checked: 5,
  proposed: 1,
  changed: 0,
  write_back: false,
  rows_present: 0,
  no_rows: true,
  skipped_oos: 0,
  skipped_low_sample: 0,
  skipped_no_data: 0,
  skipped_ic_unavailable: 0,
  skipped_pinned: 0,
  skipped_disabled: 0,
  proposals: [proposal],
  concurrency: {
    scope: 'cluster',
    cross_worker_exclusive: true,
    cache_scope: 'process',
    note: '跨 worker 互斥生效(Redis 租约): 同一时刻最多一轮实际计算',
  },
  ...over,
})

async function renderAndCalibrate(payload: Record<string, unknown>) {
  api.list.mockResolvedValue({ items: [weightRow] })
  api.calibrate.mockResolvedValue(payload)
  render(<FactorWeightsPanel />)
  await waitFor(() => expect(api.list).toHaveBeenCalled())
  fireEvent.click(screen.getByRole('button', { name: '计算建议' }))
}

describe('因子权重面板 · 已生效', () => {
  beforeEach(() => {
    api.list.mockReset()
    api.calibrate.mockReset()
    api.update.mockReset()
    api.getMyPermissions.mockResolvedValue({
      effective: ['manage_settings'],
      granted: [],
      role: 'owner',
    })
  })

  afterEach(() => cleanup())

  it('只读结果按后端 changed/write_back 显示为「不落库」', async () => {
    await renderAndCalibrate(calibration())
    expect(await screen.findByText('已生效: 0(只读建议, 不落库)')).toBeTruthy()
  })

  it('后端真的回写时不谎报 0 —— 显示真实条数并提醒已生效', async () => {
    await renderAndCalibrate(calibration({ changed: 2, write_back: true }))
    expect(await screen.findByText('已生效: 2(后端已回写生效权重)')).toBeTruthy()
    expect(screen.queryByText(/只读建议, 不落库/)).toBeNull()
  })

  it('后端即使自报「跨 worker」, 面板也只说「仅本进程」—— 不替后端许下它没有的保证', async () => {
    // 2026-11-12 复审三: 后端已撤掉 Redis 租约, `concurrency.scope` 恒为 process。
    // 这里故意喂一份「集群互斥」的旧结构, 面板仍然不得显示「跨 worker」(那会
    // 让人以为多 worker 下真的串行), 只能如实说仅本进程 + 缓存按 worker。
    await renderAndCalibrate(calibration())
    expect(await screen.findByText(/并发保护: 仅本进程/)).toBeTruthy()
    expect(screen.queryByText(/并发保护: 跨 worker/)).toBeNull()
    expect(screen.getByText(/缓存按 worker/)).toBeTruthy()
  })

  it('Redis 降级时显示「仅本进程」, 不宣称跨 worker', async () => {
    await renderAndCalibrate(
      calibration({
        concurrency: {
          scope: 'process',
          cross_worker_exclusive: false,
          cache_scope: 'process',
          note: 'Redis 不可用: 只有本进程内的单飞, 多 worker 下可能各有 1 轮',
        },
      }),
    )
    expect(await screen.findByText(/并发保护: 仅本进程/)).toBeTruthy()
    expect(screen.queryByText(/并发保护: 跨 worker/)).toBeNull()
  })
})

describe('因子权重面板 · 失败要显式(2026-11-12 复审三)', () => {
  beforeEach(() => {
    api.list.mockReset()
    api.calibrate.mockReset()
    api.update.mockReset()
    api.getMyPermissions.mockResolvedValue({
      effective: ['manage_settings'],
      granted: [],
      role: 'owner',
    })
  })

  afterEach(() => cleanup())

  it('标定计算失败(后端 503)→ 显示失败与关联号, 不残留「已生效」建议', async () => {
    api.list.mockResolvedValue({ items: [weightRow] })
    api.calibrate.mockRejectedValue(
      new Error('标定计算失败; 本次未做任何改动。错误参考号: ERR-ABC12345(详情见服务端日志)'),
    )
    render(<FactorWeightsPanel />)
    await waitFor(() => expect(api.list).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: '计算建议' }))

    expect(await screen.findByText(/标定失败: .*ERR-ABC12345/)).toBeTruthy()
    // 失败不能顺带展示一份「已生效」/建议表格, 那会让人以为算成功了
    expect(screen.queryByText(/已生效/)).toBeNull()
    expect(screen.queryByText(/只读标定建议 ·/)).toBeNull()
  })

  it('列表读取失败(后端 503)→ 显示加载失败, 不显示「暂无因子权重数据」', async () => {
    api.list.mockRejectedValue(
      new Error('读取因子权重失败; 本次未做任何改动。错误参考号: ERR-DEF67890(详情见服务端日志)'),
    )
    render(<FactorWeightsPanel />)

    expect(await screen.findByText(/读取因子权重失败/)).toBeTruthy()
    expect(screen.queryByText('暂无因子权重数据')).toBeNull()
  })
})

describe('审计页 action 标签', () => {
  it('权重类动作有中文标签(不再显示裸英文 action)', () => {
    expect(ACTION_LABELS.update_factor_weight?.label).toBe('因子权重改动')
    expect(ACTION_LABELS.rebalance_strategy_weights?.label).toBe('策略权重调权')
  })
})
