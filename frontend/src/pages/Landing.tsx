import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Copy, Check } from 'lucide-react'
import { BrandMark } from '@/components/BrandMark'

/* ═══════════════════════════════════════════
   DeepSeek Harness 风格 — 严格对齐排版
   ═══════════════════════════════════════════ */

/* ── 复制按钮 ── */
function CopyBtn({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className="rounded-md p-1.5 text-zinc-500 transition-colors hover:bg-white/5 hover:text-white"
      onClick={() => { navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 2000) }}
    >
      {copied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  )
}

/* ── 终端块 ── */
function TerminalBlock({ cmd }: { cmd: string }) {
  return (
    <div className="flex items-center justify-between rounded-xl border border-white/[0.06] bg-[#0c0e14] px-5 py-3.5">
      <div className="flex items-center gap-3 overflow-x-auto">
        <span className="font-mono text-[13px] text-zinc-600">$</span>
        <code className="font-mono text-[13px] text-zinc-200">{cmd}</code>
      </div>
      <CopyBtn text={cmd} />
    </div>
  )
}

/* ── 特性卡片（三列） ── */
function FeatCard({ tag, title, desc }: { tag: string; title: string; desc: string }) {
  return (
    <div className="rounded-2xl border border-white/[0.06] bg-[#0d0f15] p-7">
      <span className="mb-4 inline-block text-[11px] font-medium uppercase tracking-widest text-blue-400">{tag}</span>
      <h3 className="mb-2.5 text-[17px] font-semibold text-white">{title}</h3>
      <p className="text-[13px] leading-relaxed text-zinc-500">{desc}</p>
    </div>
  )
}

