/**
 * 战术指令按「盘中时段」排序（2026-10-19, 数智参谋门槛 P0）。
 *
 * 原来 6 个按钮等权重平铺，用户不知道先点哪个。真实使用时想问什么是**跟着时段走的**：
 * 盘前关心今天埋伏什么，9:25 关心竞价谁在抢筹，盘中关心手上的票，收盘后关心复盘与共振。
 * 所以按 session 给 group 排优先级：前 3 个做「今日推荐」，其余收进「更多指令」。
 *
 * 纯函数：时间由调用方传入（便于单测，不依赖系统时钟）。
 */

export type Session = 'preopen' | 'auction' | 'intraday' | 'closed'

export interface PresetLike {
  id: string
  group?: string | null
  requires_perm?: string | null
}

/** 各时段 group 优先级（顺序即权重，靠前更靠前展示）。 */
const GROUP_PRIORITY: Record<Session, string[]> = {
  // 盘前（开盘前到 9:15）：先看全貌，再想埋伏
  preopen: ['dossier', 'chip', 'resonance', 'demon', 'auction', 'defense'],
  // 竞价（9:15–9:30）：竞价抢筹是唯一主角
  auction: ['auction', 'dossier', 'resonance', 'chip', 'demon', 'defense'],
  // 盘中：手上的票 + 防守位优先
  intraday: ['dossier', 'defense', 'chip', 'resonance', 'demon', 'auction'],
  // 收盘后：复盘 + 找共振/妖股
  closed: ['dossier', 'resonance', 'demon', 'chip', 'defense', 'auction'],
}

/** 按北京时间判断交易时段（用本地时间即可：本产品用户均在国内）。 */
export function sessionOf(now: Date = new Date()): Session {
  const day = now.getDay()
  if (day === 0 || day === 6) return 'closed' // 周末一律按收盘后
  const mins = now.getHours() * 60 + now.getMinutes()
  if (mins < 9 * 60 + 15) return 'preopen'
  if (mins < 9 * 60 + 30) return 'auction'
  if (mins <= 15 * 60) return 'intraday'
  return 'closed'
}

export const SESSION_LABEL: Record<Session, string> = {
  preopen: '盘前',
  auction: '竞价',
  intraday: '盘中',
  closed: '盘后',
}

/**
 * 排序并切成「今日推荐（3）」+「更多」。
 * 无 group 的指令排最后（自定义/兜底项不该抢推荐位）。
 */
export function orderPresetsForSession<T extends PresetLike>(
  presets: T[],
  session: Session,
): { primary: T[]; more: T[] } {
  const priority = GROUP_PRIORITY[session] || GROUP_PRIORITY.closed
  const rank = (p: T) => {
    const idx = p.group ? priority.indexOf(p.group) : -1
    return idx === -1 ? priority.length + 1 : idx
  }
  const sorted = [...presets].sort((a, b) => {
    const d = rank(a) - rank(b)
    return d !== 0 ? d : a.id.localeCompare(b.id) // 稳定排序，避免每次刷新顺序跳动
  })
  return { primary: sorted.slice(0, 3), more: sorted.slice(3) }
}
