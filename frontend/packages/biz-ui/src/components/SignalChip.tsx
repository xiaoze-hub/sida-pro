/**
 * 信号 chip(2026-09-20 设计系统 P1: 统一几何与字重)。
 *
 * 依据: GS 点 / 机会动作 / 题材核心 / 连板 此前各页各写各的胶囊, 高度、字重、边框粗细都不同,
 * 扫视时"同一类信号长得不一样"。
 *
 * 语法: 几何统一(高 18px / 字号 11 / 圆角 4px); **强度用字重与边框表达**, 色相只承担语义
 * (4 种: 机会/风险/中性/系统), 不靠堆色相区分强弱。
 */
import { cn } from '@panwatch/base-ui'

/**
 * 语义色调:
 * - `opp`/`risk`/`system`/`neutral`: 模块角色(机会/风险/系统/中性) —— 用 --role-* 角色色;
 * - `go`/`stop`: **交易动作**语义(买入/加仓 vs 卖出/减仓) —— 用 `--gs-go`/`--gs-stop`,
 *   与价格涨跌色**同值不同名**: A 股用户看到"买入=红"符合习惯, 但代码里它是"动作色"不是"价格色",
 *   两者可以各自演进(涨跌色治理的既有约定)。
 */
export type SignalTone = 'opp' | 'risk' | 'neutral' | 'system' | 'go' | 'stop'
export type SignalStrength = 0 | 1 | 2 | 3

const TONE_CLASS: Record<SignalTone, string> = {
  opp: 'text-[hsl(var(--role-opp))] border-[hsl(var(--role-opp))]/40',
  risk: 'text-[hsl(var(--role-risk))] border-[hsl(var(--role-risk))]/40',
  neutral: 'text-muted-foreground border-border',
  system: 'text-[hsl(var(--role-system))] border-border',
  go: 'text-[hsl(var(--gs-go))] border-[hsl(var(--gs-go))]/40',
  stop: 'text-[hsl(var(--gs-stop))] border-[hsl(var(--gs-stop))]/40',
}

/** 强度: 0 无边框 / 1 hairline / 2 hairline+600 / 3 双档边框(与色相无关, 换配色不影响强弱) */
const STRENGTH_CLASS: Record<SignalStrength, string> = {
  0: 'border-transparent font-normal',
  1: 'border-border/60 font-normal',
  2: 'border-current/40 font-semibold',
  3: 'border-current font-bold',
}

export default function SignalChip({
  children,
  tone = 'neutral',
  strength = 1,
  title,
  className,
  onClick,
}: {
  children: React.ReactNode
  tone?: SignalTone
  strength?: SignalStrength
  title?: string
  className?: string
  onClick?: () => void
}) {
  const Tag = onClick ? 'button' : 'span'
  return (
    <Tag
      type={onClick ? 'button' : undefined}
      onClick={onClick}
      title={title}
      className={cn(
        'inline-flex h-[18px] items-center rounded border px-1 text-[11px] leading-none',
        TONE_CLASS[tone],
        STRENGTH_CLASS[strength],
        className,
      )}
      data-signal-tone={tone}
      data-signal-strength={strength}
    >
      {children}
    </Tag>
  )
}
