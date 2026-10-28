/**
 * 参谋结论与 TradingView / Lightweight Charts 图元同步画线服务
 */

export interface PriceLevelOverlay {
  price: number
  title: string
  color: string
  lineStyle?: number // 0: solid, 1: dotted, 2: dashed
}

export class ChartOverlayService {
  private static linesMap: Map<string, any> = new Map()

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
          color: lvl.color,
          lineWidth: 2,
          lineStyle: lvl.lineStyle ?? 2,
          axisLabelVisible: true,
          title: lvl.title,
        })
        this.linesMap.set(`${lvl.title}_${lvl.price}`, priceLine)
      } catch (e) {
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
      } catch (e) {
        // ignore
      }
    })
    this.linesMap.clear()
  }
}
