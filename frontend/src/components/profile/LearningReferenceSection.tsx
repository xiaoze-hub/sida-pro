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

interface ReferenceSummary {
  scope?: string
  labels?: string[]
  scoring_blocked?: boolean
  block_reason?: string
  note?: string
  trainable?: boolean
  auto_weighting?: boolean
  independent_of_personal?: boolean
  sample?: {
    evaluated_rows?: number | null
    directional_sample?: number | null
    deduped_directional_sample?: number | null
    excluded_neutral_rows?: number | null
    pending_rows?: number | null
    min_n?: number | null
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
}: {
  testId: string
  title: string
  subtitle: string
  data?: ReferenceSummary
  error: unknown
  pendingLabel: string
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
                className="rounded-sm bg-amber-500/10 px-1.5 py-0.5 text-[11px] text-amber-700 dark:text-amber-500"
              >
                {label}
              </span>
            ))}
          </div>
          <div className="mb-2 text-[12px] leading-5 text-muted-foreground" data-testid={`${testId}-sample`}>
            已评估 {data.sample?.evaluated_rows ?? 0} 条 · 真实分母{' '}
            {data.overall?.total ?? 0} 条(方向性动作, 去重后) · 中性排除{' '}
            {data.sample?.excluded_neutral_rows ?? 0} 条 · 待评估{' '}
            {data.sample?.pending_rows ?? 0} 条 · 重复键组{' '}
            {data.duplicate_risk?.key_groups_with_duplicates ?? 0} 个
          </div>
          {((data.excluded?.detached_account_rows ?? 0) + (data.excluded?.ghost_user_rows ?? 0)) > 0 ? (
            <div className="mb-2 text-[11px] leading-4 text-muted-foreground" data-testid={`${testId}-excluded`}>
              另有 {data.excluded?.detached_account_rows ?? 0} 条账号注销留档(detached)与{' '}
              {data.excluded?.ghost_user_rows ?? 0} 条悬空 user_id(ghost)的行 —— 既非历史无归属也非
              system, 不进入本汇总, 也不计入任何人的成绩。
            </div>
          ) : null}
          <div className="mb-2 text-[12px] leading-5 text-amber-600 dark:text-amber-500">
            来源时点不可证: 不训练、不自动调权、不宣称回测; 只作整体经验参考, 不构成任何个人成绩。
          </div>
          <ReferenceTable agents={data.agents ?? []} empty={pendingLabel} />
          {data.note ? (
            <div className="mt-2 text-[11px] leading-4 text-muted-foreground">{data.note}</div>
          ) : null}
        </>
      )}
    </div>
  )
}

export function LearningReferenceSection() {
  const { data: legacy, error: legacyError } = useApiQuery<ReferenceSummary>(
    ['learning', 'reference', 'legacy'],
    '/learning/reference/legacy',
  )
  const { data: system, error: systemError } = useApiQuery<ReferenceSummary>(
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
        subtitle="无会话的系统调度运行, 不并入任何账号; 对账仅只读预览(不写回/不改权重/不下单)"
        data={system}
        error={systemError}
        pendingLabel="暂无已评估的 system 桶记录"
      />
    </div>
  )
}

export default LearningReferenceSection
