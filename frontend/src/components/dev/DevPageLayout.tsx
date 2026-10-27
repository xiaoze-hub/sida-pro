import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Check, Copy } from 'lucide-react'
import { Button } from '@sida/base-ui/components/ui/button'
import { cn } from '@/lib/utils'
import PageHeader from '@/components/PageHeader'
import { StatusChip } from '@sida/base-ui/components/ui/status-chip'
import { SectionWorkspace, useSectionRoute, useSectionVisible } from '@/components/SectionWorkspace'

export interface SideMenuItem { id: string; label: string; icon?: ReactNode; href?: string; anchor?: string }

/** 管理与文档共享分类工作区：选中分类只显示对应内容，避免菜单与正文错位。 */
export function DevPageLayout({ title, subtitle, badge, menu, onNav, children, headerExtra, publicView = false }: {
  title: string; subtitle: string; badge?: string; menu: SideMenuItem[]; activeId?: string;
  onNav?: (id: string) => void; children: ReactNode; headerExtra?: ReactNode; publicView?: boolean
}) {
  const items = menu.filter(item => !item.href).map(item => ({ ...item, id: item.anchor ?? item.id }))
  const [selected, select] = useSectionRoute(items, items[0]?.id ?? '')
  const Root = publicView ? 'main' : 'div'
  return <Root className={publicView ? 'mx-auto w-full max-w-7xl space-y-8 px-5 py-10 text-foreground sm:px-8 sm:py-12' : 'page-container text-foreground'}>
    {publicView ? <header className="flex flex-wrap items-end justify-between gap-6 pb-2"><div className="max-w-2xl"><p className="mb-4 text-[12px] font-medium tracking-widest text-primary">开发者中心</p><h1 className="page-title">{title}</h1><p className="mt-4 text-[16px] leading-8 text-muted-foreground">{subtitle}</p></div><div className="flex flex-wrap gap-2">{headerExtra}</div></header> : <PageHeader title={title} description={subtitle} actions={headerExtra} metadata={badge ? <StatusChip tone="info">{badge}</StatusChip> : undefined} />}
    <SectionWorkspace items={items} value={selected} label={`${title}分类`} onChange={id => { select(id); onNav?.(menu.find(item => (item.anchor ?? item.id) === id)?.id ?? id) }}>
      {children}
    </SectionWorkspace>
  </Root>
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
  const [copyError, setCopyError] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setCopyError(false)
      clearTimeout(timer.current)
      timer.current = setTimeout(() => setCopied(false), 2000)
      onCopy?.()
    } catch { setCopied(false); setCopyError(true) }
  }
  return (
    <div className="card-surface group relative rounded-lg bg-s2">
      <div className="flex min-h-11 items-center justify-between border-b border-border px-3">
        <span className="font-mono text-[12px] text-muted-foreground">{language}</span>
        <Button variant="ghost" aria-label={`复制 ${language} 代码`}
          className="h-11 gap-2 px-2 text-[12px] text-muted-foreground"
          onClick={handleCopy}
        >
          {copied ? <Check size={14} /> : <Copy size={14} />}{copied ? '已复制' : '复制'}
        </Button>
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-[12px] leading-relaxed text-foreground">
        {code}
      </pre>
      {copyError && <p role="status" className="px-3 pb-3 text-[12px] text-muted-foreground">复制未完成，请选择代码后手动复制。</p>}
    </div>
  )
}

/* ── 空状态：统一走全局 EmptyState（主题感知） ── */
export { EmptyState } from '@/components/EmptyState'
