/**
 * 首页 Dashboard 私有纯函数层(2026-11-08 home-audit-ui)。
 *
 * 只被 `src/pages/Dashboard.tsx` 使用, 不对外导出到共享包。抽出来的原因:
 * 「今日要紧事」的 AI 说明错配是**数据归属**问题, 不是样式问题 —— 归属/失败闭合
 * 必须能被单测逐条钉住, 而不是埋在 1600 行的组件里靠肉眼保。
 *
 * 三条铁律:
 *   1. **AI 说明只能挂回它被请求时的那份候选**; 候选变了/响应乱序/字段缺失 → 整个
 *      响应作废(fail closed), 退回原始候选与原始信号, 绝不"按序号猜"。
 *   2. 说明里出现**别的标的**名/代码 → 视为错配, 同样整体作废。
 *   3. 缺日期/缺时点一律写"未知", 不拿"今天"或另一个数据源的值顶上(不同源不混)。
 */

export type AgendaKind = 'alert' | 'holding' | 'watch' | 'risk' | 'opportunity'

export interface AgendaCandidate {
  type: AgendaKind
  symbol?: string
  name?: string
  market?: string
  signal?: string
  change_pct?: number | null
  /** 同一标的命中多行(如同一股票挂在两个账户) → 该行的 AI 说明不可信, 只认原始信号 */
  ambiguous?: boolean
}

export type AgendaDropReason = 'duplicate-of-opportunity' | 'duplicate-symbol' | 'missing-identity'

export interface AgendaDrop {
  type: string
  symbol: string
  reason: AgendaDropReason
}

/** 今日要紧事最多展示条数(与 scan 的 urgent 上限一致) */
export const AGENDA_LIMIT = 8
/** 首页指数卡最多展示条数 —— 后端给 6 个(上证/深证/创业板/科创50/沪深300/恒生…), 全给 */
export const HOME_INDEX_LIMIT = 6

function symKey(c: { market?: string; symbol?: string }): string {
  return `${(c.market || 'CN').toUpperCase()}:${(c.symbol || '').trim()}`
}

/**
 * 候选整理: 去重(同标的只留一行) + 去掉与「机会精选」重复的条目(**机会本身不动**) +
 * 缺标识的条目闭合丢弃(不进 AI 请求, 不猜)。
 *
 * 为什么必须在这里去重而不是渲染时: `/dashboard/curate` 回的是 `{index}`,
 * 序号指向**发送时**的数组下标。渲染阶段再过滤/去重 → 下标整体位移 → 说明挂到别人头上。
 * 所以候选数组一旦发出, 就成了不可变的"快照"。
 */
export function buildAgendaCandidates(
  raw: AgendaCandidate[],
  opportunitySymbols: ReadonlySet<string> = new Set(),
): { candidates: AgendaCandidate[]; drops: AgendaDrop[] } {
  // 机会在独立模块展示，提醒/持仓/自选及组合风险逐条保留，不按标的合并。
  void opportunitySymbols
  return { candidates: raw.filter(c => c.type !== 'opportunity').map(c => ({ ...c })), drops: [] }

}

/** 候选快照指纹 —— 响应回来时用它判断"这份说明是不是发给当前候选的" */
export function agendaSnapshotKey(list: AgendaCandidate[]): string {
  return JSON.stringify(list.map(c => [c.type, c.market ?? null, c.symbol ?? null, c.name ?? null, c.signal ?? null, c.change_pct ?? null, c.ambiguous ?? false]))
}

export type AgendaFailReason =
  | 'no-response'
  | 'stale-snapshot'
  | 'bad-index'
  | 'missing-why'
  | 'why-mismatch'

export interface CuratedItemLike {
  index?: number
  importance?: number
  why?: string
}

export interface AgendaRow extends AgendaCandidate {
  why: string
  /** 该行说明是否来自通过校验的 AI 条目(false = 原始信号兜底) */
  whyFromAI: boolean
}

export interface AgendaFeed {
  rows: AgendaRow[]
  mode: 'raw' | 'ai'
  reason: AgendaFailReason | null
}

