/**
 * GS 区间色带(G区/S区)纯逻辑(2026-10-11 决策先锋主图 §1)。
 *
 * 规格 §1: GS策略是**非函数指标** —— 信号出现后次日不消失, 处于 G 区间直到出现 S
 * (反之亦然)。主图除 G/S 买卖点标记外, 还要用**色带**把当前所处区间画出来:
 *   G区=上方紫色短柱 / S区=绿色短柱(规格 §1; 颜色为跨主题视觉常量, 与既有
 *   操盘线黄线 rgba(234,179,8) 同类)。
 *
 * 本模块只放**纯函数**(可单测, 无 DOM/LWC 依赖):
 *   - `gsZonePoints` —— 由 GS 买卖点序列 + K 线日期序列, 逐根推出所属区间;
 *   - `GS_ZONE_COLOR` —— 区间 → 柱色。
 *
 * 诚实口径(与后端 `gs_strategy.compute_gs_signals` 同约定):
 *   - **只用已确认(confirmed)信号**建立区间 —— 末根 `pending`(盘中价疑似交叉)不据它染色,
 *     防\"末根假交叉\"把整张图染成未定的区间;
 *   - 首个已确认信号之前的 K 线**无区间**(不画, 不猜 G/S);
 *   - 日期不可解析/信号脏值一律跳过(不编造)。
 */

export type GsZone = 'G' | 'S'

export interface GsZonePoint {
  /** 'YYYY-MM-DD' */
  date: string
  zone: GsZone
}

export interface GsSignalLite {
  date: string
  side: 'G' | 'S'
  /** 收盘确认=true; 盘中疑似=false/缺省。缺省按**未确认**处理(不据它染色)。 */
  confirmed?: boolean
}

/** 区间 → 柱色。G区紫(规格 §1 上方紫色短柱) / S区绿。 */
export const GS_ZONE_COLOR: Record<GsZone, string> = {
  G: 'rgba(168, 85, 247, 0.38)',
  S: 'rgba(34, 197, 94, 0.38)',
}

function _day(d: unknown): string | null {
  if (typeof d !== 'string') return null
  const s = d.trim().slice(0, 10)
  return /^\d{4}-\d{2}-\d{2}$/.test(s) ? s : null
}

/**
 * 逐根 K 线推出所属 GS 区间(升序)。
 *
 * @param signals  GS 买卖点(可含未确认 pending); 只有 confirmed=true 的参与建区。
 * @param barDates K 线日期序列(升序, 'YYYY-MM-DD' 或更长字符串; 只取前 10 位)。
 * @returns 升序 `[{date, zone}]`; 首个已确认信号之前 / 无信号 → 对应日期不出现在结果里。
 */
export function gsZonePoints(
  signals: readonly GsSignalLite[] | null | undefined,
  barDates: readonly string[] | null | undefined,
): GsZonePoint[] {
  const anchors: Array<{ date: string; zone: GsZone }> = []
  for (const s of signals || []) {
    if (!s || (s.side !== 'G' && s.side !== 'S') || !s.confirmed) continue
    const d = _day(s.date)
    if (d) anchors.push({ date: d, zone: s.side })
  }
  anchors.sort((a, b) => a.date.localeCompare(b.date))

  const out: GsZonePoint[] = []
  let cur: GsZone | null = null
  let ai = 0
  for (const raw of barDates || []) {
    const d = _day(raw)
    if (!d) continue
    while (ai < anchors.length && anchors[ai].date <= d) {
      cur = anchors[ai].zone
      ai++
    }
    if (cur) out.push({ date: d, zone: cur })
  }
  return out
}
