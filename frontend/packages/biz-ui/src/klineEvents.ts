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

/**
 * 标准化**纯时间点标注** —— 图表无关(2026-10-29)。
 *
 * 与 `KlinePriceLine` 并列:价位线画成水平虚线,**时间点标注画成 K 线 marker**。
 * 两条通道互不替代:没有价位的标注不该被丢掉,也不该被塞到某个"大概的价位"上。
 *
 * 诚实口径(全部由 {@link normalizeAnnotationMarkers} 守住):
 *  - `date` 必须是**日粒度**(带时分的 `2026-10-28 10:00` 无法证明落在哪一根 K 线 → 丢弃);
 *  - `annotationId`/`actionId` 必须来自服务端(正整数),没有 id 的标记无法溯源 → 丢弃;
 *  - `label` 空 → 丢弃(图上只剩一个说不出是什么的方块);
 *  - `tone` 只认 `up/down/neutral`(未知 → 中性,不猜涨跌方向)。
 */
export interface KlineAnnotationMarker {
  /** 服务端 `kline_annotations.id` —— 图上每个记号都能对回一行 */
  annotationId: number
  /** 服务端确认动作 id(`copilot_actions.id`) */
  actionId: number
  /** 日粒度时间点 'YYYY-MM-DD' */
  date: string
  label: string
  tone: EventTone
  /** 服务端备注(有就随标记带出来,不影响定位) */
  note?: string | null
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

/** 合并后的稳定空数组（`getSnapshot`/deps 身份稳定用）。 */
const EMPTY_MERGED_LINES: KlinePriceLine[] = []

/**
 * 合并两组价位线（**已确认标注** + 后端图层 `unlock_levels`），同向同价只留一条。
 *
 * 为什么要合并（2026-10-28 复审阻断 ③）：`KlineChart` 的 `supportPressure` 是"父接管"
 * 语义 —— 一旦页面传了它（哪怕空数组），图表就把自己从 `/klines/{symbol}/summary`
 * 取到的 `unlock_levels`（筹码套牢/支撑压力）整批丢掉：参谋标注一上图，图层价线全消失；
 * 而两边都有同一价位时又会画两条重合虚线。
 *
 * 规则（与 `annotationLedger.mergeLines` / `linesForTarget` 同一口径）：
 *   1. `primary`（页面接线的服务端标注）优先 —— 标签/来源以它为准；
 *   2. `secondary`（图表自取图层）只补 primary 没有的 (方向, 2 位小数价位)；
 *   3. 非法价（非有限正数/缺方向）丢弃（不猜），按价格升序返回；
 *   4. 一侧为空（或清理后为空）时**原样返回另一侧**（同引用）—— 不制造无意义的状态变更，
 *      也不让"没传标注"的页面行为发生变化。
 */
export function mergePriceLines(
  primary: ReadonlyArray<KlinePriceLine> | null | undefined,
  secondary: ReadonlyArray<KlinePriceLine> | null | undefined,
): KlinePriceLine[] {
  const a = validLines(primary)
  const b = validLines(secondary)
  if (a.length === 0) return b.length === 0 ? EMPTY_MERGED_LINES : b
  if (b.length === 0) return a
  const byLevel = new Map<string, KlinePriceLine>()
  for (const line of [...a, ...b]) {
    const key = `${line.kind}:${Math.round(line.price * 100)}`
    if (byLevel.has(key)) continue // 先到先得：primary 已占住这个价位
    byLevel.set(key, line)
  }
  return [...byLevel.values()].sort((x, y) => x.price - y.price)
}

/**
 * 丢掉画不出来的行（非有限正数价位 / 未知方向）。
 * 全合法时**原样返回同一引用** —— 不制造无意义的新数组（父组件的 memo/effect 依赖它）。
 */
function validLines(source: ReadonlyArray<KlinePriceLine> | null | undefined): KlinePriceLine[] {
  if (!Array.isArray(source) || source.length === 0) return EMPTY_MERGED_LINES
  let dropped = false
  const out: KlinePriceLine[] = []
  for (const line of source) {
    if (
      !line ||
      typeof line.price !== 'number' ||
      !Number.isFinite(line.price) ||
      line.price <= 0 ||
      (line.kind !== 'support' && line.kind !== 'pressure')
    ) {
      dropped = true
      continue
    }
    out.push(line)
  }
  return dropped ? out : (source as KlinePriceLine[])
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

/** 标记文本上限: marker 上的字挤在 K 线之间, 太长就只剩一坨色块。 */
export const MARKER_LABEL_MAX = 12

/** 日粒度串 → 'YYYY-MM-DD'; 带时分 / 非法日期返回 null(不截断、不猜)。 */
export function dayOnlyDate(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  const s = raw.trim()
  if (!s) return null
  // `20261029` / `2026-10-29` 两种日粒度写法; **带 T/时分的一律拒**
  // (时间点落在哪一根 K 线无法证明 —— 后端 ts-only 也只接受日粒度)。
  const m = /^(\d{4})-?(\d{2})-?(\d{2})$/.exec(s)
  if (!m) return null
  const y = Number(m[1])
  const mo = Number(m[2])
  const d = Number(m[3])
  if (!Number.isInteger(y) || y < 1990 || y > 2100) return null
  if (!Number.isInteger(mo) || mo < 1 || mo > 12) return null
  if (!Number.isInteger(d) || d < 1 || d > 31) return null
  // 日历真实性: 2026-02-30 这类串不能变成一根不存在的 K 线
  const probe = new Date(Date.UTC(y, mo - 1, d))
  if (
    probe.getUTCFullYear() !== y ||
    probe.getUTCMonth() !== mo - 1 ||
    probe.getUTCDate() !== d
  ) {
    return null
  }
  return `${m[1]}-${m[2]}-${m[3]}`
}

/**
 * 纯时间点标注 → 图表 marker(图表无关)。
 *
 * 过滤规则(任一命中即丢弃, **不补正**):
 *  - `annotationId` / `actionId` 不是正整数 → 无法溯源到服务端行;
 *  - `date` 不是日粒度(带时分/非法日期) → 无法证明落在哪一根 K 线;
 *  - `label` 空 → 图上说不出这是什么;
 *  - 同一 `annotationId` 重复 → 只留第一条(同一次确认不该画两个记号)。
 *
 * 去重口径与价位线一致:同 (date, label, tone) 视为"同一件事的重复确认",
 * 保留 **annotationId 较大** 的那一条(较新的确认),便于溯源而不是画两层。
 * 返回按日期升序(图表 markers 的单调约束由 `filterMarkersInBarsRange` 再兜一次)。
 */
export function normalizeAnnotationMarkers(
  raw: ReadonlyArray<KlineAnnotationMarker> | null | undefined,
): KlineAnnotationMarker[] {
  if (!Array.isArray(raw) || raw.length === 0) return EMPTY_ANNOTATION_MARKERS
  const byId = new Map<number, KlineAnnotationMarker>()
  for (const m of raw) {
    if (!m || typeof m !== 'object') continue
    const id = Number(m.annotationId)
    if (!Number.isInteger(id) || id <= 0) continue
    const actionId = Number(m.actionId)
    if (!Number.isInteger(actionId) || actionId <= 0) continue
    const date = dayOnlyDate(m.date)
    if (!date) continue
    const label = typeof m.label === 'string' ? m.label.trim() : ''
    if (!label) continue
    const tone: EventTone = m.tone === 'up' || m.tone === 'down' ? m.tone : 'neutral'
    const marker: KlineAnnotationMarker = {
      annotationId: id,
      actionId,
      date,
      label: label.slice(0, MARKER_LABEL_MAX),
      tone,
      note: typeof m.note === 'string' && m.note.trim() ? m.note : null,
    }
    const prev = byId.get(id)
    if (!prev) byId.set(id, marker)
  }
  // 同 (date|label|tone) 只留最新确认(id 大)的那一条
  const bySlot = new Map<string, KlineAnnotationMarker>()
  for (const marker of [...byId.values()].sort((a, b) => a.annotationId - b.annotationId)) {
    bySlot.set(`${marker.date}|${marker.label}|${marker.tone}`, marker)
  }
  return [...bySlot.values()].sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.annotationId - b.annotationId))
}

/** 稳定空数组(父组件 memo / effect 依赖身份稳定)。 */
const EMPTY_ANNOTATION_MARKERS: KlineAnnotationMarker[] = []
