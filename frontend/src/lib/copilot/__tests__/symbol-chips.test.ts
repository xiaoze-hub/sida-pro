import { describe, expect, it } from 'vitest'
import { extractSymbolChips } from '@/lib/copilot/symbolChips'
import { publicAssistantContent } from '@/lib/copilot/public-content'

describe('extractSymbolChips', () => {
  it('redacts historical upstream errors while preserving ordinary answers', () => {
    expect(publicAssistantContent('抱歉，AI 服务暂时不可用：Error code: 400 trace_id=835610')).toBe('抱歉，AI 服务暂时不可用，请稍后重试。')
    expect(publicAssistantContent('北方稀土（600111）')).toBe('北方稀土（600111）')
  })

  it('recognizes securities without turning identifiers or errors into stock links', () => {
    expect(extractSymbolChips('北方稀土（600111）与 002050.SZ')).toEqual(['600111', '002050'])
    expect(extractSymbolChips('SH600111 与 sz000001')).toEqual(['600111', '000001'])
    expect(extractSymbolChips('trace_id=fa436bf5c77c52dcb09961d154e6ba02 request_id=req_835610abc')).toEqual([])
    expect(extractSymbolChips('抱歉，AI 服务暂时不可用：Error code: 400 trace_id=835610')).toEqual([])
  })
})
