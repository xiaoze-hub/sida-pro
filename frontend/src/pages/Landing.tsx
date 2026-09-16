import { useEffect, useRef, useState } from 'react'
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
  Zap,
  Shield,
  Globe,
} from 'lucide-react'
import { BrandMark } from '@/components/BrandMark'

/* ─── 粒子星场 ─── */
function ParticleField() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let animId: number
    let particles: { x: number; y: number; vx: number; vy: number; r: number; a: number }[] = []
    const COUNT = 180
    const LINK_DIST = 120

    const resize = () => {
      canvas.width = window.innerWidth
      canvas.height = window.innerHeight
    }
    resize()
    window.addEventListener('resize', resize)

    for (let i = 0; i < COUNT; i++) {
      particles.push({
        x: Math.random() * canvas.width,
        y: Math.random() * canvas.height,
        vx: (Math.random() - 0.5) * 0.4,
        vy: (Math.random() - 0.5) * 0.4,
        r: Math.random() * 1.5 + 0.5,
        a: Math.random() * 0.5 + 0.2,
      })
    }

    const draw = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height)

      // 粒子
      for (const p of particles) {
        p.x += p.vx
        p.y += p.vy
        if (p.x < 0) p.x = canvas.width
        if (p.x > canvas.width) p.x = 0
        if (p.y < 0) p.y = canvas.height
        if (p.y > canvas.height) p.y = 0

        ctx.beginPath()
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2)
        ctx.fillStyle = `rgba(34, 211, 238, ${p.a})`
        ctx.fill()
      }

      // 连线
      for (let i = 0; i < particles.length; i++) {
        for (let j = i + 1; j < particles.length; j++) {
          const dx = particles[i].x - particles[j].x
          const dy = particles[i].y - particles[j].y
          const dist = Math.sqrt(dx * dx + dy * dy)
          if (dist < LINK_DIST) {
            ctx.beginPath()
            ctx.moveTo(particles[i].x, particles[i].y)
            ctx.lineTo(particles[j].x, particles[j].y)
            ctx.strokeStyle = `rgba(34, 211, 238, ${0.12 * (1 - dist / LINK_DIST)})`
            ctx.lineWidth = 0.5
            ctx.stroke()
          }
        }
      }

      animId = requestAnimationFrame(draw)
    }
    draw()

    return () => {
      cancelAnimationFrame(animId)
      window.removeEventListener('resize', resize)
    }
  }, [])

  return <canvas ref={canvasRef} className="absolute inset-0 z-0" style={{ pointerEvents: 'none' }} />
}

/* ─── 打字机效果 ─── */
function Typewriter({ text, speed = 60 }: { text: string; speed?: number }) {
  const [displayed, setDisplayed] = useState('')
  const [done, setDone] = useState(false)

  useEffect(() => {
    let i = 0
    const timer = setInterval(() => {
      if (i < text.length) {
        setDisplayed(text.slice(0, i + 1))
        i++
      } else {
        setDone(true)
        clearInterval(timer)
      }
    }, speed)
    return () => clearInterval(timer)
  }, [text, speed])

  return (
    <span>
      {displayed}
      {!done && <span className="animate-pulse text-cyan-400">|</span>}
    </span>
  )
}

/* ─── 浮动数字背景 ─── */
function FloatingNumbers() {
  const nums = ['000001', '600519', '300750', '+3.2%', '-1.8%', '¥11.82', 'Zjl_HB', 'L2', 'MACD', 'RSI']
  return (
    <div className="absolute inset-0 z-0 overflow-hidden pointer-events-none">
      {nums.map((n, i) => (
        <span
          key={i}
          className="absolute text-[10px] font-mono text-cyan-500/10 select-none"
          style={{
            left: `${10 + (i * 9) % 80}%`,
            top: `${15 + (i * 13) % 70}%`,
            animation: `float ${8 + i * 2}s ease-in-out infinite alternate`,
            animationDelay: `${i * 0.7}s`,
          }}
        >
          {n}
        </span>
      ))}
      <style>{`@keyframes float { from { transform: translateY(0) } to { transform: translateY(-20px) } }`}</style>
    </div>
  )
}

