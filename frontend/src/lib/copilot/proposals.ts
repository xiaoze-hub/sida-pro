/**
 * 提案面板的纯逻辑层（2026-10-22，AI 参谋工作台）。
 *
 * 背景：参谋的分析维度写死在手写 YAML 配方里。用户问到配方没覆盖的维度时，
 * 采集层把缺口记进 `harness_proposals`，但页面上看不见。本面板 = 「参谋缺什么能力」
 * 的可见清单 + 人的裁决入口。
 *
 * 这个文件只放**类型 + 纯函数**（排序 / 格式化 / 文案），不碰 React、不发请求：
 *  - 排序规则藏在组件里最容易做错（后端给的是时间序，用户要看的是次数序）；
 *  - 空值语义（缺失不补 0）必须单点定义，否则各分支各写一套；
 *  - 纯函数才能被 vitest 直接钉住。
 *
 * 数据契约（后端已就绪，前端不改）：
 *  GET  /copilot/proposals?status=pending&limit=50
 *  POST /copilot/proposals/{id}/decide  {action: accept|reject|snooze}
 *  GET  /copilot/proposals/stats → {items, top, summary:{total,pending,accept,reject,snooze}}
 *
 * ⚠ 产品红线（设计文档 §5）：采纳 ≠ 已生效。`decide()` 只改数据库 status，
 * 一个字都不改 YAML 配方。UI 上的提示文案因此不是锦上添花，是诚信底线。
 */
import { safeInt, safeNum } from '@/lib/format'

/** 四种落库状态。`pending` 之外三个都是终态（同一维度再次被提会新建 pending 行）。 */
export type ProposalStatus = 'pending' | 'accept' | 'reject' | 'snooze'
/** 可提交的裁决动作（= 三个终态，不含 pending）。 */
export type ProposalAction = 'accept' | 'reject' | 'snooze'
/** 过滤带：四态 + 全部。 */
export type ProposalTab = ProposalStatus | 'all'

/** 后端 `sum(list_proposals)` 返回的单条提案。 */
export interface ProposalRow {
  id: number
  dimension: string
  normalized_dimension?: string | null
  reason?: string | null
  evidence?: string | null
  symbol?: string | null
  recipe?: string | null
  status: string
  /** 这是「被提过几次」= 决策依据；后端保证有值，但**前端仍按可能缺失处理** */
  seen_count?: number | null
  first_seen_at?: string | null
  last_seen_at?: string | null
  decided_at?: string | null
}

/** stats 端点按维度聚合后的一行（`rows` = 合并了几条原始记录）。 */
export interface ProposalStatItem {
  dimension: string
  normalized_dimension?: string | null
  seen_count?: number | null
  status?: string | null
  rows?: number | null
  first_seen_at?: string | null
  last_seen_at?: string | null
}

export interface ProposalSummary {
  total: number
  pending: number
  accept: number
  reject: number
  snooze: number
}

export interface ProposalStats {
  items: ProposalStatItem[]
  top: ProposalStatItem | null
  summary: ProposalSummary
}

/** 过滤带五个 tab（顺序 = 生命周期顺序 + 全部收尾）。 */
export const PROPOSAL_TABS: { value: ProposalTab; label: string }[] = [
  { value: 'pending', label: '待审' },
  { value: 'accept', label: '采纳' },
  { value: 'reject', label: '忽略' },
  { value: 'snooze', label: '搁置' },
  { value: 'all', label: '全部' },
]

/** 状态 → 中文。`accept` 叫「采纳」不叫「已生效」—— 后者是幻觉（见文件头红线）。 */
const STATUS_LABEL: Record<ProposalStatus, string> = {
  pending: '待审',
  accept: '采纳',
  reject: '忽略',
  snooze: '搁置',
}

/** 裁决动作 → 中文（三个操作按钮的文案，与状态徽标同字）。 */
export const ACTION_LABEL: Record<ProposalAction, string> = {
  accept: '采纳',
  reject: '忽略',
  snooze: '搁置',
}

/** 汇总带上的四项；「全部」只在 stats.summary.total 里，不进这一行。 */
export const SUMMARY_ITEMS: { key: keyof Omit<ProposalSummary, 'total'>; label: string }[] = [
  { key: 'pending', label: '待审' },
  { key: 'accept', label: '采纳' },
  { key: 'reject', label: '忽略' },
  { key: 'snooze', label: '搁置' },
]

/**
 * 证据展示上限：设计文档 §3「≤200 字 + 滚动」。
 * 只截**显示**，不改数据 —— 完整证据仍可从后端原样取回。
 */
export const EVIDENCE_LIMIT = 200

/** 状态归一化：只认四个小写字面量，其余原样返回（不猜、不默认成 pending）。 */
export function normalizeStatus(raw: unknown): string {
  const s = String(raw ?? '').trim().toLowerCase()
  return s
}

/** 状态文案：未知状态原样上屏（宁可显示生值，也不假装它是「待审」）。缺失 → `--`。 */
export function statusLabel(raw: unknown): string {
  const s = normalizeStatus(raw)
  if (!s) return '--'
  return STATUS_LABEL[s as ProposalStatus] ?? s
}

