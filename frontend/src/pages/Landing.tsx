import { useState } from 'react'
import { ArrowDown, ArrowRight, Check, PlayCircle, Code2, Database, Moon, ShieldCheck, Smartphone, Sun, Menu, X, BookOpen, Server, ArrowUpRight, Download } from 'lucide-react'
import { Link } from 'react-router-dom'
import { BrandMark } from '@/components/BrandMark'
import { ProductVideo } from '@/components/landing/ProductVideo'
import { useTheme } from '@/hooks/use-theme'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@panwatch/base-ui/components/ui/tabs'

const media = '/media/product/'
const navigation = [{ href: '#overview', label: '产品概览' }, { href: '#workflow', label: '产品功能' }, { href: '#demo', label: '操作演示' }, { href: '#resources', label: '部署与文档' }, { href: '#faq', label: '常见问题' }]
const demos = [
  {
    id: 'market', title: '行情工作台', label: '01 / 看数据',
    description: '从持仓与关注列表进入标的，在图表里切换周期、叠加指标。',
    poster: 'market-poster.webp', duration: '00:31',
    steps: ['打开持仓 / 关注股切换器，确认市场与代码。', '切换日 K、周 K，叠加 MACD 等技术指标。', '结合来源、最新 K 线日期与缺失提示阅读数据。'],
    note: '录制中的股票仅用于演示界面操作，不代表推荐。',
  },
  {
    id: 'assistant', title: '数智参谋', label: '02 / 查过程',
    description: '围绕一个技术问题组织对话，展开工具调用，查看分析经过。',
    poster: 'assistant-poster.webp', duration: '00:25',
    steps: ['在完整工作台中打开已有会话。', '展开“过程”，查看工具、参数与执行耗时。', '打开过程面板，继续追溯已有运行记录。'],
    note: '演示回放已有工具记录，未生成新的 AI 结论；使用 AI 需配置模型。',
  },
  {
    id: 'system', title: '系统状态', label: '03 / 知状态',
    description: '把服务依赖和连接依据放在一张图里，哪里需要配置一眼可见。',
    poster: 'system-poster.webp', duration: '00:18',
    steps: ['查看数据库、缓存、AI、调度等模块的状态。', '点击数据库节点，在侧边面板查看检测依据。', '刷新状态，按模块更新结果并保留快照。'],
    note: '流动表示最近检测连通，非实时请求流量；未配置与异常如实显示。',
  },
] as const

const capabilities = [
  { icon: Database, title: '先确认数据，再讨论结论', text: '行情、K 线与资金指标保留来源和口径。没有取到的数据明确显示缺失，不用猜测填补。' },
  { icon: ShieldCheck, title: '每一步都有上下文', text: '会话、工具过程、运行记录与口径对照各有位置，让你能回头检查分析所依据的内容。' },
  { icon: Code2, title: '按自己的方式接入', text: '通过设置管理模型与通知，使用 API Key 调用 Skill 接口。具体能力取决于数据源与服务配置。' },
]

