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
 * 2026-11-08 计算可视性与时效:
 *   - 后台计算有上限: 超过 60s 仍是 pending → 显式「计算超时」而非无限装作在算;
 *   - 加载中 / 后台计算中 / 超时 / 失败 四种文案互不混淆, 超时与失败都带「重试」入口;
 *   - 拿到真快照时展示后端给的 `captured_at` / `age_s`, 后端没给就写「数据时效未知」,
 *     绝不用前端当前时间倒推一个假时刻; `stale` 快照明示「可能已过期」;
 *   - 已有数据但本次刷新失败 → 保留旧图 + 显式标注, 不让失败把画布清空。
 *
 * 2026-11-08 复审回归修正(本轮):
 *   a. 时效会走表 —— `age_s` 是后端**响应那一刻**算出来的, 直接渲染就等于把「5分钟前」永久冻结
 *      (画布越挂越像刚采的)。现在以「收到响应的本地时刻」为基准往后推, 只推进已知的 age_s,
 *      仍不拿前端时钟去猜采集时刻;
 *   b. 回补单飞 —— 旧实现每 1.5s 硬发一次, 慢响应(>1.5s)会被后发的请求顶掉序号, 算完的结果
 *      反被当成过期响应丢弃。现在在途那笔没回来就不发下一笔, 序号只用于「手动重试丢弃旧请求」;
 *   c. 说法即行为 —— 写「不再自动补齐」就真的停: 超时后回补与 60s 自动刷新一起停, 只有手动重试
 *      (或重新挂载)才恢复, 不再一边宣称停止一边继续请求;
 *   d. 旧图刷新失败有手动重试入口(此前只有一行「仍展示上次成功数据」);
 *   e. 响应缺 `items` 不再把已有画布覆盖成空分布。
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
/** 时效文案的走表间隔: age_s 只在收到响应那一刻为真, 之后必须按本地时钟自己往前走 */
const AGE_TICK_MS = 5_000

