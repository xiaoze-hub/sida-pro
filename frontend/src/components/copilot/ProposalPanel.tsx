/**
 * 提案面板（2026-10-22 新增，AI 参谋工作台）。
 *
 * 背景：参谋的分析维度写死在 11 份手写 YAML 配方里。用户问到配方没覆盖的维度
 * （「股东户数变化」）时，采集层把这个缺口记进 `harness_proposals`，但**页面上看不见** ——
 * 攒下来的维度躺在库里，用户不知道参谋缺什么，也没法拍板补哪个。
 * 本面板 = 「参谋缺什么能力」的可见清单 + 人的裁决入口。
 *
 * 定位：这个面板的价值是**排序**，不是展示。一行一条、hairline 分隔、不用卡片；
 * 次数排在维度名右边最显眼处（决策依据是「这个维度我提了多少次」，不是时间）。
 *
 * 三条硬约束（违反即返工）：
 *  ① **采纳 ≠ 已生效**：`decide()` 只改 status，一个字都不改 YAML 配方。采纳成功后必须
 *     显示「配方仍需人工修改后才会生效」——这句是产品诚信底线，不是装饰（设计文档 §5）。
 *  ② **空不等于零**：没提案说「还没有提案」，不说「0 个提案」；`seen_count` 缺失显示 `--`。
 *  ③ **错不等于空**：接口挂了显式报「加载失败 + 重试」，绝不伪装成空态。
 *
 * 视觉：零阴影、靠 `border-border/40` hairline 分层；间距只取 8/12/20 档（p-2/3/5/8）；
 * 字号只取 10/11/12/13；数字 `tabular-nums`；面板内唯一彩色是「次数 ×N」与状态徽标。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ACTION_LABEL,
  PROPOSAL_TABS,
  buildCandidateToolsLine,
  buildMetaLine,
  buildSummaryItems,
  buildTopLine,
  clipEvidence,
  formatSeenCount,
  isEmptyOverall,
  sortProposals,
  statusLabel,
  type ProposalAction,
  type ProposalRow,
  type ProposalTab,
} from '@/lib/copilot/proposals'
import { useCopilotProposals } from '@/hooks/useCopilotProposals'

/** 状态徽标配色：面板内唯一允许的彩色（待审=强调 / 采纳=机会 / 忽略=中性 / 搁置=系统）。 */
function badgeClass(status: string): string {
  switch (status) {
    case 'pending':
      return 'text-role-watch'
    case 'accept':
      return 'text-role-opp'
    case 'snooze':
      return 'text-role-system'
    default:
      // reject 与任何未知状态：中性灰，不为了「好看」给它编一个颜色
      return 'text-muted-foreground'
  }
}

/** 三个操作固定顺序（采纳在前：它是唯一需要额外解释后果的动作）。 */
const ACTIONS: ProposalAction[] = ['accept', 'reject', 'snooze']

