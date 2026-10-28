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

export interface KlinePriceLinePayload {
  price: number
  kind: 'support' | 'pressure'
  label: string
}

const STORAGE_PREFIX = 'sida_kline_overlay:'

export class ChartOverlayService {
  private static linesMap: Map<string, any> = new Map()

  /**
   * 将价位线持久化到会话存储并广播全局事件
   * 确保无论个股工作台当前是否已挂载，后续切入页面或刷新时均能还原画线
   */
  public static saveOverlay(symbol: string | undefined, levels: PriceLevelOverlay[]) {
    const lines: KlinePriceLinePayload[] = levels.map((lvl) => ({
      price: lvl.price,
      kind: lvl.kind ?? (lvl.title.includes('支撑') || lvl.title.includes('防守') ? 'support' : 'pressure'),
      label: lvl.title,
    }))

    if (symbol && typeof sessionStorage !== 'undefined') {
      try {
        sessionStorage.setItem(`${STORAGE_PREFIX}${symbol.toUpperCase()}`, JSON.stringify(lines))
      } catch {
        // sessionStorage 不可用时静默
      }
    }

    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent('sida-kline-overlay', {
          detail: {
            symbol,
            lines,
          },
        }),
      )
    }
  }

  /**
   * 从会话存储读取某标的的持久化画线
   */
  public static getStoredOverlay(symbol: string): KlinePriceLinePayload[] | null {
    if (!symbol || typeof sessionStorage === 'undefined') return null
    try {
      const raw = sessionStorage.getItem(`${STORAGE_PREFIX}${symbol.toUpperCase()}`)
      if (!raw) return null
      const parsed = JSON.parse(raw)
      return Array.isArray(parsed) ? parsed : null
    } catch {
      return null
    }
  }

  /**
   * 清除某标的的画线并广播清除事件
   */
  public static clearOverlay(symbol: string) {
    if (typeof sessionStorage !== 'undefined') {
      try {
        sessionStorage.removeItem(`${STORAGE_PREFIX}${symbol.toUpperCase()}`)
      } catch {
        // ignore
      }
    }
    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent('sida-kline-overlay', {
          detail: {
            symbol,
            lines: [],
          },
        }),
      )
    }
  }

  /**
   * 派发全局 K 线画线叠加事件（兼容方法）
   */
  public static emitOverlay(symbol: string | undefined, levels: PriceLevelOverlay[]) {
    this.saveOverlay(symbol, levels)
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
