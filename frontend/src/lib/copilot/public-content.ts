/** 兼容早期已保存的上游异常，避免展示或复制内部标识。 */
export function publicAssistantContent(text: string): string {
  return text.replace(/^\s*(?:抱歉，)?AI 服务暂时不可用[：:][\s\S]*/, '抱歉，AI 服务暂时不可用，请稍后重试。')
}