export default function LandingPage() {
  const { theme, toggleTheme } = useTheme()
  const [menuOpen, setMenuOpen] = useState(false)
  return (
    <div className="min-h-screen bg-background text-foreground">
      <a href="#landing-main" className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:bg-card focus:p-4">跳到主要内容</a>
      <header className="sticky top-0 z-30 border-b border-border bg-card" onKeyDown={event => { if (event.key === 'Escape') setMenuOpen(false) }}>
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-5 py-3 sm:px-8">
          <Link to="/" className="flex shrink-0 items-center gap-3" aria-label="SIDA 数智分析首页">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary text-primary-foreground"><BrandMark className="h-6 w-6" /></span>
            <span className="hidden leading-tight min-[360px]:block"><span className="block text-[16px] font-semibold tracking-tight">SIDA Pro</span><span className="mt-1 block text-[12px] text-muted-foreground">数智分析</span></span>
          </Link>
          <nav aria-label="官网导航" className="hidden items-center gap-1 lg:flex">
            {navigation.map(item => <a key={item.href} href={item.href} className="inline-flex min-h-11 items-center rounded-lg px-3 text-[14px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground">{item.label}</a>)}
          </nav>
          <div className="flex items-center gap-1 sm:gap-2">
            <Button variant="ghost" size="icon" className="h-11 w-11" onClick={toggleTheme} aria-label={theme === 'dark' ? '切换浅色模式' : '切换深色模式'}>
              {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
            </Button>
            <Button asChild variant="ghost" className="h-11 px-2 text-foreground sm:px-3"><Link to="/login">登录工作台<ArrowUpRight size={16} className="hidden sm:block" /></Link></Button>
            <Button variant="ghost" size="icon" className="h-11 w-11 lg:hidden" aria-label={menuOpen ? '收起网站导航' : '展开网站导航'} aria-expanded={menuOpen} aria-controls="landing-mobile-nav" onClick={() => setMenuOpen(!menuOpen)}>{menuOpen ? <X size={20} /> : <Menu size={20} />}</Button>
          </div>
        </div>
        {menuOpen && <nav id="landing-mobile-nav" aria-label="手机网站导航" className="grid border-t border-border bg-card px-5 py-3 lg:hidden">{navigation.map(item => <a key={item.href} href={item.href} onClick={() => setMenuOpen(false)} className="flex min-h-11 items-center rounded-lg px-3 text-[14px] font-medium hover:bg-accent">{item.label}</a>)}</nav>}
      </header>

      <main id="landing-main">
        <section id="overview" className="mx-auto max-w-7xl scroll-mt-24 px-5 pb-12 pt-10 sm:px-8 sm:pt-14">
          <div className="grid items-end gap-8 lg:grid-cols-12 lg:gap-12">
            <div className="lg:col-span-7">
              <p className="mb-5 text-[12px] font-medium tracking-widest text-primary">股票研究 · 数据分析 · 过程追溯</p>
              <h1 className="text-[36px] font-semibold leading-tight tracking-tight sm:text-[48px] lg:text-[48px]">AI 股票技术分析，<br />让研究有据可查。</h1>
            </div>
            <div className="lg:col-span-5">
              <p className="max-w-lg text-[16px] leading-8 text-muted-foreground">整合市场全景、个股图表、关注列表与 AI 对话，连接数据查看、技术分析和过程复核，构建完整的个人研究工作流。</p>
              <div className="mt-6 flex flex-wrap gap-3">
                <Button asChild size="lg" className="h-12 gap-5 rounded-xl px-5"><Link to="/login">进入研究工作台<ArrowUpRight size={20} /></Link></Button>
                <Button asChild size="lg" variant="ghost" className="h-12 gap-3 px-2 text-foreground hover:bg-transparent hover:text-primary"><a href="#demo"><PlayCircle size={22} /><span className="underline decoration-border underline-offset-4">浏览操作演示</span></a></Button>
              </div>
              <p className="mt-4 text-[12px] leading-6 text-muted-foreground">基于已有数据的技术分析 · 非投资建议 · 支持自行部署</p>
            </div>
          </div>
          <figure className="mt-10 overflow-hidden rounded-xl bg-card p-3 shadow-sm sm:mt-12 sm:p-4">
            <div className="flex flex-wrap items-center justify-between gap-2 bg-card px-1 pb-3 pt-1 text-[12px] text-muted-foreground sm:px-5">
              <span className="font-medium text-foreground">个股研究工作台</span>
              <a href="#demo" className="inline-flex items-center gap-1 hover:text-primary">查看实际操作<ArrowDown size={14} /></a>
            </div>
            <img src={`${media}workbench-desktop.webp`} width="1440" height="1100" alt="SIDA 深色个股工作台实拍：行情摘要、日 K、技术指标与筹码标注" fetchPriority="high" className="aspect-[1440/1100] h-auto w-full rounded-xl bg-s2 object-contain" />
            <figcaption className="px-4 py-3 text-[12px] leading-6 text-muted-foreground sm:px-5">真实界面录制于 2026-10-27 · v0.82.2 本地环境。画面为当时的数据与配置快照，非当前行情。</figcaption>
          </figure>
        </section>

        <section id="demo" className="scroll-mt-24 bg-card py-14 sm:py-20" aria-labelledby="demo-title">
          <div className="mx-auto max-w-7xl px-5 sm:px-8">
            <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
              <div><p className="mb-3 text-[12px] font-medium tracking-widest text-primary">PRODUCT IN ACTION</p><h2 id="demo-title" className="text-[28px] font-semibold tracking-tight sm:text-[36px]">核心功能操作演示</h2></div>
              <p className="max-w-md text-[14px] leading-7 text-muted-foreground">真实浏览器操作，剪去等待时间。点击播放，支持字幕、进度拖动和全屏观看。</p>
            </div>
            <Tabs defaultValue="market">
              <TabsList aria-label="产品操作演示" className="mb-6 gap-3 border-b border-border pb-2">
                {demos.map(demo => <TabsTrigger key={demo.id} value={demo.id} className="min-h-12 rounded-none bg-transparent px-3 text-[14px] text-muted-foreground hover:bg-transparent hover:text-foreground data-[state=active]:bg-transparent data-[state=active]:font-semibold data-[state=active]:text-foreground data-[state=active]:underline data-[state=active]:decoration-2 data-[state=active]:underline-offset-8">{demo.title}</TabsTrigger>)}
              </TabsList>
              {demos.map(demo => (
                <TabsContent value={demo.id} key={demo.id}>
                  <div className="grid items-start gap-6 lg:grid-cols-12 lg:gap-8">
                    <div className="min-w-0 lg:col-span-9">
                      <ProductVideo key={demo.id} title={demo.title} src={`${media}${demo.id}.mp4`} poster={`${media}${demo.poster}`} captions={`${media}${demo.id}.zh.vtt`} duration={demo.duration} />
                    </div>
                    <div className="lg:col-span-3">
                      <p className="text-[12px] font-medium text-primary">{demo.label}</p>
                      <h3 className="mt-3 text-[20px] font-semibold">{demo.title}</h3>
                      <p className="mt-3 text-[14px] leading-7 text-muted-foreground">{demo.description}</p>
                      <ol className="mt-5 space-y-4 text-[14px] leading-6">
                        {demo.steps.map((step, index) => <li key={step} className="flex gap-3"><span className="font-mono text-muted-foreground">0{index + 1}</span><span>{step}</span></li>)}
                      </ol>
                      <p className="mt-5 text-[12px] leading-6 text-muted-foreground">{demo.note}</p>
                      <a href={`${media}${demo.id}.mp4`} download className="mt-4 inline-flex min-h-11 items-center gap-2 text-[14px] font-medium text-muted-foreground underline decoration-border underline-offset-4 hover:text-foreground"><Download size={15} />下载演示视频<ArrowDown size={15} /></a>
                    </div>
                  </div>
                </TabsContent>
              ))}
            </Tabs>
          </div>
        </section>

        <section id="workflow" className="mx-auto max-w-7xl scroll-mt-24 px-5 py-14 sm:px-8 sm:py-20" aria-labelledby="workflow-title">
          <p className="mb-3 text-[12px] font-medium tracking-widest text-primary">A CONNECTED WORKSPACE</p>
          <h2 id="workflow-title" className="max-w-2xl text-[28px] font-semibold tracking-tight sm:text-[36px]">从市场到标的，<br className="sm:hidden" />从问题到证据。</h2>
          <div className="mt-9 grid gap-6 lg:grid-cols-2">
            <figure className="overflow-hidden rounded-xl bg-card p-3">
              <img src={`${media}dashboard-dark.webp`} width="1440" height="960" alt="市场全景首页，显示指数走势、市场状态与可折叠看板" loading="lazy" className="h-auto w-full rounded-xl" />
              <figcaption className="p-6"><h3 className="text-[20px] font-semibold">先建立市场上下文</h3><p className="mt-2 text-[14px] leading-7 text-muted-foreground">指数、市场全景与组合工作台集中展示。按需要折叠和定制看板，缺失的数据保留说明。</p></figcaption>
            </figure>
            <figure className="overflow-hidden rounded-xl bg-card p-3">
              <img src={`${media}assistant-process.webp`} width="1440" height="960" alt="数智参谋已有会话展开五步工具调用的真实记录" loading="lazy" className="h-auto w-full rounded-xl" />
              <figcaption className="p-6"><h3 className="text-[20px] font-semibold">把问题带进分析过程</h3><p className="mt-2 text-[14px] leading-7 text-muted-foreground">完整页与迷你助手共用会话。结合图表提出问题，回看工具步骤与已有运行记录。</p></figcaption>
            </figure>
          </div>
          <div className="mt-10 grid gap-8 md:grid-cols-3">
            {capabilities.map(({ icon: Icon, title, text }) => <div key={title}><Icon className="mb-4 text-primary" size={22} /><h3 className="text-[16px] font-semibold">{title}</h3><p className="mt-3 text-[14px] leading-7 text-muted-foreground">{text}</p></div>)}
          </div>
        </section>

        <section className="bg-card py-14 sm:py-20" aria-labelledby="mobile-title">
          <div className="mx-auto grid max-w-6xl items-center gap-10 px-5 sm:px-8 lg:grid-cols-2 lg:gap-20">
            <div>
              <Smartphone className="mb-5 text-primary" size={26} />
              <h2 id="mobile-title" className="text-[28px] font-semibold leading-tight tracking-tight sm:text-[36px]">桌面与移动端，<br />一致的分析体验。</h2>
              <p className="mt-5 max-w-md text-[16px] leading-8 text-muted-foreground">手机打开助手即可覆盖可用空间。输入默认一行，回车换行；由你点击输入，不自动唤起键盘。</p>
              <ul className="mt-6 space-y-3 text-[14px]">
                {['桌面与手机自适应布局', '浅色、深色与外观偏好', '持仓与关注股按账号管理'].map(text => <li key={text} className="flex items-center gap-3"><Check size={16} className="text-primary" />{text}</li>)}
              </ul>
              <a href={`${media}mobile-assistant.webp`} target="_blank" rel="noreferrer" className="mt-6 inline-flex min-h-11 items-center gap-2 text-[14px] font-medium text-primary">查看手机截图<ArrowRight size={16} /></a>
            </div>
            <figure className="mx-auto w-full max-w-xs overflow-hidden rounded-xl">
              <ProductVideo title="手机助手" src={`${media}mobile.mp4`} poster={`${media}mobile-assistant.webp`} captions={`${media}mobile.zh.vtt`} duration="00:16" portrait />
            </figure>
          </div>
        </section>

        <section id="resources" className="mx-auto max-w-7xl scroll-mt-24 px-5 pt-14 sm:px-8 sm:pt-20" aria-labelledby="resources-title">
          <div className="grid items-center gap-8 rounded-xl bg-card p-6 sm:p-8 lg:grid-cols-2">
            <div><Server size={24} className="mb-4 text-primary" /><h2 id="resources-title" className="text-[28px] font-semibold tracking-tight">可管理、可配置的研究环境</h2><p className="mt-4 text-[14px] leading-7 text-muted-foreground">通过系统状态确认依赖连接，在设置中管理模型与通知。自行部署使用 PostgreSQL 与 Redis；预测引擎按需单独配置。</p><Button asChild variant="ghost" className="mt-5 gap-3 px-0 text-foreground hover:bg-transparent hover:text-primary"><Link to="/developers"><BookOpen size={18} />查阅开发者文档<ArrowUpRight size={16} /></Link></Button><p className="mt-3 text-[12px] text-muted-foreground">文档与 API 管理需登录当前实例。</p></div>
            <figure><img src={`${media}system-light.webp`} width="1440" height="960" alt="系统状态连接图：服务、数据库与缓存正常，未配置和异常模块明确标记" loading="lazy" className="h-auto w-full rounded-lg" /><figcaption className="mt-3 text-[12px] leading-6 text-muted-foreground">系统状态实拍 · 点击节点查看连接依据与检测结果</figcaption></figure>
          </div>
        </section>

        <section id="faq" className="mx-auto max-w-7xl scroll-mt-24 px-5 py-14 sm:px-8 sm:py-20" aria-labelledby="faq-title">
          <div className="grid gap-8 lg:grid-cols-12 lg:gap-16">
            <div className="lg:col-span-5"><p className="mb-3 text-[12px] font-medium tracking-widest text-primary">BEFORE YOU START</p><h2 id="faq-title" className="text-[28px] font-semibold tracking-tight sm:text-[36px]">使用与配置说明</h2><p className="mt-5 text-[14px] leading-7 text-muted-foreground">工具帮助你整理与核查数据。分析由可用数据与模型共同决定，最终判断始终由你完成。</p></div>
            <div className="space-y-3 lg:col-span-7">
              {[
                ['它会推荐买卖股票吗？', 'SIDA 用于股票技术分析与研究。AI 输出、图表标注、预测和报告均不构成投资建议，也不保证收益。'],
                ['部署后所有功能都能直接用吗？', '不能。行情取决于来源网络、权限与历史数据；AI 需配置模型服务；通知需配置渠道；预测引擎需单独部署。系统状态会区分正常、未检测、未配置与异常。'],
                ['怎样开始使用？', '通过“进入工作台”登录，注册是否开放由实例管理员决定。自行部署请先准备 PostgreSQL、Redis 与认证配置，再按仓库运行手册启动。'],
                ['截图里的数据是实时的吗？', '此页视频和截图录制于 2026-10-27，保留了当时的来源、时间与缺失提示。它们是产品操作演示，不是实时行情或实盘业绩。'],
              ].map(([question, answer]) => <details key={question} className="rounded-xl bg-card p-5"><summary className="cursor-pointer text-[16px] font-medium">{question}</summary><p className="mt-4 text-[14px] leading-7 text-muted-foreground">{answer}</p></details>)}
            </div>
          </div>
        </section>

        <section className="bg-s2 px-5 py-14 text-center sm:px-8 sm:py-20">
          <p className="text-[12px] font-medium tracking-widest text-primary">YOUR NEXT ANALYSIS STARTS HERE</p>
          <h2 className="mt-4 text-[28px] font-semibold tracking-tight sm:text-[36px]">开启你的下一次技术分析</h2>
          <p className="mt-4 text-[16px] leading-8 text-muted-foreground">打开图表，提出问题，让每次分析都有据可查。</p>
          <Button asChild size="lg" className="mt-7 h-12 gap-5 rounded-xl px-6"><Link to="/login">进入研究工作台<ArrowUpRight size={20} /></Link></Button>
          <p className="mt-5 text-[12px] text-muted-foreground">已有账号直接登录 · 新账号遵循当前实例的注册规则</p>
        </section>
      </main>
      <footer className="mx-auto flex max-w-7xl flex-col justify-between gap-5 px-5 py-8 text-[12px] leading-6 text-muted-foreground sm:px-8 md:flex-row">
        <p>© 2026 SIDA Pro · 数智分析<br />本平台数据仅供参考，不构成投资建议。市场有风险，投资需谨慎。</p>
        <nav aria-label="页脚导航" className="flex flex-wrap gap-6"><a href="#demo" className="hover:text-primary">操作演示</a><Link to="/developers" className="hover:text-primary">开发者文档</Link><Link to="/tiers" className="hover:text-primary">账户方案</Link><Link to="/login" className="hover:text-primary">登录工作台</Link></nav>
      </footer>
    </div>
  )
}
