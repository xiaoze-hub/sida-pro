import { Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Clock, FileText, Loader2, Play } from 'lucide-react'

import TabbedPage, { type TabDef } from '@/components/TabbedPage'
import { lazyWithRetry } from '@/lib/lazy-with-retry'
import { premarketApi, type PremarketRunResponse } from '@panwatch/api'

/**
 * 投研枢纽页(设计稿 v2.0 §4.3「历史并入报告」):「分析」+「底稿归档」两 Tab。
 *
 * ⚠️ 2026-09-01 补做: 该合并项属第 1 块(§4.3 四项 Tab 合并), 其产物曾被
 * 同步事故冲掉; 重建时派活清单只列了三项, 此项被遗漏 —— 本次对照设计稿补齐。
 *
 * 旧路由 /history 由 App.tsx 重定向到 /reports?tab=history, 书签不失效。
 *
 * 2026-10-23 入口扶正: 默认 Tab 改为「分析」(原 history Tab, 承载盘前/盘后分析),
 * 原「报告」Tab 改名「底稿归档」并排第二位(?tab= URL 兼容由 TabbedPage 保持)。
 * 顶部操作区:「▶ 立即分析」(premarketApi.run 入队 + 30s 轮询今日记录)
 * 与「问参谋」(带参跳 /ai-copilot)。
 */
const ReportsPage = lazyWithRetry(() => import('@/pages/Reports'), 'reports-page')
const HistoryPage = lazyWithRetry(() => import('@/pages/History'), 'history-page')

const REPORTS_TABS: TabDef[] = [
  {
    key: 'history',
    label: '报告中心',
    icon: Clock,
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <HistoryPage />
      </Suspense>
    ),
  },
  {
    key: 'reports',
    label: '底稿归档',
    icon: FileText,
    perm: 'view_reports',
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <ReportsPage />
      </Suspense>
    ),
  },
]

/** 「立即分析」入队后的轮询间隔: 30s 拉一次今日记录, 拿到非空即停(不静默挂死) */
const RUN_POLL_INTERVAL_MS = 30_000
/** 问参谋默认提问: 盘前分析 → 主线/持仓关键位上屏 */
const ASK_COPILOT_QUERY = '按今天的盘前分析，帮我梳理主线与持仓关键位并画到图上'

function TabLoading() {
  return (
    <div className="flex h-[40vh] items-center justify-center text-[12px] text-muted-foreground">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 加载中…
    </div>
  )
}

export default function ReportsHubPage({ myPerms, isOwner }: { myPerms?: Set<string> | null; isOwner?: () => boolean }) {
  const navigate = useNavigate()
  /** idle=可触发 / starting=run 请求中 / queued=已入队+轮询 / error=显式错误(可重试) */
  const [runState, setRunState] = useState<'idle' | 'starting' | 'queued' | 'error'>('idle')
  const [runError, setRunError] = useState('')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  // 卸载必须清轮询定时器, 否则页面关掉 30s 心跳仍在跑
  useEffect(() => stopPolling, [stopPolling])

  const startRun = useCallback(async () => {
    if (runState === 'starting' || runState === 'queued') return // 防连点
    setRunError('')
    setRunState('starting')
    try {
      const res: PremarketRunResponse = await premarketApi.run()
      if (!res?.queued) {
        setRunState('error')
        setRunError(res?.message || '分析任务未入队')
        return
      }
      setRunState('queued')
      stopPolling()
      pollRef.current = setInterval(() => {
        void premarketApi
          .getToday({ cacheMode: 'reload' })
          .then((d) => {
            if (d && !d.empty) {
              stopPolling()
              setRunState('idle') // 今日记录已出 → 可再次触发
            }
          })
          .catch((e) => {
            stopPolling()
            const err = e as { status?: number; message?: string } | null
            // 429/报错都显式落内联提示, 不静默吞错
            setRunError(
              err?.status === 429
                ? `请求过于频繁（429）：${err?.message || '请稍后再试'}`
                : `分析结果获取失败：${err?.message || '服务暂不可用'}`,
            )
            setRunState('error')
          })
      }, RUN_POLL_INTERVAL_MS)
    } catch (e) {
      const err = e as { status?: number; message?: string } | null
      setRunError(
        err?.status === 429
          ? `请求过于频繁（429）：${err?.message || '请稍后再试'}`
          : `分析启动失败：${err?.message || '服务暂不可用'}`,
      )
      setRunState('error')
    }
  }, [runState, stopPolling])

  const askCopilot = useCallback(() => {
    navigate(`/ai-copilot?agent=premarket_strategist&q=${encodeURIComponent(ASK_COPILOT_QUERY)}`)
  }, [navigate])

  const running = runState === 'starting' || runState === 'queued'

  return (
    <div className="space-y-3">
      {/* 顶部操作区(入口扶正 2026-10-23): 立即分析 + 问参谋, hairline 无卡片 */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          data-testid="premarket-run-btn"
          onClick={() => void startRun()}
          disabled={running}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border/60 px-3 text-[12px] font-medium text-foreground transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-50"
        >
          {running ? (
            <>
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> 分析生成中…
            </>
          ) : (
            <>
              <Play className="h-3.5 w-3.5" /> 立即分析
            </>
          )}
        </button>
        <button
          type="button"
          data-testid="premarket-ask-copilot-btn"
          onClick={askCopilot}
          className="inline-flex h-8 items-center rounded-md px-2 text-[12px] text-muted-foreground transition-colors hover:text-primary"
        >
          问参谋
        </button>
        {runState === 'error' && runError ? (
          <span data-testid="premarket-run-error" className="text-[11px] text-destructive">
            {runError}
          </span>
        ) : null}
      </div>
      <TabbedPage tabs={REPORTS_TABS} defaultTab="history" myPerms={myPerms} isOwner={isOwner} />
    </div>
  )
}