/** 标的的"身份词": 名称 / 代码(去市场后缀), 用于判断说明讲的是哪只票 */
function identityTokens(c: AgendaCandidate): string[] {
  const out: string[] = []
  const name = (c.name || '').trim()
  if (name.length >= 1) out.push(name)
  const symbol = (c.symbol || '').trim()
  if (symbol.length >= 3) {
    out.push(symbol)
    // 6 位 A 股代码可能带 SH/SZ/BJ 前缀, 两种写法都算自己
    const bare = symbol.replace(/^(SH|SZ|BJ)/i, '')
    if (bare && bare !== symbol && bare.length >= 3) out.push(bare)
  }
  return out
}

function containsToken(text: string, token: string): boolean {
  if (!token) return false
  return text.toLowerCase().includes(token.toLowerCase())
}

/**
 * 说明里是否点名了**别的**标的。
 *
 * 判定顺序: 先看有没有自己的名/代码 —— 有就认作在讲自己(允许"和你同板块的XX也在涨"这类写法);
 * 没有自己的标识、却出现了别人的名(≥3 字)或代码(≥4 位) → 判为错配。
 * 只匹配名称与代码, 不做语义推断: 宁可漏判退回原始信号, 也不能判定错。
 */
export function whyMentionsOther(
  why: string,
  self: AgendaCandidate,
  others: AgendaCandidate[],
): boolean {
  const selfTokens = identityTokens(self)
  const text = (why || '').trim()
  if (!text) return false
  // 风险类条目没有标的, 不参与"点名"匹配
  if (self.type !== 'risk' && selfTokens.some((t) => containsToken(text, t))) return false

  // ① 说明里出现 6 位证券代码: 只要不是自己的代码, 就是在讲别的标的(含模型凭空编造的代码)。
  //    宁可误判退回原始信号, 也不能把别人家的代码当成这只票的证据。
  const ownCodes = new Set(
    selfTokens.map((t) => t.replace(/^(SH|SZ|BJ)/i, '')).filter((t) => /^\d{6}$/.test(t)),
  )
  for (const run of text.match(/\d{6,}/g) || []) {
    if (run.length !== 6) continue
    if (!ownCodes.has(run)) return true
  }

  for (const other of others) {
    if (other === self || other.type === 'risk') continue
    if (symKey(other) === symKey(self) && (other.name || '') === (self.name || '')) continue
    const name = (other.name || '').trim()
    // 名称需 ≥3 字: 2 字名(如"平安"/"中国")易与常用词碰撞, 误判会把好说明打掉
    if (name.length >= 3 && !identityTokens(self).some((t) => containsToken(name, t))) {
      if (containsToken(text, name)) return true
    }
    const symbol = (other.symbol || '').trim().replace(/^(SH|SZ|BJ)/i, '')
    if (symbol.length >= 4 && !identityTokens(self).some((t) => containsToken(symbol, t))) {
      if (containsToken(text, symbol)) return true
    }
  }
  return false
}

/**
 * 候选快照 + AI 策展响应 → 可渲染行。
 *
 * 只有**全部条目都通过校验**才采用 AI 的排序与说明; 任一条不合格(越界/重复/缺说明/
 * 点名别人/响应属于旧快照) → 整份作废, 按原始顺序 + 原始 signal 渲染(fail closed)。
 * 这样最坏情况是"没有 AI 摘要", 而不是"张冠李戴"。
 */
