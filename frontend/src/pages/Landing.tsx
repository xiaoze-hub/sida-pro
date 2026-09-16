import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Copy, Check } from 'lucide-react'
import { BrandMark } from '@/components/BrandMark'

/* ═══════════════════════════════════════════════════
   深海粒子场 — 气泡上浮 + 光点连线
   ═══════════════════════════════════════════════════ */
function DeepSeaBg() {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const c = ref.current; if (!c) return
    const x = c.getContext('2d'); if (!x) return
    let id: number
    const P: { px: number; py: number; r: number; v: number; o: number; h: number }[] = []
    const N = 140
    const rs = () => { c.width = innerWidth; c.height = innerHeight }
    rs(); addEventListener('resize', rs)
    for (let i = 0; i < N; i++) P.push({ px: Math.random() * c.width, py: Math.random() * c.height, r: Math.random() * 2 + 0.4, v: Math.random() * 0.35 + 0.08, o: Math.random() * 0.45 + 0.1, h: 190 + Math.random() * 30 })
    const d = () => {
      const g = x.createLinearGradient(0, 0, 0, c.height)
      g.addColorStop(0, 'rgba(4,8,22,0.35)'); g.addColorStop(0.5, 'rgba(2,6,18,0.25)'); g.addColorStop(1, 'rgba(0,2,10,0.45)')
      x.fillStyle = g; x.fillRect(0, 0, c.width, c.height)
      for (const p of P) {
        p.py -= p.v; p.px += Math.sin(p.py * 0.004) * 0.2
        if (p.py < -8) { p.py = c.height + 8; p.px = Math.random() * c.width }
        x.beginPath(); x.arc(p.px, p.py, p.r, 0, Math.PI * 2)
        x.fillStyle = `hsla(${p.h},75%,62%,${p.o})`; x.fill()
        if (p.r > 1.4) { x.beginPath(); x.arc(p.px, p.py, p.r * 3.5, 0, Math.PI * 2); x.fillStyle = `hsla(${p.h},75%,62%,${p.o * 0.07})`; x.fill() }
      }
      for (let i = 0; i < P.length; i++) for (let j = i + 1; j < P.length; j++) {
        const dx = P[i].px - P[j].px, dy = P[i].py - P[j].py, dd = Math.sqrt(dx * dx + dy * dy)
        if (dd < 110) { x.beginPath(); x.moveTo(P[i].px, P[i].py); x.lineTo(P[j].px, P[j].py); x.strokeStyle = `rgba(56,189,248,${0.07 * (1 - dd / 110)})`; x.lineWidth = 0.5; x.stroke() }
      }
      id = requestAnimationFrame(d)
    }
    d()
    return () => { cancelAnimationFrame(id); removeEventListener('resize', rs) }
  }, [])
  return <canvas ref={ref} className="fixed inset-0 z-0" style={{ pointerEvents: 'none' }} />
}

/* ── ASCII 原子 ── */
const ATOMS = [
  `    .  *  .
  *   \\|/   *
.  -- ( @ ) --  .
  *   /|\\   .
    .  *  .`,
  `    *  .  *
  .   \\|/   .
*  -- ( @ ) --  *
  .   /|\\   .
    *  .  *`,
]
function AsciiAtom() {
  const [f, setF] = useState(0)
  useEffect(() => { const t = setInterval(() => setF(v => (v + 1) % ATOMS.length), 700); return () => clearInterval(t) }, [])
  return <pre className="select-none font-mono text-[10px] leading-[1.15] text-cyan-400/20 md:text-[11px]">{ATOMS[f]}</pre>
}

/* ── 打字机 ── */
function TW({ t, s = 45 }: { t: string; s?: number }) {
  const [d, setD] = useState(''); const [e, setE] = useState(false)
  useEffect(() => { let i = 0; const x = setInterval(() => { if (i < t.length) { setD(t.slice(0, i + 1)); i++ } else { setE(true); clearInterval(x) } }, s); return () => clearInterval(x) }, [t, s])
  return <span>{d}{!e && <span className="animate-pulse text-cyan-300">|</span>}</span>
}

/* ── 玻璃卡片 ── */
function Glass({ tag, title, desc }: { tag: string; title: string; desc: string }) {
  return (
    <div className="group relative overflow-hidden rounded-2xl border border-cyan-400/10 bg-gradient-to-b from-cyan-500/[0.05] to-blue-600/[0.02] backdrop-blur-xl p-7 transition-all duration-500 hover:border-cyan-400/25">
      <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-cyan-400/25 to-transparent opacity-0 transition-opacity group-hover:opacity-100" />
      <span className="mb-4 inline-block font-mono text-[10px] uppercase tracking-[0.2em] text-cyan-400/70">{tag}</span>
      <h3 className="mb-2.5 text-[17px] font-semibold text-white/90">{title}</h3>
      <p className="text-[13px] leading-relaxed text-slate-400">{desc}</p>
    </div>
  )
}

