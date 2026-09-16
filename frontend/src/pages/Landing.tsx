import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  LineChart,
  Code2,
  ArrowRight,
  Shield,
  BarChart3,
  Brain,
  Layers,
} from 'lucide-react'
import { BrandMark } from '@/components/BrandMark'

/* ═══════════════════════════════════════════
   DeepSeek Harness 风格落地页
   极简深色 · 卡片布局 · 专业开发者调性
   ═══════════════════════════════════════════ */

/* ── 背景网格 ── */
function GridBg() {
  return (
    <div className="absolute inset-0 z-0">
      <div
        className="absolute inset-0 opacity-[0.03]"
        style={{
          backgroundImage: `linear-gradient(rgba(255,255,255,0.1) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.1) 1px, transparent 1px)`,
          backgroundSize: '60px 60px',
        }}
      />
      <div className="absolute top-0 left-1/2 -translate-x-1/2 h-[600px] w-[1200px] rounded-full bg-blue-500/[0.04] blur-[150px]" />
    </div>
  )
}

/* ── 打字机 ── */
function Typewriter({ text, speed = 50 }: { text: string; speed?: number }) {
  const [displayed, setDisplayed] = useState('')
  const [done, setDone] = useState(false)
  useEffect(() => {
    let i = 0
    const t = setInterval(() => {
      if (i < text.length) { setDisplayed(text.slice(0, i + 1)); i++ }
      else { setDone(true); clearInterval(t) }
    }, speed)
    return () => clearInterval(t)
  }, [text, speed])
  return <span>{displayed}{!done && <span className="animate-pulse text-blue-400">|</span>}</span>
}

/* ── 功能卡片 ── */
function FeatureCard({ icon: Icon, title, desc, tag }: { icon: any; title: string; desc: string; tag?: string }) {
  return (
    <div className="group relative rounded-2xl border border-white/[0.06] bg-[#0f1117] p-6 transition-all duration-300 hover:border-white/[0.12] hover:bg-[#12141c]">
      {tag && (
        <span className="mb-3 inline-block rounded-md bg-blue-500/10 px-2 py-0.5 text-[10px] font-medium text-blue-400">
          {tag}
        </span>
      )}
      <div className="mb-4 inline-flex h-10 w-10 items-center justify-center rounded-xl bg-white/[0.04] text-blue-400 ring-1 ring-white/[0.06]">
        <Icon className="h-5 w-5" />
      </div>
      <h3 className="mb-2 text-[15px] font-semibold text-white">{title}</h3>
      <p className="text-[13px] leading-relaxed text-zinc-400">{desc}</p>
    </div>
  )
}

/* ── 代码展示块 ── */
function CodeShowcase() {
  return (
    <div className="relative overflow-hidden rounded-2xl border border-white/[0.06] bg-[#0c0e14]">
      <div className="flex items-center gap-2 border-b border-white/[0.06] px-4 py-2.5">
        <div className="flex gap-1.5">
          <div className="h-2.5 w-2.5 rounded-full bg-red-500/60" />
          <div className="h-2.5 w-2.5 rounded-full bg-yellow-500/60" />
          <div className="h-2.5 w-2.5 rounded-full bg-green-500/60" />
        </div>
        <span className="ml-2 font-mono text-[11px] text-zinc-600">terminal</span>
      </div>
      <pre className="overflow-x-auto p-5 font-mono text-[12px] leading-[1.8]">
        <code>
          <span className="text-zinc-600">$ </span>
          <span className="text-blue-400">curl</span>
          <span className="text-zinc-300"> -X POST </span>
          <span className="text-green-400">"https://api.sida.dev/skills/get_stock_quote/run"</span>
          <span className="text-zinc-300"> \</span>
          {'\n'}
          <span className="text-zinc-300">  -H </span>
          <span className="text-green-400">"X-API-Key: sk_YOUR_KEY"</span>
          <span className="text-zinc-300"> \</span>
          {'\n'}
          <span className="text-zinc-300">  -d </span>
          <span className="text-green-400">'&#123;"symbol": "000001"&#125;'</span>
          {'\n\n'}
          <span className="text-zinc-600"># Response</span>
          {'\n'}
          <span className="text-zinc-300">&#123;</span>
          {'\n'}
          <span className="text-zinc-300">  </span>
          <span className="text-blue-300">"code"</span>
          <span className="text-zinc-300">: </span>
          <span className="text-orange-400">0</span>
          <span className="text-zinc-300">,</span>
          {'\n'}
          <span className="text-zinc-300">  </span>
          <span className="text-blue-300">"data"</span>
          <span className="text-zinc-300">: &#123;</span>
          {'\n'}
          <span className="text-zinc-300">    </span>
          <span className="text-blue-300">"price"</span>
          <span className="text-zinc-300">: </span>
          <span className="text-orange-400">11.82</span>
          <span className="text-zinc-300">,</span>
          {'\n'}
          <span className="text-zinc-300">    </span>
          <span className="text-blue-300">"change_pct"</span>
          <span className="text-zinc-300">: </span>
          <span className="text-orange-400">-0.25</span>
          <span className="text-zinc-300">,</span>
          {'\n'}
          <span className="text-zinc-300">    </span>
          <span className="text-blue-300">"main_net"</span>
          <span className="text-zinc-300">: </span>
          <span className="text-orange-400">39269757</span>
          {'\n'}
          <span className="text-zinc-300">  &#125;</span>
          {'\n'}
          <span className="text-zinc-300">&#125;</span>
        </code>
      </pre>
    </div>
  )
}

