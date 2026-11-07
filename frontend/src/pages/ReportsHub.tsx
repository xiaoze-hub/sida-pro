import { Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Clock, FileText, Loader2, Play } from 'lucide-react'

import TabbedPage, { type TabDef } from '@/components/TabbedPage'
import { lazyWithRetry } from '@/lib/lazy-with-retry'
import { premarketApi, type PremarketRunResponse } from '@sida/api'

/**
 * 投研枢纽页(设计稿 v2.0 §4.3「历史并入报告」):「分析记录」+「定时任务归档」两 Tab。
 *
 * ⚠️ 2026-09-01 补做: 该合并项属第 1 块(§4.3 四项 Tab 合并), 其产物曾被
 * 同步事故冲掉; 重建时派活清单只列了三项, 此项被遗漏 —— 本次对照设计稿补齐。
 *
 * 旧路由 /history 由 App.tsx 重定向到 /reports?tab=history, 书签不失效。
 *
 * 2026-10-23 入口扶正: 默认 Tab 改为「分析」(原 history Tab, 承载盘前/盘后分析),
 * 原「报告」Tab 改名「底稿归档」并排第二位(?tab= URL 兼容由 TabbedPage 保持)。
 *
 * P1 页签按**真实数据源**正名(去歧义):
 *   - `history` → 「分析记录」: AnalysisHistory 表里的账号内记录(`/history`, 按用户隔离),
 *     可筛选状态 / 重试 / 续问参谋;「立即分析」产出的就是这类记录;
 *   - `reports` → 「定时任务归档」: cron 跑完写盘的 markdown 原文
 *     (`~/.hermes/cron/output/<job>/`, 文件归档、按任务分组、只读), 不是账号内记录。
 *   Tab key(`history` / `reports`)与 `perm` 保持原样 —— 旧书签、推送链接、权限过滤
 *   一律不受改名影响。两页签各带一条来源说明, 读的人不必猜「哪个才是我的分析」。
 *
 * 顶部操作区:「▶ 立即分析」(premarketApi.run 入队 + 30s 轮询今日记录)
 * 与「问参谋」(带参跳 /ai-copilot)。
 */
const ReportsPage = lazyWithRetry(() => import('@/pages/Reports'), 'reports-page')
const HistoryPage = lazyWithRetry(() => import('@/pages/History'), 'history-page')

const REPORTS_TABS: TabDef[] = [
  {
    key: 'history',
    label: '分析记录',
    icon: Clock,
    hint: '分析历史按账号权限展示；管理员还可查看归属未知的历史记录。可重试、可续问参谋。',
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <TabSourceNote
          source="history"
          label="分析记录"
          detail="按账号权限展示；管理员可查看归属未知的历史记录。可筛选、可重试及续问参谋"
          origin="分析历史"
        />
        <HistoryPage />
      </Suspense>
    ),
  },
  {
    key: 'reports',
    label: '定时任务归档',
    icon: FileText,
    perm: 'view_reports',
    hint: '定时任务（cron）跑完写盘的 markdown 原文归档，按任务分组、只读；不在账号分析记录里 · 数据源：~/.hermes/cron/output/<job>/',
    render: () => (
      <Suspense fallback={<TabLoading />}>
        <TabSourceNote
          source="reports"
          label="定时任务归档"
          detail="定时任务写出的 markdown 原文，按任务分组只读；不是账号内的分析记录"
          origin="cron 输出目录（文件）"
        />
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

/**
 * 页签来源说明(去歧义): 两个页签是**两种不同的数据源**, 页签名只说"是哪一种",
 * 这一行补"从哪来、能不能改", 免得把定时任务的归档文件当成自己的分析记录(反之亦然)。
 * 挂在各自 Tab 的 render 里 → 只有该 Tab 激活时出现, 不用在 Hub 里重算当前 Tab。
 */
function TabSourceNote({
  source,
  label,
  detail,
  origin,
}: {
  source: 'history' | 'reports'
  label: string
  detail: string
  origin: string
}) {
  return (
    <div
      data-testid="reports-hub-source-note"
      data-source={source}
      className="mb-3 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md bg-s2 px-3 py-2 text-[12px]"
    >
      <span className="font-medium text-foreground">{label}</span>
      <span className="text-muted-foreground">{detail}</span>
      <span className="ml-auto font-mono text-muted-foreground" title="数据源">
        {origin}
      </span>
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
          title="生成今日盘前分析，结果进「分析记录」页签"
          onClick={() => void startRun()}
          disabled={running}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-3 text-[12px] font-medium text-foreground transition-colors hover:bg-accent disabled:pointer-events-none disabled:opacity-50"
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
          <span data-testid="premarket-run-error" className="text-[12px] text-destructive">
            {runError}
          </span>
        ) : null}
      </div>
      <TabbedPage tabs={REPORTS_TABS} defaultTab="history" myPerms={myPerms} isOwner={isOwner} />
    </div>
  )
}
