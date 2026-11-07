// @vitest-environment jsdom
/**
 * 待确认「改设置」那一行必须让人看见**这次动作**会改什么（2026-11-07 复审阻断项）。
 *
 * 旧界面只显示一个裸 key（`notify_quiet_hours`），用户点「确认」时不知道旧值是什么、
 * 新值是什么、更不知道这一步不可撤销。这里渲染真实组件，断言：
 *   - 旧值 / 新值 / 三态 / 风险都在；
 *   - 旧值取自后端 diff，payload 里模型编的 before 不上屏；
 *   - 没有 diff 时给红色告警，而不是编一个旧值。
 *
 * 断言风格：只用 vitest 原生 matcher，不引 jest-dom（本仓库 src 下用例统一如此）。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { SettingPendingPreview } from '../SettingPendingPreview'

afterEach(() => cleanup())

const text = (id: string) => screen.getByTestId(id).textContent ?? ''

describe('SettingPendingPreview', () => {
  it('显示旧值、新值、三态与不可撤销风险', () => {
    render(
      <SettingPendingPreview
        action={{
          payload: { key: 'notify_retry_attempts', before: '999', before_present: true },
          diff: [
            {
              field: 'notify_retry_attempts',
              before: '2',
              after: '3',
              change: 'update',
              before_state: 'value',
            },
          ],
        }}
      />,
    )

    expect(text('setting-pending-key')).toBe('通知失败重试次数')
    expect(text('setting-pending-before')).toBe('2')
    expect(text('setting-pending-before')).not.toContain('999')
    expect(text('setting-pending-after')).toBe('3')
    expect(text('setting-pending-state')).toContain('已配置明确值')
    expect(text('setting-pending-risk')).toContain('不可撤销')
    // 绝不出现与运行时相反的「空 = 关闭」
    expect(text('setting-pending-risk')).not.toContain('空值关闭')
  })

  it('absent 三态说清"生效的是环境变量默认值"', () => {
    render(
      <SettingPendingPreview
        action={{
          payload: { key: 'notify_quiet_hours' },
          diff: [
            { field: 'notify_quiet_hours', before: '--', after: '09:00-10:00', change: 'add', before_state: 'absent' },
          ],
        }}
      />,
    )
    expect(text('setting-pending-state')).toContain('环境变量默认值')
    expect(text('setting-pending-state')).not.toContain('关闭')
  })

  it('后端没给 diff：红色告警 + 不编旧值', () => {
    render(
      <SettingPendingPreview
        action={{ payload: { key: 'notify_quiet_hours', before: '23:00-07:00' }, diff: [] }}
      />,
    )
    expect(text('setting-pending-before')).toBe('--')
    expect(text('setting-pending-risk')).toContain('不要确认')
    expect(screen.getByTestId('setting-pending-risk').className).toContain('text-role-risk')
  })
})
