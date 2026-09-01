/**
 * K 线事件标注 · 标准化数据层(设计稿 v2.1 §10/§12, P1 协作件)。
 *
 * ## 定位(为什么有这个文件)
 *
 * 图表组件正在从自研 SVG(InteractiveKline)切换到 Lightweight Charts v5,
 * 但 **L4 事件数据不该跟着图表库换**。本模块把"后端 summary 事件"归一化成
 * 图表无关的标准结构, 任何图表实现(旧 SVG / lightweight-charts / 未来的)
 * 都直接消费这里的输出:
 *
 *     后端 /klines/{symbol}/summary
 *            │
 *            ▼
 *     normalizeKlineEvents()  ──▶  KlineEventPoint[]   (事件点: 涨停/龙虎榜/拆单簇…)
 *     normalizePriceLines()   ──▶  KlinePriceLine[]    (价位线: 筹码峰/套牢分界/支撑压力)
 *            │
 *            ▼
 *     任意 K 线图表组件(通过 markers / priceLines / 自绘辅助层渲染)
 *
 * ## ⚠️ 诚实口径(红线)
 *
 * - kind 未在 `KLINE_EVENT_KINDS` 白名单内 → **过滤**, 不猜
 * - date 缺失 / 无法归一化 → **过滤**, 不给图表喂脏点
 * - 价位线 price 非有限正数 → **过滤**
 * - 字段缺失就缺失(可选字段保持 undefined), 不补 0 / 不补假 label
 *
 * ## 与后端的契约(单一事实来源)
 *
 * 后端 `src/core/l4_events.py` 产出的 kind 与前端 `InteractiveKline` 的
 * `KlineEventKind` 一一对应 —— 本模块的白名单就是这份契约的守门员:
 * 后端加了新 kind 而这里没登记, 新事件会被显式过滤(而不是渲染成空壳),
 * 迫使前后端同步更新契约。
 */

// ---------------------------------------------------------------------------
// 契约类型
// ---------------------------------------------------------------------------

/** 后端 l4_events 与前端约定的十种事件(§5.3)。新增 kind 必须两边同步。 */
export const KLINE_EVENT_KINDS = [
  'limit_up', // 涨停
  'limit_down', // 跌停
  'dragon_tiger', // 龙虎榜
  'announcement', // 公告
  'split_cluster', // 拆单簇(.tck)
  'cancel_anomaly', // 撤单异常
  'support', // 托盘
  'pressure', // 压盘
  'unlock', // 解套盘位
  'my_trade', // 我的买卖点(暂缓: 等 summary 透传 user_id)
] as const

export type KlineEventKind = (typeof KLINE_EVENT_KINDS)[number]

/** §5.3 事件图标; 解套盘位/支撑压力是"价位线语义", 无独立图标 */
export const KIND_ICON: Partial<Record<KlineEventKind, string>> = {
  dragon_tiger: '涨',
  split_cluster: '拆',
  cancel_anomaly: '⚠撤',
  support: '🛡托',
  pressure: '🔒压',
  my_trade: '我',
}

/** 事件中文兜底标签(后端 label 缺失时用) */
export const KIND_LABEL: Record<KlineEventKind, string> = {
  limit_up: '涨停',
  limit_down: '跌停',
  dragon_tiger: '龙虎榜',
  announcement: '公告',
  split_cluster: '拆单簇',
  cancel_anomaly: '撤单异常',
  support: '托盘',
  pressure: '压盘',
  unlock: '解套盘位',
  my_trade: '我的买卖点',
}

/** 事件配色语义: up=利多(红) / down=利空(绿) / 中性色由消费方自定 */
export type EventTone = 'up' | 'down' | 'neutral'

export const KIND_TONE: Record<KlineEventKind, EventTone> = {
  limit_up: 'up',
  dragon_tiger: 'up',
  split_cluster: 'up',
  limit_down: 'down',
  cancel_anomaly: 'down',
  announcement: 'neutral',
  my_trade: 'neutral',
  unlock: 'neutral',
  support: 'up',
  pressure: 'down',
}

/** 标准化事件点 —— 图表无关 */
export interface KlineEventPoint {
  /** 'YYYY-MM-DD'(8 位无横杠会归一化, 见 normalizeDate) */
  date: string
  kind: KlineEventKind
  /** 展示标签: 后端 label 优先, 缺失回退 KIND_LABEL */
  label: string
  /** §5.3 图标; 价位线语义的事件无图标(undefined) */
  icon?: string
  tone: EventTone
  /** ---- 以下为度量字段, 后端按事件类型给出, 缺失保持 undefined ---- */
  price?: number
  shares?: number
  amount?: number
  count?: number
  time?: string
}

