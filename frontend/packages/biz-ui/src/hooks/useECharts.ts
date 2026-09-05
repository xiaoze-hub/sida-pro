import { useRef, useCallback } from 'react'
import echarts from '../lib/echarts-core'
import { SIDA_THEME_NAME } from '../lib/echarts-theme'
import type { EChartsType } from 'echarts/core'

/**
 * v0.5.0: useECharts — 统一 ECharts 实例生命周期 hook。
 *
 * - 自动 init / dispose
 * - ResizeObserver 替代 window.resize（容器尺寸变化自动重绘，无 window 泄漏）
 * - 返回 chart { ref, chartRef, resize }
 * - 2026-09-05: ref 改 callback ref — 容器是数据到了才挂载时（如涨跌分布），
 *   mount期 init 因 ref.current==null 直接 return，chart 永为 null 导致永久空白。
 *   callback ref 在容器挂载瞬间 init，卸载时 dispose。
 */
export function useECharts(themeName: string = SIDA_THEME_NAME) {
  const chartRef = useRef<EChartsType | null>(null)
  const roRef = useRef<ResizeObserver | null>(null)

  const disposeAll = useCallback(() => {
    try {
      roRef.current?.disconnect()
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
  }, [])

  const ref = useCallback(
    (el: HTMLDivElement | null) => {
      disposeAll()
      if (!el) return
      try {
        const chart = echarts.init(el, themeName)
        chartRef.current = chart
        chart.resize()
      } catch (e) {
        console.warn('[useECharts] init failed', e)
        return
      }
      try {
        const ro = new ResizeObserver(() => {
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
    },
    [themeName, disposeAll],
  )

  const resize = useCallback(() => {
    try {
      chartRef.current?.resize()
    } catch {
      /* ignore */
    }
  }, [])

  return { ref, chartRef, resize }
}