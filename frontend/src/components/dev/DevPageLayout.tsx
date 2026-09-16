import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Menu, X } from 'lucide-react'
import { cn } from '@/lib/utils'

/* ── 侧边栏菜单项 ── */
export interface SideMenuItem {
  id: string
  label: string
  icon?: ReactNode
  href?: string   // 外链/路由跳转
  anchor?: string // 页内锚点
}

/* ── 页面布局: 顶栏 + 侧边栏 + 主内容 ── */
export function DevPageLayout({
  title,
  subtitle,
  badge,
  menu,
  activeId,
  onNav,
  children,
  headerExtra,
}: {
  title: string
  subtitle: string
  badge?: string
  menu: SideMenuItem[]
  activeId?: string
  onNav?: (id: string) => void
  children: ReactNode
  headerExtra?: ReactNode
}) {
  const [sidebarOpen, setSidebarOpen] = useState(false)

  const handleClick = (item: SideMenuItem) => {
    if (item.anchor) {
      const el = document.getElementById(item.anchor)
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' })
        setSidebarOpen(false)
      }
    }
    onNav?.(item.id)
    setSidebarOpen(false)
  }

  return (
    <div className="min-h-screen bg-[#0a0a12] text-white">
      {/* 顶部标题栏 */}
      <header className="sticky top-0 z-30 border-b border-white/5 bg-[#0a0a12]/80 backdrop-blur-xl">
        <div className="flex items-center gap-4 px-4 py-4 md:px-8">
          {/* 移动端菜单按钮 */}
          <button
            className="rounded-md p-1.5 text-slate-400 hover:bg-white/5 hover:text-white md:hidden"
            onClick={() => setSidebarOpen(!sidebarOpen)}
          >
            {sidebarOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
          </button>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2.5">
              <h1 className="text-[18px] font-bold tracking-tight text-white md:text-[20px]">{title}</h1>
              {badge && (
                <span className="rounded-full bg-cyan-500/10 border border-cyan-500/20 px-2 py-0.5 text-[10px] font-mono text-cyan-400">
                  {badge}
                </span>
              )}
            </div>
            <p className="mt-0.5 text-[12px] text-slate-500">{subtitle}</p>
          </div>
          {headerExtra}
        </div>
      </header>

      <div className="flex">
        {/* 侧边栏 */}
        <aside
          className={cn(
            'fixed inset-y-0 left-0 z-20 w-56 border-r border-white/5 bg-[#0c0c16] pt-[73px] transition-transform md:sticky md:top-[73px] md:z-0 md:h-[calc(100vh-73px)] md:translate-x-0 md:pt-0',
            sidebarOpen ? 'translate-x-0' : '-translate-x-full'
          )}
        >
          <nav className="flex flex-col gap-0.5 p-3">
            {menu.map(item => {
              const isActive = activeId === item.id
              if (item.href) {
                return (
                  <Link
                    key={item.id}
                    to={item.href}
                    className={cn(
                      'flex items-center gap-2.5 rounded-lg px-3 py-2 text-[12px] transition-colors',
                      isActive
                        ? 'bg-cyan-500/10 text-cyan-300 font-medium'
                        : 'text-slate-400 hover:bg-white/5 hover:text-white'
                    )}
                    onClick={() => setSidebarOpen(false)}
                  >
                    {item.icon}
                    {item.label}
                  </Link>
                )
              }
              return (
                <button
                  key={item.id}
                  className={cn(
                    'flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-[12px] transition-colors',
                    isActive
                      ? 'bg-cyan-500/10 text-cyan-300 font-medium'
                      : 'text-slate-400 hover:bg-white/5 hover:text-white'
                  )}
                  onClick={() => handleClick(item)}
                >
                  {item.icon}
                  {item.label}
                </button>
              )
            })}
          </nav>
        </aside>

        {/* 遮罩 */}
        {sidebarOpen && (
          <div className="fixed inset-0 z-10 bg-black/50 md:hidden" onClick={() => setSidebarOpen(false)} />
        )}

        {/* 主内容 */}
        <main className="min-w-0 flex-1 px-4 py-6 md:px-8">
          <div className="mx-auto max-w-4xl">{children}</div>
        </main>
      </div>
    </div>
  )
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
  return (
    <section id={id} className="mb-8 scroll-mt-24">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            {icon && <span className="text-cyan-400">{icon}</span>}
            <h2 className="text-[15px] font-semibold text-white">{title}</h2>
          </div>
          {description && <p className="mt-1 text-[12px] text-slate-500">{description}</p>}
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
    <div className={cn('rounded-xl border border-white/5 bg-white/[0.02] p-4 backdrop-blur-sm', className)}>
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
    <div className="group relative rounded-lg border border-white/5 bg-[#0d0d18]">
      <div className="flex items-center justify-between border-b border-white/5 px-3 py-1.5">
        <span className="font-mono text-[10px] text-slate-600">{language}</span>
        <button
          className="rounded px-1.5 py-0.5 text-[10px] text-slate-500 opacity-0 transition-opacity group-hover:opacity-100 hover:text-cyan-400"
          onClick={handleCopy}
        >
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-[11px] leading-relaxed text-cyan-100/80">
        {code}
      </pre>
    </div>
  )
}

/* ── 空状态 ── */
export function EmptyState({ icon, title, desc, action }: { icon?: ReactNode; title: string; desc?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-white/10 bg-white/[0.01] py-12">
      {icon && <div className="mb-3 text-slate-600">{icon}</div>}
      <p className="text-[13px] font-medium text-slate-400">{title}</p>
      {desc && <p className="mt-1 text-[11px] text-slate-600">{desc}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}
