import { useEffect } from 'react'
import { useECharts } from '@sida/biz-ui/hooks/useECharts'

/**
 * 市场温度仪表盘(v0.4.7, Dashboard 情绪周期区)。
 * v0.5.0: 使用 useECharts hook (ResizeObserver 替代 window.resize)。
 *
 * 2026-11-08 完整性: 原实现用 `metrics?.x ?? 0` 把缺失字段当 0 代入
 * `高度×15 + 晋级率×40 + 封板率×45`, 于是缺两项时也会画出一个 0-100 的分数、
 * 指针指向它 —— 看起来就是「完整口径算出的市场温度」。缺字段与真 0 是两回事:
 *   - 缺任一关键量(null/undefined/NaN) ⇒ 不再出分, 显式「不完整」并点名缺哪几项,
 *     表盘不画指针/进度弧(不给缺失数据一个伪造落点);
 *   - 0 是真值(0 板 / 0% 晋级) ⇒ 照常算分;
 *   - 三项齐全 ⇒ 算法与历史逐位一致, 不改口径。
 */

export interface GaugeMetrics {
  max_height: number | null
  promo_rate: number | null
  seal_rate: number | null
}

export type GaugeMetricKey = 'max_height' | 'promo_rate' | 'seal_rate'

export interface GaugeScore {
  /** 三项齐全时的 0-100 分; 缺项时为 null(不冒充) */
  score: number | null
  missing: GaugeMetricKey[]
  complete: boolean
}

const GAUGE_METRIC_LABEL: Record<GaugeMetricKey, string> = {
  max_height: '高度',
  promo_rate: '晋级率',
  seal_rate: '封板率',
}

/** 有限数字才算有值: null/undefined/NaN/Infinity 一律算缺; 0 是合法真值。 */
function isUsableNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/**
 * 市场温度评分(唯一口径)。
 * 完整: round(高度×15 + 晋级率×40 + 封板率×45), 上限 100 —— 与原实现逐位一致。
 * 缺项: score=null + missing 列表; 绝不把缺项当 0 算出一个「完整」分数。
 */
export function computeGaugeScore(metrics: GaugeMetrics | null | undefined): GaugeScore {
  const missing: GaugeMetricKey[] = []
  if (!isUsableNumber(metrics?.max_height)) missing.push('max_height')
  if (!isUsableNumber(metrics?.promo_rate)) missing.push('promo_rate')
  if (!isUsableNumber(metrics?.seal_rate)) missing.push('seal_rate')
  if (missing.length > 0) return { score: null, missing, complete: false }

  const h = metrics!.max_height as number
  const promo = metrics!.promo_rate as number
  const seal = metrics!.seal_rate as number
  return { score: Math.min(100, Math.round(h * 15 + promo * 40 + seal * 45)), missing: [], complete: true }
}

/** 阶段 → 指针色(对齐 MarketPhaseCard 配色语义) */
const PHASE_COLOR: Record<string, string> = {
  ice: '#3b82f6',
  ignite: '#06b6d4',
  rally: '#ef4444',
  climax: '#7f1d1d',
  ebb: '#f97316',
  repair: '#6b7280',
  accumulating: '#64748b',
}

const PHASE_LABEL: Record<string, string> = {
  ice: '冰点',
  ignite: '启动',
  rally: '主升',
  climax: '高潮',
  ebb: '退潮',
  repair: '修复',
  accumulating: '积累中',
}

export default function SentimentGauge({
  phase,
  metrics,
}: {
  phase: string | null
  metrics: GaugeMetrics | null
}) {
  const { ref, chartRef } = useECharts()

  // 2026-09-05 中文两行标签改由 HTML 叠加层渲染:
  // ECharts gauge detail 富文本 `\n` 在该版本下不换行(渲染成 `60n市场温度·修复` 挤出一行掉到表外)，
  // HTML 定位完全可控，不依赖图表库换行语义。
  const { score, missing, complete } = computeGaugeScore(metrics)
  const pointerColor = PHASE_COLOR[phase ?? 'accumulating'] ?? '#64748b'
  const label = phase ? (PHASE_LABEL[phase] ?? phase) : '--'
  const missingText = missing.map((k) => GAUGE_METRIC_LABEL[k]).join('/')

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    chart.setOption({
      series: [
        {
          type: 'gauge',
          startAngle: 180,
          endAngle: 0,
          min: 0,
          max: 100,
          radius: '95%',
          center: ['50%', '78%'],
          axisLine: {
            lineStyle: {
              width: 10,
              color: [
                [0.2, '#3b82f6'],
                [0.4, '#06b6d4'],
                [0.6, '#f97316'],
                [0.85, '#ef4444'],
                [1, '#7f1d1d'],
              ],
            },
          },
          // 2026-09-05 质感: 进度弧渐变覆盖 + 指针阴影，交易所大屏感。
          // 2026-11-08 缺项时整条进度弧/指针关掉: 没有分数就没有落点。
          progress: {
            show: complete,
            width: 10,
            itemStyle: {
              color: {
                type: 'linear',
                x: 0,
                y: 0,
                x2: 1,
                y2: 0,
                colorStops: [
                  { offset: 0, color: '#3b82f6' },
                  { offset: 0.5, color: pointerColor },
                  { offset: 1, color: pointerColor },
                ],
              },
              shadowColor: pointerColor,
              shadowBlur: 8,
            },
          },
          pointer: {
            show: complete,
            length: '62%',
            width: 4,
            itemStyle: { color: pointerColor, shadowColor: pointerColor, shadowBlur: 6 },
          },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
          // 2026-09-05 detail 交给 HTML 叠加层渲染，此处置空避免双写。
          detail: { show: false },
          data: [{ value: score ?? 0 }],
        },
      ],
    })
  }, [phase, metrics, score, complete, pointerColor, chartRef])

  return (
    // 2026-09-05 容器加高到 154px: 圆心在 78%(120px)，下方 34px 正好放下两行字，
    // 指针只扫上半圆 → 文字与表盘零重叠。
    <div
      className="relative h-[154px] w-full"
      data-testid="sentiment-gauge"
      data-gauge-complete={complete ? 'true' : 'false'}
      data-gauge-missing={missing.join(' ')}
    >
      <div ref={ref} className="absolute inset-0" />
      {/* 表盘内两行标签: 分数 + 市场温度·阶段(不缺项) / -- + 不完整·缺哪几项(缺项)，定位圆心之下，指针扫不到 */}
      <div className="pointer-events-none absolute inset-x-0 text-center" style={{ top: 'calc(78% + 6px)' }}>
        <div
          className="font-mono text-[20px] font-bold leading-none tabular-nums"
          style={{ color: complete ? pointerColor : '#8e8e96' }}
        >
          {complete ? score : '--'}
        </div>
        {complete ? (
          <div className="mt-1 text-[12px] leading-none text-[#8e8e96]" data-testid="gauge-status">
            市场温度 · {label}
          </div>
        ) : (
          <div
            className="mt-1 text-[12px] leading-none text-amber-600 dark:text-amber-500"
            data-testid="gauge-status"
          >
            不完整 · 缺{missingText}
          </div>
        )}
      </div>
      {/* 缺项时给读屏/悬停一句完整说明: 数据缺失不是 0 度 */}
      {!complete && (
        <span className="sr-only">
          市场温度数据不完整, 缺少{missingText}; 未按 0 计算, 当前不给出温度分数
        </span>
      )}
    </div>
  )
}
