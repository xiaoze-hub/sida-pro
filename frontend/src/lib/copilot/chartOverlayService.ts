/**
 * 参谋结论与 TradingView / Lightweight Charts 图元同步画线服务
 */

export interface PriceLevelOverlay {
  price: number
  title: string
  color?: string
  kind?: 'support' | 'pressure'
  lineStyle?: number // 0: solid, 1: dotted, 2: dashed
}

export class ChartOverlayService {
  private static linesMap: Map<string, any> = new Map()

  /**
   * 派发全局 K 线画线叠加事件，使当前打开的工作台或宿主图表立即响应渲染
   */
  public static emitOverlay(symbol: string | undefined, levels: PriceLevelOverlay[]) {
    if (typeof window === 'undefined') return
    window.dispatchEvent(
      new CustomEvent('sida-kline-overlay', {
        detail: {
          symbol,
          lines: levels.map((lvl) => ({
            price: lvl.price,
            kind: lvl.kind ?? (lvl.title.includes('支撑') || lvl.title.includes('防守') ? 'support' : 'pressure'),
            label: lvl.title,
          })),
        },
      }),
    )
  }

  /**
   * 将参谋推导出的支撑、压力点位或止损位直接绘制在当前 K 线图表系列上
   */
  public static injectDecisionLevels(series: any, levels: PriceLevelOverlay[]) {
    if (!series || typeof series.createPriceLine !== 'function') {
      return
    }

    // 先清除已有的旧画线
    this.clearOverlays(series)

    levels.forEach((lvl) => {
      try {
        const priceLine = series.createPriceLine({
          price: lvl.price,
          color: lvl.color ?? (lvl.kind === 'support' ? '#22c55e' : '#ef4444'),
          lineWidth: 2,
          lineStyle: lvl.lineStyle ?? 2,
          axisLabelVisible: true,
          title: lvl.title,
        })
        this.linesMap.set(`${lvl.title}_${lvl.price}`, priceLine)
      } catch {
        // 捕获可能已销毁的 series 引用
      }
    })
  }

  public static clearOverlays(series: any) {
    if (!series || typeof series.removePriceLine !== 'function') {
      this.linesMap.clear()
      return
    }

    this.linesMap.forEach((line) => {
      try {
        series.removePriceLine(line)
      } catch {
        // ignore
      }
    })
    this.linesMap.clear()
  }
}
