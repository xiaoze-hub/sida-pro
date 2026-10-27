import { Suspense, useEffect, useState, type ComponentProps } from 'react'
import type AiCopilotPage from '@/pages/AiCopilot'
import ErrorBoundary from '@/components/ErrorBoundary'
import { lazyWithRetry } from '@/lib/lazy-with-retry'

type ConversationModule = { default: typeof AiCopilotPage }
type Loader = () => Promise<ConversationModule>
const loadConversation: Loader = () => import('@/pages/AiCopilot')

function LoadingConversation({ onRetry }: { onRetry: () => void }) {
  const [slow, setSlow] = useState(false)
  useEffect(() => {
    const timer = window.setTimeout(() => setSlow(true), 12000)
    return () => window.clearTimeout(timer)
  }, [])
  return (
    <div className="grid h-full min-h-[240px] place-items-center p-4 text-center text-[14px] text-muted-foreground" role="status" aria-live="polite">
      <div>
        <p>{slow ? '数智参谋加载时间较长，请检查网络后重试。' : '正在打开数智参谋…'}</p>
        {slow && <button type="button" className="mt-3 text-primary" onClick={onRetry}>重新加载参谋</button>}
      </div>
    </div>
  )
}

/** 仅在加载失败或超时时换新 lazy 实例；正常切换工作区时保持会话。 */
export function AssistantConversationLoader({ loader = loadConversation, ...props }: ComponentProps<typeof AiCopilotPage> & { loader?: Loader }) {
  const [attempt, setAttempt] = useState(0)
  const [Conversation, setConversation] = useState(() => lazyWithRetry(loader, 'assistant-conversation'))
  const retry = () => {
    setConversation(() => lazyWithRetry(loader, 'assistant-conversation'))
    setAttempt((previous) => previous + 1)
  }
  return (
    <ErrorBoundary key={attempt} fallback={() => (
      <div className="p-4 text-[14px]" role="alert">数智参谋暂时无法显示。<button type="button" className="ml-2 text-primary" onClick={retry}>重新加载参谋</button></div>
    )}>
      <Suspense fallback={<LoadingConversation onRetry={retry} />}>
        <Conversation {...props} />
      </Suspense>
    </ErrorBoundary>
  )
}