/* ─── 玻璃卡片 ─── */
function GlassCard({ icon: Icon, title, desc, delay = 0 }: { icon: any; title: string; desc: string; delay?: number }) {
  return (
    <div
      className="group relative rounded-xl border border-cyan-500/10 bg-white/[0.02] backdrop-blur-sm p-5 transition-all duration-300 hover:border-cyan-500/30 hover:bg-white/[0.05]"
      style={{ animationDelay: `${delay}ms` }}
    >
      <div className="absolute inset-0 rounded-xl bg-gradient-to-b from-cyan-500/5 to-transparent opacity-0 transition-opacity group-hover:opacity-100" />
      <div className="relative">
        <div className="mb-3 inline-flex h-10 w-10 items-center justify-center rounded-lg bg-cyan-500/10 text-cyan-400">
          <Icon className="h-5 w-5" />
        </div>
        <h3 className="mb-1.5 text-[14px] font-semibold text-white">{title}</h3>
        <p className="text-[12px] leading-relaxed text-gray-400">{desc}</p>
      </div>
    </div>
  )
}

/* ─── 霓虹按钮 ─── */
function NeonButton({ to, children, variant = 'primary' }: { to: string; children: React.ReactNode; variant?: 'primary' | 'ghost' }) {
  if (variant === 'primary') {
    return (
      <Link
        to={to}
        className="inline-flex items-center gap-2 rounded-lg bg-cyan-500 px-6 py-2.5 text-[13px] font-semibold text-black transition-all hover:bg-cyan-400 hover:shadow-[0_0_20px_rgba(34,211,238,0.4)]"
      >
        {children}
      </Link>
    )
  }
  return (
    <Link
      to={to}
      className="inline-flex items-center gap-2 rounded-lg border border-cyan-500/30 px-6 py-2.5 text-[13px] font-medium text-cyan-400 transition-all hover:border-cyan-400 hover:bg-cyan-500/10"
    >
      {children}
    </Link>
  )
}

/* ─── 主组件 ─── */
const FEATURES = [
  { icon: LineChart, title: '行情分析', desc: 'A 股全市场行情、K 线工作台、板块热力图与主力资金多口径分析。' },
  { icon: Sparkles, title: 'AI 助手', desc: '内置 AI Agent 覆盖复盘、研报、机会挖掘，自然语言直达结论。' },
  { icon: Code2, title: 'Skill API', desc: '标准 REST 接口开放全部分析能力，API Key 鉴权，按量限流。' },
  { icon: Database, title: '多数据源', desc: '聚合多家行情与资讯源，自动健康检查与降级切换，口径透明。' },
]

const STEPS = [
  { icon: UserPlus, step: '01', title: '注册账号', desc: '邮箱注册，即刻开通。' },
  { icon: KeyRound, step: '02', title: '获取 Key', desc: '自动签发 API Key。' },
  { icon: Terminal, step: '03', title: '开始调用', desc: '一行命令接入分析能力。' },
]

const STATS = [
  { value: '30+', label: 'Skill 接口' },
  { value: '5000+', label: 'A 股覆盖' },
  { value: '<2s', label: 'P95 延迟' },
  { value: '99.9%', label: '可用性' },
]

