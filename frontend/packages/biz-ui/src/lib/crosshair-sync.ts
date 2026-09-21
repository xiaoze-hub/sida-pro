/**
 * 同页多图十字线联动(2026-09-20 设计系统 P1)。
 *
 * 依据: 全仓有两个图表各调 `subscribeCrosshairMove`, 但**没有任何**跨图同步 —— 鼠标停在
 * 某一天的 K 线上, 分时图/资金面板并不会跟着移到同一天。对行情终端来说这是"专业感"的核心:
 * 用"同一个交易日"这根轴把屏上所有图连成一体。
 *
 * 实现要点:
 *  - 按**time**同步(不是按像素比例), 所以两个图无视窗差异也能对齐到同一根 K 线;
 *  - 注册表是模块级的(同页共享), 组件卸载时必须反注册(否则跨页残留);
 *  - 回环保护: 应用远端十字线时会触发自己的 move 事件 ⇒ 用 applying 标记屏蔽, 否则来回震荡。
 */
export interface CrosshairPayload {
  /** 时间(lightweight-charts 的 Time 型: 数值时间戳 / 字符串 / BusinessDay 对象) */
  time: unknown
  price: number
}

interface Subscriber {
  apply: (p: CrosshairPayload) => void
  clear: () => void
}

const subscribers = new Set<Subscriber>()
/** 正在应用远端位置 → 自己的 move 事件直接丢弃(防回环) */
let applying = false

export function registerCrosshair(sub: Subscriber): () => void {
  subscribers.add(sub)
  return () => {
    subscribers.delete(sub)
  }
}

/** 广播本图十字线位置; `source` 用于跳过自己。 */
export function broadcastCrosshair(p: CrosshairPayload | null, source: Subscriber) {
  if (applying) return
  applying = true
  try {
    for (const s of subscribers) {
      if (s === source) continue
      if (p) s.apply(p)
      else s.clear()
    }
  } finally {
    applying = false
  }
}

/** 测试用: 当前注册数(验证卸载真的反注册了) */
export function __subscriberCount(): number {
  return subscribers.size
}