/* ── 主页面 ── */
export default function LandingPage() {
  const [installTab, setInstallTab] = useState<'quick' | 'source'>('quick')
  const base = typeof window !== 'undefined' ? window.location.origin : ''

  return (
    <div className="min-h-screen bg-[#08090d] text-white">

      {/* ─── 导航 ─── */}
      <header className="sticky top-0 z-50 border-b border-white/[0.04] bg-[#08090d]/80 backdrop-blur-xl">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <Link to="/" className="flex items-center gap-2">
            <BrandMark />
            <span className="text-[15px] font-semibold">SIDA</span>
          </Link>
          <nav className="flex items-center gap-6">
            <Link to="/developers" className="text-[13px] text-zinc-400 hover:text-white transition-colors">开发者文档</Link>
            <Link to="/login" className="text-[13px] text-zinc-400 hover:text-white transition-colors">登录</Link>
            <Link
              to="/login?mode=register"
              className="rounded-lg bg-white px-4 py-1.5 text-[13px] font-medium text-black hover:bg-zinc-200 transition-colors"
            >
              注册
            </Link>
          </nav>
        </div>
      </header>

      {/* ─── Hero（居中） ─── */}
      <section className="mx-auto max-w-3xl px-6 pt-24 pb-16 text-center">
        <h1 className="text-[32px] font-bold leading-tight tracking-tight md:text-[48px]">
          一切皆<span className="text-blue-400">接口</span>
        </h1>
        <p className="mx-auto mt-5 max-w-xl text-[15px] leading-relaxed text-zinc-400">
          SIDA 开发者预览版面向全球开发者开放测试。行情、资金、情报、决策等全部分析能力均通过标准 REST API 开放，可自由组合与集成。
        </p>
        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
          <Link to="/login?mode=register" className="rounded-lg bg-white px-5 py-2 text-[13px] font-medium text-black hover:bg-zinc-200 transition-colors">
            开始使用
          </Link>
          <Link to="/developers" className="rounded-lg border border-white/[0.1] bg-white/[0.03] px-5 py-2 text-[13px] font-medium text-white hover:bg-white/[0.06] transition-colors">
            开发者文档
          </Link>
          <Link to="/api-keys" className="rounded-lg border border-white/[0.1] bg-white/[0.03] px-5 py-2 text-[13px] font-medium text-white hover:bg-white/[0.06] transition-colors">
            API Key
          </Link>
        </div>
      </section>

      {/* ─── 安装区 ─── */}
      <section className="mx-auto max-w-3xl px-6 pb-20">
        <h2 className="mb-6 text-center text-[18px] font-semibold">一键使用</h2>
        {/* Tab */}
        <div className="mb-4 flex justify-center gap-1">
          {([['quick', '快速体验'], ['source', '源码安装']] as const).map(([k, label]) => (
            <button
              key={k}
              className={`rounded-lg px-4 py-1.5 text-[13px] transition-colors ${
                installTab === k ? 'bg-white/[0.08] text-white' : 'text-zinc-500 hover:text-zinc-300'
              }`}
              onClick={() => setInstallTab(k)}
            >
              {label}
            </button>
          ))}
        </div>
        {installTab === 'quick' ? (
          <TerminalBlock cmd={`curl -sL ${base}/api/skills/install.sh | bash -s -- YOUR_API_KEY`} />
        ) : (
          <TerminalBlock cmd="git clone https://github.com/your-org/sida-pro.git" />
        )}
        <div className="mt-4 flex justify-center gap-3">
          <Link to="/login?mode=register" className="text-[13px] text-zinc-500 hover:text-white transition-colors">查看文档</Link>
          <Link to="/api-keys" className="text-[13px] text-zinc-500 hover:text-white transition-colors">API Key</Link>
          <Link to="/developers" className="text-[13px] text-zinc-500 hover:text-white transition-colors">Skill 目录</Link>
        </div>
      </section>

      {/* ─── 公式标题 ─── */}
      <section className="mx-auto max-w-3xl px-6 pb-16 text-center">
        <h2 className="text-[28px] font-bold tracking-tight md:text-[36px]">
          数据 = 行情 + <span className="text-blue-400">分析</span>
        </h2>
        <p className="mx-auto mt-4 max-w-lg text-[14px] leading-relaxed text-zinc-500">
          SIDA 让数据在真实场景中产生价值。行情是原料，分析是引擎，API 是出口。
        </p>
      </section>

      {/* ─── 三列特性 ─── */}
      <section className="mx-auto max-w-5xl px-6 pb-20">
        <div className="grid grid-cols-1 gap-5 md:grid-cols-3">
          <FeatCard
            tag="DATA ENGINE"
            title="数据引擎"
            desc="聚合多家行情与资讯源，自动健康检查与降级切换。K 线、逐笔、资金流、新闻全覆盖。"
          />
          <FeatCard
            tag="AI ANALYSIS"
            title="AI 分析"
            desc="内置 Agent 覆盖复盘、研报、机会挖掘。主力意图识别、拆单追踪、情绪周期判定。"
          />
          <FeatCard
            tag="API GATEWAY"
            title="API 网关"
            desc="30+ Skill 接口标准化输出。Key 鉴权、按量限流、多档位配额，即插即用。"
          />
        </div>
      </section>

      {/* ─── 设计思路（左右两栏） ─── */}
      <section className="mx-auto max-w-5xl px-6 pb-20">
        <h2 className="mb-10 text-center text-[22px] font-bold tracking-tight md:text-[28px]">
          设计思路
        </h2>
        <div className="grid grid-cols-1 gap-10 md:grid-cols-2 md:gap-16">
          <div>
            <h3 className="mb-3 text-[16px] font-semibold text-white">一切皆接口</h3>
            <p className="text-[13px] leading-relaxed text-zinc-500">
              SIDA 的全部分析能力均通过标准 REST API 开发。行情查询、资金流向、AI 分析、策略信号——所有功能都有对应的 Skill 接口，可通过 API Key 直接调用，也可在网页端实时体验。
            </p>
          </div>
          <div>
            <h3 className="mb-3 text-[16px] font-semibold text-white">口径透明可追溯</h3>
            <p className="text-[13px] leading-relaxed text-zinc-500">
              每个接口都标注数据口径（tick/eastmoney4/ths），资金类指标必须声明方向语义。数据缺失显式标注「无数据」，绝不编造数字。多源数据自动降级，口径标签始终可见。
            </p>
          </div>
        </div>
      </section>

      {/* ─── 代码示例 ─── */}
      <section className="mx-auto max-w-3xl px-6 pb-20">
        <h2 className="mb-6 text-center text-[18px] font-semibold">调用示例</h2>
        <div className="overflow-hidden rounded-2xl border border-white/[0.06] bg-[#0c0e14]">
          <div className="flex items-center gap-2 border-b border-white/[0.06] px-4 py-2.5">
            <div className="flex gap-1.5">
              <div className="h-2.5 w-2.5 rounded-full bg-red-500/50" />
              <div className="h-2.5 w-2.5 rounded-full bg-yellow-500/50" />
              <div className="h-2.5 w-2.5 rounded-full bg-green-500/50" />
            </div>
            <span className="ml-2 font-mono text-[11px] text-zinc-600">bash</span>
          </div>
          <pre className="overflow-x-auto p-5 font-mono text-[12px] leading-[1.9] text-zinc-300">
            <code>
              <span className="text-zinc-600">$ </span>
              <span className="text-blue-400">curl</span> -X POST <span className="text-emerald-400">"{base}/api/skills/get_stock_quote/run"</span> \{'\n'}
              {'  '}-H <span className="text-emerald-400">"X-API-Key: sk_YOUR_KEY"</span> \{'\n'}
              {'  '}-d <span className="text-emerald-400">'&#123;"symbol": "000001"&#125;'</span>{'\n\n'}
              <span className="text-zinc-600"># → &#123;"code": 0, "data": &#123;"price": 11.82, ...&#125;&#125;</span>
            </code>
          </pre>
        </div>
      </section>

      {/* ─── 底部 CTA ─── */}
      <section className="mx-auto max-w-3xl px-6 pb-20 text-center">
        <h2 className="text-[22px] font-bold tracking-tight md:text-[26px]">开始构建</h2>
        <p className="mx-auto mt-3 max-w-md text-[14px] text-zinc-500">
          注册账号，获取 API Key，立即接入 SIDA 的全部分析能力。
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          <Link to="/login?mode=register" className="rounded-lg bg-white px-5 py-2 text-[13px] font-medium text-black hover:bg-zinc-200 transition-colors">
            免费注册
          </Link>
          <Link to="/developers" className="rounded-lg border border-white/[0.1] bg-white/[0.03] px-5 py-2 text-[13px] font-medium text-white hover:bg-white/[0.06] transition-colors">
            阅读文档
          </Link>
        </div>
        <div className="mt-6 flex justify-center gap-4">
          <Link to="/developers" className="text-[12px] text-zinc-600 hover:text-zinc-400 transition-colors">开发者文档</Link>
          <Link to="/api-keys" className="text-[12px] text-zinc-600 hover:text-zinc-400 transition-colors">API Key</Link>
          <Link to="/login" className="text-[12px] text-zinc-600 hover:text-zinc-400 transition-colors">登录</Link>
        </div>
      </section>

      {/* ─── Footer ─── */}
      <footer className="border-t border-white/[0.04] px-6 py-8">
        <div className="mx-auto flex max-w-5xl flex-col items-center justify-between gap-3 md:flex-row">
          <div className="flex items-center gap-2">
            <BrandMark />
            <span className="text-[12px] text-zinc-700">开源 · © 2026 SIDA 版权所有</span>
          </div>
          <div className="flex items-center gap-4 text-[12px] text-zinc-700">
            <Link to="/developers" className="hover:text-zinc-500 transition-colors">文档</Link>
            <Link to="/login" className="hover:text-zinc-500 transition-colors">登录</Link>
            <Link to="/login?mode=register" className="hover:text-zinc-500 transition-colors">注册</Link>
          </div>
        </div>
        <p className="mt-4 text-center text-[11px] text-zinc-800">
          本平台不构成投资建议 · 市场有风险，投资需谨慎
        </p>
      </footer>
    </div>
  )
}