/** 标准化价位线 —— 图表无关(支撑/压力/套牢区) */
export interface KlinePriceLine {
  price: number
  /** support=支撑(下沿) / pressure=压力(上沿/套牢) */
  kind: 'support' | 'pressure'
  label?: string
  /** 该价位占比(如筹码峰占比), 后端给了才有 */
  ratio?: number | null
}

// ---------------------------------------------------------------------------
// 输入形状(后端 summary 的宽松子集, 只声明实际消费的字段)
// ---------------------------------------------------------------------------

/** 后端事件行的宽松形状 */
export interface RawKlineEvent {
  date?: string | null
  kind?: string | null
  label?: string | null
  price?: number | null
  shares?: number | null
  amount?: number | null
  count?: number | null
  time?: string | null
}

/** 后端解套价位线的宽松形状(l4_events.unlock_levels_from_chips 产出) */
export interface RawPriceLine {
  price?: number | null
  kind?: string | null
  label?: string | null
  ratio?: number | null
}

const _KIND_SET = new Set<string>(KLINE_EVENT_KINDS)

// ---------------------------------------------------------------------------
// 归一化函数
// ---------------------------------------------------------------------------

/**
 * 8 位无横杠日期('20260901') → '2026-09-01'; 其他格式截前 10 位。
 * 无法解析返回 null(调用方过滤, 不编造)。
 */
export function normalizeDate(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  const s = raw.trim()
  if (!s) return null
  if (/^\d{8}$/.test(s)) return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}`
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10)
  return null
}

/** 数值字段守卫: null/undefined/NaN → undefined(不补 0) */
function num(v: unknown): number | undefined {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  return undefined
}

/**
 * 后端事件数组 → 标准化事件点数组。
 *
 * 过滤规则(任一命中即丢弃该点):
 *   - date 缺失或无法归一化
 *   - kind 不在 KLINE_EVENT_KINDS 白名单
 *
 * 返回**按日期升序**稳定排序(图表 markers 通常要求时间有序)。
 * 输入为空/全被过滤 → [](显式空, 图表不画任何标注)。
 */
export function normalizeKlineEvents(raw: ReadonlyArray<RawKlineEvent> | null | undefined): KlineEventPoint[] {
  if (!Array.isArray(raw)) return []
  const out: KlineEventPoint[] = []
  for (const e of raw) {
    if (!e || typeof e !== 'object') continue
    const kind = typeof e.kind === 'string' ? e.kind : ''
    if (!_KIND_SET.has(kind)) continue
    const date = normalizeDate(e.date)
    if (!date) continue
    const k = kind as KlineEventKind
    out.push({
      date,
      kind: k,
      label: (typeof e.label === 'string' && e.label.trim()) || KIND_LABEL[k],
      icon: KIND_ICON[k],
      tone: KIND_TONE[k],
      price: num(e.price),
      shares: num(e.shares),
      amount: num(e.amount),
      count: num(e.count),
      time: typeof e.time === 'string' ? e.time : undefined,
    })
  }
  // 稳定排序: 同日多事件保持后端顺序( rear 意义上更晚的事件在后 )
  return out.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0))
}

/**
 * 后端解套价位线数组 → 标准化价位线数组。
 *
 * 过滤规则: price 非有限正数 → 丢弃; kind 只认 support/pressure,
 * 其他值(含缺失)按价格语义回退: 无法判断 → 丢弃(不猜)。
 * 返回按价格升序(图表从下往上画)。
 */
export function normalizePriceLines(raw: ReadonlyArray<RawPriceLine> | null | undefined): KlinePriceLine[] {
  if (!Array.isArray(raw)) return []
  const out: KlinePriceLine[] = []
  for (const l of raw) {
    if (!l || typeof l !== 'object') continue
    const price = num(l.price)
    if (price === undefined || price <= 0) continue
    let kind: 'support' | 'pressure'
    if (l.kind === 'support') kind = 'support'
    else if (l.kind === 'pressure') kind = 'pressure'
    else continue // 缺失/未知 → 不猜
    out.push({
      price,
      kind,
      label: typeof l.label === 'string' && l.label.trim() ? l.label : undefined,
      ratio: l.ratio === null ? null : num(l.ratio),
    })
  }
  return out.sort((a, b) => a.price - b.price)
}
