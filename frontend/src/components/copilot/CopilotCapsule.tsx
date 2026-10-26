import { useCallback, useEffect, useRef, useState } from 'react'
import { GripVertical, Sparkles } from 'lucide-react'
import { useCopilotBrief } from '@/hooks/useCopilotBrief'
import { safeFixed, safePercent } from '@/lib/format'
import {
  CAPSULE_DRAG_HINT,
  CAPSULE_FALLBACK,
  CAPSULE_HIDE,
  CAPSULE_HINT,
  CAPSULE_RESTORE,
  QUICK_QUESTIONS,
} from '@/lib/copilot/copy'
import { registerCapsulePresence } from '@/lib/copilot/capsule-presence'
import { toneBar, toneMeta } from '@/lib/copilot/tone'
import type { CopilotBrief } from '@/lib/copilot/types'

const hiddenKey = (symbol: string) => `copilot_capsule_hidden:${symbol}`
/** 拖拽位置按**浏览器**记忆（不是按标的）：用户挪开一次，换票也不该跳回右下角。 */
const POSITION_KEY = 'copilot_capsule_position'
const VIEWPORT_MARGIN = 12
const SNAP_DISTANCE = 72
const DRAG_THRESHOLD = 6
/** 胶囊缺省尺寸（jsdom / 首帧取不到 offsetWidth 时用于边界计算）。 */
const FALLBACK_W = 264
const FALLBACK_H = 56

interface CapsulePosition {
  x: number
  y: number
}

interface DragState {
  pointerId: number
  offsetX: number
  offsetY: number
  startX: number
  startY: number
  moved: boolean
}

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), Math.max(min, max))
}

function readStoredPosition(): CapsulePosition | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(POSITION_KEY)
    if (!raw) return null
    const pos = JSON.parse(raw) as Partial<CapsulePosition>
    if (Number.isFinite(pos.x) && Number.isFinite(pos.y)) {
      return { x: Number(pos.x), y: Number(pos.y) }
    }
  } catch {
    // 隐私模式 / 脏数据 → 用缺省锚点
  }
  return null
}

function clampToViewport(pos: CapsulePosition, el: HTMLElement | null): CapsulePosition {
  if (typeof window === 'undefined') return pos
  const w = el?.offsetWidth || FALLBACK_W
  const h = el?.offsetHeight || FALLBACK_H
  return {
    x: clamp(pos.x, VIEWPORT_MARGIN, window.innerWidth - w - VIEWPORT_MARGIN),
    y: clamp(pos.y, VIEWPORT_MARGIN, window.innerHeight - h - VIEWPORT_MARGIN),
  }
}

/** 拖拽结束吸附：靠近左/右边缘就贴边，中间位置保持自由（用户想放哪就放哪）。 */
function snapToEdge(pos: CapsulePosition, el: HTMLElement | null): CapsulePosition {
  if (typeof window === 'undefined') return pos
  const w = el?.offsetWidth || FALLBACK_W
  const maxX = window.innerWidth - w - VIEWPORT_MARGIN
  const x =
    pos.x - VIEWPORT_MARGIN <= SNAP_DISTANCE
      ? VIEWPORT_MARGIN
      : maxX - pos.x <= SNAP_DISTANCE
        ? maxX
        : pos.x
  return { ...pos, x }
}

