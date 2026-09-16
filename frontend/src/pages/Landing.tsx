import { Link } from 'react-router-dom'
import {
  LineChart,
  Sparkles,
  Code2,
  Database,
  ArrowRight,
  KeyRound,
  Terminal,
  UserPlus,
} from 'lucide-react'
import { BrandMark } from '@/components/BrandMark'
import { Button } from '@panwatch/base-ui/components/ui/button'

/**
 * 官网落地页(2026-09-16): 未登录访问 / 时展示。
 * 风格对标 DeepSeek 开放平台: 深色主题、简洁现代、无渐变/emoji。
 */

const FEATURES = [
  {
    icon: LineChart,
    title: '行情分析',
    desc: 'A 股全市场行情、K 线工作台、板块热力图与主力资金多口径分析，数据能力一目了然。',
  },
  {
    icon: Sparkles,
    title: 'AI 助手',
    desc: '内置 AI Agent 覆盖复盘、研报、机会挖掘等场景，自然语言直达分析结论。',
  },
  {
    icon: Code2,
    title: 'Skill API',
    desc: '标准 REST 接口开放全部分析能力，API Key 鉴权，按量限流，快速集成到自有系统。',
  },
  {
    icon: Database,
    title: '多数据源',
    desc: '聚合多家行情与资讯源，自动健康检查与降级切换，口径标签透明可追溯。',
  },
]

const STEPS = [
  {
    icon: UserPlus,
    step: '01',
    title: '注册账号',
    desc: '创建免费账号，即刻开通平台访问权限。',
  },
  {
    icon: KeyRound,
    step: '02',
    title: '获取 API Key',
    desc: '注册后自动签发 sk_ 前缀密钥，在控制台管理与查看用量。',
  },
  {
    icon: Terminal,
    step: '03',
    title: '调用接口',
    desc: '携带 X-API-Key 调用 Skill 接口，几分钟完成接入。',
  },
]

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* 顶部导航 */}
      <header className="sticky top-0 z-40 border-b border-border/60 bg-background/90 backdrop-blur">
        <div className="mx-auto max-w-6xl px-4 h-14 flex items-center justify-between">
          <Link to="/" className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-xl bg-primary flex items-center justify-center">
              <BrandMark className="w-4 h-4 text-white" />
            </div>
            <span className="text-[15px] font-bold">数智分析 SIDA</span>
          </Link>
          <nav className="flex items-center gap-2">
            <Button variant="ghost" size="sm" asChild>
              <Link to="/developers">开发者文档</Link>
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <Link to="/login">登录</Link>
            </Button>
            <Button size="sm" asChild>
              <Link to="/login?mode=register">注册</Link>
            </Button>
          </nav>
        </div>
      </header>

      {/* Hero */}
      <section className="mx-auto max-w-6xl px-4 pt-20 pb-16 md:pt-28 md:pb-20">
        <div className="max-w-3xl">
          <p className="text-sm font-medium text-primary mb-4">A 股智能数据分析平台</p>
          <h1 className="text-4xl md:text-5xl font-bold leading-tight tracking-tight">
            数据 · 分析 · 决策
            <br />
            AI 全链路打通
          </h1>
          <p className="mt-6 text-base md:text-lg text-muted-foreground leading-relaxed max-w-2xl">
            数智分析 SIDA 为个人投资者与开发者提供一站式 A 股行情分析、AI 研报与开放 API。
            注册即用，也可通过 Skill API 将分析能力接入你自己的应用。
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <Button size="lg" asChild>
              <Link to="/login?mode=register">
                立即注册
                <ArrowRight className="ml-1.5 w-4 h-4" />
              </Link>
            </Button>
            <Button size="lg" variant="outline" asChild>
              <Link to="/developers">查看文档</Link>
            </Button>
          </div>
        </div>
      </section>

      {/* 核心功能 */}
      <section className="border-t border-border/60">
        <div className="mx-auto max-w-6xl px-4 py-16 md:py-20">
          <h2 className="text-2xl md:text-3xl font-bold">核心功能</h2>
          <p className="mt-2 text-muted-foreground">从看盘到集成，一个平台覆盖完整链路。</p>
          <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {FEATURES.map(({ icon: Icon, title, desc }) => (
              <div
                key={title}
                className="rounded-xl border border-border bg-card p-5 hover:border-primary/40 transition-colors"
              >
                <div className="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center mb-4">
                  <Icon className="w-5 h-5 text-primary" />
                </div>
                <h3 className="text-[15px] font-semibold">{title}</h3>
                <p className="mt-2 text-sm text-muted-foreground leading-relaxed">{desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* 快速开始 */}
      <section className="border-t border-border/60 bg-card/40">
        <div className="mx-auto max-w-6xl px-4 py-16 md:py-20">
          <h2 className="text-2xl md:text-3xl font-bold">快速开始</h2>
          <p className="mt-2 text-muted-foreground">三步完成接入，无需繁琐审批。</p>
          <div className="mt-10 grid gap-4 md:grid-cols-3">
            {STEPS.map(({ icon: Icon, step, title, desc }) => (
              <div key={step} className="rounded-xl border border-border bg-background p-6">
                <div className="flex items-center gap-3 mb-4">
                  <div className="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center">
                    <Icon className="w-5 h-5 text-primary" />
                  </div>
                  <span className="text-xs font-medium text-muted-foreground tracking-wider">
                    STEP {step}
                  </span>
                </div>
                <h3 className="text-[15px] font-semibold">{title}</h3>
                <p className="mt-2 text-sm text-muted-foreground leading-relaxed">{desc}</p>
              </div>
            ))}
          </div>
          <div className="mt-10">
            <Button asChild>
              <Link to="/login?mode=register">
                开始使用
                <ArrowRight className="ml-1.5 w-4 h-4" />
              </Link>
            </Button>
          </div>
        </div>
      </section>

      {/* 底部 */}
      <footer className="border-t border-border/60">
        <div className="mx-auto max-w-6xl px-4 py-10 flex flex-col md:flex-row md:items-center md:justify-between gap-4">
          <div className="flex items-center gap-2.5">
            <div className="w-7 h-7 rounded-lg bg-primary flex items-center justify-center">
              <BrandMark className="w-3.5 h-3.5 text-white" />
            </div>
            <div>
              <div className="text-sm font-semibold">数智分析 SIDA</div>
              <div className="text-xs text-muted-foreground mt-0.5">
                分析结果仅供参考，不构成投资建议
              </div>
            </div>
          </div>
          <div className="flex items-center gap-5 text-sm text-muted-foreground">
            <Link to="/developers" className="hover:text-foreground transition-colors">
              开发者文档
            </Link>
            <Link to="/login" className="hover:text-foreground transition-colors">
              登录
            </Link>
            <Link to="/login?mode=register" className="hover:text-foreground transition-colors">
              注册
            </Link>
          </div>
          <p className="text-xs text-muted-foreground">
            © {new Date().getFullYear()} SIDA. All rights reserved.
          </p>
        </div>
      </footer>
    </div>
  )
}