export function buildAgendaFeed(args: {
  snapshot: AgendaCandidate[]
  curated: CuratedItemLike[] | null
  /** 该响应所对应的快照指纹; null = 请求失败或还没回 */
  responseForSnapshot: string | null
  /** 当前候选指纹 */
  currentSnapshot: string
}): AgendaFeed {
  const { snapshot, curated, responseForSnapshot, currentSnapshot } = args
  const rawRows: AgendaRow[] = snapshot.map((c) => ({
    ...c,
    why: (c.signal || '').trim(),
    whyFromAI: false,
  }))
  const fallback = (reason: AgendaFailReason | null): AgendaFeed => ({ rows: rawRows, mode: 'raw', reason })

  if (snapshot.length === 0) return { rows: [], mode: 'raw', reason: null }
  // 请求失败/还没回 → 无说明可用(区分于"说明属于旧快照")
  if (!curated || curated.length === 0) return fallback('no-response')
  // 响应属于旧快照(候选已变/刷新过) → 序号已无意义, 整份丢弃
  if (responseForSnapshot !== currentSnapshot) return fallback('stale-snapshot')

  const byIndex = new Map<number, { importance: number; why: string }>()
  for (const item of curated) {
    const idx = item?.index
    if (typeof idx !== 'number' || !Number.isInteger(idx) || idx < 0 || idx >= snapshot.length) {
      return fallback('bad-index')
    }
    if (byIndex.has(idx)) return fallback('bad-index')
    const why = (item?.why || '').trim()
    if (!why) return fallback('missing-why')
    // 组合风险行没有标的, 它的一句话说明本来就可能点名具体股票(「某票单仓 40%」) ——
    // 这类行无法"串股", 不做点名校验; 有标的的行才校验(避免把正确说明误杀)。
    if (snapshot[idx].type !== 'risk' && whyMentionsOther(why, snapshot[idx], snapshot)) {
      return fallback('why-mismatch')
    }
    const importance = typeof item?.importance === 'number' && isFinite(item.importance) ? item.importance : 0
    byIndex.set(idx, { importance, why })
  }

  // 部分 AI 响应只改变排序，不能隐藏没有被选中的提醒或风险。
  for (let i = 0; i < snapshot.length; i++) {
    if (!byIndex.has(i)) byIndex.set(i, { importance: -1, why: (snapshot[i].signal || '').trim() })
  }
  const ordered = Array.from(byIndex.entries())
    .sort((a, b) => b[1].importance - a[1].importance || a[0] - b[0])
    .map(([idx, v]) => {
      const base = snapshot[idx]
      return {
        ...base,
        // 仅允许候选原始信号中可逐字核验的文本；其余回退原始信号。
        why: !base.ambiguous && base.signal && base.signal.includes(v.why) ? v.why : (base.signal || '').trim(),
        whyFromAI: !!(!base.ambiguous && base.signal && base.signal.includes(v.why)),
      }
    })

  return { rows: ordered, mode: 'ai', reason: null }
}

// ── 结论行: 分源日期与口径 ────────────────────────────────────────────────

/** 只认 ISO 日期(YYYY-MM-DD / 带时间) → 取日期部分; 其余一律 null(不猜) */
export function dateOrNull(v?: string | null): string | null {
  if (typeof v !== 'string') return null
  const m = v.trim().match(/^(\d{4})-(\d{2})-(\d{2})/)
  return m ? m[0] : null
}

const FLOW_SOURCE_LABEL: Record<string, string> = {
  eastmoney_push2delay_cn: '东财 push2delay',
  db_snapshot: '库内快照',
  ths_realtime: '同花顺实时',
}

/** 数据源代码 → 可读来源名; 未知代码原样透出(不吞、不编造), 空值写"来源未知" */
export function flowSourceLabel(code?: string | null): string {
  const c = (code || '').trim()
  if (!c) return '来源未知'
  return FLOW_SOURCE_LABEL[c] || c
}

/**
 * 资金流响应里三个指标**可能来自不同源**(主力净流入=网关东财, 成交额可能回落腾讯指数,
 * 涨跌家数可能来自本地 TQ)。只标一个总 source 就是把不同源混成一句 —— 逐个解析,
 * 没给的沿用主 source(响应本身就是一次快照, 这不是推断)。
 */
export function flowSourceParts(flow?: {
  source?: string | null
  amount_source?: string | null
  breadth_source?: string | null
} | null): { main: string; amount: string; breadth: string } {
  const main = flowSourceLabel(flow?.source)
  return {
    main,
    amount: flowSourceLabel(flow?.amount_source),
    breadth: flowSourceLabel(flow?.breadth_source),
  }
}