/**
 * 个股工作台**常驻唤醒胶囊**（设计稿 §8.7；2026-10-18 升级为可拖拽大胶囊）。
 *
 * 只做两件事，绝不新写一套聊天：
 *   ① 轻量展示「当前标的 tone + 一句话定性」；
 *   ② 点击时派发 `panwatch-open-chat`，由既有 `ChatWidget` 建带 `initial_context` 的新会话
 *      （ChatWidget.tsx 已有监听，零新增通道 / 零 SSE 重复实现）。
 *
 * 入口唯一性：胶囊挂载即向 `capsule-presence` 登记，ChatWidget 据此**隐藏自己的圆形 FAB**
 * （只留这一个更高级的入口）；离开工作台注销后 FAB 自动回来 —— 不做全局删除。
 *
 * 拖拽（本次升级）：按住胶囊任意非按钮区域即可拖动；位移 < 6px 视为点击（不会误吞唤醒），
 * 松手吸附左右边缘并把位置写进 localStorage；窗口缩放时重新夹紧，永不拖出视口。
 * 这是为了把「副驾驶」从固定角落解放出来 —— 右下角盘口面板是用户高频盯的区域。
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
  const [pos, setPos] = useState<CapsulePosition | null>(readStoredPosition)
  const [dragging, setDragging] = useState(false)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<DragState | null>(null)
  const posRef = useRef<CapsulePosition | null>(pos)
  /** 拖拽后的 click 必须吞掉：否则松手瞬间会顺手把聊天面板打开。 */
  const suppressClickRef = useRef(false)

  posRef.current = pos

  // 入口唯一性登记：挂载即声明"工作台已有胶囊，FAB 让位"。
  useEffect(() => registerCapsulePresence(), [])

  useEffect(() => {
    try {
      setHidden(localStorage.getItem(hiddenKey(symbol)) === '1')
    } catch {
      setHidden(false)
    }
  }, [symbol])

  // 首次挂载校正一次: 上次可能在大屏拖到右侧, 这次换小窗 → 位置会落在视口外。
  // 只跑一次(空依赖), 不跟拖拽中的 pos 变化跑 —— 否则每次 pointermove 都写一次 localStorage。
  useEffect(() => {
    const current = posRef.current
    if (!current) return
    const next = clampToViewport(current, rootRef.current)
    if (next.x === current.x && next.y === current.y) return
    setPos(next)
    try {
      localStorage.setItem(POSITION_KEY, JSON.stringify(next))
    } catch {
      /* 隐私模式忽略 */
    }
  }, [])

  // 视口变化（旋转/缩放）后把记忆位置夹回可见区，避免"上次拖到边缘，这次看不见"。
  useEffect(() => {
    if (!pos) return
    const onResize = () => {
      const next = clampToViewport(posRef.current ?? pos, rootRef.current)
      setPos(next)
      try {
        localStorage.setItem(POSITION_KEY, JSON.stringify(next))
      } catch {
        /* 隐私模式忽略 */
      }
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [pos])

  const persist = useCallback((next: CapsulePosition) => {
    try {
      localStorage.setItem(POSITION_KEY, JSON.stringify(next))
    } catch {
      /* localStorage 不可用（隐私模式）→ 仅本会话生效 */
    }
  }, [])

  const handlePointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    const target = event.target as HTMLElement
    // 只有拖拽把手与外壳空白处允许拖拽，点击内部按钮（如唤醒主体、关闭按钮）不劫持为拖拽
    if (target.closest('[data-capsule-no-drag]') || target.closest('[data-testid="copilot-capsule-open"]')) return
    const el = rootRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    dragRef.current = {
      pointerId: event.pointerId,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
    }
    setDragging(true)
    try {
      el.setPointerCapture?.(event.pointerId)
    } catch {
      /* 老浏览器 / jsdom 不支持捕获 → 依然能靠 move 事件拖动 */
    }
  }

  const handlePointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    const el = rootRef.current
    if (!drag || !el || drag.pointerId !== event.pointerId) return
    const dx = event.clientX - drag.startX
    const dy = event.clientY - drag.startY
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < DRAG_THRESHOLD) return
    drag.moved = true
    const next = clampToViewport(
      { x: event.clientX - drag.offsetX, y: event.clientY - drag.offsetY },
      el,
    )
    setPos(next)
  }

  const handlePointerUp = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    const el = rootRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    dragRef.current = null
    setDragging(false)
    try {
      if (el?.hasPointerCapture?.(event.pointerId)) el.releasePointerCapture(event.pointerId)
    } catch {
      /* 同上 */
    }
    if (!drag.moved) return
    const snapped = snapToEdge(clampToViewport(posRef.current ?? { x: 0, y: 0 }, el), el)
    setPos(snapped)
    persist(snapped)
    suppressClickRef.current = true
    // click 在 pointerup 之后同步派发；下一轮事件循环再解除抑制。
    window.setTimeout(() => {
      suppressClickRef.current = false
    }, 0)
  }

  const handleClickCapture = (event: React.MouseEvent<HTMLDivElement>) => {
    if (!suppressClickRef.current) return
    event.preventDefault()
    event.stopPropagation()
    suppressClickRef.current = false
  }

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

  const positionStyle = pos
    ? ({ left: `${pos.x}px`, top: `${pos.y}px` } as React.CSSProperties)
    : undefined
  const positionClass = pos ? '' : 'bottom-36 right-4 md:bottom-20'
  const dragHandlers = {
    onPointerDown: handlePointerDown,
    onPointerMove: handlePointerMove,
    onPointerUp: handlePointerUp,
    onPointerCancel: handlePointerUp,
    onClickCapture: handleClickCapture,
  }

  if (hidden) {
    return (
      <div
        ref={rootRef}
        data-testid="copilot-capsule-restore-wrap"
        style={positionStyle}
        className={`fixed z-40 ${positionClass}`}
      >
        <button
          type="button"
          onClick={restore}
          title={CAPSULE_RESTORE}
          aria-label={CAPSULE_RESTORE}
          data-testid="copilot-capsule-restore"
          className="flex h-9 w-9 cursor-pointer items-center justify-center rounded-full border border-role-watch/45 bg-s2 text-role-watch shadow-[0_8px_24px_rgba(0,0,0,0.4)] transition-transform hover:scale-105"
        >
          <Sparkles className="h-4 w-4" />
        </button>
      </div>
    )
  }

  const meta = data ? toneMeta(data.verdict.intent_tone) : null
  const label = data?.verdict.intent_label || CAPSULE_FALLBACK
  const bar = data ? toneBar(data.verdict.intent_tone) : 'bg-role-watch'

  return (
    <div
      ref={rootRef}
      {...dragHandlers}
      data-testid="copilot-capsule"
      style={{
        ...positionStyle,
        boxShadow:
          '0 14px 40px rgba(0,0,0,0.45), 0 0 0 1px hsl(var(--role-watch) / 0.25), 0 0 28px hsl(var(--role-watch) / 0.32)',
      }}
      className={`fixed z-40 max-w-[calc(100vw-1.5rem)] touch-none select-none ${positionClass} ${
        dragging ? 'cursor-grabbing' : 'cursor-grab'
      }`}
    >
      <div className="relative flex h-14 items-stretch overflow-hidden rounded-xl border border-role-watch/40 bg-s2">
        {/* 左侧色条: tone 越大越醒目(机会=琥珀/风险=红), 外发光由外壳承担 */}
        <span aria-hidden="true" className={`w-1.5 shrink-0 ${bar}`} />
        <button
          type="button"
          onClick={open}
          title={CAPSULE_HINT}
          aria-label={CAPSULE_HINT}
          data-testid="copilot-capsule-open"
          className="flex min-w-0 flex-1 items-center gap-2.5 px-3 text-left transition-colors hover:bg-s3"
        >
          <span className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-role-watch/15 ring-1 ring-role-watch/35">
            <Sparkles className="h-4 w-4 text-role-watch" />
            <span className="absolute -right-0.5 -top-0.5 h-2 w-2 animate-pulse rounded-full bg-role-opp" />
          </span>
          <span className="min-w-0">
            <span className="flex items-center gap-1.5">
              <span className="text-[10px] font-semibold text-role-watch">
                {meta?.icon ?? '✦'}
              </span>
              <span className="max-w-[168px] truncate text-[13px] font-semibold text-foreground">
                {meta ? label : `${CAPSULE_FALLBACK} · ${symbol}`}
              </span>
            </span>
            <span className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <span className="font-mono">{symbol}</span>
              {stockName ? <span className="max-w-[96px] truncate">{stockName}</span> : null}
              <span className="text-role-watch">点击唤醒</span>
            </span>
          </span>
        </button>
        <span
          aria-hidden="true"
          title={CAPSULE_DRAG_HINT}
          data-capsule-drag-handle="true"
          className="flex w-6 cursor-grab active:cursor-grabbing items-center justify-center text-muted-foreground hover:text-foreground md:flex"
        >
          <GripVertical className="h-4 w-4" />
        </span>
        <button
          type="button"
          aria-label={CAPSULE_HIDE}
          title={CAPSULE_HIDE}
          onClick={collapse}
          data-testid="copilot-capsule-hide"
          data-capsule-no-drag="true"
          className="flex w-8 shrink-0 items-center justify-center border-l border-border/40 text-[13px] text-muted-foreground transition-colors hover:bg-s3 hover:text-foreground"
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