/* ── 终端块 ── */
function Term({ cmd }: { cmd: string }) {
  const [ok, setOk] = useState(false)
  return (
    <div className="flex items-center justify-between rounded-xl border border-cyan-400/10 bg-[#080c18]/80 backdrop-blur-sm px-5 py-3.5">
      <div className="flex items-center gap-3 overflow-x-auto">
        <span className="font-mono text-[13px] text-cyan-500/50">$</span>
        <code className="font-mono text-[13px] text-cyan-100/80">{cmd}</code>
      </div>
      <button className="rounded-md p-1.5 text-slate-500 hover:bg-white/5 hover:text-cyan-300 transition-colors" onClick={() => { navigator.clipboard.writeText(cmd); setOk(true); setTimeout(() => setOk(false), 2000) }}>
        {ok ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
    </div>
  )
}

/* ── 分隔线 ── */
function Div() {
  return (
    <div className="flex items-center justify-center gap-3 py-1">
      <span className="h-px flex-1 bg-gradient-to-r from-transparent to-cyan-500/15" />
      <span className="font-mono text-[10px] text-cyan-500/25">◆</span>
      <span className="h-px flex-1 bg-gradient-to-l from-transparent to-cyan-500/15" />
    </div>
  )
}

/* ═══ 主页面 ═══ */
export default function LandingPage() {
  const [tab, setTab] = useState<'quick' | 'source'>('quick')
  const base = typeof window !== 'undefined' ? window.location.origin : ''

  return (
    <div className="relative min-h-screen bg-[#030814] text-white">
      <DeepSeaBg />
      <div className="absolute top-0 left-1/2 -translate-x-1/2 h-[500px] w-[1000px] rounded-full bg-blue-600/[0.05] blur-[150px] pointer-events-none z-0" />

      {/* 导航 */}
      <header className="sticky top-0 z-50 border-b border-cyan-500/5 bg-[#030814]/80 backdrop-blur-xl">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <Link to="/" className="flex items-center gap-2">
            <BrandMark />
            <span className="text-[15px] font-semibold tracking-wide">SIDA</span>
            <span className="hidden font-mono text-[9px] text-cyan-500/40 sm:inline">v1.0</span>
          </Link>
          <nav className="flex items-center gap-5">
            <Link to="/developers" className="font-mono text-[12px] text-slate-500 hover:text-cyan-300 transition-colors">docs</Link>
            <Link to="/login" className="font-mono text-[12px] text-slate-500 hover:text-cyan-300 transition-colors">login</Link>
            <Link to="/login?mode=register" className="rounded-lg border border-cyan-400/25 bg-cyan-500/10 px-4 py-1.5 font-mono text-[12px] text-cyan-300 hover:bg-cyan-500/20 transition-all">register</Link>
          </nav>
        </div>
      </header>

      {/* Hero */}
      <section className="relative z-10 mx-auto max-w-3xl px-6 pt-24 pb-16 text-center">
        <div className="mb-4 flex justify-center opacity-50"><AsciiAtom /></div>
        <div className="mb-4 font-mono text-[10px] tracking-[0.3em] text-cyan-400/50">[ AI-POWERED ANALYSIS PLATFORM ]</div>
        <h1 className="text-[32px] font-bold leading-tight tracking-tight md:text-[48px]">
          一切皆<span className="bg-gradient-to-r from-cyan-300 to-blue-500 bg-clip-text text-transparent">接口</span>
        </h1>
        <p className="mx-auto mt-5 max-w-xl text-[14px] leading-relaxed text-slate-400 md:text-[15px]">
          <TW t="SIDA 开发者预览版面向全球开发者开放。行情、资金、情报、决策等全部分析能力均通过标准 REST API 开放，可自由组合与集成。" />
        </p>
        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
          <Link to="/login?mode=register" className="rounded-lg bg-gradient-to-r from-cyan-500 to-blue-600 px-5 py-2 text-[13px] font-semibold text-white shadow-[0_0_18px_rgba(34,211,238,0.2)] hover:shadow-[0_0_28px_rgba(34,211,238,0.35)] transition-all">开始使用</Link>
          <Link to="/developers" className="rounded-lg border border-cyan-400/20 bg-white/[0.03] px-5 py-2 text-[13px] font-medium text-cyan-300 hover:border-cyan-400/40 hover:bg-cyan-500/10 transition-all">开发者文档</Link>
          <Link to="/api-keys" className="rounded-lg border border-cyan-400/20 bg-white/[0.03] px-5 py-2 text-[13px] font-medium text-cyan-300 hover:border-cyan-400/40 hover:bg-cyan-500/10 transition-all">API Key</Link>
        </div>
      </section>

      <div className="relative z-10 mx-auto max-w-3xl px-6"><Div /></div>

      {/* 安装区 */}
      <section className="relative z-10 mx-auto max-w-3xl px-6 py-16">
        <h2 className="mb-6 text-center text-[18px] font-semibold text-white/90">一键使用</h2>
        <div className="mb-4 flex justify-center gap-1">
          {([['quick', '快速体验'], ['source', '源码安装']] as const).map(([k, l]) => (
            <button key={k} className={`rounded-lg px-4 py-1.5 font-mono text-[12px] transition-colors ${tab === k ? 'bg-cyan-500/10 text-cyan-300 border border-cyan-500/20' : 'text-slate-600 hover:text-slate-400'}`} onClick={() => setTab(k)}>{l}</button>
          ))}
        </div>
        <Term cmd={tab === 'quick' ? `curl -sL ${base}/api/skills/install.sh | bash -s -- YOUR_API_KEY` : 'git clone https://github.com/your-org/sida-pro.git'} />
        <div className="mt-4 flex justify-center gap-4 font-mono text-[11px]">
          <Link to="/developers" className="text-slate-600 hover:text-cyan-400 transition-colors">查看文档</Link>
          <Link to="/api-keys" className="text-slate-600 hover:text-cyan-400 transition-colors">API Key</Link>
          <Link to="/developers" className="text-slate-600 hover:text-cyan-400 transition-colors">Skill 目录</Link>
        </div>
      </section>

      <div className="relative z-10 mx-auto max-w-3xl px-6"><Div /></div>

      {/* 公式标题 */}
      <section className="relative z-10 mx-auto max-w-3xl px-6 py-16 text-center">
        <h2 className="text-[28px] font-bold tracking-tight md:text-[36px]">
          数据 = 行情 + <span className="bg-gradient-to-r from-cyan-300 to-blue-500 bg-clip-text text-transparent">分析</span>
        </h2>
        <p className="mx-auto mt-4 max-w-lg text-[13px] leading-relaxed text-slate-500">
          SIDA 让数据在真实场景中产生价值。行情是原料，分析是引擎，API 是出口。
        </p>
      </section>

      {/* 三列特性 */}
      <section className="relative z-10 mx-auto max-w-5xl px-6 pb-16">
        <div className="grid grid-cols-1 gap-5 md:grid-cols-3">
          <Glass tag="DATA ENGINE" title="数据引擎" desc="聚合多家行情与资讯源，自动健康检查与降级切换。K 线、逐笔、资金流、新闻全覆盖。" />
          <Glass tag="AI ANALYSIS" title="AI 分析" desc="内置 Agent 覆盖复盘、研报、机会挖掘。主力意图识别、拆单追踪、情绪周期判定。" />
          <Glass tag="API GATEWAY" title="API 网关" desc="30+ Skill 接口标准化输出。Key 鉴权、按量限流、多档位配额，即插即用。" />
        </div>
      </section>

      <div className="relative z-10 mx-auto max-w-5xl px-6"><Div /></div>

      {/* 设计思路 */}
      <section className="relative z-10 mx-auto max-w-5xl px-6 py-16">
        <h2 className="mb-10 text-center text-[22px] font-bold tracking-tight md:text-[28px] text-white/90">设计思路</h2>
        <div className="grid grid-cols-1 gap-10 md:grid-cols-2 md:gap-16">
          <div>
            <h3 className="mb-3 text-[16px] font-semibold text-cyan-300">一切皆接口</h3>
            <p className="text-[13px] leading-relaxed text-slate-500">
              SIDA 的全部分析能力均通过标准 REST API 开放。行情查询、资金流向、AI 分析、策略信号——所有功能都有对应的 Skill 接口，可通过 API Key 直接调用，也可在网页端实时体验。
            </p>
          </div>
          <div>
            <h3 className="mb-3 text-[16px] font-semibold text-cyan-300">口径透明可追溯</h3>
            <p className="text-[13px] leading-relaxed text-slate-500">
              每个接口都标注数据口径（tick/eastmoney4/ths），资金类指标必须声明方向语义。数据缺失显式标注「无数据」，绝不编造数字。多源数据自动降级，口径标签始终可见。
            </p>
          </div>
        </div>
      </section>

      <div className="relative z-10 mx-auto max-w-3xl px-6"><Div /></div>

      {/* 代码示例 */}
      <section className="relative z-10 mx-auto max-w-3xl px-6 py-16">
        <h2 className="mb-6 text-center text-[18px] font-semibold text-white/90">调用示例</h2>
        <div className="overflow-hidden rounded-2xl border border-cyan-400/10 bg-[#080c18]/80 backdrop-blur-sm">
          <div className="flex items-center gap-2 border-b border-cyan-400/10 px-4 py-2.5">
            <div className="flex gap-1.5">
              <div className="h-2.5 w-2.5 rounded-full bg-red-500/50" />
              <div className="h-2.5 w-2.5 rounded-full bg-yellow-500/50" />
              <div className="h-2.5 w-2.5 rounded-full bg-green-500/50" />
            </div>
            <span className="ml-2 font-mono text-[10px] text-cyan-500/40">bash</span>
          </div>
          <pre className="overflow-x-auto p-5 font-mono text-[12px] leading-[1.9]">
            <code>
              <span className="text-cyan-500/50">$ </span>
              <span className="text-cyan-400">curl</span>
              <span className="text-slate-300"> -X POST </span>
              <span className="text-emerald-400">"{base}/api/skills/get_stock_quote/run"</span>
              <span className="text-slate-300"> \</span>{'\n'}
              <span className="text-slate-300">  -H </span>
              <span className="text-emerald-400">"X-API-Key: sk_YOUR_KEY"</span>
              <span className="text-slate-300"> \</span>{'\n'}
              <span className="text-slate-300">  -d </span>
              <span className="text-emerald-400">'&#123;"symbol": "000001"&#125;'</span>{'\n\n'}
              <span className="text-cyan-500/40"># → &#123;"code": 0, "data": &#123;"price": 11.82, ...&#125;&#125;</span>
            </code>
          </pre>
        </div>
      </section>

      <div className="relative z-10 mx-auto max-w-3xl px-6"><Div /></div>

      {/* CTA */}
      <section className="relative z-10 mx-auto max-w-3xl px-6 py-16 text-center">
        <h2 className="text-[22px] font-bold tracking-tight md:text-[26px] text-white/90">开始构建</h2>
        <p className="mx-auto mt-3 max-w-md text-[13px] text-slate-500">
          注册账号，获取 API Key，立即接入 SIDA 的全部分析能力。
        </p>
        <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
          <Link to="/login?mode=register" className="rounded-lg bg-gradient-to-r from-cyan-500 to-blue-600 px-5 py-2 text-[13px] font-semibold text-white shadow-[0_0_18px_rgba(34,211,238,0.2)] hover:shadow-[0_0_28px_rgba(34,211,238,0.35)] transition-all">免费注册</Link>
          <Link to="/developers" className="rounded-lg border border-cyan-400/20 bg-white/[0.03] px-5 py-2 text-[13px] font-medium text-cyan-300 hover:border-cyan-400/40 hover:bg-cyan-500/10 transition-all">阅读文档</Link>
        </div>
        <div className="mt-6 flex justify-center gap-4 font-mono text-[11px]">
          <Link to="/developers" className="text-slate-700 hover:text-cyan-400 transition-colors">开发者文档</Link>
          <Link to="/api-keys" className="text-slate-700 hover:text-cyan-400 transition-colors">API Key</Link>
          <Link to="/login" className="text-slate-700 hover:text-cyan-400 transition-colors">登录</Link>
        </div>
      </section>

      {/* Footer */}
      <footer className="relative z-10 border-t border-cyan-500/5 px-6 py-8">
        <div className="mx-auto flex max-w-5xl flex-col items-center justify-between gap-3 md:flex-row">
          <div className="flex items-center gap-2">
            <BrandMark />
            <span className="font-mono text-[11px] text-slate-700">开源 · © 2026 SIDA</span>
          </div>
          <div className="flex items-center gap-4 font-mono text-[11px] text-slate-700">
            <Link to="/developers" className="hover:text-cyan-400 transition-colors">docs</Link>
            <Link to="/login" className="hover:text-cyan-400 transition-colors">login</Link>
            <Link to="/login?mode=register" className="hover:text-cyan-400 transition-colors">register</Link>
          </div>
        </div>
        <p className="mt-4 text-center font-mono text-[10px] text-slate-800">
          本平台不构成投资建议 · market involves risk · invest with caution
        </p>
      </footer>
    </div>
  )
}