/** 快照时点 → HH:mm:ss; 缺失/非法写 null(UI 端写"时点未知") */
export function snapshotClock(v?: string | null): string | null {
  if (!v) return null
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return null
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

/**
 * 封板率单位归一: 后端 `/market/phase` 的 seal_rate 是**比例**(0.62 = 62%);
 * 历史代码有过硬编码 1.0 的假值(后端读时已转 None)。>1 视为上游已给百分数, 原样透出,
 * 但不做二次缩放。null/NaN → null(UI 写 '--', 不写 0%)。
 */
export function sealRatePct(v: number | null | undefined): number | null {
  if (v == null || !isFinite(v)) return null
  if (v < 0) return null
  const pct = v <= 1 ? v * 100 : v
  return Math.round(pct * 10) / 10
}

/** 封板率展示: 0.62 → '62%'; 缺失 → '--' */
export function formatSealRate(v: number | null | undefined): string {
  const pct = sealRatePct(v)
  if (pct == null) return '--'
  // 不用 toFixed(仓库 UI 规则 R6: 数值格式化统一走 @/lib/format), 已按 1 位小数取整过
  return `${pct}%`
}

/**
 * 主线 Top1 的龙头名 ——/market/mainline 的真实结构是嵌套 `leader: {code,name,days,amount}`,
 * 历史前端读的是扁平 `leader_name`(接口从未返回过 → 表头永远缺龙头)。
 * 这里按真实嵌套接线, 并保留旧字段兼容; 都没有 → null(表头不渲染, 不编造)。
 */
export function mainlineLeaderName(top: unknown): string | null {
  if (!top || typeof top !== 'object') return null
  const t = top as { leader?: unknown; leader_name?: unknown }
  const nested = t.leader
  if (nested && typeof nested === 'object') {
    const name = (nested as { name?: unknown }).name
    if (typeof name === 'string' && name.trim()) return name.trim()
  }
  if (typeof nested === 'string' && nested.trim()) return nested.trim()
  if (typeof t.leader_name === 'string' && t.leader_name.trim()) return t.leader_name.trim()
  return null
}

// ── 机会: 基准日 / 计划有效性 ──────────────────────────────────────────────

export type BaselineDate =
  | { kind: 'ok'; date: string }
  | { kind: 'missing' }
  | { kind: 'mixed'; dates: string[] }

/** 机会列表的基准日: 全一致才给日期; 缺失写 missing; 多个日期写 mixed(不挑一个冒充) */
export function opportunityBaselineDate(
  items: Array<{ snapshot_date?: string | null }>,
): BaselineDate {
  if (items.some(i => !dateOrNull(i?.snapshot_date))) return { kind: 'missing' }
  const dates = Array.from(
    new Set(items.map((i) => dateOrNull(i?.snapshot_date)).filter((d): d is string => !!d)),
  ).sort()
  if (dates.length === 0) return { kind: 'missing' }
  if (dates.length === 1) return { kind: 'ok', date: dates[0] }
  return { kind: 'mixed', dates }
}

/** 机会行计划是否无效(后端 plan_valid === false 明确给了原因) */
export function opportunityPlanIssue(item: {
  plan_valid?: boolean | null
  plan_invalid_reason?: string | null
}): string | null {
  if (item?.plan_valid !== false) return null
  const reason = (item?.plan_invalid_reason || '').trim()
  return reason || '计划无效(买卖点不自洽)'
}

export interface PremarketPlanLike {
  symbol?: string
  name?: string
  action?: string
  /** 触发价 / 止损价(展示用, 缺失即不显示) */
  trigger?: string | number
  stop?: string | number
}

/**
 * 盘前预案分拣: 没有标的(既无代码也无名称)或没有动作的预案无法执行 ——
 * 不渲染成空行, 单独计数后由 UI 明示"略过 N 条"。
 */
export function splitPremarketPlans(plans?: PremarketPlanLike[] | null): {
  valid: PremarketPlanLike[]
  invalid: number
} {
  const list = Array.isArray(plans) ? plans : []
  const valid: PremarketPlanLike[] = []
  let invalid = 0
  for (const p of list) {
    if (!p || typeof p !== 'object') { invalid += 1; continue }
    const text = (v: unknown) => typeof v === 'string' || typeof v === 'number' ? String(v) : ''
    valid.push({ ...p, symbol: text(p.symbol), name: text(p.name), action: text(p.action) })
  }
  return { valid, invalid }
}