export default function LandingPage() {
  return (
    <div className="relative min-h-screen bg-[#0a0a0f] text-white overflow-hidden">
      {/* 背景层 */}
      <ParticleField />
      <FloatingNumbers />
      {/* 顶部光晕 */}
      <div className="absolute top-0 left-1/2 -translate-x-1/2 h-[400px] w-[800px] rounded-full bg-cyan-500/5 blur-[120px] pointer-events-none" />

      {/* 导航 */}
      <header className="relative z-10 flex items-center justify-between px-6 py-4 md:px-12">
        <Link to="/" className="flex items-center gap-2.5">
          <BrandMark />
          <span className="text-[15px] font-bold tracking-wide">SIDA</span>
        </Link>
        <nav className="flex items-center gap-3">
          <Link to="/developers" className="rounded-md px-3 py-1.5 text-[12px] text-gray-400 transition-colors hover:text-white">
            开发者文档
          </Link>
          <Link to="/login" className="rounded-md px-3 py-1.5 text-[12px] text-gray-400 transition-colors hover:text-white">
            登录
          </Link>
          <Link
            to="/login?mode=register"
            className="rounded-md bg-cyan-500/10 border border-cyan-500/30 px-3.5 py-1.5 text-[12px] font-medium text-cyan-400 transition-all hover:bg-cyan-500/20"
          >
            注册
          </Link>
        </nav>
      </header>

      {/* Hero */}
      <section className="relative z-10 flex flex-col items-center px-6 pt-20 pb-16 text-center md:pt-28">
        <div className="mb-4 inline-flex items-center gap-1.5 rounded-full border border-cyan-500/20 bg-cyan-500/5 px-3 py-1 text-[11px] text-cyan-400">
          <Zap className="h-3 w-3" />
          AI 驱动 · 实时分析
        </div>

        <h1 className="mb-4 text-[36px] font-bold leading-tight tracking-tight md:text-[52px]">
          <span className="bg-gradient-to-r from-cyan-300 via-cyan-400 to-blue-500 bg-clip-text text-transparent">
            数智分析 SIDA
          </span>
        </h1>

        <p className="mb-8 max-w-xl text-[14px] leading-relaxed text-gray-400 md:text-[15px]">
          <Typewriter text="AI 驱动的 A 股智能分析平台 — 行情 · 资金 · 情报 · 决策" speed={50} />
        </p>

        <div className="mb-12 flex flex-wrap items-center justify-center gap-3">
          <NeonButton to="/login?mode=register">
            立即注册 <ArrowRight className="h-3.5 w-3.5" />
          </NeonButton>
          <NeonButton to="/developers" variant="ghost">
            查看文档
          </NeonButton>
        </div>

        {/* 数据统计 */}
        <div className="flex flex-wrap items-center justify-center gap-8 md:gap-12">
          {STATS.map((s, i) => (
            <div key={i} className="text-center">
              <div className="text-[22px] font-bold text-cyan-400 md:text-[28px]">{s.value}</div>
              <div className="text-[11px] text-gray-500">{s.label}</div>
            </div>
          ))}
        </div>
      </section>

      {/* 核心功能 */}
      <section className="relative z-10 px-6 pb-16 md:px-12">
        <div className="mx-auto max-w-4xl">
          <h2 className="mb-8 text-center text-[18px] font-semibold text-white md:text-[20px]">
            核心能力
          </h2>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {FEATURES.map((f, i) => (
              <GlassCard key={i} icon={f.icon} title={f.title} desc={f.desc} delay={i * 100} />
            ))}
          </div>
        </div>
      </section>

      {/* 快速开始 */}
      <section className="relative z-10 px-6 pb-20 md:px-12">
        <div className="mx-auto max-w-3xl">
          <h2 className="mb-8 text-center text-[18px] font-semibold text-white md:text-[20px]">
            三步开始
          </h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            {STEPS.map((s, i) => (
              <div
                key={i}
                className="relative rounded-xl border border-cyan-500/10 bg-white/[0.02] p-5 text-center backdrop-blur-sm"
              >
                <div className="mx-auto mb-3 inline-flex h-10 w-10 items-center justify-center rounded-full bg-cyan-500/10 text-cyan-400">
                  <s.icon className="h-5 w-5" />
                </div>
                <div className="mb-1 text-[11px] font-mono text-cyan-500/60">{s.step}</div>
                <h3 className="mb-1 text-[13px] font-semibold text-white">{s.title}</h3>
                <p className="text-[11px] text-gray-400">{s.desc}</p>
                {i < 2 && (
                  <ArrowRight className="absolute right-[-10px] top-1/2 hidden h-4 w-4 -translate-y-1/2 text-cyan-500/30 md:block" />
                )}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* 底部信任标识 */}
      <section className="relative z-10 border-t border-white/5 px-6 py-8 md:px-12">
        <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-center gap-6 text-[11px] text-gray-500">
          <span className="inline-flex items-center gap-1.5"><Shield className="h-3.5 w-3.5 text-cyan-500/50" /> 数据加密</span>
          <span className="inline-flex items-center gap-1.5"><Globe className="h-3.5 w-3.5 text-cyan-500/50" /> 多源聚合</span>
          <span className="inline-flex items-center gap-1.5"><Zap className="h-3.5 w-3.5 text-cyan-500/50" /> 实时分析</span>
        </div>
      </section>

      {/* Footer */}
      <footer className="relative z-10 border-t border-white/5 px-6 py-6 md:px-12">
        <div className="mx-auto flex max-w-4xl flex-col items-center justify-between gap-3 md:flex-row">
          <p className="text-[11px] text-gray-600">© 2026 SIDA · 数智分析平台</p>
          <div className="flex items-center gap-4">
            <Link to="/developers" className="text-[11px] text-gray-500 hover:text-gray-300">开发者文档</Link>
            <Link to="/login" className="text-[11px] text-gray-500 hover:text-gray-300">登录</Link>
            <Link to="/login?mode=register" className="text-[11px] text-gray-500 hover:text-gray-300">注册</Link>
          </div>
        </div>
        <p className="mt-3 text-center text-[10px] text-gray-700">
          本平台不构成投资建议，市场有风险，投资需谨慎。
        </p>
      </footer>
    </div>
  )
}
