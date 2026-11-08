import { useCallback, useEffect, useRef, useState } from 'react'
import { usePausedInterval } from '@sida/base-ui'
import { useECharts } from '@sida/biz-ui/hooks/useECharts'
import { readStockColors } from '@sida/biz-ui/lib/stock-colors'
import { fetchAPI } from '@sida/api'

/**
 * 全市场涨跌分布双向柱状图(v0.4.7, Dashboard 大盘区)。
 * v0.5.0: 使用 useECharts hook (ResizeObserver 替代 window.resize)。
 *
 * perf(2026-10-02, 照 404c2d5 首屏手法): 后端自 2026-09-18 起已**异步化** —— 缓存未命中时
 * 立即返回 `{pending:true, 全 0}` 并在后台线程算(约数秒到数十秒), 前端需自行回收结果。
 * 此前组件只在 **60s 定时器**上重拉: 后台几秒就算完的分布, 首屏画布要等到下一个 60s tick 才出现
 * (B1 走查「涨跌分布 canvas 迟迟不 settle」的代码侧成因)。
 * 现在:
 *   1. 先出壳 —— 首次计算期间占满同高度(160px)+ 显式文案, 不跳版、不用 0 假装有分布;
 *   2. 后补数 —— 命中 `pending` 时以 1.5s 间隔回补轮询, 算完立即画, 不再等 60s;
 *   3. 竞态守卫 —— 请求序号(seqRef)丢弃过期响应, 慢响应不会覆盖新结果;
 *   4. 失败与空数据显式区分 —— 失败态可见, 缺数据透传后端 note, 绝不渲染成 0 柱。
 *
 * 2026-11-08 计算可视性与时效(本轮):
 *   - 后台计算有上限: 超过 60s 仍是 pending → 显式「计算超时」而非无限装作在算;
 *   - 加载中 / 后台计算中 / 超时 / 失败 四种文案互不混淆, 超时与失败都带「重试」入口;
 *   - 拿到真快照时展示后端给的 `captured_at` / `age_s`, 后端没给就写「数据时效未知」,
 *     绝不用前端当前时间倒推一个假时刻; `stale` 快照明示「可能已过期」;
 *   - 已有数据但本次刷新失败 → 保留旧图 + 显式标注, 不让失败把画布清空。
 */

interface BreadthItem {
  bucket: string
  count: number
}
/** 后端 `/market-data/breadth-distribution` 的响应(见 src/web/api/market_data.py) */
interface BreadthResp {
  count: number
  total: number
  items: BreadthItem[]
  note?: string
  /** 后端异步任务尚未算完 */
  pending?: boolean
  /** 快照采集时刻(后端本地时间 ISO, 无时区) */
  captured_at?: string | null
  /** 快照年龄(秒), 后端 _breadth_with_age 计算 */
  age_s?: number | null
  /** 后端判定已陈旧(>15min) */
  stale?: boolean
  source?: string
}

/** 常规刷新间隔(与后端 300s 缓存节奏一致, 保持原 60s 不变) */
const REFRESH_MS = 60_000
/** 命中 pending 时的回补间隔/上限(≈60s, 覆盖首次后台计算窗口) */
const BACKFILL_MS = 1_500
const BACKFILL_MAX_TRIES = 40
/** 该端点后端立即返回(异步), 显式短超时: 慢时快速失败显式降级, 不霸占连接 */
const REQUEST_TIMEOUT_MS = 10_000
/** 后台计算的等待上限: 与回补轮询总窗口一致, 超过即显式「计算超时」 */
const PENDING_TIMEOUT_MS = BACKFILL_MS * BACKFILL_MAX_TRIES
/** 明显偏慢的提示阈值(正常后台计算约数秒) */
const PENDING_SLOW_MS = 20_000

/** 快照时效来源: 只用后端给的字段, 前端不臆测 */
export interface BreadthFreshnessMeta {
  captured_at?: string | null
  age_s?: number | null
  stale?: boolean
  source?: string
}

function formatClock(d: Date): string {
  const p2 = (n: number) => String(n).padStart(2, '0')
  // 快照是同交易日内的分钟级数据, 月-日 + 时:分足够定位; 不追加年份避免长文案。
  return `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`
}

function formatAge(ageS: number): string {
  const s = Math.max(0, Math.floor(ageS))
  if (s < 60) return `${s}秒前`
  if (s < 3600) return `${Math.floor(s / 60)}分钟前`
  if (s < 86_400) return `${Math.floor(s / 3600)}小时前`
  return `${Math.floor(s / 86_400)}天前`
}

/**
 * 快照时效文案(纯函数, 供 UI 与单测共用)。
 * - 有 captured_at 且可解析 → 「数据时间 MM-DD HH:MM」;
 * - 有 age_s → 追加相对时效; 只有 age_s 时写「数据更新于 X前」;
 * - 两者都没有/都不可解析 → 「数据时效未知」(不拿当前时间编一个时刻);
 * - stale=true → 追加「可能已过期」。
 */
