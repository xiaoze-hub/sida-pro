/** 兼容早期已保存的上游异常，避免展示或复制内部标识。 */
export function publicAssistantContent(text: string): string {
  return text.replace(/^\s*(?:抱歉，)?AI 服务暂时不可用[：:][\s\S]*/, '抱歉，AI 服务暂时不可用，请稍后重试。')
}

/** 公开错误只使用固定文案；异常可能含上游地址、请求标识或敏感参数。 */
export function publicResearchError(error: unknown): string {
  const text = typeof error === 'string' ? error : error instanceof Error ? error.message : ''
  if (/未收到服务端终稿/.test(text)) {
    return '未收到服务端终稿，回答可能不完整。请先查看本轮过程，再决定是否重新提问。'
  }
  return '本次分析未能完成，请检查网络并查看本轮过程；确认状态后可重新提问。'
}

