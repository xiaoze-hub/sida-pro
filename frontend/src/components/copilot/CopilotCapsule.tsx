import { useEffect, useState } from 'react'
import { Sparkles } from 'lucide-react'
import { useCopilotBrief } from '@/hooks/useCopilotBrief'
import { safeFixed, safePercent } from '@/lib/format'
import {
  CAPSULE_FALLBACK,
  CAPSULE_HIDE,
  CAPSULE_HINT,
  CAPSULE_RESTORE,
  QUICK_QUESTIONS,
} from '@/lib/copilot/copy'
import { toneMeta } from '@/lib/copilot/tone'
import type { CopilotBrief } from '@/lib/copilot/types'

const hiddenKey = (symbol: string) => `copilot_capsule_hidden:${symbol}`

/**
 * 个股工作台右下角**常驻唤醒胶囊**（设计稿 §8.7，2026-10-18）。
 *
 * 只做两件事，绝不新写一套聊天：
 *   ① 轻量展示「当前标的 tone + 一句话定性」；
 *   ② 点击时派发 `panwatch-open-chat`，由既有 `ChatWidget` 建带 `initial_context` 的新会话
 *      （ChatWidget.tsx 已有监听，零新增通道 / 零 SSE 重复实现）。
 *
 * 布局避让：ChatWidget 的 FAB 在 `bottom-20 right-4 md:bottom-5`（48×48），
 * 胶囊放 `bottom-36 right-4 md:bottom-20`，两档都在 FAB 正上方并留 ~12px 空隙。
 *
 * 收起后**必须可恢复**：收起只写当前标的的 localStorage，同时原地缩成一个 ✨ 小圆钮
 * （常驻组件最常见的可用性事故就是"误点一次永久失去入口"）。
 */
export function CopilotCapsule({
  symbol,
  market = 'CN',
  stockName,
}: {
  symbol: string
  market?: string
  stockName?: string
}) {
  const { data } = useCopilotBrief(symbol, market, { timeoutMs: 15000 })
  const [hidden, setHidden] = useState(false)

  useEffect(() => {
    try {
      setHidden(localStorage.getItem(hiddenKey(symbol)) === '1')
    } catch {
      setHidden(false)
    }
  }, [symbol])

  const collapse = () => {
    try {
      localStorage.setItem(hiddenKey(symbol), '1')
    } catch {
      /* localStorage 不可用（隐私模式）→ 仅本会话收起，不报错 */
    }
    setHidden(true)
  }

  const restore = () => {
    try {
      localStorage.removeItem(hiddenKey(symbol))
    } catch {
      /* 同上 */
    }
    setHidden(false)
  }

  const open = () => {
    window.dispatchEvent(
      new CustomEvent('panwatch-open-chat', {
        detail: {
          symbol,
          market,
          stockName: stockName || data?.name || '',
          pageContext: buildSnapshot(symbol, data, stockName),
        },
      }),
    )
  }

  if (hidden) {
    return (
      <button
        type="button"
        onClick={restore}
        title={CAPSULE_RESTORE}
        aria-label={CAPSULE_RESTORE}
        data-testid="copilot-capsule-restore"
        className="fixed bottom-36 right-4 z-40 flex h-8 w-8 items-center justify-center rounded-full border border-role-watch/40 bg-s2 text-role-watch shadow-float hover:bg-s3 md:bottom-20"
      >
        <Sparkles className="h-3.5 w-3.5" />
      </button>
    )
  }

  const meta = data ? toneMeta(data.verdict.intent_tone) : null
  const label = data?.verdict.intent_label || CAPSULE_FALLBACK

  return (
    <div className="fixed bottom-36 right-4 z-40 md:bottom-20" data-testid="copilot-capsule">
      <div className="flex items-stretch overflow-hidden rounded-full border border-role-watch/40 bg-s2 shadow-float">
        <button
          type="button"
          onClick={open}
          title={CAPSULE_HINT}
          aria-label={CAPSULE_HINT}
          data-testid="copilot-capsule-open"
          className="flex h-9 items-center gap-1.5 px-3 text-[12px] text-foreground hover:bg-s3"
        >
          <Sparkles className="h-3.5 w-3.5 shrink-0 text-role-watch" />
          <span className="max-w-[132px] truncate">
            {meta ? `${meta.icon} ${label}` : `${CAPSULE_FALLBACK} · ${symbol}`}
          </span>
        </button>
        <button
          type="button"
          aria-label={CAPSULE_HIDE}
          title={CAPSULE_HIDE}
          onClick={collapse}
          data-testid="copilot-capsule-hide"
          className="flex h-9 w-7 items-center justify-center border-l border-border/40 text-[11px] text-muted-foreground hover:text-foreground"
        >
          ×
        </button>
      </div>
    </div>
  )
}

/**
 * 页面快照 → 注入 system prompt 的文本。
 *
 * 为什么要把 brief 压成文本：胶囊零输入唤醒时，模型第一句就该知道"用户在看哪只票、
 * 证据是什么"，否则会重新问一遍「你想了解哪只股票」——那是把副驾驶做成问答机器人。
 * 末尾**必须**提醒快照有时效：模型不得把 5 分钟前的数字当实时值复述。
 */
export function buildSnapshot(
  symbol: string,
  data: CopilotBrief | null,
  stockName?: string,
): string {
  if (!data) {
    return `[页面] 个股 ${symbol} ${stockName || ''}，用户从工作台胶囊进入，尚无参谋快照。`
  }
  const q = data.quote
  const chips = data.evidence.chips
  const micro = data.evidence.microstructure
  const res = data.evidence.resonance
  const b = micro.b_cancel_ratio
  const s = micro.s_cancel_ratio
  return [
    `[数智参谋快照 ${data.as_of}] ${data.symbol} ${data.name || stockName || ''}`,
    `现价 ${safeFixed(q.close, 2)}（${safePercent(q.change_pct, 2, true)}）`,
    `意图：${data.verdict.intent_label}（${data.verdict.intent_tone}，置信 ${safeFixed(
      data.verdict.confidence,
      2,
    )}）`,
    `底仓成本 ${safeFixed(chips.main_cost, 2)}；70%带 ${safeFixed(
      chips.cost_70_low,
      2,
    )}–${safeFixed(chips.cost_70_high, 2)}；获利盘 ${
      chips.profit_ratio == null ? '--' : `${safeFixed(chips.profit_ratio, 1)}%`
    }`,
    `三指标：${res.resonance_state || '--'}；机构活跃度 ${safeFixed(res.activity, 2)}`,
    `撤单：买撤 ${b == null ? '--' : `${safeFixed(b * 100, 1)}%`} / 卖撤 ${
      s == null ? '--' : `${safeFixed(s * 100, 1)}%`
    }`,
    `常用追问：${QUICK_QUESTIONS.join(' | ')}`,
    '注意：以上为快照，若用户询问当前实时值，必须调用工具重新取数。',
  ].join('\n')
}
