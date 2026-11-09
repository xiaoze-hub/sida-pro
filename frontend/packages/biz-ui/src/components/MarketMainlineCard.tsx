import { useCallback, useEffect, useRef, useState } from 'react'
import { usePausedInterval } from '@sida/base-ui'
import { RefreshCw, AlertTriangle, ChevronDown, ChevronRight, Layers, Crown, Info } from 'lucide-react'
import { fetchAPI } from '@sida/api'
import SectionHeader from '@sida/biz-ui/components/SectionHeader'

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
  note?: string
}

/** 数据时点/来源元信息(后端 data_meta) */
export interface MainlineDataMeta {
  as_of?: string | null
  computed_at?: string
  pool_source?: string
  concept_source?: string | null
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
  concept_unranked?: MainlineGroup[]
  concept_stats?: MainlineConceptStats
  data_meta?: MainlineDataMeta
  note?: string
  cache_ts?: number
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

/** 时点显示: '2026-11-09' → '11-09'; 完整时间戳保留原样的日期段 */
function fmtAsOf(v: string | null | undefined): string {
  if (!v) return '时点未知'
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(v)
  if (m) return `${m[2]}-${m[3]}`
  return v.slice(0, 16)
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
  const leaderName = g.leader?.name || '--'
  const leaderDays = g.leader?.days ?? 0
  const isConcept = g.dimension === 'concept'
  const shared = g.leader_shared_concepts ?? []

  const leaderTitle = isConcept
    ? `龙头 ${leaderName} ${leaderDays}板 · 组内排序第一，非专属驱动${shared.length ? `；同属 ${shared.join('/')}` : ''}`
    : `龙头 ${leaderName} ${leaderDays}板`

  return (
    <div className="border-b border-border overflow-hidden">
      {/* 主行: 可点击展开 */}
      <button
        type="button"
        onClick={onToggle}
        className="w-full px-2.5 py-2 flex items-center gap-2 hover:bg-s2 transition-colors text-left"
      >
        <span className="text-muted-foreground shrink-0">
          {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[14px] font-semibold text-foreground truncate" title={g.name}>{g.name}</span>
            <span className={`text-[12px] tabular-nums font-mono ${tone.text}`}>
              {g.score != null ? Number(g.score).toFixed(1) : '--'}
            </span>
            {isConcept ? (
              <span
                className="text-[12px] text-muted-foreground border border-border rounded px-1 shrink-0"
                title="概念维度: 一只股票可同属多个概念, 各家数不可相加"
              >
                概念
              </span>
            ) : null}
          </div>
          {/* 主线分条形: width = score%, 底色 + 主色叠加 */}
          <div className="mt-1 h-1.5 rounded-full bg-white/10 overflow-hidden">
            <div
              className={`h-full ${tone.bar} transition-all duration-300`}
              style={{ width: `${Math.max(2, Math.min(100, score))}%` }}
            />
          </div>
        </div>
        {/* 关键指标: 涨停数 / 最高板 */}
        <div className="flex items-center gap-2.5 shrink-0 text-[12px] tabular-nums">
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
        </div>
        {/* 龙头 */}
        <div className="hidden md:flex items-center gap-1 shrink-0 max-w-[100px]">
          <Crown className="w-3 h-3 text-amber-500 shrink-0" />
          <span className="text-[12px] text-amber-600 dark:text-amber-400 truncate" title={leaderTitle}>
            {leaderName}
            {leaderDays > 1 ? <span className="ml-0.5 text-muted-foreground">{leaderDays}B</span> : null}
          </span>
        </div>
      </button>

      {/* 展开区: 成分股标签流 */}
      {expanded && g.constituents.length > 0 ? (
        <div className="px-2.5 pb-2 pt-1 border-t border-border">
          <div className="flex items-center gap-1.5 mb-1.5 text-[12px] text-muted-foreground">
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

/** 数据口径条: 时点 / 来源 / 概念不可相加 (有概念数据才显示) */
function ConceptCaliber({ stats, meta, poolSize }: {
  stats?: MainlineConceptStats
  meta?: MainlineDataMeta
  poolSize?: number
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
  const asOf = meta?.as_of ? fmtAsOf(meta.as_of) : '时点未知'
  const source = meta?.concept_source ?? '概念源未提供'

  return (
    <div className="mb-2 rounded-md bg-s2 px-2 py-1.5 text-[12px] text-muted-foreground leading-relaxed">
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
            时点 <span className="font-mono text-foreground">{asOf}</span>
            {meta?.computed_at ? <span className="ml-1 font-mono">(算于 {meta.computed_at.slice(11, 19)})</span> : null}
            <span className="mx-1">·</span>
            源 <span className="font-mono">{source}</span>
            <span className="mx-1">·</span>
            <span title="概念榜的「龙头」只是本概念内排序第一，不是这只票的专属驱动">龙头仅组内排序</span>
          </div>
          {!meta?.as_of && meta?.as_of_note ? <div className="mt-0.5">{meta.as_of_note}</div> : null}
        </div>
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
}: {
  title: string
  groups: MainlineGroup[]
  unranked: MainlineGroup[]
  expandedKey: string | null
  onToggle: (k: string) => void
  limit: number
  viewKey: string
}) {
  const rows = groups.slice(0, limit)
  return (
    <div className="min-w-0">
      <div className="flex items-center justify-between px-2.5 pb-1 text-[12px] text-muted-foreground">
        <span>{title}</span>
        <span className="tabular-nums">Top {rows.length}</span>
      </div>
      {rows.length === 0 ? (
        <p className="px-2.5 py-2 text-[12px] text-muted-foreground">
          {viewKey === 'concept' ? '暂无概念归属数据' : '暂无行业主线数据'}
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
    return <div role="status" className="min-w-0"><SectionHeader title="市场主线" /><p className="py-6 text-[12px] text-muted-foreground">正在加载市场主线…</p></div>
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

  if (ranked.length === 0 && conceptRanked.length === 0) {
    return (
      <div className="border-t border-border pt-2.5">
        <SectionHeader
          title="市场主线"
          action={
            updatedAt ? (
              <span className="text-[12px] text-muted-foreground font-mono">
                {updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}
              </span>
            ) : undefined
          }
        />
        {/* 2026-09-23 修: 原来无论什么原因都写"非交易日/涨停池为空" —— 而当日实测是
            "涨停池有 33 只、分属 26 个板块, 但没有任何板块达到 ≥3 只涨停的入榜门槛"。
            归因说错比不显示更糟: 用户会以为系统没数据/没开盘。现在按 filter_stats 说真话。 */}
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <AlertTriangle className="w-3.5 h-3.5" />
          <span>{error || note || honestReason}</span>
        </div>
        {hasConcept ? null : (
          <p className="mt-1 text-[12px] text-muted-foreground">概念维度: 暂无概念归属数据（未以行业数据代替）</p>
        )}
      </div>
    )
  }

  const top = ranked.slice(0, 10)
  const conceptTop = conceptRanked.slice(0, 10)
  const rest = ranked.slice(10)
  const conceptRest = conceptRanked.slice(10)

  return (
    <div className="border-t border-border pt-2.5">
      <SectionHeader
        title="市场主线"
        action={
          <>
            <DimensionTabs
              view={activeView}
              onChange={setView}
              industryCount={ranked.length}
              conceptCount={conceptRanked.length}
            />
            {activeView === 'concept' || activeView === 'both' ? (
              <span className="hidden lg:inline text-[12px] text-muted-foreground">
                {activeView === 'concept' ? (
                  <>概念 Top {conceptTop.length}{conceptRest.length > 0 ? ` / ${conceptRanked.length}` : ''}</>
                ) : (
                  <>行业 {top.length} vs 概念 {conceptTop.length}</>
                )}
              </span>
            ) : (
              <span className="text-[12px] text-muted-foreground">
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

      {/* 错误细条: 与 data 并存(上次成功数据不丢) */}
      {error ? (
        <div className="mb-2 flex items-center gap-1.5 text-[12px] text-amber-600 dark:text-amber-500">
          <AlertTriangle className="w-3 h-3" />
          <span>本次刷新失败: {error} — 仍展示上次成功数据</span>
        </div>
      ) : null}

      {/* 概念口径条: 时点/来源/重叠不可相加/龙头仅组内排序 */}
      {activeView === 'concept' || activeView === 'both' ? (
        <ConceptCaliber stats={conceptStats} meta={meta} poolSize={poolSize} />
      ) : null}

      {/* 并列视图: 行业 / 概念 同屏, 各 Top6 */}
      {activeView === 'both' ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-3">
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
          />
        </div>
      ) : null}

      {/* 概念单列 */}
      {activeView === 'concept' ? (
        conceptTop.length === 0 ? (
          <p className="py-3 text-[12px] text-muted-foreground">
            {conceptStats?.note || `概念榜为空: 涨停池 ${poolSize ?? '--'} 只, 没有概念达到「≥${minPerGroup} 只涨停」的入榜门槛`}
            <span className="block mt-0.5">未以行业数据代替概念。</span>
          </p>
        ) : (
          <>
            <div className="space-y-1 lg:max-h-[300px] lg:overflow-y-auto">
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
            {conceptUnranked.length > 0 ? (
              <div className="mt-2 pt-2 border-t border-border">
                <div className="text-[12px] text-muted-foreground mb-1">
                  概念未入榜(涨停 {`<`}{conceptStats?.min_limitup_for_rank ?? minPerGroup} 家): {conceptUnranked.length} 个
                </div>
                <div className="flex flex-wrap gap-1">
                  {conceptUnranked.map((g) => (
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
            ) : null}
          </>
        )
      ) : null}

      {/* 行业单列 */}
      {activeView === 'industry' ? (
        <>
          <div className="space-y-1 lg:max-h-[300px] lg:overflow-y-auto">
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
