import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import EvidenceCard from '@/components/copilot/EvidenceCard'
import { getCopilotRunDetail, type CopilotRunDetail } from '@/lib/copilot/api'

/**
 * 单轮证据卡页面（2026-10-25）。
 *
 * 为什么要单独成页而不是塞进列表展开：证据链会越来越长（工具名 + 参数 + 结果摘要），
 * 在表格行里展开会把 20 行的表撑成一屏一条；独立页还能被分享/收藏（后续加只读链接）。
 *
 * 诚实纪律：id 不存在 → 明说"没找到这一轮"，不显示空卡片假装加载完；
 * 取数失败 → 显示原因 + 重试，不回落成"暂无证据"。
 */
export default function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const [detail, setDetail] = useState<CopilotRunDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!runId) return
    setLoading(true)
    try {
      const d = await getCopilotRunDetail(runId)
      setDetail(d)
      setError(null)
    } catch (e) {
      setDetail(null)
      // 404 与网络错误要分开说：前者是"没这一轮"，后者才是"取不到"
      const msg = e instanceof Error && /404/.test(e.message) ? '没找到这一轮运行' : '证据读取失败'
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [runId])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="mx-auto max-w-4xl px-4 py-4" data-testid="run-detail-page">
      <div className="mb-3 flex items-center gap-3">
        <button
          type="button"
          onClick={() => navigate('/copilot/runs')}
          className="text-[12px] text-muted-foreground hover:text-foreground"
          data-testid="run-detail-back"
        >
          ← 任务列表
        </button>
        <span className="text-[14px] font-medium text-foreground">单轮证据</span>
        {runId && (
          <span className="font-mono text-[12px] text-muted-foreground">{runId.slice(0, 8)}</span>
        )}
        <button
          type="button"
          onClick={() => void load()}
          className="ml-auto text-[12px] text-muted-foreground hover:text-foreground"
          data-testid="run-detail-refresh"
        >
          刷新
        </button>
      </div>

      {!runId ? (
        <div className="text-[12px] text-muted-foreground" data-testid="run-detail-bad-id">
          缺少运行 id
        </div>
      ) : (
        <EvidenceCard
          run={detail?.run ?? null}
          steps={detail?.steps ?? null}
          loading={loading}
          error={error}
        />
      )}
    </div>
  )
}