export function ProposalPanel({ className = '' }: { className?: string }) {
  const [tab, setTab] = useState<ProposalTab>('pending')
  const { rows, stats, loading, refreshing, error, decideError, decidingId, decide, reload } =
    useCopilotProposals(tab)

  /** 展开证据的行 id（一次只开一行：侧栏只有 320px，多条展开会把人挤丢）。 */
  const [expandedId, setExpandedId] = useState<number | null>(null)
  /** 刚采纳成功的提案 id —— 用来挂那条「仍需人工改配方」的提示。 */
  const [acceptedId, setAcceptedId] = useState<number | null>(null)

  // 切 tab / 重试后，展开态与「刚采纳」提示都该清零（它们依附于上一屏的上下文）。
  useEffect(() => {
    setExpandedId(null)
    setAcceptedId(null)
  }, [tab])

  const sorted = useMemo(() => sortProposals(rows, tab), [rows, tab])
  const summaryItems = useMemo(() => buildSummaryItems(stats?.summary), [stats])
  const topLine = useMemo(() => buildTopLine(stats), [stats])
  const overallEmpty = isEmptyOverall(stats)

  const onDecide = useCallback(
    async (row: ProposalRow, action: ProposalAction) => {
      const ok = await decide(row.id, action)
      if (ok) setAcceptedId(action === 'accept' ? row.id : null)
    },
    [decide],
  )

  return (
    <section className={`flex flex-col gap-3 px-3 pb-3 ${className}`} data-testid="copilot-proposals-panel">
      {/* ① 汇总带：一行四个数 + 一行「最该补」。stats 挂了整条不显示（不用 0 冒充） */}
      {stats ? (
        <div className="flex flex-col gap-2" data-testid="proposal-summary">
          <div className="flex flex-wrap items-baseline gap-2 text-[11px] text-muted-foreground">
            {summaryItems.map((item, i) => (
              <span key={item.key} className="flex items-baseline gap-2">
                {i > 0 ? <span aria-hidden="true">·</span> : null}
                <span>
                  {item.label}
                  <span className="ml-1 tabular-nums text-foreground" data-testid={`proposal-count-${item.key}`}>
                    {item.value}
                  </span>
                </span>
              </span>
            ))}
          </div>
          {topLine ? (
            <p className="text-[11px] text-muted-foreground" data-testid="proposal-top">
              最该补：
              {/* 全屏唯一彩色权重 —— 这句话的重点就是维度名与次数 */}
              <span className="text-role-watch">{topLine.dimension}</span>
              <span className="ml-1 tabular-nums text-role-watch">{topLine.count}</span>
            </p>
          ) : null}
        </div>
      ) : null}

      {/* ② 过滤带：与主 tab bar 同款样式（不另立一套） */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border/40 pb-2">
        {PROPOSAL_TABS.map((t) => (
          <button
            key={t.value}
            type="button"
            data-testid={`proposal-tab-${t.value}`}
            aria-pressed={tab === t.value}
            onClick={() => setTab(t.value)}
            className={`rounded-md border px-2 py-0.5 text-[11px] transition ${
              tab === t.value
                ? 'border-role-watch/60 text-role-watch'
                : 'border-border/60 text-muted-foreground hover:text-foreground'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* 采纳提示：不弹 toast 不弹窗，就在列表上方一行 muted 文案（设计文档 §5） */}
      {acceptedId !== null ? (
        <p role="status" data-testid="proposal-accept-note" className="text-[11px] text-muted-foreground">
          已采纳 · 配方仍需人工修改后才会生效
        </p>
      ) : null}

      {decideError ? (
        <p role="alert" data-testid="proposal-decide-error" className="text-[11px] text-role-risk">
          {decideError}
        </p>
      ) : null}

      {/* 静默刷新失败：列表还在（上一次的数据），只在上面补一行 —— 不能因为刷新挂了就把列表抹掉 */}
      {!loading && error && sorted.length > 0 ? (
        <div
          role="alert"
          data-testid="proposal-refresh-error"
          className="flex items-center gap-2 text-[11px] text-muted-foreground"
        >
          <span>{error}</span>
          <button type="button" onClick={reload} className="text-role-watch hover:underline">
            重试
          </button>
        </div>
      ) : null}

      {/* ③ 列表：加载骨架 → 错误 → 空态 → 行 */}
      {loading ? (
        <div className="flex flex-col gap-3" data-testid="proposal-loading" aria-busy="true">
          {[0, 1, 2].map((i) => (
            <div key={i} className="flex flex-col gap-2 border-b border-border/30 py-2">
              <div className="h-3 w-1/2 animate-pulse rounded bg-s3" />
              <div className="h-2.5 w-4/5 animate-pulse rounded bg-s3/70" />
              <div className="h-2 w-1/3 animate-pulse rounded bg-s3/50" />
            </div>
          ))}
        </div>
      ) : error && sorted.length === 0 ? (
        <div
          role="alert"
          data-testid="proposal-error"
          className="flex items-center gap-2 py-3 text-[11px] text-muted-foreground"
        >
          <span>{error}</span>
          <button type="button" data-testid="proposal-retry" onClick={reload} className="text-role-watch hover:underline">
            重试
          </button>
        </div>
      ) : sorted.length === 0 ? (
        overallEmpty ? (
          <div className="flex flex-col items-center gap-2 py-8 text-center" data-testid="proposal-empty">
            <p className="text-[13px] text-foreground">还没有提案</p>
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              当你在对话里问到一个配方没覆盖的维度时，参谋会记在这里
            </p>
          </div>
        ) : (
          <p data-testid="proposal-empty-tab" className="py-3 text-[11px] text-muted-foreground">
            这个分类下没有提案
          </p>
        )
      ) : (
        <ul className="flex flex-col" data-refreshing={refreshing ? '1' : '0'}>
          {sorted.map((row) => {
            const meta = buildMetaLine(row)
            const candidatesLine = buildCandidateToolsLine(row)
            const evidence = clipEvidence(row.evidence)
            const expanded = expandedId === row.id
            const pending = decidingId === row.id
            return (
              <li
                key={row.id}
                data-testid="proposal-row"
                data-proposal-id={row.id}
                data-status={row.status}
                className="border-b border-border/30"
              >
                <div className="flex items-start gap-2 pt-2">
                  <button
                    type="button"
                    aria-expanded={evidence ? expanded : undefined}
                    data-testid="proposal-expand"
                    onClick={() => setExpandedId((v) => (v === row.id ? null : row.id))}
                    disabled={!evidence}
                    className="min-w-0 flex-1 cursor-pointer text-left disabled:cursor-default"
                  >
                    <div className="flex items-baseline gap-2">
                      <span
                        data-testid="proposal-dimension"
                        className="min-w-0 flex-1 truncate text-[13px] text-foreground"
                      >
                        {row.dimension || '--'}
                      </span>
                      <span
                        data-testid="proposal-seen"
                        className="min-w-6 shrink-0 text-right tabular-nums text-[11px] text-role-watch"
                      >
                        {formatSeenCount(row.seen_count)}
                      </span>
                      <span
                        data-testid="proposal-status"
                        className={`shrink-0 rounded bg-s2 px-1 text-[10px] ${badgeClass(row.status)}`}
                      >
                        {statusLabel(row.status)}
                      </span>
                    </div>
                    {row.reason ? (
                      <p
                        data-testid="proposal-reason"
                        className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted-foreground"
                      >
                        {row.reason}
                      </p>
                    ) : null}
                    {meta ? (
                      <p data-testid="proposal-meta" className="mt-1 truncate text-[10px] text-muted-foreground">
                        {meta}
                      </p>
                    ) : null}
                    {/* P2: 已有工具覆盖这个维度 → 先挂工具，而不是排期补数据源。
                        只做提示（审核人决定），不在这里改配方。 */}
                    {candidatesLine ? (
                      <p
                        data-testid="proposal-candidate-tools"
                        title={candidatesLine}
                        className="mt-1 truncate font-mono text-[10px] text-muted-foreground"
                      >
                        {candidatesLine}
                      </p>
                    ) : null}
                  </button>
                </div>

                {expanded && evidence ? (
                  <div className="mt-2 border-l border-border/40 pl-2">
                    <p
                      data-testid="proposal-evidence"
                      className="scrollbar max-h-40 overflow-y-auto whitespace-pre-wrap text-[11px] leading-relaxed text-muted-foreground"
                    >
                      {evidence}
                    </p>
                  </div>
                ) : null}

                <div className="flex items-center justify-end gap-1 py-2">
                  {pending ? <span className="text-[10px] text-muted-foreground/80">处理中…</span> : null}
                  {ACTIONS.map((action) => {
                    const current = row.status === action
                    return (
                      <button
                        key={action}
                        type="button"
                        data-testid={`proposal-action-${action}`}
                        data-proposal-id={row.id}
                        disabled={current || pending || decidingId !== null}
                        onClick={() => void onDecide(row, action)}
                        className={`px-1 py-0.5 text-[11px] transition-colors disabled:opacity-50 ${
                          current ? 'text-muted-foreground/60' : 'text-muted-foreground hover:text-foreground'
                        }`}
                      >
                        {ACTION_LABEL[action]}
                      </button>
                    )
                  })}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

export default ProposalPanel