/**
 * 次数徽标：`×N`；**缺失给 `--`，绝不补 0**。
 *
 * 为什么不能补 0：`seen_count` 缺失 = 「不知道被提过几次」，而 0 = 「一次都没提过」，
 * 两件事。补 0 会让这条提案在按次数排序时被踩到最底，看起来像「没人提」。
 */
export function formatSeenCount(raw: unknown): string {
  const text = safeInt(raw, '')
  return text === '' ? '--' : `×${text}`
}

/**
 * 首现时间 → `MM-DD`（列表元信息行只放得下日期）。
 *
 * 认不出就是 null —— 调用方据此**整段省略**，不显示 `-- 起` 这种噪音。
 * 后端给的是 `YYYY-MM-DD HH:MM:SS`（也兼容 ISO 的 `T`/带时区写法）。
 */
export function formatFirstSeen(raw: unknown): string | null {
  const s = String(raw ?? '').trim()
  if (!s) return null
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s)
  if (!m) return null
  return `${m[2]}-${m[3]}`
}

/**
 * 元信息行：`标的 · 配方 · 首现日期起`，**缺失项整段省略**（不出现 `-- · --`）。
 * 三项全缺 → null（这一行干脆不渲染，省下的高度给理由）。
 */
export function buildMetaLine(row: Pick<ProposalRow, 'symbol' | 'recipe' | 'first_seen_at'>): string | null {
  const symbol = String(row.symbol ?? '').trim()
  const recipe = String(row.recipe ?? '').trim()
  const first = formatFirstSeen(row.first_seen_at)
  const parts: string[] = []
  if (symbol) parts.push(symbol)
  if (recipe) parts.push(recipe)
  if (first) parts.push(`${first} 起`)
  return parts.length ? parts.join(' · ') : null
}

/**
 * 证据文本截断：超长截到 `EVIDENCE_LIMIT` 并补省略号；空/缺 → null（行不可展开）。
 * 判长用 Array.from 按**码点**数，避免把 emoji/生僻字切成半个代理对。
 */
export function clipEvidence(raw: unknown, limit = EVIDENCE_LIMIT): string | null {
  const s = String(raw ?? '')
  if (!s.trim()) return null
  const chars = Array.from(s)
  if (chars.length <= limit) return s
  return `${chars.slice(0, limit).join('')}…`
}

/**
 * 列表排序（设计文档 §3）：
 *  - **待审 tab**：按 `seen_count` 降序 —— 用户决策依据是「这个维度我提了多少次」；
 *    次数相同按 `last_seen_at` 降序（越近越靠前）；再相同按后端原始下标（排序稳定）。
 *  - 其余 tab：保持后端顺序（`last_seen_at` 降序，时间序在这一侧更自然）。
 *
 * 缺失的 `seen_count` 排最后（`-Infinity`），且**不参与补 0** —— 它没有次数可比。
 */
export function sortProposals(rows: ProposalRow[], tab: ProposalTab): ProposalRow[] {
  const list = Array.isArray(rows) ? rows.slice() : []
  if (tab !== 'pending') return list

  const seen = (r: ProposalRow): number => {
    const n = safeNum(r.seen_count)
    return n === null ? Number.NEGATIVE_INFINITY : n
  }
  const lastSeen = (r: ProposalRow): string => String(r.last_seen_at ?? '')

  return list
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const ac = seen(a.row)
      const bc = seen(b.row)
      // 缺失（-Infinity）一律沉底：没有次数就没有比较资格，也不能当 0 参与排序。
      if (ac !== bc) {
        if (ac === Number.NEGATIVE_INFINITY) return 1
        if (bc === Number.NEGATIVE_INFINITY) return -1
        return bc - ac
      }
      const byLast = lastSeen(b.row).localeCompare(lastSeen(a.row))
      if (byLast !== 0) return byLast
      return a.index - b.index
    })
    .map((it) => it.row)
}

/**
 * 「最该补」行：维度名 + 次数。`top` 为空 → null（不编一个「暂无」的假条目）。
 * 返回结构化而非字符串，因为维度名与次数要分别上色（次数是唯一彩色权重）。
 */
export function buildTopLine(
  stats: ProposalStats | null | undefined,
): { dimension: string; count: string } | null {
  const top = stats?.top
  if (!top) return null
  const dimension = String(top.dimension ?? '').trim()
  if (!dimension) return null
  return { dimension, count: formatSeenCount(top.seen_count) }
}

/**
 * 汇总带的四个数（固定顺序，缺字段给 `--`）。
 * 只在 stats 拿到时渲染 —— stats 挂了就整条不显示，不用「0」冒充「不知道」。
 */
export function buildSummaryItems(
  summary: ProposalSummary | null | undefined,
): { key: string; label: string; value: string }[] {
  return SUMMARY_ITEMS.map((it) => ({
    key: it.key,
    label: it.label,
    value: safeInt(summary ? summary[it.key] : null, '--'),
  }))
}

/**
 * 是否「一条提案都没有」（全局空态）而非「这个分类下没有」（分 tab 空态）。
 *
 * 判据只能是 `summary.total === 0`：stats 拿不到（null）时无法断言库里是空的，
 * 此时按分 tab 空态说话 —— 宁可少说一句，也不谎称「没有提案」。
 */
export function isEmptyOverall(stats: ProposalStats | null | undefined): boolean {
  const total = stats?.summary?.total
  return typeof total === 'number' && total === 0
}
