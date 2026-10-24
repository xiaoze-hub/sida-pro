// @vitest-environment jsdom
/**
 * Lane B 回归钉子（2026-10-24）
 *
 * 两个 P0 行为在这里锁死：
 * 1. 只带页签参数的地址（`/notifications?tab=alerts`）**不得**被当成「新建提醒规则」的预填意图
 *    —— 此前进入提醒页就自动弹窗，并预选第一只自选股。
 * 2. 路由切换时统一关闭遗留弹层 —— 此前切到 /admin 后弹窗仍在，遮罩盖住整页。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseAlertPrefill } from '../../src/lib/price-alert-prefill'
import { closeOpenDialogs } from '../../src/lib/dialog-route-guard'

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('parseAlertPrefill：只有个股意图才预填', () => {
  it('纯页签参数不触发（本次 bug 的直接回归）', () => {
    expect(parseAlertPrefill('?tab=alerts')).toBeNull()
    expect(parseAlertPrefill('?tab=alerts&page=2')).toBeNull()
  })

  it('空 search / undefined / null 不触发', () => {
    expect(parseAlertPrefill('')).toBeNull()
    expect(parseAlertPrefill(undefined)).toBeNull()
    expect(parseAlertPrefill(null)).toBeNull()
  })

  it('只有 name 没有代码时也不触发（避免无标的弹窗）', () => {
    expect(parseAlertPrefill('?tab=alerts&name=中粮糖业')).toBeNull()
  })

  it('带 symbol 时解析出预填意图，代码大写去空格、市场默认 CN', () => {
    expect(parseAlertPrefill('?tab=alerts&symbol=%20600519%20&name=贵州茅台')).toEqual({
      stockId: 0,
      symbol: '600519',
      market: 'CN',
      name: '贵州茅台',
    })
  })

  it('带 stock_id 时解析出 stockId，并保留显式 market', () => {
    expect(parseAlertPrefill('?stock_id=42&market=hk')).toEqual({
      stockId: 42,
      symbol: '',
      market: 'HK',
      name: '',
    })
  })

  it('非法 stock_id 不会当作意图', () => {
    expect(parseAlertPrefill('?stock_id=abc&tab=alerts')).toBeNull()
    expect(parseAlertPrefill('?stock_id=-3')).toBeNull()
  })
})

describe('closeOpenDialogs：路由切换关闭遗留弹层', () => {
  it('存在 role=dialog 时派发 Escape，并返回数量', () => {
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.appendChild(dialog)
    const onEscape = vi.fn()
    document.addEventListener('keydown', onEscape)

    expect(closeOpenDialogs(document)).toBe(1)
    expect(onEscape).toHaveBeenCalledTimes(1)
    const event = onEscape.mock.calls[0][0] as KeyboardEvent
    expect(event.key).toBe('Escape')
  })

  it('alertdialog 同样计入', () => {
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'alertdialog')
    document.body.appendChild(dialog)
    expect(closeOpenDialogs(document)).toBe(1)
  })

  it('没有弹层时不派发事件（避免干扰普通页面）', () => {
    const onEscape = vi.fn()
    document.addEventListener('keydown', onEscape)
    expect(closeOpenDialogs(document)).toBe(0)
    expect(onEscape).not.toHaveBeenCalled()
  })
})
