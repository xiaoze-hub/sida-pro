// @vitest-environment jsdom
/**
 * MessageActions 交互回归。
 *
 * 覆盖四件事：
 *  1. 复制：写剪贴板 + 回调 onCopy + 成功提示（1.5s 回落，用假定时器验）；
 *  2. 重新生成：**只有 assistant 且 canRegenerate=true** 才出现；
 *  3. 编辑重发：只有 user 消息出现；
 *  4. 删除：需二次确认（inline），确认才回调 onDelete；
 *  外加：每个按钮都有 aria-label（读屏可达）。
 *
 * 断言风格：只用 vitest 原生 matcher，不引 jest-dom（tsc -b 会扫 src）。
 */
import type { ComponentProps } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { MessageActions } from '@/components/copilot/MessageActions'

type ActionsOverrides = Partial<
  Pick<
    ComponentProps<typeof MessageActions>,
    'role' | 'canRegenerate' | 'canEdit' | 'copyText' | 'copiedLabel'
  >
>

function renderActions(overrides: ActionsOverrides = {}) {
  const props = {
    role: 'assistant' as const,
    onCopy: vi.fn<() => void>(),
    onRegenerate: vi.fn<() => void>(),
    onEdit: vi.fn<() => void>(),
    onDelete: vi.fn<() => void>(),
    ...overrides,
  }
  const utils = render(<MessageActions {...props} />)
  return { props, ...utils }
}

beforeEach(() => {
  // jsdom 没有 clipboard —— 补一个可断言的假实现
  Object.assign(navigator, {
    clipboard: {
      writeText: vi.fn(async () => undefined),
    },
  })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('MessageActions 复制', () => {
  it('点复制调用 onCopy 并写剪贴板', async () => {
    const { props } = renderActions({ copyText: '这段要复制' })
    fireEvent.click(screen.getByTestId('message-action-copy'))
    await vi.waitFor(() => expect(props.onCopy).toHaveBeenCalledTimes(1))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('这段要复制')
  })

  it('复制成功后 1.5s 显示「已复制」，到期回落', async () => {
    vi.useFakeTimers()
    renderActions({ copyText: 'abc' })
    const btn = screen.getByTestId('message-action-copy')
    expect(btn.getAttribute('title')).toBe('复制')
    fireEvent.click(btn)
    // 剪贴板写成功后提示（await act 让 promise 微任务 + React 更新都落定）
    await act(async () => {})
    expect(screen.getByTestId('message-action-copy').getAttribute('title')).toBe('已复制')
    // 1.5s 后回落（act 包裹，否则 React 更新不会刷进 DOM）
    await act(async () => {
      vi.advanceTimersByTime(1500)
    })
    expect(screen.getByTestId('message-action-copy').getAttribute('title')).toBe('复制')
  })

  it('剪贴板不可用时静默（不抛错），仍回调 onCopy', async () => {
    ;(navigator.clipboard.writeText as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error('denied'),
    )
    const { props } = renderActions({ copyText: 'abc' })
    fireEvent.click(screen.getByTestId('message-action-copy'))
    await vi.waitFor(() => expect(props.onCopy).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('message-action-copy').getAttribute('title')).toBe('复制')
  })
})

describe('MessageActions 重新生成', () => {
  it('assistant + canRegenerate 才显示', () => {
    renderActions({ role: 'assistant', canRegenerate: true })
    expect(screen.queryByTestId('message-action-regenerate')).toBeTruthy()
  })

  it('assistant 但 canRegenerate 不为 true（不是最后一条）→ 不显示', () => {
    renderActions({ role: 'assistant', canRegenerate: false })
    expect(screen.queryByTestId('message-action-regenerate')).toBeNull()
  })

  it('user 消息不显示重新生成', () => {
    renderActions({ role: 'user', canRegenerate: true })
    expect(screen.queryByTestId('message-action-regenerate')).toBeNull()
  })

  it('点重新生成回调 onRegenerate', () => {
    const { props } = renderActions({ role: 'assistant', canRegenerate: true })
    fireEvent.click(screen.getByTestId('message-action-regenerate'))
    expect(props.onRegenerate).toHaveBeenCalledTimes(1)
  })
})

describe('MessageActions 编辑重发', () => {
  it('user 消息显示编辑按钮', () => {
    renderActions({ role: 'user' })
    expect(screen.queryByTestId('message-action-edit')).toBeTruthy()
  })

  it('assistant 消息不显示编辑按钮', () => {
    renderActions({ role: 'assistant' })
    expect(screen.queryByTestId('message-action-edit')).toBeNull()
  })

  it('点编辑回调 onEdit', () => {
    const { props } = renderActions({ role: 'user' })
    fireEvent.click(screen.getByTestId('message-action-edit'))
    expect(props.onEdit).toHaveBeenCalledTimes(1)
  })
})

describe('MessageActions 删除二次确认', () => {
  it('第一次点删除只出确认框，不回调 onDelete', () => {
    const { props } = renderActions()
    fireEvent.click(screen.getByTestId('message-action-delete'))
    expect(props.onDelete).not.toHaveBeenCalled()
    expect(screen.queryByTestId('message-delete-confirm-box')).toBeTruthy()
  })

  it('确认后回调 onDelete', () => {
    const { props } = renderActions()
    fireEvent.click(screen.getByTestId('message-action-delete'))
    fireEvent.click(screen.getByTestId('message-action-delete-confirm'))
    expect(props.onDelete).toHaveBeenCalledTimes(1)
  })

  it('取消后不回调 onDelete，且回到删除按钮', () => {
    const { props } = renderActions()
    fireEvent.click(screen.getByTestId('message-action-delete'))
    fireEvent.click(screen.getByTestId('message-action-delete-cancel'))
    expect(props.onDelete).not.toHaveBeenCalled()
    expect(screen.queryByTestId('message-action-delete')).toBeTruthy()
  })
})

describe('MessageActions 无障碍', () => {
  it('每个按钮都有 aria-label', () => {
    renderActions({ role: 'assistant', canRegenerate: true })
    const labels = ['复制消息', '重新生成', '删除消息']
    for (const label of labels) {
      const el = screen.getByLabelText(label)
      expect(el).toBeTruthy()
    }
  })

  it('user 消息的编辑按钮也有 aria-label', () => {
    renderActions({ role: 'user' })
    expect(screen.getByLabelText('编辑并重发')).toBeTruthy()
  })
})