/** 快照时效来源: 只用后端给的字段, 前端不臆测 */
export interface BreadthFreshnessMeta {
  captured_at?: string | null
  age_s?: number | null
  stale?: boolean
  source?: string
  /**
   * 收到该响应的本地时刻(ms)。age_s 的推进基准 —— 只从这一刻起往后加已知的年龄,
   * 不用前端时钟去反推采集时刻。缺它就按「静态 age_s」渲染(历史调用方语义不变)。
   */
  received_at?: number | null
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
 * - 有 captured_at 且可解析 → 「数据时间 MM-DD HH:MM」(后端墙钟, 原样展示);
 * - 相对年龄 → 以 `age_s` 为已知起点 + 自 `received_at` 起流逝的本地时间:
 *     有时刻时追加「· N前」, 只有 age_s 时写「数据更新于 N前」;
 * - age_s 缺失/不可用 → 「数据时效未知」(不拿当前时间编一个时刻);
 * - stale=true(后端判定) → 追加「可能已过期」。
 *
 * `received_at` 缺省时退化为静态 age_s: 语义与历史调用一致(不假装知道它还在变旧)。
 */
export function formatBreadthFreshness(
  meta: BreadthFreshnessMeta | null | undefined,
  nowMs: number = Date.now(),
): string {
  const raw = typeof meta?.captured_at === 'string' ? meta.captured_at.trim() : ''
  let at: Date | null = null
  if (raw) {
    const d = new Date(raw)
    if (!Number.isNaN(d.getTime())) at = d
  }
  const reportedAge =
    typeof meta?.age_s === 'number' && Number.isFinite(meta.age_s) && meta.age_s >= 0 ? meta.age_s : null
  const receivedAt =
    typeof meta?.received_at === 'number' && Number.isFinite(meta.received_at) ? meta.received_at : null

  let ageS: number | null = null
  if (reportedAge != null) {
    ageS =
      receivedAt != null && Number.isFinite(nowMs)
        ? reportedAge + Math.max(0, (nowMs - receivedAt) / 1000)
        : reportedAge
  }

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
  /** 计时用时钟(等待秒数按 1s 推进, 已有数据时按 AGE_TICK_MS 推进时效文案) */
  const [nowMs, setNowMs] = useState(() => Date.now())
  /** 请求序号: 丢弃被手动重试顶掉的旧响应(竞态守卫) */
  const seqRef = useRef(0)
  /** 单飞标记: 在途请求未回来前不发下一笔(避免自己顶掉自己的结果) */
  const inFlightRef = useRef(false)
  const mountedRef = useRef(true)
  /** 超时后真的停表: 60s 自动刷新到此为止, 直到手动重试 */
  const haltedRef = useRef(false)

  const load = useCallback(async (force = false) => {
    // 单飞: 上一笔还在路上就不再发 —— 旧实现每 1.5s 硬发, 慢响应必被顶掉序号而丢结果
    if (!force && inFlightRef.current) return
    const seq = ++seqRef.current
    inFlightRef.current = true
    try {
      const res = await fetchAPI<BreadthResp>('/market-data/breadth-distribution', {
        timeoutMs: REQUEST_TIMEOUT_MS,
      })
      if (!mountedRef.current || seq !== seqRef.current) return
      if (res?.pending) {
        // 先出壳: 后台任务在跑, 保留占位并等回补, 不写 0 柱。
        // 不清 error: 上一次刷新失败的事实要留到真快照落地为止(否则一行 pending 就把失败抹掉)
        setPending(true)
        setPendingSince((t) => t ?? Date.now())
        setNowMs(Date.now())
        return
      }
      setPending(false)
      setPendingSince(null)
      if (!Array.isArray(res?.items)) {
        // 响应缺分布数据: 保留已有画布 + 显式失败, 不把旧图覆盖成空分布
        setError('响应缺少分布数据')
        return
      }
      setError('')
      setItems(res.items)
      setNote(res.note ?? '')
      setMeta({
        captured_at: typeof res.captured_at === 'string' ? res.captured_at : null,
        age_s: typeof res.age_s === 'number' && Number.isFinite(res.age_s) ? res.age_s : null,
        stale: res.stale === true,
        source: typeof res.source === 'string' ? res.source : undefined,
        received_at: Date.now(),
      })
      setNowMs(Date.now())
    } catch (e) {
      if (!mountedRef.current || seq !== seqRef.current) return
      setPending(false)
      // 失败后必须清掉等待起点: 否则下一次 pending 会沿用上一轮的旧起点, 立刻判成「超时」
      setPendingSince(null)
      // 失败 ≠ 无数据: 从未拿到数据时走显式失败态; 已有数据时保留旧值并标注本次失败
      setError(e instanceof Error && e.message ? e.message : '请求失败')
    } finally {
      // 只有「当前这一笔」才清标记(被顶掉的旧请求不许误清新请求的占位)
      if (seq === seqRef.current) inFlightRef.current = false
    }
  }, [])

  /** 立即重试: 丢弃在途响应、恢复自动刷新、清掉失败/等待记账, 再拉一次 */
  const retry = useCallback(() => {
    haltedRef.current = false
    seqRef.current += 1
    inFlightRef.current = false
    setError('')
    setPending(false)
    setPendingSince(null)
    setNowMs(Date.now())
    void load(true)
  }, [load])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const elapsedMs = pendingSince != null ? Math.max(0, nowMs - pendingSince) : 0
  /** 后台计算超出等待上限: 不再假装在算, 给显式超时态 */
  const timedOut = items === null && pending && elapsedMs >= PENDING_TIMEOUT_MS

  // 超时即停表: 自动刷新读这个标记, 保证「已停止自动重试」的说法与行为一致
  useEffect(() => {
    haltedRef.current = timedOut
  }, [timedOut])

  // 页签 keep-alive(2026-10-15): 隐藏页签停表, 回前台补一次。
  const autoRefresh = useCallback(() => {
    // 计算超时后不再自动请求(回补与 60s 刷新一起停), 只等手动重试
    if (haltedRef.current) return
    void load()
  }, [load])
  usePausedInterval(autoRefresh, REFRESH_MS, { deps: [autoRefresh] })

  // 后补数: pending 期间以 BACKFILL_MS 间隔回补, 算完即停; 每笔结算后才排下一笔(不叠请求)
  useEffect(() => {
    if (!pending || timedOut) return
    let tries = 0
    let cancelled = false
    let timer: number | null = null
    const step = async () => {
      tries += 1
      await load()
      if (cancelled || tries >= BACKFILL_MAX_TRIES) return
      timer = window.setTimeout(() => {
        void step()
      }, BACKFILL_MS)
    }
    timer = window.setTimeout(() => {
      void step()
    }, BACKFILL_MS)
    return () => {
      cancelled = true
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [pending, timedOut, load])

  // 计时: 已有数据时按 AGE_TICK_MS 让时效文案往前走; 后台计算中按 1s 推进等待秒数
  useEffect(() => {
    if (items !== null) {
      const timer = window.setInterval(() => setNowMs(Date.now()), AGE_TICK_MS)
      return () => window.clearInterval(timer)
    }
    if (!pending || timedOut) return
    const timer = window.setInterval(() => setNowMs(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [items, pending, timedOut])

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
              : null
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
              : null
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
              涨跌分布后台计算超时(已等待 {Math.floor(elapsedMs / 1000)}s, 仍未算完) —— 已停止自动重试与自动刷新, 请点重试重新开始
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
        <span className="text-[12px]">{formatBreadthFreshness(meta, nowMs)}</span>
      </div>
    )
  }
  return (
    <div>
      {error && (
        <div className="mb-1 flex items-center justify-between gap-2 text-[12px] text-amber-600 dark:text-amber-500">
          <span>最近一次刷新失败: {error} — 仍展示上次成功数据</span>
          {retryButton}
        </div>
      )}
      <div ref={ref} className="h-[160px] w-full" />
      {/* 数据时效: 只用后端 captured_at/age_s(+收到时刻走表); 缺失即「数据时效未知」 */}
      <div className="mt-1 text-right text-[12px] leading-none text-muted-foreground">
        {formatBreadthFreshness(meta, nowMs)}
        {pending ? ' · 后台重算中' : ''}
      </div>
    </div>
  )
}
