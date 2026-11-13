import { useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { Button } from '@sida/base-ui/components/ui/button'
import { fetchAPI } from '@sida/api'
import { useApiQuery } from '@/hooks/useApiQuery'

/**
 * 历史无归属经验参考 + system 桶独立口径(2026-11-13)。
 *
 * 三路隔离的 UI 侧:
 *  - 历史无归属(生产 4.4 万+ 条已评估旧记录): 只展示**全局聚合**, 明确"非个人成绩";
 *  - system 桶(无会话系统调度运行): 独立展示, 不并入任何账号;
 *  - 个人命中率仍在 `Agent 命中榜`, 本组件不读取、不合并、不反推。
 *
 * 硬约束(与后端契约一致, 不因前端展示而放松):
 *  - 不渲染个股/用户标识/行级明细(响应里也没有这些字段);
 *  - 不训练、不自动调权、不宣称回测: 页面只写"仅经验参考";
 *  - 评分阻断时不给任何数字, 只显示阻断原因。
 */

interface ReferenceAgent {
  agent: string
  hit: number
  total: number
  hit_rate: number | null
  avg_return_pct: number | null
  qualified: boolean
  small_sample?: boolean
}

interface ReferenceAutomation {
  enabled?: boolean
  running?: boolean
  cron?: string
  timezone?: string
  next_run_at?: string | null
  manual_endpoint?: string
  manual_owner_only?: boolean
  note?: string
}

interface ReconcileResult {
  evaluated?: number
  writes?: number
  total_pending?: number | null
  all_pending_rows?: number | null
  batch_size?: number | null
  limit?: number
  has_more?: boolean | null
  truncated?: boolean | null
  batch_capped_by_limit?: boolean | null
  eligible?: number
  skipped_not_due?: number
  skipped_not_mature?: number
  skipped_invalid_date?: number
  skipped_no_quote?: number
  skipped_no_base_price?: number
  skipped_symbol_cap?: number
  skipped_concurrent?: number
  kline_error_rows?: number
  fetch_timeout_rows?: number
  symbols_fetched?: number
  data_date_min?: string | null
  data_date_max?: string | null
  version?: string
  collected_at?: string | null
  db_committed?: boolean
  write_failed?: boolean
  scoring_blocked?: boolean
  note?: string
}

interface ReferenceSummary {
  scope?: string
  labels?: string[]
  scoring_blocked?: boolean
  block_reason?: string
  note?: string
  trainable?: boolean
  auto_weighting?: boolean
  independent_of_personal?: boolean
  automation?: ReferenceAutomation
  sample?: {
    evaluated_rows?: number | null
    directional_sample?: number | null
    deduped_directional_sample?: number | null
    excluded_neutral_rows?: number | null
    pending_rows?: number | null
    unknown_status_rows?: number | null
    min_n?: number | null
  }
  unknown?: {
    status_rows?: number | null
    superseded_rows?: number | null
    pending_rows?: number | null
    invalid_return_rows?: number | null
    excluded_neutral_rows?: number | null
    note?: string
  }
  duplicate_risk?: {
    key_groups_with_duplicates?: number | null
    rows_in_duplicate_groups?: number | null
    note?: string
  }
  excluded?: {
    detached_account_rows?: number | null
    ghost_user_rows?: number | null
    note?: string
  }
  overall?: {
    hit_count?: number | null
    total?: number | null
    raw_total?: number | null
    hit_rate?: number | null
    basis?: string | null
  }
  agents?: ReferenceAgent[]
}

function ReferenceTable({ agents, empty }: { agents: ReferenceAgent[]; empty: string }) {
  if (!agents.length) {
    return <div className="text-[12px] text-muted-foreground">{empty}</div>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[12px]">
        <thead>
          <tr className="border-b border-border text-muted-foreground">
            <th className="px-2 py-1 font-medium">Agent</th>
            <th className="px-2 py-1 text-right font-medium">命中率</th>
            <th className="px-2 py-1 text-right font-medium">命中</th>
            <th className="px-2 py-1 text-right font-medium">样本</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/40">
          {agents.map(a => (
            <tr key={a.agent} data-testid={`reference-agent-row-${a.agent}`}>
              <td className="px-2 py-1 text-foreground">{a.agent}</td>
              <td className="px-2 py-1 text-right font-mono text-foreground">
                {a.hit_rate != null ? `${a.hit_rate}%` : '--'}
              </td>
              <td className="px-2 py-1 text-right font-mono text-muted-foreground">
                {a.hit}/{a.total}
              </td>
              <td className="px-2 py-1 text-right">
                {a.qualified ? (
                  <span className="text-muted-foreground">参评</span>
                ) : (
                  <span className="text-amber-600 dark:text-amber-500">样本不足</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ReferenceBlock({
  testId,
  title,
  subtitle,
  data,
  error,
  pendingLabel,
  warning,
}: {
  testId: string
  title: string
  subtitle: string
  data?: ReferenceSummary
  error: unknown
  pendingLabel: string
  warning?: string
}) {
  return (
    <div className="mt-4" data-testid={testId}>
      <div className="mb-2 flex flex-wrap items-baseline gap-2">
        <span className="text-[12px] font-semibold text-foreground">{title}</span>
        <span className="text-[12px] text-muted-foreground">{subtitle}</span>
      </div>
      {error ? (
        <div className="text-[12px] text-amber-600 dark:text-amber-500" data-testid={`${testId}-error`}>
          {title}加载失败: {error instanceof Error ? error.message : '未知错误'}
        </div>
      ) : !data ? (
        <div className="text-[12px] text-muted-foreground">加载中…</div>
      ) : data.scoring_blocked ? (
        <div className="text-[12px] text-amber-600 dark:text-amber-500" data-testid={`${testId}-blocked`}>
          评分阻断: {data.block_reason || data.note || 'schema 体检未通过'} — 本区块不给任何数字,
          不训练、不调权、不写回
        </div>
      ) : (
        <>
          <div className="mb-1 flex flex-wrap gap-1.5" data-testid={`${testId}-labels`}>
            {(data.labels ?? []).map(label => (
              <span
                key={label}
                className="rounded-sm bg-amber-500/10 px-1.5 py-0.5 text-[12px] text-amber-700 dark:text-amber-500"
              >
                {label}
              </span>
            ))}
          </div>
          <div className="mb-2 text-[12px] leading-5 text-muted-foreground" data-testid={`${testId}-sample`}>
            已评估 {data.sample?.evaluated_rows ?? 0} 条 · 真实分母{' '}
            {data.overall?.total ?? 0} 条(方向性动作, 去重后) · 中性排除{' '}
            {data.sample?.excluded_neutral_rows ?? 0} 条 · 待评估{' '}
            {data.sample?.pending_rows ?? 0} 条(含未成熟/缺失) · 未知终态{' '}
            {data.unknown?.status_rows ?? data.sample?.unknown_status_rows ?? 0} 条(单列) · 重复键组{' '}
            {data.duplicate_risk?.key_groups_with_duplicates ?? 0} 个
          </div>
          {((data.excluded?.detached_account_rows ?? 0) + (data.excluded?.ghost_user_rows ?? 0)) > 0 ? (
            <div className="mb-2 text-[12px] leading-4 text-muted-foreground" data-testid={`${testId}-excluded`}>
              另有 {data.excluded?.detached_account_rows ?? 0} 条账号注销留档(detached)与{' '}
              {data.excluded?.ghost_user_rows ?? 0} 条悬空 user_id(ghost)的行 —— 既非历史无归属也非
              system, 不进入本汇总, 也不计入任何人的成绩。
            </div>
          ) : null}
          <div className="mb-2 text-[12px] leading-5 text-amber-600 dark:text-amber-500">
            {warning || '来源时点不可证: 不训练、不自动调权、不宣称回测; 只作整体经验参考, 不构成任何个人成绩。'}
          </div>
          <ReferenceTable agents={data.agents ?? []} empty={pendingLabel} />
          {data.note ? (
            <div className="mt-2 text-[12px] leading-4 text-muted-foreground">{data.note}</div>
          ) : null}
        </>
      )}
    </div>
  )
}

function SystemReconcilePanel({
  automation,
  isOwner,
  onFinished,
}: {
  automation?: ReferenceAutomation
  isOwner: boolean
  onFinished: () => Promise<unknown>
}) {
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<ReconcileResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const trigger = async () => {
    setRunning(true)
    setError(null)
    try {
      const r = await fetchAPI<ReconcileResult>(
        '/learning/reference/system/reconcile/run?limit=50',
        { method: 'POST' },
      )
      setResult(r)
      await onFinished()
    } catch (e) {
      setError(e instanceof Error ? e.message : '对账失败')
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="mt-3 border-t border-border pt-2" data-testid="profile-system-reconcile">
      <div className="mb-1 text-[12px] leading-5 text-muted-foreground" data-testid="profile-system-automation">
        自动对账: {automation ? (automation.enabled ? '已注册' : '未注册') : '状态未知'}
        {automation?.running ? ' · 调度器运行中' : automation ? ' · 调度器未运行' : ''}
        {' · '}每工作日 15:40({automation?.timezone || 'Asia/Shanghai'})
        {automation?.next_run_at ? ` · 下次 ${automation.next_run_at}` : ''}
      </div>
      <div className="mb-2 text-[12px] leading-4 text-muted-foreground">
        {automation?.note || '只写 system 桶的成熟结果, 不改权重、不下单、不触发交易。'}
      </div>
      {isOwner ? (
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={() => void trigger()}
          disabled={running}
          data-testid="profile-system-reconcile-button"
        >
          <RefreshCw className={`mr-1 h-3 w-3 ${running ? 'animate-spin' : ''}`} />
          {running ? '对账中…' : '立即对账 system 成熟样本'}
        </Button>
      ) : (
        <div className="text-[12px] text-muted-foreground" data-testid="profile-system-reconcile-owner-only">
          手动触发仅管理员可用; 系统仍会在每交易日收盘后自动对账。
        </div>
      )}
      {error ? (
        <div className="mt-2 text-[12px] text-amber-600 dark:text-amber-500" data-testid="profile-system-reconcile-error">
          对账失败: {error}
        </div>
      ) : null}
      {result ? (
        <div className="mt-2 rounded-sm bg-muted px-2 py-1.5 text-[12px] leading-5 text-muted-foreground" data-testid="profile-system-reconcile-result">
          {result.scoring_blocked ? (
            <div className="text-amber-600 dark:text-amber-500">评分阻断: 本轮未读取、未写入任何结果。</div>
          ) : (
            <>
              <div>
                已持久化 {result.evaluated ?? 0} 条 · 候选总数 {result.total_pending ?? '--'} 条 · 本批{' '}
                {result.batch_size ?? 0}/{result.limit ?? '--'} 条
                {result.truncated ? ' · 已截断(其余下轮继续, 未把批次当总数)' : ''}
              </div>
              <div>
                未知单列: 无当日行情 {result.skipped_no_quote ?? 0} · 无基准价{' '}
                {result.skipped_no_base_price ?? 0} · 行情故障 {result.kline_error_rows ?? 0}
                (超时 {result.fetch_timeout_rows ?? 0}) · 未成熟 {result.skipped_not_mature ?? 0} · 未到期{' '}
                {result.skipped_not_due ?? 0} · 坏日期 {result.skipped_invalid_date ?? 0} · 标的超限{' '}
                {result.skipped_symbol_cap ?? 0} · 并发跳过 {result.skipped_concurrent ?? 0}
              </div>
              <div>
                数据日 {result.data_date_min || '--'} ~ {result.data_date_max || '--'} · 规则版本{' '}
                {result.version || '--'} · 采集 {result.collected_at || '--'}
              </div>
              <div>
                事务: {result.db_committed ? '已提交' : '未提交'}
                {result.write_failed ? ' · 写入失败已回滚(无假结果)' : ''} · 不改权重/不下单
              </div>
              {result.note ? <div className="mt-1">{result.note}</div> : null}
            </>
          )}
        </div>
      ) : null}
    </div>
  )
}

export function LearningReferenceSection({ isOwner = false }: { isOwner?: boolean }) {
  const { data: legacy, error: legacyError } = useApiQuery<ReferenceSummary>(
    ['learning', 'reference', 'legacy'],
    '/learning/reference/legacy',
  )
  const { data: system, error: systemError, refetch: refetchSystem } = useApiQuery<ReferenceSummary>(
    ['learning', 'reference', 'system'],
    '/learning/reference/system',
  )

  return (
    <div className="mt-5 border-t border-border pt-4" data-testid="profile-learning-reference">
      <div className="mb-1 flex flex-wrap items-baseline gap-2">
        <span className="text-[12px] font-semibold text-foreground">历史经验参考 · 系统样本</span>
        <span className="text-[12px] text-muted-foreground">
          历史无归属 · 未验证 · 仅经验参考 · 非个人成绩
        </span>
      </div>
      <ReferenceBlock
        testId="profile-history-reference"
        title="历史无归属经验参考"
        subtitle="加 user_id 之前的存量, 归属不可证明, 不计入任何人的命中率"
        data={legacy}
        error={legacyError}
        pendingLabel="暂无已评估的历史无归属记录"
      />
      <ReferenceBlock
        testId="profile-system-reference"
        title="system 桶(独立口径)"
        subtitle="无会话的系统调度运行, 不并入任何账号; 成熟结果持久对账, 只写 system 行"
        data={system}
        error={systemError}
        pendingLabel="暂无已评估的 system 桶记录"
        warning="system 结果可审计(数据日期/规则版本/采集时间); 仅经验参考, 不并入个人成绩、不训练、不自动调权、不下单。"
      />
      <SystemReconcilePanel
        automation={system?.automation}
        isOwner={isOwner}
        onFinished={refetchSystem}
      />
    </div>
  )
}

export default LearningReferenceSection
