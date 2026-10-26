import { useState, type ReactNode } from 'react'
import { cn } from '@/lib/utils'
import PageHeader from '@/components/PageHeader'
import { StatusChip } from '@panwatch/base-ui/components/ui/status-chip'
import { SectionWorkspace, useSectionRoute, useSectionVisible } from '@/components/SectionWorkspace'

export interface SideMenuItem { id: string; label: string; icon?: ReactNode; href?: string; anchor?: string }

/** 管理与文档共享分类工作区：选中分类只显示对应内容，避免菜单与正文错位。 */
export function DevPageLayout({ title, subtitle, badge, menu, onNav, children, headerExtra }: {
  title: string; subtitle: string; badge?: string; menu: SideMenuItem[]; activeId?: string;
  onNav?: (id: string) => void; children: ReactNode; headerExtra?: ReactNode
}) {
  const items = menu.filter(item => !item.href).map(item => ({ ...item, id: item.anchor ?? item.id }))
  const [selected, select] = useSectionRoute(items, items[0]?.id ?? '')
  return <div className="page-container text-foreground">
    <PageHeader title={title} description={subtitle} actions={headerExtra} metadata={badge ? <StatusChip tone="info">{badge}</StatusChip> : undefined} />
    <SectionWorkspace items={items} value={selected} label={`${title}分类`} onChange={id => { select(id); onNav?.(menu.find(item => (item.anchor ?? item.id) === id)?.id ?? id) }}>
      {children}
    </SectionWorkspace>
  </div>
}

/* ── 章节块 ── */
export function Section({
  id,
  title,
  description,
  icon,
  children,
  action,
}: {
  id?: string
  title: string
  description?: string
  icon?: ReactNode
  children: ReactNode
  action?: ReactNode
}) {
  const visible = useSectionVisible(id)
  if (!visible) return null
  return (
    <section id={id} className="analysis-section">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            {icon && <span className="text-primary">{icon}</span>}
            <h2 className="text-[16px] font-semibold text-foreground">{title}</h2>
          </div>
          {description && <p className="mt-1 text-[12px] text-muted-foreground">{description}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  )
}

/* ── 信息卡片 ── */
export function InfoCard({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={'card-surface ' + (cn('rounded-lg bg-s2 p-4', className))}>
      {children}
    </div>
  )
}

/* ── 代码块 ── */
export function CodeBlock({ code, language = 'bash', onCopy }: { code: string; language?: string; onCopy?: () => void }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = () => {
    navigator.clipboard.writeText(code)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
    onCopy?.()
  }
  return (
    <div className="card-surface group relative rounded-lg bg-s2">
      <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
        <span className="font-mono text-[12px] text-muted-foreground">{language}</span>
        <button
          className="rounded px-1.5 py-0.5 text-[12px] text-muted-foreground transition-colors hover:text-primary"
          onClick={handleCopy}
        >
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-[12px] leading-relaxed text-foreground">
        {code}
      </pre>
    </div>
  )
}

/* ── 空状态：统一走全局 EmptyState（主题感知） ── */
export { EmptyState } from '@/components/EmptyState'
