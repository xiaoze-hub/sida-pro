/**
 * 口径词典(单一来源) —— 决策先锋官方投教口径, 2026-09-18 由用户逐条给定。
 *
 * **为什么单独一个模块**: 这几个词(明盘/暗盘/GS 信号)在页面上被反复引用, 一旦各处各写一版,
 * 就会出现"同一指标两种说法"——比数字错更难发现。所有 UI 文案必须引用这里, 不许就地重写;
 * 有测试(`tests/lib/caliber-glossary.test.ts`)把关键事实钉住, 改动必须是有意的。
 *
 * ## 官方口径(原文要点)
 * - **GS 信号** = AI 大数据多因子**机会 / 风险**信号, **盘中实时**。
 *   · **G 信号 = 机会 + 防卖飞**（趋势转好, 提示别过早卖出）
 *   · **S 信号 = 震荡或下跌 + 避深套**（提示别在弱势里硬扛）
 * - **主力明盘资金**(主力大单) = 免费软件**只统计单笔 >30 万**的单子。
 *   **局限**: 散户买卖 30 万也被算进去 → **不能代表真正的主力**。
 * - **主力暗盘资金** = 公募/私募/机构/游资/量化**拆单**资金: 例如某机构买入 1 亿, 用 AI 量化拆单工具
 *   把它拆成很多 **30 万以下的买单** → **不计入明盘**; 但这些都是**同一账户**买入的 → 记为**暗盘**。
 *
 * ## 使用纪律
 * - 明盘/暗盘是**两套口径**, 数字不可互相校准、不可相加成一个"总主力"(仓库硬约束: 方向冲突时优先采信逐笔);
 * - 页面里凡出现"主力"二字, 必须带口径徽标(明盘 / 暗盘 / 逐笔), 否则读者会以为在说同一件事。
 */

export interface GlossaryEntry {
  /** 展示名(用作徽标/标题) */
  name: string
  /** 一句话定义(可直接放 tooltip 首行) */
  oneLine: string
  /** 逐条要点(渲染成短列表; 每条都是一个**可验证的事实**, 不是形容词) */
  points: string[]
  /** 常见误读(用来提醒读者不要怎么理解) */
  caveat?: string
}

/** GS 信号(AI 大数据多因子机会 / 风险, 盘中实时)。 */
export const GS_SIGNAL: GlossaryEntry & {
  g: { label: string; meaning: string }
  s: { label: string; meaning: string }
} = {
  name: 'GS 信号',
  oneLine: 'AI 大数据多因子机会 / 风险信号，盘中实时更新。',
  points: [
    'G 信号 = 机会 + 防卖飞',
    'S 信号 = 震荡或下跌 + 避深套',
  ],
  caveat: '是机会/风险提示，不是买卖指令；颜色遵循 A 股惯例（红=机会方向，绿=风险方向）。',
  g: { label: 'G', meaning: '机会 + 防卖飞' },
  s: { label: 'S', meaning: '震荡或下跌 + 避深套' },
}

/** 主力明盘资金(主力大单) —— 免费软件口径。 */
export const MINGPAN: GlossaryEntry = {
  name: '主力明盘资金',
  oneLine: '免费软件统计的单笔超过 30 万的成交，也就是常说的“主力大单”。',
  points: [
    '口径：只统计单笔 > 30 万的单子',
    '散户买卖 30 万也计入其中',
    '所以它不能代表真正的主力',
  ],
  caveat: '主力可以把大单拆小、可以对倒做量，明盘因此并不等于主力真实动向。',
}

/** 主力暗盘资金 —— 拆单口径。 */
export const ANPAN: GlossaryEntry = {
  name: '主力暗盘资金',
  oneLine: '公募、私募、机构、游资、量化用拆单工具下单所形成的资金。',
  points: [
    '一笔大单被拆成很多 30 万以下的买单，因此不计入明盘',
    '例如某机构买入 1 亿，拆成大量小单，明盘看不到',
    '但这些单是同一账户买入的，这部分记为暗盘',
  ],
  caveat: '暗盘与明盘是两套口径，数字不能互相校准，也不能相加成一个“总主力”。',
}

/** 数据源口径徽标用词(与仓库既有徽标一致: tick / eastmoney4 / ths)。 */
export const CALIBER_BADGE: Record<'tick' | 'eastmoney4' | 'ths' | 'dark', string> = {
  tick: '逐笔',
  eastmoney4: '东财四档',
  ths: '同花顺',
  dark: '暗盘',
}

/** 给 tooltip 用的多行文本(各页统一格式: 定义 → 要点 → 提醒)。 */
export function glossaryTooltip(e: GlossaryEntry): string {
  const pts = e.points.map((p) => `· ${p}`).join('\n')
  return `${e.oneLine}\n${pts}${e.caveat ? `\n${e.caveat}` : ''}`
}

/** GS 徽标文案: `G 机会+防卖飞` / `S 震荡或下跌+避深套`。 */
export function gsBadge(kind: 'G' | 'S'): string {
  return kind === 'G' ? `${GS_SIGNAL.g.label} ${GS_SIGNAL.g.meaning}` : `${GS_SIGNAL.s.label} ${GS_SIGNAL.s.meaning}`
}
