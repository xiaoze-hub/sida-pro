import { useEffect, useRef, useCallback } from 'react'
import echarts from '../lib/echarts-core'
import { SIDA_THEME_NAME } from '../lib/echarts-theme'
import type { EChartsType } from 'echarts/core'

/**
 * v0.5.0: useECharts — 统一 ECharts 实例生命周期 hook。
 *
 * - 自动 init / dispose
 * - ResizeObserver 替代 window.resize（容器尺寸变化自动重绘，无 window 泄漏）
 * - 返回 chart { ref, chartRef, resize }
 */
export function useECharts(themeName: string = SIDA_THEME_NAME) {
  const ref = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<EChartsType | null>(null)
  const roRef = useRef<ResizeObserver | null>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return

    let chart: EChartsType | null = null
    try {
      chart = echarts.init(el, themeName)
      chartRef.current = chart
      chart.resize()
    } catch (e) {
      console.warn('[useECharts] init failed', e)
      return
    }

    let ro: ResizeObserver | null = null
    try {
      ro = new ResizeObserver(() => {
        try {
          chartRef.current?.resize()
        } catch {
          /* ignore */
        }
      })
      ro.observe(el)
      roRef.current = ro
    } catch {
      /* ignore */
    }

    return () => {
      try {
        ro?.disconnect()
      } catch {
        /* ignore */
      }
      roRef.current = null
      try {
        chartRef.current?.dispose()
      } catch {
        /* ignore */
      }
      chartRef.current = null
    }
  }, [themeName])

  const resize = useCallback(() => {
    try {
      chartRef.current?.resize()
    } catch {
      /* ignore */
    }
  }, [])

  return { ref, chartRef, resize }
}