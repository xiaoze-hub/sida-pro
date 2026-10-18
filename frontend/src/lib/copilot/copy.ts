/**
 * 参谋文案唯一源（被 `tests/components/copilot-copy-contract.test.ts` 正则扫描）。
 *
 * 为什么要收口：合规词表是**硬门禁**（设计稿 §1.4）。文案散落在各组件里时，
 * CI 无法判定"某处又写了一句买卖指令"，只能靠人肉 review —— 收在这一个文件里，
 * 一条正则就能拦住整站回归。
 *
 * 禁用词（命中即红）：单点买卖指令、主力动作定性、收益承诺、仓位极端词。
 * 具体词表见 `tests/components/copilot-copy-contract.test.ts` 的 FORBIDDEN 正则 ——
 * 本文件正文**不复述**这些词，否则门禁会把注释本身当成违规文案。
 * 允许的客观口径：底仓成本 / 筹码沉淀区 / 高位筹码兑现压力 /
 * 加仓观察位 / 减仓防守位 / 观察不动 / 分批 / 控制单笔仓位。
 */
import type { Tone } from './types'

/** 每张卡片 / 胶囊 / 一级页底部固定尾注（合规锚点，不得删） */
export const DISCLAIMER = '以上为盘口与筹码数据的客观推演，不构成投资建议。'

/** 卡片标题（三个触点同一心智：数智参谋） */
export const CARD_TITLE = '数智参谋'
export const CARD_SUBTITLE = '筹码 · 共振 · 量价 · 盘口 · 竞价 · 妖股因子'

/** 三段式决策输出的三个小标题（对应 verdict 的三个字段） */
export const SECTION_INTENT = '意图定性'
export const SECTION_DEFENSE = '关键防守位'
export const SECTION_PLAN = '操盘预案'
/** 证据条标题（哪些证据可用 / 权限不足 / 缺失） */
export const EVIDENCE_LABEL = '证据'

export const TONE_META: Record<Tone, { label: string; badge: string; bar: string; icon: string }> = {
  opportunity: {
    label: '机会',
    badge: 'bg-role-opp/15 text-role-opp border-role-opp/40',
    bar: 'bg-role-opp',
    icon: '▲',
  },
  neutral: {
    label: '中性',
    badge: 'bg-s3 text-muted-foreground border-border/60',
    bar: 'bg-role-system',
    icon: '＝',
  },
  caution: {
    label: '谨慎',
    badge: 'bg-role-watch/15 text-role-watch border-role-watch/40',
    bar: 'bg-role-watch',
    icon: '!',
  },
  risk: {
    label: '风险',
    badge: 'bg-role-risk/15 text-role-risk border-role-risk/40',
    bar: 'bg-role-risk',
    icon: '▼',
  },
}

export const DEFENSE_LABEL: Record<string, string> = {
  first: '第一防守',
  core: '核心生命线',
  probe: '弹簧针低点',
  cost: '底仓成本',
  cap: '强压制',
}

/** 交易阶段（后端 session.phase 原样映射，前端不自己按时钟推算） */
export const SESSION_LABEL: Record<string, string> = {
  pre_open: '盘前',
  auction: '竞价',
  intraday: '盘中',
  closed: '收盘',
  non_trading: '非交易日',
}
export const STALE_HINT = '快照可能过期'

export const AUDIENCE_LABEL = { holder: '持仓者', empty: '空仓者' } as const

export const EVIDENCE_NAME: Record<string, string> = {
  chips: '底仓筹码',
  resonance: '三指标共振',
  microstructure: '盘口撤单',
  wyckoff: '量价结构',
  auction: '竞价异动',
  demon: '妖股因子',
  theme: '主线身位',
}

/** 降级时的诚实文案：原因来自后端，不在这里编 */
export const DEGRADED_HINT = (reason: string | null) => (reason ? `暂无：${reason}` : '暂无数据')
export const GATED_HINT = '需要更高权限，升级后可见'
export const SNAPSHOT_HINT = (d: string | null) => (d ? `收盘快照 ${d}` : '收盘快照')

/** 关键价位缺失时的诚实降级（不编价位） */
export const NO_DEFENSE_HINT = '不给防守线（不猜价位）'
export const NO_PLAN_HINT = '筹码 / 盘口数据不完整，本次不给具体操作价位。'

/** 空态 / 加载 / 失败（失败只陈述原文，不推断原因） */
export const LOADING_HINT = '正在汇总筹码、共振、盘口与量价证据…'
export const EMPTY_HINT = '暂无参谋结论：先看下方历史建议流水。'
export const ERROR_TITLE = '参谋数据暂不可用'
export const RETRY = '重试'
export const REFRESH = '刷新'

/** 胶囊（个股工作台右下角） */
export const CAPSULE_FALLBACK = '盘口参谋'
export const CAPSULE_HIDE = '收起参谋胶囊'
export const CAPSULE_RESTORE = '重新展开参谋胶囊'
export const CAPSULE_HINT = '打开盘口参谋（已预注入当前标的上下文）'
/** 拖拽提示：胶囊可自由移动并吸附左右边缘（位置按浏览器记忆）。 */
export const CAPSULE_DRAG_HINT = '按住拖动可移动 · 点击唤醒数智参谋'

/** 胶囊零输入唤醒时预置的三个追问（会随会话注入 system prompt） */
export const QUICK_QUESTIONS = [
  '主力底仓成本在什么位置？现在获利盘多少？',
  '买方是不是在挂假托单？撤单率说明了什么？',
  '跌到多少必须止损？关键防守位在哪？',
]

/** /ai-copilot 一级页 */
export const COPILOT_PAGE_TITLE = '数智参谋'
export const COPILOT_PAGE_SUBTITLE = '筹码 · 共振 · 量价 · 盘口 · 竞价 · 妖股因子 全量穿透'
export const PRESET_FALLBACK_HINT = '指令池暂不可用（接口失败），可直接在右侧提问。'
export const PRESET_GROUP_LABEL: Record<string, string> = {
  auction: '竞价',
  chip: '筹码',
  resonance: '共振',
  demon: '妖股',
  defense: '防守',
}
export const SEND = '发送'
export const PAGE_EMPTY_EXAMPLES = [
  '今天 9:25 谁在爆量抢筹？',
  '600111 底仓成本多少，现在追安全吗？',
  '我的持仓里谁已经跌破 70% 筹码下沿？',
]
