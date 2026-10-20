// @vitest-environment jsdom
/**
 * 数智参谋页附件上传（2026-10-19）：
 *  - 点回形针/粘贴/拖拽三种入口都能把文件交给 /api/chat/upload；
 *  - 上传后出现预览 chip，可移除；
 *  - 发送时正文带 `[附件: 名称]`，并把 `image_data` 作为第 5 参传给流式接口（图片走多模态）。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  sendMessageStream: vi.fn().mockResolvedValue(undefined),
  createConversation: vi.fn().mockResolvedValue({ id: 7 }),
}))

vi.mock('@panwatch/api', () => ({
  chatApi: apiMock,
}))

vi.mock('../../src/lib/copilot/api', () => ({
  getCopilotMetrics: vi.fn(async () => ({ days: 7, runs: 0, ok_rate: null, avg_tools: null, p95_elapsed_ms: null, by_recipe: [] })),

  getPendingActions: vi.fn(async () => []),
  applyPendingAction: vi.fn(async () => ({})),
  rejectPendingAction: vi.fn(async () => ({})),
  reportViewState: vi.fn(async () => ({ saved: true })),

  getCopilotPresets: vi.fn().mockResolvedValue([]),
  getCopilotBrief: vi.fn().mockResolvedValue(null),
}))

vi.mock('../../src/lib/copilot/symbolChips', () => ({
  extractSymbolChips: () => [],
}))

vi.mock('../../src/components/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}))

vi.mock('../../src/components/copilot/CopilotBriefCard', () => ({
  CopilotBriefCard: () => <div data-testid="brief-card" />,
}))

import AiCopilotPage from '../../src/pages/AiCopilot'

const IMG_DATA = 'data:image/png;base64,iVBORw0KGgo='

beforeEach(() => {
  // jsdom 未实现 Element.scrollTo（页面挂载后会滚动到底部）
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  apiMock.uploadAttachment.mockReset()
  apiMock.sendMessageStream.mockClear()
  apiMock.createConversation.mockClear()
})

afterEach(cleanup)

function pickFile(name = 'kline.png', type = 'image/png') {
  const input = screen.getByTestId('copilot-attach-input') as HTMLInputElement
  const file = new File(['hello'], name, { type })
  fireEvent.change(input, { target: { files: [file] } })
  return file
}

describe('数智参谋附件上传', () => {
  it('上传图片后显示预览 chip，发送时正文带附件名且透传 image_data', async () => {
    apiMock.uploadAttachment.mockResolvedValue({ filename: 'kline.png', text: '', image_data: IMG_DATA })
    render(<AiCopilotPage />)

    pickFile()

    // 预览 chip 出现（图片走缩略图）
    await waitFor(() => expect(screen.getByAltText('kline.png')).toBeTruthy())
    expect(apiMock.uploadAttachment).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('copilot-attach-remove')).toBeTruthy()

    // 附件在场时即使不打字也能发送
    fireEvent.click(screen.getByLabelText('发送'))

    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    const [convId, content, , , imageData] = apiMock.sendMessageStream.mock.calls[0]
    expect(convId).toBe(7)
    expect(content).toContain('[附件: kline.png]')
    expect(imageData).toBe(IMG_DATA)
  })

  it('移除附件后不再带 image_data', async () => {
    apiMock.uploadAttachment.mockResolvedValue({ filename: 'doc.pdf', text: '报表正文', image_data: undefined })
    render(<AiCopilotPage />)

    pickFile('doc.pdf', 'application/pdf')
    await waitFor(() => expect(screen.getByText('doc.pdf')).toBeTruthy())
    expect(screen.getByText(/4 字已解析/)).toBeTruthy()

    fireEvent.click(screen.getByTestId('copilot-attach-remove'))
    await waitFor(() => expect(screen.queryByTestId('copilot-attach-remove')).toBeNull())

    fireEvent.change(screen.getByPlaceholderText(/问筹码|补充说明/), { target: { value: '这份报表怎么样' } })
    fireEvent.click(screen.getByLabelText('发送'))

    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    const [, content, , , imageData] = apiMock.sendMessageStream.mock.calls[0]
    expect(content).toBe('这份报表怎么样')
    expect(imageData).toBeUndefined()
  })

  it('图片无 OCR 文本时不报错、不塞「未解析」，正文提示直接看图', async () => {
    // 生产实测：容器未装 tesseract → 图片 OCR 失败，但 image_data 正常
    apiMock.uploadAttachment.mockResolvedValue({
      filename: 'shot.png',
      text: '',
      image_data: IMG_DATA,
      error: '图片 OCR 失败: tesseract is not installed',
    })
    render(<AiCopilotPage />)

    pickFile('shot.png', 'image/png')

    await waitFor(() => expect(screen.getByText('图片已附加，将先由视觉模型转写再分析')).toBeTruthy())
    // OCR 失败不得冒泡成错误条
    expect(screen.queryByText(/附件上传失败/)).toBeNull()

    fireEvent.click(screen.getByLabelText('发送'))
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    const [, content, , , imageData] = apiMock.sendMessageStream.mock.calls[0]
    expect(content).toContain('（图片，已由视觉模型转写）')
    expect(content).not.toContain('未解析出内容')
    expect(imageData).toBe(IMG_DATA)
  })

  it('超过 20MB 直接拒绝、不上传', async () => {
    render(<AiCopilotPage />)
    const input = screen.getByTestId('copilot-attach-input') as HTMLInputElement
    const big = new File(['x'], 'big.png', { type: 'image/png' })
    Object.defineProperty(big, 'size', { value: 21 * 1024 * 1024 })
    fireEvent.change(input, { target: { files: [big] } })

    await waitFor(() => expect(screen.getByText('附件超过 20MB 上限')).toBeTruthy())
    expect(apiMock.uploadAttachment).not.toHaveBeenCalled()
  })
})
