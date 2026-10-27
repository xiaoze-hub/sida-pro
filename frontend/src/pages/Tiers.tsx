import { useCallback, useEffect, useState } from 'react'
import { GithubMark } from '@/components/icons/GithubMark'
import { usePublicSeo } from '@/hooks/use-public-seo'
import { Link } from 'react-router-dom'
import { ArrowRight, ArrowUpRight, Check, Loader2, Server } from 'lucide-react'
import { applyPro, fetchProApplyStatus, fetchTiers, isAuthenticated } from '@sida/api'
import type { ProApplyStatus, TierRow, TiersResp } from '@sida/api'
import { Button } from '@sida/base-ui/components/ui/button'
import { StatusChip } from '@sida/base-ui/components/ui/status-chip'
import PageHeader from '@/components/PageHeader'
import { PublicSiteFrame } from '@/components/public/PublicSiteChrome'
import { PROJECT_REPOSITORY, COMMUNITY_DEPLOYMENT, COMMUNITY_LICENSE, COMMUNITY_REPOSITORY } from '@/lib/product-links'

function GroupList({ row, otherItems }: { row: TierRow; otherItems: Set<string> }) {
  return <div className="space-y-5">{row.groups.map(group => <div key={group.group}>
    <h4 className="mb-2 text-[12px] font-semibold text-muted-foreground">{group.group}</h4>
    <ul className="space-y-2">{group.items.map(item => <li key={item} className="flex items-start gap-2 text-[14px] leading-6"><Check size={16} className={`mt-1 shrink-0 ${otherItems.has(item) ? 'text-muted-foreground' : 'text-primary'}`} /><span>{item}</span></li>)}</ul>
  </div>)}</div>
}

