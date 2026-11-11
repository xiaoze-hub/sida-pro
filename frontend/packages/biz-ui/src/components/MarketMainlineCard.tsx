import { useCallback, useEffect, useRef, useState } from 'react'
import { usePausedInterval } from '@sida/base-ui'
import { RefreshCw, AlertTriangle, ChevronDown, ChevronRight, Layers, Crown, Info } from 'lucide-react'
import { fetchAPI } from '@sida/api'
import SectionHeader from '@sida/biz-ui/components/SectionHeader'
import { mainlineAsOfDate, mainlineAsOfText, mainlineCacheText } from '@sida/biz-ui/lib/mainline-meta'
import './market-mainline.css'

/**
 * 市场主线识别卡片 (2026-08-24, v0.3.0)
 * 数据 GET /api/market/mainline (fetchAPI 自动补 /api 前缀)。
 *
 * 设计:
 *   - 顶部 Top10 主线横向列表(默认全收): 每行展示 主线名 / 主线分条形(横条宽=相对 100%)
 *     / 涨停家数 / 最高板 / 龙头股名。
 *   - 点击行展开该主线成分股(小标签流式排列; 标签上叠 days chip 表示连板数)。
 *   - 30s 轮询刷新(前端节奏, 后端自带 60s 进程内缓存)。
 *   - 数据风格对齐 DarkFlowCards / MainFlowCompareCard: hairline 分隔 + 紧凑(Phase 1 去卡片化)。
 *
 * 2026-11-09 概念维度接入(后端 commit 8403e2bf):
 *   - 后端新增 concept_groups / concept_unranked / concept_stats / data_meta, 与行业维度**并列独立**。
 *   - 一股多概念 ⇒ 概念家数**不可相加**; 龙头只是「本概念内排序第一」, 不是该股专属驱动。
 *     两句口径都原样展示, 不许前端二次归因。
 *   - 行业数据绝不冒充概念: 概念页只读 concept_groups, 为空就说空(不回退行业榜)。
 *   - 缺省视图有热点概念时 = 并列(行业 Top / 概念 Top 同屏), 否则行业。
 *
 * 颜色:
 *   - 涨停数 ≥5 → 主线条染玫瑰色; ≥3 → 青色; <3 不入榜(走单独底部 chip)。
 *   - 龙头股用 Crown 图标 + 琥珀色突出。
 */

export interface MainlineConstituent {
  code: string
  name: string
  days: number
  amount: number
  /** 该股在本池内归属的概念个数(仅概念维度提供) */
  concept_count?: number
}

export interface MainlineGroup {
  name: string
  dimension?: 'industry' | 'concept'
  limit_up_count: number
  ge2_count: number
  max_boards: number
  boards_sum: number
  rungs: number
  score: number | null
  leader: {
    code: string
    name: string
    days: number
    amount: number
  }
  constituents: MainlineConstituent[]
  /** 概念维度: 龙头的其他归属概念(证明龙头非专属驱动) */
  leader_shared_concepts?: string[]
  /** 概念维度: 后端给出的龙头口径原文 */
  leader_basis?: string
  /** 概念维度: 组内仅属该概念的股票占比 */
  exclusive_ratio?: number
}

export interface MainlineFilterStats {
  broad_filtered: number
  below_min: number
  ranked: number
}

/** 概念维度统计口径(后端 concept_stats, 原样展示, 不重算) */
export interface MainlineConceptStats {
  distinct_stocks?: number
  tagged_stocks?: number
  untagged_stocks?: number
  multi_concept_stocks?: number
  member_count_sum?: number
  overlap_excess?: number
  overlap_ratio?: number
  broad_filtered?: number
  summable?: boolean
  min_limitup_for_rank?: number
  /** 旧版本缓存载荷(概念键上线前)标记: 空概念不代表今日无概念 */
  legacy_payload?: boolean
  note?: string
}

/** 数据时点/来源元信息(后端 data_meta) */
export interface MainlineDataMeta {
  as_of?: string | null
  computed_at?: string
  pool_source?: string
  concept_source?: string | null
  concept_source_note?: string
  concept_status?: string
  concept_note?: string
  as_of_note?: string
  dimensions?: { industry?: string; concept?: string }
}