/* ── 统计数字 ── */
function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div className="text-center">
      <div className="text-[28px] font-bold tracking-tight text-white md:text-[36px]">{value}</div>
      <div className="mt-1 text-[12px] text-zinc-500">{label}</div>
    </div>
  )
}

/* ── 主页面 ── */
const FEATURES = [
  { icon: LineChart, title: '实时行情分析', desc: 'A 股全市场行情、K 线工作台、板块热力图、主力资金多口径穿透分析。', tag: 'MARKET' },
  { icon: Brain, title: 'AI 智能助手', desc: '内置 Agent 覆盖复盘、研报、机会挖掘，自然语言直达分析结论。', tag: 'AI' },
  { icon: Code2, title: 'Skill API', desc: '30+ REST 接口开放全部分析能力，Key 鉴权，按量限流，即插即用。', tag: 'API' },
  { icon: Layers, title: '多源数据', desc: '聚合行情/资讯/资金多源，自动降级切换，口径标签透明可追溯。', tag: 'DATA' },
  { icon: BarChart3, title: '资金流向', desc: '明盘/暗盘/L2 多维度资金分析，主力意图识别，大单拆单追踪。', tag: 'FLOW' },
  { icon: Shield, title: '企业级安全', desc: '数据加密传输，Key 权限隔离，操作审计日志，配额精细控制。', tag: 'SEC' },
]

const STEPS = [
  { num: '01', title: '注册账号', desc: '邮箱注册，自动签发 API Key' },
  { num: '02', title: '阅读文档', desc: '查看 Skill 目录和调用示例' },
  { num: '03', title: '开始调用', desc: '一行 curl 验证连通性' },
]

