/**
 * `MissingFields` —— 缺数折叠(设计稿 v3.0 §七 6.1 / 开发计划 P0-3)。
 *
 * 为什么要它: 诚实口径要求"无数据显式标 `--`", 但**一屏 73 个 `--` 是噪声**(实测个股页)。
 * 缺数据不该占位, 也不该被藏起来 —— 正确做法是**聚合成一行, 并说清原因**, 需要时展开看逐项。
 *
 * 用法(两种粒度):
 *  ① 区块级: 整块都没数据 → 用一行代替整块
 *     `<MissingFields title="盘口速览" items={[{label:'十档买卖额', reason:'TQ 未连接'},
 *                                             {label:'主力净额',   reason:'当日无成交'}]} />`
 *  ② 行内级: 只有个别字段缺 → 照旧显示 `--`, 不用本组件(决策关键位必须留在原位)
 *
 * 三条不做: ①**不补 0**(0 是真实值, 不能冒充"没有数据");
 *          ②**不隐藏原因**(折叠的是重复, 不是真相);
 *          ③**不折叠决策关键位**(缺数原因本身是信息 —— 比如"TQ 未连接"就是可操作线索)。
 */
import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

export interface MissingField {
  /** 缺什么(字段/区块名, 短) */
  label: string
  /** 为什么缺 —— 必填, 不允许"不知道为什么"就折叠 */
  reason: string
}

export interface MissingFieldsProps {
  /** 缺项 */
  items: MissingField[]
  /** 区块名(用于拼"盘口速览 缺 16 项"), 省略则只显示总数 */
  title?: string
  /** 折叠态前缀文案(默认"本页缺") */
  prefix?: string
  /** 初始是否展开(默认收起) */
  defaultOpen?: boolean
  className?: string
}

/** 把原因去重压缩成一句(相同原因只出现一次, 超过 2 条折成"+N")。 */
export function reasonSummary(items: MissingField[]): string {
  const uniq: string[] = []
  for (const it of items) {
    const r = (it.reason || '').trim()
    if (r && !uniq.includes(r)) uniq.push(r)
  }
  if (uniq.length === 0) return ''
  if (uniq.length <= 2) return uniq.join(' · ')
  return `${uniq.slice(0, 2).join(' · ')} 等 ${uniq.length} 类原因`
}

export default function MissingFields({
  items,
  title,
  prefix = '本页缺',
  defaultOpen = false,
  className = '',
}: MissingFieldsProps) {
  const [open, setOpen] = useState(defaultOpen)
  if (items.length === 0) return null

  const head =
    items.length === 1
      ? `${items[0].label} 缺失`
      : `${title ? `${title} ` : ''}${prefix} ${items.length} 项`
  const summary = reasonSummary(items)

  return (
    <div className={`text-[11px] text-muted-foreground ${className}`} data-testid="missing-fields">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        data-testid="missing-fields-toggle"
        className="flex w-full items-center gap-1 rounded px-1 py-0.5 text-left hover:text-foreground"
      >
        {open ? <ChevronDown className="h-3 w-3 shrink-0" /> : <ChevronRight className="h-3 w-3 shrink-0" />}
        <span className="truncate">
          {head}
          {summary ? ` · ${summary}` : ''}
        </span>
      </button>

      {open && (
        <ul className="mt-1 space-y-0.5 border-l border-border/60 pl-2" data-testid="missing-fields-list">
          {items.map((it) => (
            <li key={`${it.label}-${it.reason}`} className="flex gap-2">
              <span className="shrink-0 text-foreground/80">{it.label}</span>
              <span className="text-muted-foreground/80">{it.reason}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
