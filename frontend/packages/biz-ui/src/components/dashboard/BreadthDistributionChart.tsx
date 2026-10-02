import { useCallback, useEffect, useRef, useState } from 'react'
import { useECharts } from '@panwatch/biz-ui/hooks/useECharts'
import { readStockColors } from '@panwatch/biz-ui/lib/stock-colors'
import { fetchAPI } from '@panwatch/api'

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
 */

interface BreadthItem {
  bucket: string
  count: number
}
interface BreadthResp {
  count: number
  total: number
  items: BreadthItem[]
  note?: string
  /** 后端异步任务尚未算完(参见 src/web/api/market_data.py:breadth_distribution) */
  pending?: boolean
}

/** 常规刷新间隔(与后端 300s 缓存节奏一致, 保持原 60s 不变) */
const REFRESH_MS = 60_000
/** 命中 pending 时的回补间隔/上限(≈60s, 覆盖首次后台计算窗口) */
const BACKFILL_MS = 1_500
const BACKFILL_MAX_TRIES = 40
/** 该端点后端立即返回(异步), 显式短超时: 慢时快速失败显式降级, 不霸占连接 */
const REQUEST_TIMEOUT_MS = 10_000

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
  /** 后端后台任务尚未算完 → 显示“计算中”占位并回补 */
  const [pending, setPending] = useState(false)
  /** 拉取失败且从未拿到过数据 → 显式失败态(与“暂无分布数据”严格区分) */
  const [failed, setFailed] = useState(false)
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
      setFailed(false)
      if (res?.pending) {
        // 先出壳: 后台任务在跑, 保留占位并等回补, 不写 0 柱
        setPending(true)
        return
      }
      setPending(false)
      setItems(res?.items ?? [])
      setNote(res?.note ?? '')
    } catch {
      if (!mountedRef.current || seq !== seqRef.current) return
      setPending(false)
      // 失败 ≠ 无数据: items 为空时走显式失败态; 已有数据时保留旧值(渲染分支不受 failed 影响)
      setFailed(true)
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void load()
    const timer = window.setInterval(() => void load(), REFRESH_MS)
    return () => {
      mountedRef.current = false
      window.clearInterval(timer)
    }
  }, [load])

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

  if (items === null) {
    // 先出壳: 首次取数/后台计算期间占满同高度(160px), 文案显式 —— 不跳版, 也不用 0 柱假装有分布
    return (
      <div className="flex h-[160px] items-center justify-center px-2 text-center text-[11px] text-muted-foreground">
        {failed
          ? '涨跌分布加载失败, 稍后自动重试'
          : pending
            ? '涨跌分布后台计算中…(算完自动补齐)'
            : '加载中…'}
      </div>
    )
  }
  if (items.length === 0 || items.every((i) => i.count === 0)) {
    return (
      <div className="flex h-[160px] items-center justify-center text-[11px] text-muted-foreground">
        {note || '暂无分布数据'}
      </div>
    )
  }
  return (
    <div>
      <div ref={ref} className="h-[160px] w-full" />
    </div>
  )
}