export default function LandingPage() {
  return (
    <div className="relative min-h-screen bg-[#0a0b0f] text-white">
      <GridBg />

      {/* 导航 */}
      <header className="relative z-10">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
          <Link to="/" className="flex items-center gap-2.5">
            <BrandMark />
            <span className="text-[15px] font-semibold tracking-tight">SIDA</span>
          </Link>
          <nav className="hidden items-center gap-8 md:flex">
            <Link to="/developers" className="text-[13px] text-zinc-400 transition-colors hover:text-white">文档</Link>
            <Link to="/developers" className="text-[13px] text-zinc-400 transition-colors hover:text-white">API</Link>
            <Link to="/login" className="text-[13px] text-zinc-400 transition-colors hover:text-white">登录</Link>
            <Link
              to="/login?mode=register"
              className="rounded-lg bg-white px-4 py-1.5 text-[13px] font-medium text-black transition-colors hover:bg-zinc-200"
            >
              注册
            </Link>
          </nav>
          {/* 移动端 */}
          <div className="flex items-center gap-3 md:hidden">
            <Link to="/login" className="text-[13px] text-zinc-400">登录</Link>
            <Link to="/login?mode=register" className="rounded-lg bg-white px-3 py-1.5 text-[12px] font-medium text-black">注册</Link>
          </div>
        </div>
      </header>

      {/* Hero */}
      <section className="relative z-10 mx-auto max-w-6xl px-6 pt-20 pb-24 text-center md:pt-28">
        <div className="mb-5 inline-flex items-center gap-2 rounded-full border border-white/[0.08] bg-white/[0.03] px-3.5 py-1 text-[12px] text-zinc-400">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
          开发者预览版已开放
        </div>

        <h1 className="mx-auto max-w-3xl text-[36px] font-bold leading-[1.15] tracking-tight md:text-[56px]">
          <span className="text-white">一切皆</span>
          <span className="bg-gradient-to-r from-blue-400 to-blue-600 bg-clip-text text-transparent">接口</span>
        </h1>

        <p className="mx-auto mt-5 max-w-xl text-[15px] leading-relaxed text-zinc-400 md:text-[16px]">
          <Typewriter text="AI 驱动的 A 股智能分析平台 — 行情 · 资金 · 情报 · 决策，全部通过 API 开放。" speed={40} />
        </p>

        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
          <Link
            to="/login?mode=register"
            className="inline-flex items-center gap-2 rounded-xl bg-white px-6 py-2.5 text-[14px] font-medium text-black transition-colors hover:bg-zinc-200"
          >
            开始使用 <ArrowRight className="h-4 w-4" />
          </Link>
          <Link
            to="/developers"
            className="inline-flex items-center gap-2 rounded-xl border border-white/[0.1] bg-white/[0.03] px-6 py-2.5 text-[14px] font-medium text-white transition-colors hover:bg-white/[0.06]"
          >
            <Code2 className="h-4 w-4" /> 查看文档
          </Link>
        </div>

        {/* 统计 */}
        <div className="mt-16 flex flex-wrap items-center justify-center gap-12 md:gap-20">
          <Stat value="30+" label="Skill 接口" />
          <Stat value="5000+" label="A 股覆盖" />
          <Stat value="<2s" label="P95 延迟" />
          <Stat value="99.9%" label="可用性" />
        </div>
      </section>

      {/* 代码展示 */}
      <section className="relative z-10 mx-auto max-w-4xl px-6 pb-20">
        <CodeShowcase />
      </section>

      {/* 核心功能 */}
      <section className="relative z-10 mx-auto max-w-6xl px-6 pb-20">
        <div className="mb-10 text-center">
          <h2 className="text-[24px] font-bold tracking-tight text-white md:text-[30px]">核心能力</h2>
          <p className="mt-2 text-[14px] text-zinc-500">一套 API，覆盖 A 股分析全链路</p>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((f, i) => (
            <FeatureCard key={i} {...f} />
          ))}
        </div>
      </section>

      {/* 快速开始 */}
      <section className="relative z-10 mx-auto max-w-4xl px-6 pb-24">
        <div className="mb-10 text-center">
          <h2 className="text-[24px] font-bold tracking-tight text-white md:text-[30px]">快速开始</h2>
          <p className="mt-2 text-[14px] text-zinc-500">三步接入，10 分钟完成第一次调用</p>
        </div>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {STEPS.map((s, i) => (
            <div key={i} className="relative rounded-2xl border border-white/[0.06] bg-[#0f1117] p-6">
              <div className="mb-3 font-mono text-[32px] font-bold text-white/[0.06]">{s.num}</div>
              <h3 className="mb-1.5 text-[15px] font-semibold text-white">{s.title}</h3>
              <p className="text-[13px] text-zinc-500">{s.desc}</p>
              {i < 2 && (
                <div className="absolute -right-2.5 top-1/2 hidden -translate-y-1/2 md:block">
                  <ArrowRight className="h-4 w-4 text-white/[0.1]" />
                </div>
              )}
            </div>
          ))}
        </div>
      </section>

      {/* CTA */}
      <section className="relative z-10 mx-auto max-w-4xl px-6 pb-24">
        <div className="rounded-2xl border border-white/[0.06] bg-gradient-to-b from-[#0f1117] to-[#0a0b0f] p-10 text-center">
          <h2 className="text-[22px] font-bold text-white md:text-[26px]">准备好了吗？</h2>
          <p className="mx-auto mt-2 max-w-md text-[14px] text-zinc-500">
            注册账号，获取 API Key，立即开始使用 SIDA 的全部分析能力。
          </p>
          <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
            <Link
              to="/login?mode=register"
              className="inline-flex items-center gap-2 rounded-xl bg-white px-6 py-2.5 text-[14px] font-medium text-black transition-colors hover:bg-zinc-200"
            >
              免费注册 <ArrowRight className="h-4 w-4" />
            </Link>
            <Link
              to="/developers"
              className="inline-flex items-center gap-2 rounded-xl border border-white/[0.1] px-6 py-2.5 text-[14px] font-medium text-white transition-colors hover:bg-white/[0.06]"
            >
              阅读文档
            </Link>
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="relative z-10 border-t border-white/[0.04]">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 px-6 py-8 md:flex-row">
          <div className="flex items-center gap-2.5">
            <BrandMark />
            <span className="text-[13px] text-zinc-600">© 2026 SIDA</span>
          </div>
          <div className="flex items-center gap-6 text-[12px] text-zinc-600">
            <Link to="/developers" className="transition-colors hover:text-zinc-400">文档</Link>
            <Link to="/login" className="transition-colors hover:text-zinc-400">登录</Link>
            <Link to="/login?mode=register" className="transition-colors hover:text-zinc-400">注册</Link>
          </div>
        </div>
        <p className="pb-6 text-center text-[11px] text-zinc-700">
          本平台不构成投资建议 · 市场有风险，投资需谨慎
        </p>
      </footer>
    </div>
  )
}
