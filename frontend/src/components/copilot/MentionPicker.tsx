import type { MentionAgent, MentionStock } from '@/lib/copilot/api'

export type MentionTab = 'stock' | 'agent'

/**
 * `@` 面板（纯展示 + 键盘选中，状态由页面持有）。
 *
 * 为什么交给页面管状态：输入框、光标位置、chip 列表要和 textarea 联动，
 * 面板自己拿状态会和输入法组合事件打架。
 */
export function MentionPicker({
  tab,
  onTab,
  stocks,
  agents,
  active,
  onHover,
  onPick,
  loading,
}: {
  tab: MentionTab
  onTab: (t: MentionTab) => void
  stocks: MentionStock[]
  agents: MentionAgent[]
  active: number
  onHover: (i: number) => void
  onPick: (item: MentionStock | MentionAgent) => void
  loading?: boolean
}) {
  const rows: (MentionStock | MentionAgent)[] = tab === 'stock' ? stocks : agents
  return (
    <div
      data-testid="copilot-mention-panel"
      className="absolute bottom-full left-0 z-30 mb-1 w-[320px] overflow-hidden rounded-lg border border-border/70 bg-s1 shadow-float"
    >
      <div className="flex border-b border-border/60">
        {(['stock', 'agent'] as MentionTab[]).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => onTab(t)}
            data-testid={`copilot-mention-tab-${t}`}
            className={`flex-1 px-2 py-1.5 text-[11px] ${
              tab === t ? 'bg-s2 text-role-watch' : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            {t === 'stock' ? '股票' : '智能体'}
          </button>
        ))}
      </div>

      <div className="max-h-[220px] overflow-y-auto py-1">
        {loading && rows.length === 0 ? (
          <div className="px-2.5 py-2 text-[11px] text-muted-foreground">检索中…</div>
        ) : null}
        {!loading && rows.length === 0 ? (
          <div className="px-2.5 py-2 text-[11px] text-muted-foreground">
            {tab === 'stock' ? '自选里没有匹配的股票' : '没有匹配的智能体'}
          </div>
        ) : null}

        {rows.map((item, i) => {
          const isStock = item.type === 'stock'
          const key = isStock ? item.symbol : item.id
          return (
            <button
              key={key}
              type="button"
              onMouseEnter={() => onHover(i)}
              onMouseDown={(e) => e.preventDefault()}      // 别让 textarea 先失焦
              onClick={() => onPick(item)}
              data-testid={`copilot-mention-item-${key}`}
              className={`flex w-full items-center gap-2 px-2.5 py-1.5 text-left ${
                i === active ? 'bg-s2' : ''
              }`}
            >
              <span className="w-4 shrink-0 text-center text-[12px]">
                {isStock ? '📈' : (item as MentionAgent).icon}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[12px] text-foreground">
                  {isStock ? item.name : (item as MentionAgent).label}
                </span>
                <span className="block truncate text-[10px] text-muted-foreground">
                  {isStock ? `${item.symbol}${item.py ? ` · ${item.py}` : ''}` : (item as MentionAgent).desc}
                </span>
              </span>
            </button>
          )
        })}
      </div>

      <div className="border-t border-border/60 px-2 py-1 text-[10px] text-muted-foreground">
        ↑↓ 选择 · Enter 确认 · Esc 关闭
      </div>
    </div>
  )
}