export function formatBreadthFreshness(meta: BreadthFreshnessMeta | null | undefined): string {
  const raw = typeof meta?.captured_at === 'string' ? meta.captured_at.trim() : ''
  let at: Date | null = null
  if (raw) {
    const d = new Date(raw)
    if (!Number.isNaN(d.getTime())) at = d
  }
  const ageS =
    typeof meta?.age_s === 'number' && Number.isFinite(meta.age_s) && meta.age_s >= 0 ? meta.age_s : null

  let text = ''
  if (at) {
    text = `数据时间 ${formatClock(at)}`
    if (ageS != null) text += ` · ${formatAge(ageS)}`
  } else if (ageS != null) {
    text = `数据更新于 ${formatAge(ageS)}`
  } else {
    return '数据时效未知'
  }
  if (meta?.stale === true) text += ' · 可能已过期'
  return text
}

/** 桶 → 是否下跌侧(A股惯例): 含“跌”或负区间为跌侧，其余(含平盘)为涨侧。 */
function isDownBucket(bucket: string): boolean {
  if (bucket === '-1~1%') return false
  const neg = ['跌停', '<-5%', '-5~-3%', '-3~-1%']
  return neg.includes(bucket) || bucket.startsWith('-') || bucket.startsWith('<')
}

export default function BreadthDistributionChart() {
  const { ref, chartRef } = useECharts()
  const [items, setItems] = useState<BreadthItem[] | null>(null)
  const [note, setNote] = useState('')
  /** 快照时效元数据(仅来自后端响应) */
  const [meta, setMeta] = useState<BreadthFreshnessMeta | null>(null)
  /** 后端后台任务尚未算完 → 显示“计算中”占位并回补 */
  const [pending, setPending] = useState(false)
  /** 进入后台计算的时刻(用于算已等待时长; 首次进入即锁定, 不随后续 pending 响应刷新) */
  const [pendingSince, setPendingSince] = useState<number | null>(null)
  /** 最近一次拉取失败的原因; 有旧数据时只作标注, 无数据时即失败态 */
  const [error, setError] = useState('')
  /** 等待计时用的时钟(仅 pending 期间每秒推进) */
  const [clock, setClock] = useState(() => Date.now())
  /** 请求序号: 丢弃过期响应(竞态守卫) */
  const seqRef = useRef(0)
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    const seq = ++seqRef.current
    try {
      const res = await fetchAPI<BreadthResp>('/market-data/breadth-distribution', {
        timeoutMs: REQUEST_TIMEOUT_MS,
      })
      if (!mountedRef.current || seq !== seqRef.current) return
      setError('')
      if (res?.pending) {
        // 先出壳: 后台任务在跑, 保留占位并等回补, 不写 0 柱
        setPending(true)
        setPendingSince((t) => t ?? Date.now())
        setClock(Date.now())
        return
      }
      setPending(false)
      setPendingSince(null)
      setItems(res?.items ?? [])
      setNote(res?.note ?? '')
      setMeta({
        captured_at: typeof res?.captured_at === 'string' ? res.captured_at : null,
        age_s: typeof res?.age_s === 'number' && Number.isFinite(res.age_s) ? res.age_s : null,
        stale: res?.stale === true,
        source: typeof res?.source === 'string' ? res.source : undefined,
      })
    } catch (e) {
      if (!mountedRef.current || seq !== seqRef.current) return
      setPending(false)
      // 失败 ≠ 无数据: 从未拿到数据时走显式失败态; 已有数据时保留旧值并标注本次失败
      setError(e instanceof Error && e.message ? e.message : '请求失败')
    }
  }, [])

  /** 立即重试: 丢弃在途响应、清掉失败/等待记账, 再拉一次 */
  const retry = useCallback(() => {
    seqRef.current += 1
    setError('')
    setPending(false)
    setPendingSince(null)
    setClock(Date.now())
    void load()
  }, [load])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])
  // 页签 keep-alive(2026-10-15): 隐藏页签停表, 回前台补一次。
  usePausedInterval(() => void load(), REFRESH_MS, { deps: [load] })

  // 后补数: pending 期间以 BACKFILL_MS 间隔回补, 算完即停(最多 BACKFILL_MAX_TRIES 次)
  useEffect(() => {
    if (!pending) return
    let tries = 0
    const timer = window.setInterval(() => {
      tries += 1
      void load()
      if (tries >= BACKFILL_MAX_TRIES) window.clearInterval(timer)
    }, BACKFILL_MS)
    return () => window.clearInterval(timer)
  }, [pending, load])

  const elapsedMs = pendingSince != null ? Math.max(0, clock - pendingSince) : 0
  /** 后台计算超出等待上限: 不再假装在算, 给显式超时态 */
  const timedOut = items === null && pending && elapsedMs >= PENDING_TIMEOUT_MS

  // 等待计时: 仅在「后台计算中且还没有数据」时每秒推进; 超时/有数据即停表
  useEffect(() => {
    if (!pending || items !== null || timedOut) return
    const timer = window.setInterval(() => setClock(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [pending, items, timedOut])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !items || items.length === 0) return
    const sc = readStockColors()
    // 2026-09-05 交易所大屏式双向镜像柱: 左绿(跌)/右红(涨)，中央0轴，渐变+圆角。
    const ordered = [...items].reverse()
    const downs = ordered.filter((i) => isDownBucket(i.bucket))
    const ups = ordered.filter((i) => !isDownBucket(i.bucket))
    const cats = [...downs.map((i) => i.bucket)].reverse().concat(ups.map((i) => i.bucket))
    chart.setOption({
      grid: { left: 56, right: 56, top: 4, bottom: 4 },
      xAxis: { type: 'value', show: false },
      yAxis: {
        type: 'category',
        data: cats,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { fontSize: 10, color: '#8e8e96' },
      },
      series: [
        {
          type: 'bar',
          name: '跌',
          data: cats.map((c) => {
            const f = downs.find((i) => i.bucket === c)
            return f
              ? {
                  value: -f.count,
                  itemStyle: {
                    color: { type: 'linear', x: 1, y: 0, x2: 0, y2: 0, colorStops: [{ offset: 0, color: sc.down }, { offset: 1, color: sc.down + '55' }] },
                    borderRadius: [2, 0, 0, 2],
                  },
                }
              : null;
          }),
          barWidth: '55%',
          label: {
            show: true,
            position: 'left',
            fontSize: 10,
            fontFamily: 'monospace',
            color: '#c4c4cb',
            formatter: (p: { value: number | null }) => (p.value == null ? '' : String(Math.abs(p.value))),
          },
        },
        {
          type: 'bar',
          name: '涨',
          data: cats.map((c) => {
            const f = ups.find((i) => i.bucket === c)
            return f
              ? {
                  value: f.count,
                  itemStyle: {
                    color: { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [{ offset: 0, color: sc.up }, { offset: 1, color: sc.up + '55' }] },
                    borderRadius: [0, 2, 2, 0],
                  },
                }
              : null;
          }),
          barWidth: '55%',
          label: {
            show: true,
            position: 'right',
            fontSize: 10,
            fontFamily: 'monospace',
            color: '#c4c4cb',
          },
        },
      ],
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    })
  }, [items, chartRef])

  const retryButton = (
    <button
      type="button"
      onClick={retry}
      className="rounded border border-border px-2.5 py-1 text-[12px] text-primary hover:bg-s2"
    >
      重试
    </button>
  )

  if (items === null) {
    // 先出壳: 首次取数/后台计算期间占满同高度(160px), 文案显式 —— 不跳版, 也不用 0 柱假装有分布
    return (
      <div className="flex h-[160px] flex-col items-center justify-center gap-2 px-2 text-center text-[12px] text-muted-foreground">
        {error ? (
          <>
            <span>涨跌分布加载失败: {error}</span>
            {retryButton}
          </>
        ) : timedOut ? (
          <>
            <span>
              涨跌分布后台计算超时(已等待 {Math.floor(elapsedMs / 1000)}s, 仍未算完) —— 不再自动补齐, 可重试
            </span>
            {retryButton}
          </>
        ) : pending ? (
          <span>
            涨跌分布后台计算中…{elapsedMs >= 1_000 ? ` 已等待 ${Math.floor(elapsedMs / 1000)}s` : ''}
            {elapsedMs >= PENDING_SLOW_MS ? '，耗时较长' : ''}
          </span>
        ) : (
          <span>加载中…</span>
        )}
      </div>
    )
  }
  if (items.length === 0 || items.every((i) => i.count === 0)) {
    return (
      <div className="flex h-[160px] flex-col items-center justify-center gap-1 text-center text-[12px] text-muted-foreground">
        <span>{note || '暂无分布数据'}</span>
        {/* 空分布也可能是真实快照(非交易日): 同样标时效, 不臆测 */}
        <span className="text-[12px]">{formatBreadthFreshness(meta)}</span>
      </div>
    )
  }
  return (
    <div>
      {error && (
        <div className="mb-1 text-[12px] text-amber-600 dark:text-amber-500">
          本次刷新失败: {error} — 仍展示上次成功数据
        </div>
      )}
      <div ref={ref} className="h-[160px] w-full" />
      {/* 数据时效: 只用后端 captured_at/age_s; 缺失即「数据时效未知」 */}
      <div className="mt-1 text-right text-[12px] leading-none text-muted-foreground">
        {formatBreadthFreshness(meta)}
      </div>
    </div>
  )
}
