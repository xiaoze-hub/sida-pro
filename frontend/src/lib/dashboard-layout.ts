/**
 * 看板轻量定制纯逻辑 (A1, 2026-09-10 借鉴 OpenTerminal Widget 工作台, 轻量版):
 * 模块显隐 + 组内上下排序 + localStorage 持久化。不做自由拖拽(设计语言冲突, 方案 §三 A1)。
 *
 * 布局分 3 组(与 Dashboard.tsx 的实际网格一一对应, 组间不可互换, 组内排序):
 *   main      全宽纵列(指数条/KPI/市场全景/资金流)
 *   duo       双列(异动池|涨跌分布)
 *   workspace 工作台+次级同一 12 列网格(要紧事|体检|机会精选|最新报告|机会发现)
 */

export type DashboardGroup = 'main' | 'duo' | 'workspace'

export type DashboardModuleId =
  | 'indices' | 'kpi' | 'overview' | 'fundflow'
  | 'anomalies' | 'breadth'
  | 'agenda' | 'portfolio' | 'picks'
  | 'reports' | 'discover'

export interface DashboardModuleDef {
  id: DashboardModuleId
  label: string
  group: DashboardGroup
}

export const DASHBOARD_MODULES: DashboardModuleDef[] = [
  { id: 'indices', label: '指数走势', group: 'main' },
  { id: 'kpi', label: '市场 KPI 带', group: 'main' },
  { id: 'overview', label: '市场全景(情绪·主线)', group: 'main' },
  { id: 'fundflow', label: '大盘资金流', group: 'main' },
  { id: 'anomalies', label: '异动池', group: 'duo' },
  { id: 'breadth', label: '涨跌分布', group: 'duo' },
  { id: 'agenda', label: '今日要紧事', group: 'workspace' },
  { id: 'portfolio', label: '组合体检', group: 'workspace' },
  { id: 'picks', label: '机会精选', group: 'workspace' },
  { id: 'reports', label: '最新报告', group: 'workspace' },
  { id: 'discover', label: '机会发现', group: 'workspace' },
]

export const DASHBOARD_GROUP_LABEL: Record<DashboardGroup, string> = {
  main: '全宽区',
  duo: '双列区',
  workspace: '工作台与次级',
}

export const DASHBOARD_LAYOUT_KEY = 'panwatch_dashboard_layout_v1'

export interface DashboardLayout {
  /** 全局顺序(渲染时各组取交集并按此排序) */
  order: DashboardModuleId[]
  hidden: DashboardModuleId[]
}

const ALL_IDS: DashboardModuleId[] = DASHBOARD_MODULES.map((m) => m.id)
const ID_SET = new Set<string>(ALL_IDS)

export function defaultLayout(): DashboardLayout {
  return { order: [...ALL_IDS], hidden: [] }
}

const groupOf = (id: DashboardModuleId): DashboardGroup =>
  DASHBOARD_MODULES.find((m) => m.id === id)!.group

/** 容错归一: 丢弃未知 id(旧版本遗留), 缺失 id 按默认顺序补到末尾, hidden 去重。 */
export function normalizeLayout(raw: unknown): DashboardLayout {
  const def = defaultLayout()
  if (raw === null || typeof raw !== 'object') return def
  const o = raw as { order?: unknown; hidden?: unknown }
  const rawOrder = Array.isArray(o.order) ? o.order : []
  const seen = new Set<string>()
  const order: DashboardModuleId[] = []
  for (const x of rawOrder) {
    if (typeof x === 'string' && ID_SET.has(x) && !seen.has(x)) {
      order.push(x as DashboardModuleId)
      seen.add(x)
    }
  }
  for (const id of ALL_IDS) if (!seen.has(id)) order.push(id)
  const rawHidden = Array.isArray(o.hidden) ? o.hidden : []
  const hidden = ALL_IDS.filter((id) => rawHidden.includes(id))
  return { order, hidden }
}

export function isHidden(layout: DashboardLayout, id: DashboardModuleId): boolean {
  return layout.hidden.includes(id)
}

export function toggleModule(layout: DashboardLayout, id: DashboardModuleId): DashboardLayout {
  const hidden = isHidden(layout, id)
    ? layout.hidden.filter((x) => x !== id)
    : [...layout.hidden, id]
  return { ...layout, hidden }
}

/** 同一组内与相邻模块交换(组边界为 no-op); 递归跳过被隐藏的槽位由调用方语义决定 —— 这里相邻=配置序相邻。 */
export function moveModule(
  layout: DashboardLayout,
  id: DashboardModuleId,
  dir: -1 | 1,
): DashboardLayout {
  const group = groupOf(id)
  const members = layout.order.filter((x) => groupOf(x) === group)
  const pos = members.indexOf(id)
  if (pos < 0) return layout
  const target = members[pos + dir]
  if (target === undefined) return layout // 组内边界, no-op
  const order = [...layout.order]
  const a = order.indexOf(id)
  const b = order.indexOf(target)
  order[a] = target
  order[b] = id
  return { ...layout, order }
}

export function resetLayout(): DashboardLayout {
  return defaultLayout()
}

/** 渲染用: 组内第几个(赋给 CSS order); 未配置的放末尾。 */
export function orderIndex(layout: DashboardLayout, id: DashboardModuleId): number {
  const group = groupOf(id)
  const members = layout.order.filter((x) => groupOf(x) === group)
  return members.indexOf(id)
}

interface ReadStorage { getItem(key: string): string | null }
interface WriteStorage { setItem(key: string, value: string): void }

export function loadLayout(storage?: ReadStorage): DashboardLayout {
  try {
    const st = storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined)
    if (!st) return defaultLayout()
    const raw = st.getItem(DASHBOARD_LAYOUT_KEY)
    if (!raw) return defaultLayout()
    return normalizeLayout(JSON.parse(raw))
  } catch {
    return defaultLayout() // 存储损坏 → 回默认, 不崩
  }
}

export function saveLayout(layout: DashboardLayout, storage?: WriteStorage): void {
  try {
    const st = storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined)
    if (!st) return
    st.setItem(DASHBOARD_LAYOUT_KEY, JSON.stringify(layout))
  } catch {
    // 隐私模式/配额满 → 静默降级为"本次会话有效", 不影响功能
  }
}
