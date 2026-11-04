import { describe, expect, it, vi } from 'vitest'

const fetchMock = vi.hoisted(() => vi.fn())
vi.mock('@sida/api', () => ({ fetchAPI: fetchMock }))

import { submitCopilotRun } from '@/lib/copilot/jobs'

describe('后台研究提交', () => {
  it('保留用户选择的配方，且只提交一次', async () => {
    fetchMock.mockResolvedValueOnce({ run_id: 'r1', status: 'queued' })
    await submitCopilotRun('分析 600111', 21, undefined, 'deep_diagnosis')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, request] = fetchMock.mock.calls[0]
    expect(url).toBe('/copilot/runs')
    expect(JSON.parse(request.body)).toEqual({
      question: '分析 600111',
      conversation_id: 21,
      recipe: 'deep_diagnosis',
    })
  })
})