export interface MainlineResp {
  total_groups: number
  /** 涨停池只数(后端 2026-09-23 起提供) */
  total_limit_ups?: number
  /** 入榜门槛(板块需 ≥ 这么多只涨停) */
  min_limitup_for_rank?: number
  ranked_groups: MainlineGroup[]
  unranked?: MainlineGroup[]
  filter_stats?: MainlineFilterStats
  /** ── 概念独立维度(2026-11-09) ── */
  dimension?: string
  concept_groups?: MainlineGroup[]
  concept_breadth_leaders?: Array<{ name: string; limit_up_count: number }>
  concept_unranked?: MainlineGroup[]
  concept_stats?: MainlineConceptStats
  /** available | partial | unavailable(源没给概念字段 ⇒ 不等于今日无概念) */
  concept_status?: string
  concept_note?: string
  data_meta?: MainlineDataMeta
  note?: string
  cache_ts?: number
  /** ── SWR 读侧契约: 返的是不是旧值 + 载荷数据年龄秒 ── */
  stale?: boolean | null
  age_s?: number | null
}

type MainlineView = 'industry' | 'concept' | 'both'

/** 元 → 亿/万 紧凑显示(带符号), NaN → '--' */
function fmtYi(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--'
  const abs = Math.abs(v)
  const sign = v > 0 ? '+' : v < 0 ? '-' : ''
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)}亿`
  return `${sign}${(abs / 1e4).toFixed(0)}万`
}

/** 主线分档位颜色: ≥75 强 / ≥50 中 / 其余弱 */
function scoreTone(score: number | null | undefined): { bar: string; text: string; tag: string } {
  if (score == null || !Number.isFinite(score)) {
    return { bar: 'bg-white/15', text: 'text-muted-foreground', tag: '未入榜' }
  }
  if (score >= 75) return { bar: 'bg-rose-500/80', text: 'text-rose-500', tag: '强主线' }
  if (score >= 50) return { bar: 'bg-cyan-500/80', text: 'text-cyan-500', tag: '次主线' }
  return { bar: 'bg-slate-500/60', text: 'text-muted-foreground', tag: '弱主线' }
}

/** 占比 → 整数百分比(不用 toFixed, 避开 R6 棘轮) */
function fmtPct(ratio: number | null | undefined): string {
  if (ratio == null || !Number.isFinite(ratio)) return '--'
  return `${Math.round(ratio * 100)}%`
}

/**
 * 数据时点/来源/缓存时效条 —— 行业、并列、概念、空态**一律要显示**(2026-11-09 复审)。
 *
 * 之前这三条信息只出现在概念口径条里, 行业视图与空态只剩「本次拉取墙钟」:
 * 用户看不出这张榜是哪天的、是不是旧快照。口径集中在 lib/mainline-meta:
 * as_of 缺失写「时点未知」、stale 缺失写「缓存时效未知」, 绝不拿今天顶上。
 */
function DataMetaLine({
  meta,
  stale,
  ageS,
}: {
  meta?: MainlineDataMeta
  stale?: boolean | null
  ageS?: number | null
}) {
  const asOf = mainlineAsOfDate(meta?.as_of)
  const cache = mainlineCacheText(stale, ageS)
  const clock = meta?.computed_at && meta.computed_at.length >= 19 ? meta.computed_at.slice(11, 19) : null
  return (
    <div
      data-testid="mainline-meta"
      className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px] text-muted-foreground"
    >
      <span title="涨停池的数据交易日(后端 data_meta.as_of; 缺失即未知, 不拿今天顶替)">
        数据日 <b className="font-mono font-normal text-foreground">{mainlineAsOfText(meta?.as_of)}</b>
      </span>
      {clock ? <span className="font-mono">· 算于 {clock}</span> : null}
      <span title="涨停池数据源(后端按条目 source 众数给出)">
        · 池源 <span className="font-mono">{meta?.pool_source || '来源未知'}</span>
      </span>
      {cache ? (
        <span
          data-testid="mainline-cache"
          className={stale === true ? 'text-amber-600 dark:text-amber-400' : undefined}
          title="SWR 读侧: stale=返的是旧值; 年龄=载荷距上次成功聚合的时长"
        >
          · {cache}
        </span>
      ) : null}
      {!asOf && meta?.as_of_note ? (
        <span data-testid="mainline-asof-note" className="text-amber-600 dark:text-amber-400">· {meta.as_of_note}</span>
      ) : null}
    </div>
  )
}

/** 单主线行 */
function MainlineRow({
  g,
  expanded,
  onToggle,
}: {
  g: MainlineGroup
  expanded: boolean
  onToggle: () => void
}) {
  const tone = scoreTone(g.score)
  const score = g.score ?? 0
  const scoreText = g.score != null ? Number(g.score).toFixed(1) : '--'
  const leaderName = g.leader?.name || '--'
  const leaderDays = g.leader?.days ?? 0
  const isConcept = g.dimension === 'concept'
  const shared = g.leader_shared_concepts ?? []

  const leaderTitle = isConcept
    ? `龙头 ${leaderName} ${leaderDays}板 · 组内排序第一，非专属驱动${shared.length ? `；同属 ${shared.join('/')}` : ''}`
    : `龙头 ${leaderName} ${leaderDays}板`

  return (
    <div data-mainline-row-wrap className="border-b border-border">
      {/* 主行: 可点击展开。布局由 market-mainline.css 按**列宽**切换(窄列三行, 宽列一行),
          行内文字一律可省略(min-w-0 + ellipsis), 不允许固定宽度块把邻居挤出容器。 */}
      <button
        type="button"
        onClick={onToggle}
        data-testid="mainline-row"
        className="mainline-row w-full px-2.5 py-2 hover:bg-s2 transition-colors text-left"
      >
        <div className="mainline-row__body">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-muted-foreground shrink-0">
              {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
            </span>
            <span
              data-mainline-name
              className="mainline-row__name min-w-0 text-[14px] font-semibold text-foreground"
              title={g.name}
            >
              {g.name}
            </span>
            <span
              data-mainline-score
              className={`shrink-0 text-[12px] tabular-nums font-mono ${tone.text}`}
              title={`主线分 ${scoreText}`}
            >
              {scoreText}
            </span>
          </div>
          {/* 主线分条形: width = score%, 底色 + 主色叠加 */}
          <div className="mt-1 h-1.5 rounded-full bg-white/10 overflow-hidden">
            <div
              className={`h-full ${tone.bar} transition-all duration-300`}
              style={{ width: `${Math.max(2, Math.min(100, score))}%` }}
            />
          </div>
        </div>
        {/* 关键指标 + 龙头: 窄列整体换行到名称下面, 宽列右对齐贴边 */}
        <div
          data-mainline-metrics
          className="mainline-row__metrics flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[12px] tabular-nums"
        >
          <span
            className="text-stock-up font-mono"
            title={isConcept ? '本概念内涨停家数(与其他概念重叠计数, 不可相加)' : '涨停家数'}
          >
            {g.limit_up_count}家
          </span>
          <span className="text-amber-600 dark:text-amber-400 font-mono" title="最高板">
            {g.max_boards}板
          </span>
          <span className="text-muted-foreground font-mono" title="梯队档位">
            {g.rungs}档
          </span>
          {isConcept ? (
            <span
              className="shrink-0 rounded border border-border px-1 text-[12px] text-muted-foreground"
              title="概念维度: 一只股票可同属多个概念, 各家数不可相加"
            >
              概念
            </span>
          ) : null}
          <span data-mainline-leader className="mainline-row__leader inline-flex items-center gap-1">
            <Crown className="w-3 h-3 shrink-0 text-amber-500" />
            <span
              className="mainline-row__leader-name text-[12px] text-amber-600 dark:text-amber-400"
              title={leaderTitle}
            >
              {leaderName}
            </span>
            {leaderDays > 1 ? (
              <span className="shrink-0 font-mono text-[12px] text-muted-foreground">{leaderDays}B</span>
            ) : null}
          </span>
        </div>
      </button>

      {/* 展开区: 成分股标签流 */}
      {expanded && g.constituents.length > 0 ? (
        <div className="px-2.5 pb-2 pt-1 border-t border-border">
          <div className="flex flex-wrap items-center gap-1.5 mb-1.5 text-[12px] text-muted-foreground">
            <Layers className="w-3 h-3" />
            <span>成分股 {g.constituents.length} 只</span>
            <span className="text-muted-foreground">·</span>
            <span>成交额 {fmtYi(g.constituents.reduce((s, c) => s + (c.amount || 0), 0))}</span>
            {isConcept && g.exclusive_ratio != null ? (
              <>
                <span className="text-muted-foreground">·</span>
                <span title="组内仅属本概念(不与其他概念重叠)的股票占比">独占 {fmtPct(g.exclusive_ratio)}</span>
              </>
            ) : null}
          </div>
          <div className="flex flex-wrap gap-1">
            {g.constituents.map((c) => {
              const isLeader = c.code && g.leader?.code && c.code === g.leader.code
              return (
                <span
                  key={c.code || c.name}
                  className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[12px] transition-colors ${
                    isLeader
                      ? 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400'
                      : 'border-border bg-card text-foreground hover:border-primary/40'
                  }`}
                  title={`${c.name}(${c.code}) ${c.days}板 成交${fmtYi(c.amount)}${
                    isConcept && c.concept_count ? ` · 同属${c.concept_count}个概念` : ''
                  }`}
                >
                  <span className="font-medium truncate max-w-[64px]" title={c.name}>{c.name}</span>
                  {c.days > 1 ? (
                    <span className="text-[12px] font-mono text-stock-up shrink-0">
                      {c.days}B
                    </span>
                  ) : null}
                </span>
              )
            })}
          </div>
          {isConcept ? (
            <p className="mt-1.5 text-[12px] text-muted-foreground leading-relaxed">
              {g.leader_basis || '龙头仅为本概念内排序第一，非该股专属驱动'}
              {shared.length > 0 ? `（龙头同属：${shared.join('、')}）` : ''}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

/** 维度切换: 行业 / 概念 / 并列 */
function DimensionTabs({
  view,
  onChange,
  industryCount,
  conceptCount,
}: {
  view: MainlineView
  onChange: (v: MainlineView) => void
  industryCount: number
  conceptCount: number
}) {
  const items: Array<{ key: MainlineView; label: string }> = [
    { key: 'industry', label: `行业 ${industryCount}` },
    { key: 'concept', label: `概念 ${conceptCount}` },
    { key: 'both', label: '并列' },
  ]
  return (
    <div role="tablist" aria-label="主线维度" className="flex items-center gap-0.5">
      {items.map((it) => {
        const active = view === it.key
        return (
          <button
            key={it.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(it.key)}
            className={`rounded border px-1.5 py-0.5 text-[12px] transition-colors ${
              active
                ? 'border-primary/40 bg-s2 text-foreground'
                : 'border-border bg-transparent text-muted-foreground hover:text-foreground'
            }`}
          >
            {it.label}
          </button>
        )
      })}
    </div>
  )
}

/** 概念口径条: 独立维度 / 不可相加 / 去重计数 / 概念源 / 龙头口径(有概念数据才显示) */
function ConceptCaliber({ stats, meta, poolSize, empty }: {
  stats?: MainlineConceptStats
  meta?: MainlineDataMeta
  poolSize?: number
  /** 概念榜为空: 原因由空态那行统一说, 这里不再重复整段 note */
  empty?: boolean
}) {
  if (!stats && !meta) return null
  const parts: string[] = []
  if (stats?.distinct_stocks != null) parts.push(`归属去重 ${stats.distinct_stocks} 只`)
  if (stats?.member_count_sum != null) {
    parts.push(
      `家数合计 ${stats.member_count_sum}(重叠 +${stats.overlap_excess ?? 0})`,
    )
  }
  if (poolSize != null && stats?.distinct_stocks != null && poolSize > stats.distinct_stocks) {
    parts.push(`未标概念 ${Math.max(0, poolSize - stats.distinct_stocks)} 只`)
  }
  const source = meta?.concept_source ?? '概念源未提供'
  const status = meta?.concept_status ?? (stats?.legacy_payload ? 'unavailable' : undefined)
  const note = meta?.concept_note

  return (
    <div data-testid="concept-caliber" className="mb-2 rounded-md bg-s2 px-2 py-1.5 text-[12px] text-muted-foreground leading-relaxed">
      <div className="flex items-start gap-1.5">
        <Info className="w-3 h-3 mt-0.5 shrink-0" />
        <div className="min-w-0">
          <div>
            <span className="text-foreground">概念为独立维度</span>
            <span className="mx-1">·</span>
            <span title="不同概念会重复计入同一只股票">各概念家数不可相加</span>
            {parts.length > 0 ? (
              <>
                <span className="mx-1">·</span>
                <span className="tabular-nums">{parts.join(' · ')}</span>
              </>
            ) : null}
          </div>
          <div className="mt-0.5">
            概念源 <span className="font-mono">{source}</span>
            {status && status !== 'available' ? (
              <span
                data-testid="concept-caliber-status"
                className="ml-1 text-amber-600 dark:text-amber-400"
                title={note || undefined}
              >
                · {status === 'partial' ? '部分股票缺概念归属' : '源未提供概念字段'}
              </span>
            ) : null}
            <span className="mx-1">·</span>
            <span title="概念榜的「龙头」只是本概念内排序第一，不是这只票的专属驱动">龙头仅组内排序</span>
          </div>
          {!empty && note ? (
            <div data-testid="concept-caliber-note" className="mt-0.5 text-amber-600 dark:text-amber-400">{note}</div>
          ) : null}
        </div>
      </div>
    </div>
  )
}

/**
 * 概念榜为空时说清**是哪种空**(2026-11-09 复审):
 *   ① 有概念、只是都没到 ≥N 只涨停门槛 → 「未入榜」(后端 concept_unranked 就是证据);
 *   ② 一只票都没概念归属(源没给概念字段) → 「无归属」并引用后端 concept_note,
 *      绝不说成「今日没有概念题材」, 更不能拿行业榜顶上。
 * 之前一律写"暂无概念归属数据", 把 ① 误报成 ②。
 */
type ConceptEmptyKind = 'below_threshold' | 'no_attribution'

function conceptEmptyInfo(args: {
  status?: string | null
  note?: string | null
  unranked: number
  distinctStocks?: number | null
  minLimitUp: number
}): { kind: ConceptEmptyKind; text: string } {
  const { status, note, unranked, distinctStocks, minLimitUp } = args
  const distinct = distinctStocks ?? 0
  if (unranked > 0) {
    return {
      kind: 'below_threshold',
      text: `概念榜为空: ${unranked} 个概念有涨停但未达「≥${minLimitUp} 只」门槛(见下方未入榜)`,
    }
  }
  if (status === 'unavailable' || distinct <= 0) {
    return {
      kind: 'no_attribution',
      text:
        (note && note.trim()) ||
        '概念归属缺失: 数据源未提供概念字段, 不等于今日没有概念题材(未以行业数据代替)',
    }
  }
  return {
    kind: 'below_threshold',
    text: `概念榜为空: ${distinct} 只有概念归属的涨停股分散在各概念, 但没有概念达到「≥${minLimitUp} 只涨停」门槛`,
  }
}

/** 概念未入榜 chips(概念单列空态/非空态都要列, 之前只在非空分支才有) */
function ConceptUnrankedChips({ groups, minLimitUp }: { groups: MainlineGroup[]; minLimitUp: number }) {
  if (groups.length === 0) return null
  return (
    <div className="mt-2 pt-2 border-t border-border">
      <div className="text-[12px] text-muted-foreground mb-1">
        概念未入榜(涨停 {`<`}{minLimitUp} 家): {groups.length} 个
      </div>
      <div className="flex flex-wrap gap-1">
        {groups.map((g) => (
          <span
            key={g.name}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-1.5 py-0.5 text-[12px] text-muted-foreground"
            title={`${g.name} ${g.limit_up_count}家 最高${g.max_boards}板 · 概念家数与其他概念重叠, 不可相加`}
          >
            <span>{g.name}</span>
            <span className="text-stock-up font-mono">{g.limit_up_count}家</span>
            <span className="text-muted-foreground">/</span>
            <span className="text-amber-600 dark:text-amber-400 font-mono">{g.max_boards}B</span>
          </span>
        ))}
      </div>
    </div>
  )
}

/** 一列榜单(行业或概念), 供并列视图复用 */
function DimensionColumn({
  title,
  groups,
  unranked,
  expandedKey,
  onToggle,
  limit,
  viewKey,
  emptyText,
}: {
  title: string
  groups: MainlineGroup[]
  unranked: MainlineGroup[]
  expandedKey: string | null
  onToggle: (k: string) => void
  limit: number
  viewKey: string
  /** 空态原因(概念列由 conceptEmptyInfo 给, 不许写死"暂无概念归属数据") */
  emptyText?: string
}) {
  const rows = groups.slice(0, limit)
  return (
    <div data-mainline-col data-testid="mainline-col" className="mainline-col">
      <div className="flex items-center justify-between px-2.5 pb-1 text-[12px] text-muted-foreground">
        <span className="min-w-0 truncate" title={title}>{title}</span>
        <span className="shrink-0 tabular-nums">Top {rows.length}</span>
      </div>
      {rows.length === 0 ? (
        <p
          data-testid={viewKey === 'concept' ? 'concept-empty-reason' : 'industry-empty-reason'}
          className="px-2.5 py-2 text-[12px] text-muted-foreground"
        >
          {emptyText || (viewKey === 'concept' ? '概念榜为空' : '暂无行业主线数据')}
        </p>
      ) : (
        rows.map((g) => {
          const key = `${viewKey}:${g.name}`
          return (
            <MainlineRow
              key={key}
              g={g}
              expanded={expandedKey === key}
              onToggle={() => onToggle(key)}
            />
          )
        })
      )}
      {unranked.length > 0 ? (
        <div className="px-2.5 pt-1.5 text-[12px] text-muted-foreground">
          未入榜 {unranked.length} 个：{unranked.slice(0, 6).map((g) => g.name).join('、')}
          {unranked.length > 6 ? ' …' : ''}
        </div>
      ) : null}
    </div>
  )
}

export default function MarketMainlineCard() {
  const [data, setData] = useState<MainlineResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [expandedKey, setExpandedKey] = useState<string | null>(null)
  const [view, setView] = useState<MainlineView | null>(null)
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    setError('')
    try {
      const res = await fetchAPI<MainlineResp>('/market/mainline', {
        cacheMode: 'reload', // 实时, 跳过 GET 缓存
      })
      if (mountedRef.current) {
        setData(res)
        setUpdatedAt(new Date())
      }
    } catch (e) {
      if (mountedRef.current) setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])
  // 页签 keep-alive(2026-10-15): 隐藏页签停表, 回前台补一次(30s 刷新, 后端 60s 缓存兜底节奏)。
  usePausedInterval(() => {
    setLoading(true)
    void load()
  }, 30000, { deps: [load] })

  if (loading && !data) {
    return (
      <div role="status" data-mainline-card className="mainline-card min-w-0">
        <SectionHeader title="市场主线" className="mainline-header" />
        <p className="py-6 text-[12px] text-muted-foreground">正在加载市场主线…</p>
      </div>
    )
  }

  const ranked = data?.ranked_groups ?? []
  const unranked = data?.unranked ?? []
  const conceptRanked = data?.concept_groups ?? []
  const conceptUnranked = data?.concept_unranked ?? []
  const conceptStats = data?.concept_stats
  const meta = data?.data_meta
  const stats = data?.filter_stats
  const note = data?.note
  const totalGroups = data?.total_groups ?? 0
  const hasConcept = conceptRanked.length > 0 || conceptUnranked.length > 0
  // 概念可用性跟着后端走(旧缓存载荷没有顶层键时回落 data_meta)
  const conceptStatus = data?.concept_status ?? meta?.concept_status
  const conceptNote = data?.concept_note || meta?.concept_note || ''
  // 缺省: 有概念数据就并列(热点概念不被行业榜遮住); 没有概念数据老实停在行业
  const activeView: MainlineView = view ?? (hasConcept ? 'both' : 'industry')

  /** 空态的真实原因(不许一律赖"非交易日"):
   *  - 一只涨停都没有 → 可能真非交易日/源故障, 如实说"暂无涨停池数据";
   *  - 有涨停但没有板块够格 → 说清"池子 N 只、分属 M 个板块、未达门槛";
   *  - 门槛值来自后端 filter_stats, 拿不到就不写死数字。 */
  const minPerGroup = data?.min_limitup_for_rank ?? 3
  const poolSize = data?.total_limit_ups
  const honestReason =
    totalGroups === 0
      ? '暂无涨停池数据(数据源不可用或非交易日)'
      : poolSize != null
        ? `今日涨停池 ${poolSize} 只, 分属 ${totalGroups} 个板块, 均未达到「≥${minPerGroup} 只涨停」的入榜门槛`
        : `今日 ${totalGroups} 个涨停板块均未达到「≥${minPerGroup} 只涨停」的入榜门槛`

  /** 概念榜为空的原因: 未入榜(有概念、没到门槛) vs 无归属(源没给概念字段) */
  const conceptEmpty = conceptEmptyInfo({
    status: conceptStatus,
    // 顶层/ data_meta 的 concept_note 优先; 旧载荷只有 concept_stats.note, 也照样引用
    note: conceptNote || conceptStats?.note,
    unranked: conceptUnranked.length,
    distinctStocks: conceptStats?.distinct_stocks,
    minLimitUp: conceptStats?.min_limitup_for_rank ?? minPerGroup,
  })

  if (ranked.length === 0 && conceptRanked.length === 0) {
    return (
      <div data-mainline-card data-testid="mainline-empty" className="mainline-card border-t border-border pt-2.5">
        <SectionHeader
          title="市场主线"
          className="mainline-header"
          action={
            updatedAt ? (
              <span
                className="text-[12px] text-muted-foreground font-mono"
                title="本次拉取的墙钟时间(不是数据日; 数据日见下方)"
              >
                {updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}
              </span>
            ) : undefined
          }
        />
        {/* 2026-09-23 修: 原来无论什么原因都写"非交易日/涨停池为空" —— 而当日实测是
            "涨停池有 33 只、分属 26 个板块, 但没有任何板块达到 ≥3 只涨停的入榜门槛"。
            归因说错比不显示更糟: 用户会以为系统没数据/没开盘。现在按 filter_stats 说真话。
            2026-11-09 复审: 空态同样要显示数据日与缓存时效, 否则连"这份空是哪天的"都不知道。 */}
        <DataMetaLine meta={meta} stale={data?.stale} ageS={data?.age_s} />
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <AlertTriangle className="w-3.5 h-3.5" />
          <span>{error || note || honestReason}</span>
        </div>
        {hasConcept ? null : (
          <p data-testid="concept-status-note" className="mt-1 text-[12px] text-muted-foreground">
            概念维度: {conceptEmpty.text}
          </p>
        )}
      </div>
    )
  }

  const top = ranked.slice(0, 10)
  const conceptTop = conceptRanked.slice(0, 10)
  const rest = ranked.slice(10)
  const conceptRest = conceptRanked.slice(10)

  return (
    <div data-mainline-card className="mainline-card border-t border-border pt-2.5">
      <SectionHeader
        title="市场主线"
        className="mainline-header"
        action={
          <>
            <DimensionTabs
              view={activeView}
              onChange={setView}
              industryCount={ranked.length}
              conceptCount={conceptRanked.length}
            />
            {activeView === 'concept' || activeView === 'both' ? (
              <span className="mainline-header-summary min-w-0 truncate text-[12px] text-muted-foreground">
                {activeView === 'concept' ? (
                  <>概念 Top {conceptTop.length}{conceptRest.length > 0 ? ` / ${conceptRanked.length}` : ''}</>
                ) : (
                  <>行业 {top.length} vs 概念 {conceptTop.length}</>
                )}
              </span>
            ) : (
              <span className="mainline-header-summary min-w-0 truncate text-[12px] text-muted-foreground">
                Top {top.length}
                {rest.length > 0 ? ` / ${ranked.length}` : ''}
                {stats ? (
                  <span className="ml-1.5 text-muted-foreground">
                    (过滤 {stats.broad_filtered} 宽基 / 未入榜 {stats.below_min} / 共 {totalGroups} 题材)
                  </span>
                ) : null}
              </span>
            )}
            <span className="text-[12px] text-muted-foreground font-mono">
              {updatedAt ? updatedAt.toLocaleTimeString('zh-CN', { hour12: false }) : ''}
            </span>
            <button
              type="button"
              title="刷新"
              onClick={() => {
                setLoading(true)
                void load()
              }}
              className="text-muted-foreground hover:text-foreground transition-colors"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </>
        }
      />

      {/* 数据日 / 算于 / 池源 / 缓存时效 —— 所有视图共用, 不再只有概念页才有时点 */}
      <DataMetaLine meta={meta} stale={data?.stale} ageS={data?.age_s} />

      {/* 错误细条: 与 data 并存(上次成功数据不丢) */}
      {error ? (
        <div className="mb-2 flex items-center gap-1.5 text-[12px] text-amber-600 dark:text-amber-500">
          <AlertTriangle className="w-3 h-3" />
          <span>本次刷新失败: {error} — 仍展示上次成功数据</span>
        </div>
      ) : null}

      {/* 行业视图: 概念不可用/部分缺失也要可见, 否则默认视图会让人以为"今日无概念" */}
      {activeView === 'industry' && conceptStatus && conceptStatus !== 'available' ? (
        <p data-testid="concept-status-note" className="mb-2 text-[12px] text-muted-foreground">
          概念维度: {conceptNote || (conceptStatus === 'partial' ? '部分涨停股缺概念归属(数据源未提供)，未用行业数据补造' : '数据源未提供概念字段，不等于今日没有概念题材(未以行业数据代替)')}
        </p>
      ) : null}

      {/* 概念口径条: 概念源/重叠不可相加/龙头仅组内排序 */}
      {activeView === 'concept' || activeView === 'both' ? (
        <ConceptCaliber stats={conceptStats} meta={meta} poolSize={poolSize} empty={conceptTop.length === 0} />
      ) : null}

      {activeView !== 'industry' && (data?.concept_breadth_leaders?.length ?? 0) > 0 ? (
        <div data-testid="concept-breadth-leaders" className="mb-2 text-[12px] text-muted-foreground">
          概念涨停家数靠前：{data?.concept_breadth_leaders?.map((g) => `${g.name} ${g.limit_up_count}家`).join('、')}
        </div>
      ) : null}

      {/* 并列视图: 行业 / 概念 同屏, 各 Top6 */}
      {activeView === 'both' ? (
        <div data-testid="mainline-both-grid" className="mainline-both-grid">
          <DimensionColumn
            title="行业 · 单一归属"
            groups={ranked}
            unranked={unranked}
            expandedKey={expandedKey}
            onToggle={(k) => setExpandedKey(expandedKey === k ? null : k)}
            limit={6}
            viewKey="industry"
          />
          <DimensionColumn
            title="概念 · 独立维度(不可相加)"
            groups={conceptRanked}
            unranked={conceptUnranked}
            expandedKey={expandedKey}
            onToggle={(k) => setExpandedKey(expandedKey === k ? null : k)}
            limit={6}
            viewKey="concept"
            emptyText={conceptEmpty.text}
          />
        </div>
      ) : null}

      {/* 概念单列 */}
      {activeView === 'concept' ? (
        conceptTop.length === 0 ? (
          <div data-testid="concept-empty" className="py-1">
            <p data-testid="concept-empty-reason" className="py-1 text-[12px] text-muted-foreground">
              {conceptEmpty.text}
              <span className="block mt-0.5">未以行业数据代替概念。</span>
            </p>
            <ConceptUnrankedChips groups={conceptUnranked} minLimitUp={conceptStats?.min_limitup_for_rank ?? minPerGroup} />
          </div>
        ) : (
          <>
            <div data-testid="mainline-col" data-mainline-col className="mainline-col space-y-1 lg:max-h-[300px] lg:overflow-y-auto">
              {conceptTop.map((g) => {
                const key = `concept:${g.name}`
                return (
                  <MainlineRow
                    key={key}
                    g={g}
                    expanded={expandedKey === key}
                    onToggle={() => setExpandedKey(expandedKey === key ? null : key)}
                  />
                )
              })}
            </div>
            {conceptRest.length > 0 ? (
              <details className="mt-2">
                <summary className="cursor-pointer text-[12px] text-muted-foreground hover:text-foreground select-none">
                  展开 11-20 名 ({conceptRest.length})
                </summary>
                <div className="space-y-1 mt-1">
                  {conceptRest.map((g) => {
                    const key = `concept:${g.name}`
                    return (
                      <MainlineRow
                        key={key}
                        g={g}
                        expanded={expandedKey === key}
                        onToggle={() => setExpandedKey(expandedKey === key ? null : key)}
                      />
                    )
                  })}
                </div>
              </details>
            ) : null}
            <ConceptUnrankedChips
              groups={conceptUnranked}
              minLimitUp={conceptStats?.min_limitup_for_rank ?? minPerGroup}
            />
          </>
        )
      ) : null}

      {/* 行业单列 */}
      {activeView === 'industry' ? (
        <>
          <div data-testid="mainline-col" data-mainline-col className="mainline-col space-y-1 lg:max-h-[300px] lg:overflow-y-auto">
            {top.map((g) => {
              const key = `industry:${g.name}`
              return (
                <MainlineRow
                  key={key}
                  g={g}
                  expanded={expandedKey === key}
                  onToggle={() => setExpandedKey(expandedKey === key ? null : key)}
                />
              )
            })}
          </div>

          {/* 11-20 名折叠展示: 默认收起, 留个角标 */}
          {rest.length > 0 ? (
            <details className="mt-2">
              <summary className="cursor-pointer text-[12px] text-muted-foreground hover:text-foreground select-none">
                展开 11-20 名 ({rest.length})
              </summary>
              <div className="space-y-1 mt-1">
                {rest.map((g) => {
                  const key = `industry:${g.name}`
                  return (
                    <MainlineRow
                      key={key}
                      g={g}
                      expanded={expandedKey === key}
                      onToggle={() => setExpandedKey(expandedKey === key ? null : key)}
                    />
                  )
                })}
              </div>
            </details>
          ) : null}

          {/* 未入榜 chip(涨停 <3 家) */}
          {unranked.length > 0 ? (
            <div className="mt-2 pt-2 border-t border-border">
              <div className="text-[12px] text-muted-foreground mb-1">
                未入榜(涨停 {`<`}{minPerGroup} 家): {unranked.length} 个
              </div>
              <div className="flex flex-wrap gap-1">
                {unranked.map((g) => (
                  <span
                    key={g.name}
                    className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-1.5 py-0.5 text-[12px] text-muted-foreground"
                    title={`${g.name} ${g.limit_up_count}家 最高${g.max_boards}板`}
                  >
                    <span>{g.name}</span>
                    <span className="text-stock-up font-mono">{g.limit_up_count}家</span>
                    <span className="text-muted-foreground">/</span>
                    <span className="text-amber-600 dark:text-amber-400 font-mono">{g.max_boards}B</span>
                  </span>
                ))}
              </div>
            </div>
          ) : null}

          {/* 有概念数据但用户在看行业: 给一条可见入口, 不静默藏热点 */}
          {hasConcept && ranked.length === 0 ? (
            <p className="mt-2 text-[12px] text-muted-foreground">概念维度有数据, 切到「概念」查看</p>
          ) : null}
        </>
      ) : null}
    </div>
  )
}
