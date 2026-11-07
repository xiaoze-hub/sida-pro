/**
 * 待确认「通知设置」的确认前口径（2026-11-07 复审阻断项）。
 *
 * 钉住三件事：
 *  1. 旧值只认后端持久化 diff —— 模型在 payload 里编的 before 一律不显示；
 *  2. 三态如实区分（absent / null / value），且**不出现「空=关闭」**这种与运行时
 *     （`app_settings or env默认`）相反的文案；
 *  3. 后端没给 diff 时明说"不知道"，不猜、不编。
 */
import { describe, expect, it } from 'vitest'
import {
  describeSettingPreview,
  SETTING_MISSING_RISK,
  SETTING_RISK,
  SETTING_UNKNOWN_STATE_RISK,
} from '../settingPreview'

describe('describeSettingPreview', () => {
  it('旧值/新值来自后端 diff，而不是 payload 里模型自带的 before', () => {
    const preview = describeSettingPreview({
      payload: {
        key: 'notify_retry_attempts',
        value: '3',
        // 模型虚构：真实库里是 "2"
        before: '999',
        before_present: true,
      },
      diff: [
        { field: 'notify_retry_attempts', before: '2', after: '3', change: 'update', before_state: 'value' },
      ],
    })
    expect(preview.beforeLabel).toBe('2')
    expect(preview.afterLabel).toBe('3')
    expect(preview.state).toBe('value')
    expect(preview.keyLabel).toBe('通知失败重试次数')
    expect(preview.missing).toBe(false)
    expect(preview.risk).toBe(SETTING_RISK)
    expect(preview.confirmable).toBe(true)
  })

  it.each([
    ['absent', '未配置：当前生效的是环境变量默认值'],
    ['null', '已存在但没有有效值：运行时会回退到环境变量默认值'],
  ] as const)('三态 %s 明说生效的是默认值，不谎称关闭', (state, expected) => {
    const preview = describeSettingPreview({
      payload: { key: 'notify_quiet_hours' },
      diff: [{ field: 'notify_quiet_hours', before: '--', after: '09:00-10:00', change: 'add', before_state: state }],
    })
    expect(preview.state).toBe(state)
    expect(preview.stateLabel).toBe(expected)
    expect(preview.stateLabel).not.toContain('关闭')
    expect(preview.afterLabel).toBe('09:00-10:00')
  })

  it('没有后端 diff 时明说不知道，并提示不要确认', () => {
    const preview = describeSettingPreview({
      payload: { key: 'notify_quiet_hours', before: '23:00-07:00', before_present: true },
      diff: [],
    })
    expect(preview.missing).toBe(true)
    // fail closed：没有可信 diff 就不许确认
    expect(preview.confirmable).toBe(false)
    expect(preview.state).toBe('unknown')
    expect(preview.beforeLabel).toBe('--')
    expect(preview.risk).toBe(SETTING_MISSING_RISK)
    // 依然不采用 payload 里的 before
    expect(preview.beforeLabel).not.toContain('23:00')
  })

  it('diff 里缺少 before_state 时按未知处理，不自己推断', () => {
    const preview = describeSettingPreview({
      payload: { key: 'notify_quiet_hours' },
      diff: [{ field: 'notify_quiet_hours', before: '23:00-07:00', after: '22:00-06:00', change: 'update' }],
    })
    expect(preview.state).toBe('unknown')
    expect(preview.stateLabel).toContain('未知')
    // 老 pending 行（before_state 之前落库）同样 fail closed，不许盲签
    expect(preview.confirmable).toBe(false)
    expect(preview.risk).toBe(SETTING_UNKNOWN_STATE_RISK)
  })

  it('与当前值相同时标出 noop', () => {
    const preview = describeSettingPreview({
      payload: { key: 'notify_retry_attempts' },
      diff: [{ field: 'notify_retry_attempts', before: '3', after: '3', change: 'noop', before_state: 'value' }],
    })
    expect(preview.noop).toBe(true)
  })

  it('风险文案说的是「下次运行生效」，不谎称立即生效', () => {
    const preview = describeSettingPreview({
      payload: { key: 'notify_retry_attempts' },
      diff: [{ field: 'notify_retry_attempts', before: '2', after: '3', change: 'update', before_state: 'value' }],
    })
    expect(preview.risk).toContain('下一次运行起生效')
    expect(preview.risk).not.toContain('立即生效')
  })
})