// 账户能力、额度和申请状态均来自本实例；社区部署版有独立许可与功能边界。
export default function Tiers() {
  usePublicSeo('/tiers')
  const [data, setData] = useState<TiersResp | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [loggedIn] = useState(() => isAuthenticated())
  const [status, setStatus] = useState<ProApplyStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(loggedIn)
  const [statusError, setStatusError] = useState(false)
  const [applying, setApplying] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setErr('')
    try { setData(await fetchTiers()) }
    catch { setErr('账户方案暂时无法读取，请稍后重试。') }
    finally { setLoading(false) }
  }, [])
  const loadStatus = useCallback(async () => {
    setStatusLoading(true)
    setStatusError(false)
    try { setStatus(await fetchProApplyStatus()) }
    catch { setStatusError(true) }
    finally { setStatusLoading(false) }
  }, [])
  useEffect(() => { void load(); if (loggedIn) void loadStatus() }, [load, loadStatus, loggedIn])

  async function onApply() {
    if (applying) return
    setApplying(true)
    setMsg('')
    try {
      await applyPro('来自账户方案页申请')
      setSubmitted(true)
      setMsg('申请已提交，等待管理员审核。')
      // 提交成功后即使刷新失败，也保留已提交状态，避免误报失败和重复申请。
      try { setStatus(await fetchProApplyStatus()) } catch { /* 下次打开页面再读取最新状态 */ }
    } catch (error) { setMsg(error instanceof Error ? `提交失败：${error.message}` : '提交失败，请稍后再试。') }
    finally { setApplying(false) }
  }

  const free = data?.tiers.find(tier => tier.key === 'member')
  const pro = data?.tiers.find(tier => tier.key === 'pro')
  const freeItems = new Set((free?.groups ?? []).flatMap(group => group.items))
  const proItems = new Set((pro?.groups ?? []).flatMap(group => group.items))
  const approved = status?.application?.status === 'approved' || ['pro', 'owner', 'admin'].includes(status?.role ?? '')
  const pending = submitted || status?.application?.status === 'pending'
  const proAction = !loggedIn ? <Button asChild className="h-11"><Link to="/login?mode=register">注册后申请 Pro<ArrowRight size={16} /></Link></Button>
    : statusLoading ? <span className="flex min-h-11 items-center gap-2 text-[14px] text-muted-foreground"><Loader2 size={16} className="animate-spin" />读取账户状态…</span>
    : statusError ? <Button variant="outline" className="h-11" onClick={() => void loadStatus()}>重试读取账户状态</Button>
    : approved ? <StatusChip tone="success"><Check size={14} />已开通 Pro</StatusChip>
    : <Button className="h-11" disabled={applying || pending} onClick={onApply}>{applying && <Loader2 size={16} className="animate-spin" />}{applying ? '提交中…' : pending ? '申请审核中' : status?.application?.status === 'rejected' ? '重新申请 Pro' : '申请 Pro'}{!applying && !pending && <ArrowRight size={16} />}</Button>

  return <PublicSiteFrame enabled={!loggedIn}>
    <main className={loggedIn ? 'page-container space-y-8' : 'mx-auto max-w-7xl space-y-10 px-5 py-10 sm:px-8 sm:py-12'}>
      {loggedIn ? <PageHeader title="账户方案" description="选择当前实例的账户权限，或了解社区版自行部署。" /> : <header className="max-w-3xl">
        <p className="mb-4 text-[12px] font-medium tracking-widest text-primary">账户与部署</p>
        <h1 className="text-[36px] font-semibold leading-tight tracking-tight">选择适合你的研究方式</h1>
        <p className="mt-4 text-[16px] leading-8 text-muted-foreground">直接使用当前实例，按需要申请 Pro 权限；也可以获取社区开源版，在自己的环境中部署和管理。</p>
      </header>}

      <section aria-labelledby="community-title" className="grid items-center gap-6 rounded-xl bg-card p-6 sm:p-8 lg:grid-cols-[minmax(0,1fr)_auto]">
        <div className="min-w-0"><div className="mb-3 flex flex-wrap items-center gap-2"><GithubMark size={20} /><h2 id="community-title" className="text-[20px] font-semibold">社区开源版 · 免费自行部署</h2><StatusChip>AGPL-3.0</StatusChip></div>
          <p className="max-w-2xl text-[14px] leading-7 text-muted-foreground">源码公开，按仓库说明安装并配置数据源与模型。社区版功能和发布节奏以仓库为准；当前实例的账户权限与用量上限由本实例管理员管理。</p>
          <a className="mt-3 inline-flex min-h-11 max-w-full items-center gap-2 break-all text-[12px] text-muted-foreground hover:text-foreground" href={COMMUNITY_REPOSITORY} target="_blank" rel="noopener noreferrer">xiaoze-hub/Stock-Intelligent-Data-Analytics<ArrowUpRight size={14} className="shrink-0" /></a>
        </div>
        <div className="flex flex-wrap gap-2 lg:flex-col"><Button asChild variant="outline" className="h-11"><a href={COMMUNITY_REPOSITORY} target="_blank" rel="noopener noreferrer"><GithubMark size={16} />查看开源仓库<ArrowUpRight size={16} /></a></Button><Button asChild variant="ghost" className="h-11"><a href={COMMUNITY_DEPLOYMENT} target="_blank" rel="noopener noreferrer"><Server size={16} />部署说明<ArrowUpRight size={16} /></a></Button></div>
      </section>

      <section aria-labelledby="account-plans-title" className="space-y-5">
        <div className="flex flex-wrap items-end justify-between gap-3"><div><h2 id="account-plans-title" className="text-[20px] font-semibold">当前实例的账户方案</h2><p className="mt-2 text-[14px] leading-7 text-muted-foreground">能力清单与额度读取当前实例配置，开源部署与线上账户分别管理。</p></div>{data && !data.billing_enabled && <StatusChip tone="info">内测期不收费</StatusChip>}</div>
        {loading && !data && <div role="status" className="grid gap-5 md:grid-cols-2">{['免费账户', 'Pro 账户'].map(label => <div key={label} className="min-h-48 rounded-xl bg-card p-6"><p className="text-[20px] font-semibold">{label}</p><p className="mt-6 flex items-center gap-2 text-[14px] text-muted-foreground"><Loader2 size={16} className="animate-spin" />正在读取方案信息…</p></div>)}</div>}
        {err && <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-card p-5"><p className="text-[14px] text-muted-foreground">{err}</p><Button variant="outline" className="h-11" onClick={() => void load()}>重新读取方案</Button></div>}
        {data && <>
          {data.note && <p className="text-[14px] text-muted-foreground">{data.note}</p>}
          <div className="grid items-start gap-5 md:grid-cols-2">{[free, pro].map((tier, index) => tier && <article key={tier.key} className="rounded-xl bg-card p-6 sm:p-8">
            <div className="mb-5 flex items-start justify-between gap-3"><div><h3 className="text-[20px] font-semibold">{tier.label}</h3><p className="mt-2 text-[14px] text-muted-foreground">{index === 0 ? '从基础研究开始' : '需要更多分析能力时申请'}</p></div><StatusChip>{tier.count} 项能力</StatusChip></div>
            <div className="mb-6 flex min-h-11 flex-wrap items-center gap-2">{index === 0 ? <Button asChild variant="outline" className="h-11"><Link to={loggedIn ? '/' : '/login?mode=register'}>{loggedIn ? '返回工作台' : '注册免费账户'}<ArrowRight size={16} /></Link></Button> : proAction}</div>
            <div className="border-t border-border pt-6"><GroupList row={tier} otherItems={index === 0 ? proItems : freeItems} /></div>
          </article>)}</div>
          {!free && !pro && <p className="rounded-xl bg-card p-5 text-[14px] text-muted-foreground">当前实例尚未提供可展示的账户方案。</p>}
          {msg && <p role="status" className="text-[14px] text-muted-foreground">{msg}</p>}
          <div className="rounded-xl bg-card p-6"><h3 className="text-[16px] font-semibold">免费账户当前额度</h3>{data.member_limits ? <dl className="mt-5 grid grid-cols-1 gap-5 sm:grid-cols-3">{[
            ['自选股', data.member_limits.watchlist_max, '只'], ['价格预警', data.member_limits.alert_max, '条'], ['每日试用', data.member_limits.trial_daily_limit, '次'],
          ].map(([label, value, unit]) => <div key={label}><dt className="text-[12px] text-muted-foreground">{label}</dt><dd className="mt-2 text-[28px] font-semibold tabular-nums">{value}<span className="ml-2 text-[14px] font-normal text-muted-foreground">{unit}</span></dd></div>)}</dl> : <p className="mt-3 text-[14px] text-muted-foreground">额度暂未提供，以管理员配置为准。</p>}<p className="mt-4 text-[12px] leading-6 text-muted-foreground">{data.limits_note}</p></div>
          {data.demo_note && <p className="text-[12px] leading-6 text-muted-foreground">{data.demo_note}</p>}
        </>}
      </section>
      <section className="flex flex-wrap items-center justify-between gap-5 rounded-xl bg-s2 p-6"><div><h2 className="text-[16px] font-semibold">准备接入自己的工具？</h2><p className="mt-2 text-[14px] leading-7 text-muted-foreground">查看 API 鉴权、实时目录与调用说明。社区版使用与修改请遵循仓库许可。</p></div><div className="flex flex-wrap gap-3"><Button asChild variant="outline" className="h-11"><Link to="/developers">开发者文档<ArrowRight size={16} /></Link></Button><Button asChild variant="ghost" className="h-11"><a href={PROJECT_REPOSITORY} target="_blank" rel="noopener noreferrer"><GithubMark size={16} />Pro 项目仓库<ArrowUpRight size={16} /></a></Button><Button asChild variant="ghost" className="h-11"><a href={COMMUNITY_LICENSE} target="_blank" rel="noopener noreferrer">开源许可<ArrowUpRight size={16} /></a></Button></div></section>
    </main>
  </PublicSiteFrame>
}
