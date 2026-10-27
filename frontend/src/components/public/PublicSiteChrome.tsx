import { useRef, useState, type ReactNode } from 'react'
import { GithubMark } from '@/components/icons/GithubMark'
import { Link, useLocation } from 'react-router-dom'
import { ArrowUpRight, Menu, Moon, Sun, X } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { BrandMark } from '@/components/BrandMark'
import { useTheme } from '@/hooks/use-theme'
import { PROJECT_REPOSITORY } from '@/lib/product-links'

const navigation = [
  { to: '/', label: '产品概览' },
  { to: '/#demo', label: '操作演示' },
  { to: '/developers', label: '开发者文档' },
  { to: '/tiers', label: '账户方案' },
]

export function PublicSiteHeader() {
  const { pathname, hash } = useLocation()
  const { theme, toggleTheme } = useTheme()
  const [open, setOpen] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const navLinks = navigation.map(item => {
    const active = item.to === `${pathname}${hash}`
    return <Link key={item.to} to={item.to} reloadDocument={item.to.includes('#')} aria-current={active ? 'page' : undefined} onClick={() => setOpen(false)} className={`flex min-h-11 items-center rounded-lg px-3 text-[14px] transition-colors ${active ? 'bg-s2 font-semibold text-foreground' : 'text-muted-foreground hover:bg-s2 hover:text-foreground'}`}>{item.label}</Link>
  })
  return <header className="sticky top-0 z-30 border-b border-border bg-card" onKeyDown={event => { if (event.key === 'Escape') { setOpen(false); trigger.current?.focus() } }}>
    <div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-5 py-3 sm:px-8">
      <Link to="/" aria-label="SIDA 数智分析首页" className="flex shrink-0 items-center gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary text-primary-foreground"><BrandMark className="h-6 w-6" /></span>
        <span className="hidden leading-tight min-[360px]:block"><span className="block text-[16px] font-semibold">SIDA Pro</span><span className="mt-1 block text-[12px] text-muted-foreground">数智分析</span></span>
      </Link>
      <nav aria-label="官网导航" className="hidden items-center gap-1 lg:flex">{navLinks}</nav>
      <div className="flex items-center gap-1 sm:gap-2">
        <Button asChild variant="ghost" size="icon" className="hidden h-11 w-11 sm:inline-flex"><a href={PROJECT_REPOSITORY} target="_blank" rel="noopener noreferrer" aria-label="GitHub 项目仓库"><GithubMark size={18} /></a></Button>
        <Button variant="ghost" size="icon" className="h-11 w-11" onClick={toggleTheme} aria-label={theme === 'dark' ? '切换浅色模式' : '切换深色模式'}>{theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</Button>
        <Button asChild variant="ghost" className="h-11 px-2 sm:px-3"><Link to="/login">登录工作台<ArrowUpRight size={16} className="hidden sm:block" /></Link></Button>
        <Button ref={trigger} variant="ghost" size="icon" className="h-11 w-11 lg:hidden" aria-label={open ? '收起网站导航' : '展开网站导航'} aria-expanded={open} aria-controls="public-mobile-nav" onClick={() => setOpen(!open)}>{open ? <X size={20} /> : <Menu size={20} />}</Button>
      </div>
    </div>
    {open && <nav id="public-mobile-nav" aria-label="手机网站导航" className="grid border-t border-border bg-card px-5 py-3 lg:hidden">{navLinks}<a href={PROJECT_REPOSITORY} target="_blank" rel="noopener noreferrer" className="flex min-h-11 items-center gap-2 px-3 text-[14px] text-muted-foreground"><GithubMark size={16} />GitHub 项目仓库<ArrowUpRight size={14} /></a></nav>}
  </header>
}

export function PublicSiteFooter() {
  return <footer className="border-t border-border bg-card">
    <div className="mx-auto flex max-w-7xl flex-wrap justify-between gap-6 px-5 py-8 text-[12px] text-muted-foreground sm:px-8">
      <div><p className="font-medium text-foreground">SIDA Pro · 数智分析</p><p className="mt-2">技术研究与数据分析 · 非投资建议</p></div>
      <nav aria-label="页脚导航" className="flex flex-wrap items-start gap-5"><Link to="/developers" className="hover:text-foreground">开发者文档</Link><Link to="/tiers" className="hover:text-foreground">账户方案</Link><a href={PROJECT_REPOSITORY} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 hover:text-foreground"><GithubMark size={14} />GitHub 项目仓库<ArrowUpRight size={14} /></a><Link to="/terms" className="hover:text-foreground">使用条款</Link></nav>
    </div>
  </footer>
}

/** 登录后由应用工作区提供导航，公开访问时使用官网页眉、页脚。 */
export function PublicSiteFrame({ enabled = true, children }: { enabled?: boolean; children: ReactNode }) {
  return enabled ? <div className="flex min-h-screen flex-col bg-background text-foreground"><PublicSiteHeader /><div className="flex-1">{children}</div><PublicSiteFooter /></div> : <>{children}</>
}
