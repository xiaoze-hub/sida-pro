import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Code2,
  KeyRound,
  ShieldCheck,
  Gauge,
  BookOpen,
  Play,
  AlertTriangle,
  Copy,
  Check,
  Terminal,
  Lock,
} from 'lucide-react'
import { fetchAPI, getToken, isAuthenticated } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@panwatch/base-ui/components/ui/select'

/**
 * 开发者文档页(2026-09-16, 任务 2.1-2.3)。
 * - 公开可访问(无需登录), Skill 目录从 GET /api/skills/catalog 拉取, 不硬编码
 * - 在线调试台: API Key 仅存组件 state(不写 localStorage)
 * - 错误码表: 401/403/429/500 + 退避建议
 */

// ── 目录类型(与后端 /api/skills/catalog 对齐) ───────────────────────

interface SkillParamSchema {
  type?: string
  description?: string
  default?: unknown
}

interface CatalogSkill {
  name: string
  description: string
  tier_min: 'free' | 'trial' | 'pro'
  slow: boolean
  caliber: string
  params: Record<string, SkillParamSchema>
  required: string[]
}

interface TierLimit {
  daily_limit: number
  burst?: number
  refill_per_min?: number
  days?: number
}

interface CatalogResp {
  skills: CatalogSkill[]
  tiers: {
    guest?: TierLimit
    free?: TierLimit
    trial?: TierLimit
    pro?: TierLimit
  }
  endpoint?: string
}

type RunOutcome = {
  status: number
  ok: boolean
  message: string
  data: unknown
  retryAfter: string | null
  durationMs: number
}

// ── 静态文档内容(与后端行为一致; 限流数字优先用 catalog 回传) ────────

const QUICK_START_STEPS = [
  {
    title: '1. 注册 / 领取 API Key',
    desc: '调用 POST /api/keys 领取 sk_ 前缀密钥。明文只返回一次, 服务端只存 hash, 请立即妥善保存。',
    code: `curl -X POST https://<your-host>/api/keys \\
  -H 'Content-Type: application/json' \\
  -d '{"trial": true, "owner_label": "my-app"}'`,
  },
  {
    title: '2. 保存 Key 并查配额',
    desc: '把 Key 放进环境变量或密钥管理, 不要提交到仓库。用 GET /api/usage 查当日用量。',
    code: `export SIDA_API_KEY='sk_...'   # 仅本机环境变量

curl https://<your-host>/api/usage \\
  -H "X-API-Key: $SIDA_API_KEY"`,
  },
  {
    title: '3. 第一次调用',
    desc: 'POST /api/skills/{name}/run, body 为 {"args": {...}}。响应含 result / caliber / risk。',
    code: `curl -X POST https://<your-host>/api/skills/get_stock_quote/run \\
  -H "X-API-Key: $SIDA_API_KEY" \\
  -H 'Content-Type: application/json' \\
  -d '{"args": {"symbol": "600519", "market": "CN"}}'`,
  },
]

const AUTH_CHANNELS = [
  {
    tag: 'X-API-Key',
    title: 'API Key 通道 (channel=api)',
    desc: '外部集成首选。请求头带 X-API-Key: sk_...。按 Key 计量与限流, 不依赖登录态。',
    example: `-H "X-API-Key: $SIDA_API_KEY"`,
  },
  {
    tag: 'JWT',
    title: 'JWT 通道 (channel=web)',
    desc: '已登录用户可用 Authorization: Bearer <token> 调用, 与该用户名下最优 Key 共享配额。未带 Key 时自动走此通道。',
    example: `-H "Authorization: Bearer <jwt>"`,
  },
  {
    tag: 'guest',
    title: '游客通道 (channel=guest)',
    desc: '无 Key 且无 JWT 时按来源 IP 限流, 仅可调用 tier_min=free 的 skill, 适合文档页试玩。',
    example: `# 无需鉴权头(受限, 勿用于生产)`,
  },
]

const ERROR_ROWS = [
  {
    code: 401,
    title: '缺少或无效的 API Key',
    desc: 'X-API-Key 缺失、格式不是 sk_ 前缀, 或 hash 未命中任何已签发 Key。',
    example: `{"code":401,"success":false,"data":null,"message":"无效的 API Key"}`,
    advice: '核对 Key 是否完整复制; 从 POST /api/keys 重新领取。不要自动重试, 先修凭证。',
  },
  {
    code: 403,
    title: '权限不足 / 档位不够 / Key 异常',
    desc: '当前档位低于 skill 的 tier_min, 或 Key 已 disabled/frozen, 或游客调用非 free skill。',
    example: `{"code":403,"success":false,"data":null,"message":"skill get_forecast 需要 pro 档位(当前 free)"}`,
    advice: '升级档位或改用 free skill; 若 Key 被冻结/禁用, 联系管理员。不要盲目重试。',
  },
  {
    code: 429,
    title: '限流',
    desc: '日配额用尽、触发令牌桶 burst/匀速限制, 或游客 IP 超出每日次数。响应头带 Retry-After(秒)。',
    example: `# 响应头: Retry-After: 37
{"code":429,"success":false,"data":null,"message":"触发频率限制(burst 30, 匀速 15/分), 37s 后重试"}`,
    advice:
      '必须遵守 Retry-After; 自实现退避: 首次等 max(Retry-After, 1s), 之后指数退避(1s→2s→4s→8s…)并加随机抖动, 最多 3-5 次。日配额类 429 建议等到次日 UTC 零点或升级档位。',
  },
  {
    code: 500,
    title: '服务器内部错误',
    desc: 'skill handler 执行异常(上游数据源超时、解析失败等)。非参数问题。',
    example: `{"code":500,"success":false,"data":null,"message":"skill 执行失败: ..."}`,
    advice:
      '可退避重试 1-2 次(建议 2s、8s); 持续 500 记录 skill 名与时间戳并反馈, 勿在 tight loop 重试。',
  },
]

// ── 工具 ────────────────────────────────────────────────────────────

function SectionHead({
  icon: Icon,
  title,
  hint,
}: {
  icon: typeof Code2
  title: string
  hint?: string
}) {
  return (
    <div className="flex items-center gap-2.5 mb-3">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent/40 text-primary ring-1 ring-border/40">
        <Icon className="h-4 w-4" />
      </div>
      <h3 className="text-[13px] font-semibold text-foreground">{title}</h3>
      {hint && <span className="text-[10px] text-muted-foreground ml-auto hidden sm:inline">{hint}</span>}
    </div>
  )
}

function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard 不可用时静默 */
    }
  }
  return (
    <div className="relative group">
      <pre className="overflow-x-auto rounded-lg border border-border/50 bg-accent/20 px-3 py-2.5 font-mono text-[11px] leading-relaxed text-foreground/85 whitespace-pre">
        {code}
      </pre>
      <button
        type="button"
        onClick={copy}
        className="absolute top-1.5 right-1.5 rounded-md p-1.5 text-muted-foreground opacity-0 group-hover:opacity-100 hover:text-foreground hover:bg-accent transition-opacity"
        title="复制"
      >
        {copied ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
    </div>
  )
}

function tierBadgeVariant(tier: string) {
  if (tier === 'pro') return 'default' as const
  if (tier === 'trial') return 'success' as const
  return 'secondary' as const
}

/** 按 skill 参数 schema 生成调试台默认 args(只填有 default 或常见必填占位)。 */
function buildDefaultArgs(skill: CatalogSkill | null): string {
  if (!skill) return '{}'
  const args: Record<string, unknown> = {}
  const props = skill.params || {}
  for (const key of Object.keys(props)) {
    const p = props[key]
    const isRequired = (skill.required || []).includes(key)
    if (p && typeof p === 'object' && 'default' in p && p.default !== undefined && p.default !== null && p.default !== '') {
      args[key] = p.default
      continue
    }
    if (!isRequired) continue
    if (key === 'symbol') args[key] = '600519'
    else if (key === 'market') args[key] = 'CN'
    else if (key === 'question') args[key] = '今日主力净流入前10的A股'
    else if (key === 'scene') args[key] = 'overview'
    else if (p?.type === 'integer' || p?.type === 'number') args[key] = 10
    else if (p?.type === 'boolean') args[key] = false
    else args[key] = ''
  }
  return JSON.stringify(args, null, 2)
}

/**
 * 调试台专用请求: 不用 fetchAPI —— 其 401 分支会强制 logout() 跳登录,
 * 而开发者页要求未登录也能看文档/试 free skill。
 */
async function callSkillRun(
  name: string,
  args: Record<string, unknown>,
  opts: { apiKey: string; useJwt: boolean },
): Promise<RunOutcome> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (opts.apiKey.trim()) {
    headers['X-API-Key'] = opts.apiKey.trim()
  } else if (opts.useJwt) {
    const t = getToken()
    if (t) headers['Authorization'] = `Bearer ${t}`
  }
  const t0 = performance.now()
  try {
    const res = await fetch(`/api/skills/${encodeURIComponent(name)}/run`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ args }),
    })
    const durationMs = Math.round(performance.now() - t0)
    const retryAfter = res.headers.get('Retry-After')
    let body: any = null
    try {
      body = await res.json()
    } catch {
      body = null
    }
    // 响应经 ResponseWrapperMiddleware: {code, success, data, message}
    const ok = !!body?.success && res.ok
    return {
      status: res.status,
      ok,
      message: String(body?.message || (ok ? '' : `HTTP ${res.status}`)),
      data: body?.data ?? body,
      retryAfter,
      durationMs,
    }
  } catch (e: any) {
    return {
      status: 0,
      ok: false,
      message: String(e?.message || e || '网络错误'),
      data: null,
      retryAfter: null,
      durationMs: Math.round(performance.now() - t0),
    }
  }
}

// ── 调试台 ──────────────────────────────────────────────────────────

function SkillPlayground({ skills }: { skills: CatalogSkill[] }) {
  const loggedIn = isAuthenticated()
  const [apiKey, setApiKey] = useState('')
  const [authMode, setAuthMode] = useState<'key' | 'session' | 'guest'>(loggedIn ? 'session' : 'guest')
  const [skillName, setSkillName] = useState('')
  const [argsText, setArgsText] = useState('{}')
  const [running, setRunning] = useState(false)
  const [outcome, setOutcome] = useState<RunOutcome | null>(null)

  const selected = useMemo(
    () => skills.find(s => s.name === skillName) || null,
    [skills, skillName],
  )

  // 列表加载后自动选中第一个 free skill, 并预填参数
  useEffect(() => {
    if (!skillName && skills.length > 0) {
      const first = skills.find(s => s.tier_min === 'free') || skills[0]
      setSkillName(first.name)
      setArgsText(buildDefaultArgs(first))
    }
  }, [skills, skillName])

  const onSkillChange = (name: string) => {
    setSkillName(name)
    const sk = skills.find(s => s.name === name) || null
    setArgsText(buildDefaultArgs(sk))
    setOutcome(null)
  }

  const run = useCallback(async () => {
    if (!skillName || running) return
    let args: Record<string, unknown>
    try {
      const parsed = JSON.parse(argsText || '{}')
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setOutcome({
          status: 0,
          ok: false,
          message: '参数必须是 JSON 对象, 例如 {"symbol": "600519"}',
          data: null,
          retryAfter: null,
          durationMs: 0,
        })
        return
      }
      args = parsed as Record<string, unknown>
    } catch {
      setOutcome({
        status: 0,
        ok: false,
        message: '参数 JSON 解析失败, 请检查括号/引号',
        data: null,
        retryAfter: null,
        durationMs: 0,
      })
      return
    }
    setRunning(true)
    setOutcome(null)
    const result = await callSkillRun(skillName, args, {
      apiKey: authMode === 'key' ? apiKey : '',
      useJwt: authMode === 'session',
    })
    setOutcome(result)
    setRunning(false)
  }, [apiKey, argsText, authMode, running, skillName])

  return (
    <section className="border-t border-border/40 pt-4 md:pt-5 mt-4">
      <SectionHead
        icon={Play}
        title="在线调试台"
        hint="Key 仅存于当前页面内存, 刷新即失, 不写 localStorage"
      />

      <div className="rounded-xl border border-border/50 bg-accent/10 p-4 space-y-4">
        {/* 鉴权方式 */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div>
            <Label>鉴权方式</Label>
            <Select value={authMode} onValueChange={(v) => setAuthMode(v as typeof authMode)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="session" disabled={!loggedIn}>
                  当前登录会话 (JWT){!loggedIn && ' — 未登录'}
                </SelectItem>
                <SelectItem value="key">API Key</SelectItem>
                <SelectItem value="guest">游客 (无鉴权)</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="md:col-span-2">
            <Label>
              API Key
              {authMode === 'key' && (
                <span className="ml-2 text-[10px] font-normal text-amber-600">仅内存, 不持久化</span>
              )}
            </Label>
            <Input
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={authMode === 'key' ? 'sk_...' : '选择「API Key」后在此粘贴'}
              value={apiKey}
              onChange={e => setApiKey(e.target.value)}
              disabled={authMode !== 'key'}
            />
          </div>
        </div>

        {/* Skill + 参数 */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <Label>Skill</Label>
            <Select value={skillName} onValueChange={onSkillChange}>
              <SelectTrigger>
                <SelectValue placeholder={skills.length ? '选择 skill' : '目录加载中…'} />
              </SelectTrigger>
              <SelectContent>
                {skills.map(s => (
                  <SelectItem key={s.name} value={s.name}>
                    {s.name} · {s.tier_min}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {selected && (
              <p className="mt-1.5 text-[11px] text-muted-foreground line-clamp-2">
                {selected.description || '（无描述）'}
                {selected.caliber ? ` · 口径: ${selected.caliber}` : ''}
              </p>
            )}
          </div>
          <div>
            <Label>参数 (JSON)</Label>
            <textarea
              className="flex min-h-[88px] w-full rounded-xl border border-border bg-card px-3 py-2.5 font-mono text-[12px] leading-relaxed shadow-[0_1px_2px_rgba(0,0,0,0.04)] transition-all placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/30 disabled:cursor-not-allowed disabled:opacity-50"
              spellCheck={false}
              value={argsText}
              onChange={e => setArgsText(e.target.value)}
              placeholder='{"symbol": "600519", "market": "CN"}'
            />
          </div>
        </div>

        <div className="flex items-center gap-2">
          <Button onClick={run} disabled={running || !skillName} size="sm">
            {running ? (
              <>
                <span className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                运行中…
              </>
            ) : (
              <>
                <Play className="h-3.5 w-3.5" />
                Run
              </>
            )}
          </Button>
          <span className="text-[11px] text-muted-foreground">
            POST /api/skills/{'{name}'}/run
            {outcome && (
              <span className="ml-2">
                {outcome.status > 0 ? `HTTP ${outcome.status}` : '网络错误'} · {outcome.durationMs}ms
                {outcome.retryAfter ? ` · Retry-After ${outcome.retryAfter}s` : ''}
              </span>
            )}
          </span>
        </div>

        {outcome && (
          <div>
            <div className="flex items-center gap-2 mb-1.5">
              <span
                className={`text-[11px] font-medium ${outcome.ok ? 'text-emerald-600' : 'text-destructive'}`}
              >
                {outcome.ok ? '成功' : '失败'}
              </span>
              {!outcome.ok && outcome.message && (
                <span className="text-[11px] text-muted-foreground truncate">{outcome.message}</span>
              )}
            </div>
            <pre className="max-h-80 overflow-auto rounded-lg border border-border/50 bg-accent/20 px-3 py-2.5 font-mono text-[11px] leading-relaxed text-foreground/85 whitespace-pre-wrap break-all">
              {(() => {
                try {
                  return JSON.stringify(outcome.data, null, 2)
                } catch {
                  return String(outcome.data)
                }
              })()}
            </pre>
          </div>
        )}
      </div>
    </section>
  )
}

// ── 主页面 ──────────────────────────────────────────────────────────

export default function DevelopersPage() {
  const [catalog, setCatalog] = useState<CatalogResp | null>(null)
  const [catalogErr, setCatalogErr] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    fetchAPI<CatalogResp>('/skills/catalog', { cacheMode: 300 })
      .then(data => {
        if (cancelled) return
        setCatalog(data)
        setCatalogErr('')
      })
      .catch((e: any) => {
        if (cancelled) return
        setCatalogErr(String(e?.message || e || '目录加载失败'))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const skills = catalog?.skills || []
  const tiers = catalog?.tiers || {}

  const rateRows = useMemo(() => {
    const t = tiers
    return [
      {
        tier: 'guest',
        label: '游客',
        limit: t.guest?.daily_limit ?? 10,
        burst: undefined as number | undefined,
        refill: undefined as number | undefined,
        note: '按来源 IP 24h 滑动窗口; 仅 tier_min=free 的 skill',
      },
      {
        tier: 'free',
        label: 'free',
        limit: t.free?.daily_limit ?? 100,
        burst: t.free?.burst,
        refill: t.free?.refill_per_min,
        note: 'POST /api/keys 默认可领; trial 到期自动降为 free',
      },
      {
        tier: 'trial',
        label: 'trial',
        limit: t.trial?.daily_limit ?? 500,
        burst: t.trial?.burst,
        refill: t.trial?.refill_per_min,
        note: `新人试用 ${t.trial?.days ?? 10} 天, 到期降 free`,
      },
      {
        tier: 'pro',
        label: 'pro',
        limit: t.pro?.daily_limit ?? 5000,
        burst: t.pro?.burst,
        refill: t.pro?.refill_per_min,
        note: '人工审核开通; 可调用全部开放 skill(含 slow)',
      },
    ]
  }, [tiers])

  return (
    <div className="page-container sida-page-enter pb-10 max-w-5xl">
      {/* Hero */}
      <div className="border-b border-border/40 p-5 md:p-7">
        <div className="flex items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-accent/40 text-primary ring-1 ring-border/40">
            <Code2 className="h-4.5 w-4.5" />
          </div>
          <div>
            <h1 className="text-[20px] md:text-[22px] font-bold text-foreground tracking-tight">开发者文档</h1>
            <p className="text-[12px] text-muted-foreground mt-1">
              Skill Gateway HTTP API：快速开始、鉴权与限流、公开 Skill 目录、在线调试台与错误码
            </p>
          </div>
        </div>
      </div>

      {/* 快速开始 */}
      <section className="mt-6">
        <SectionHead icon={Terminal} title="快速开始" hint="注册 → 拿 Key → 第一次调用" />
        <div className="space-y-4">
          {QUICK_START_STEPS.map(s => (
            <div key={s.title} className="border-t border-border/40 pt-3">
              <h4 className="text-[13px] font-semibold text-foreground mb-1">{s.title}</h4>
              <p className="text-[12px] text-foreground/80 leading-relaxed mb-2">{s.desc}</p>
              <CodeBlock code={s.code} />
            </div>
          ))}
        </div>
      </section>

      {/* 鉴权 */}
      <section className="border-t border-border/40 pt-4 md:pt-5 mt-4">
        <SectionHead icon={ShieldCheck} title="鉴权说明" hint="X-API-Key / JWT 双通道, 无凭证降级游客" />
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {AUTH_CHANNELS.map(ch => (
            <div key={ch.tag} className="rounded-lg border border-border/50 bg-accent/10 p-3">
              <div className="flex items-center gap-2 mb-1.5">
                <Badge variant="outline">{ch.tag}</Badge>
                <span className="text-[12px] font-medium text-foreground">{ch.title}</span>
              </div>
              <p className="text-[11px] text-muted-foreground leading-relaxed mb-2">{ch.desc}</p>
              <CodeBlock code={ch.example} />
            </div>
          ))}
        </div>
        <p className="mt-3 flex items-start gap-1.5 text-[11px] text-muted-foreground">
          <Lock className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          <span>
            优先级：带 sk_ 前缀的 X-API-Key &gt; Authorization: Bearer JWT &gt; 游客。Key 明文只在创建响应出现一次，服务端仅存 sha256+盐。
          </span>
        </p>
      </section>

      {/* 限流 */}
      <section className="border-t border-border/40 pt-4 md:pt-5 mt-4">
        <SectionHead icon={Gauge} title="限流说明" hint="日配额 + 令牌桶(burst / 匀速补充)" />
        <div className="overflow-x-auto rounded-lg border border-border/50">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="border-b border-border/50 bg-accent/20 text-left text-[11px] text-muted-foreground">
                <th className="px-3 py-2 font-medium">档位</th>
                <th className="px-3 py-2 font-medium">日配额</th>
                <th className="px-3 py-2 font-medium">Burst</th>
                <th className="px-3 py-2 font-medium">匀速(次/分)</th>
                <th className="px-3 py-2 font-medium">说明</th>
              </tr>
            </thead>
            <tbody>
              {rateRows.map(r => (
                <tr key={r.tier} className="border-b border-border/30 last:border-0">
                  <td className="px-3 py-2">
                    <Badge variant={r.tier === 'guest' ? 'outline' : tierBadgeVariant(r.tier)}>{r.label}</Badge>
                  </td>
                  <td className="px-3 py-2 font-num">{r.limit}</td>
                  <td className="px-3 py-2 font-num">{r.burst ?? '—'}</td>
                  <td className="px-3 py-2 font-num">{r.refill ?? '—'}</td>
                  <td className="px-3 py-2 text-muted-foreground">{r.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-[11px] text-muted-foreground">
          超限返回 429，响应头带 <code className="font-mono">Retry-After</code>（秒）。日配额按 UTC 日重置；
          令牌桶按 key 进程内维护，多 worker 时实际 burst 可能略高，日配额仍是硬顶。
        </p>
      </section>

      {/* Skill 目录 */}
      <section className="border-t border-border/40 pt-4 md:pt-5 mt-4">
        <SectionHead
          icon={BookOpen}
          title="Skill 目录"
          hint={loading ? '加载中…' : `${skills.length} 个开放 skill · 来自 GET /api/skills/catalog`}
        />
        {catalogErr && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-[12px] text-destructive mb-3">
            目录加载失败: {catalogErr}
          </div>
        )}
        {loading && !skills.length && (
          <div className="flex items-center gap-2 text-[12px] text-muted-foreground py-4">
            <span className="w-4 h-4 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
            正在拉取公开目录…
          </div>
        )}
        {!loading && !catalogErr && skills.length === 0 && (
          <div className="text-[12px] text-muted-foreground py-4">暂无开放 skill。</div>
        )}
        {skills.length > 0 && (
          <div className="overflow-x-auto rounded-lg border border-border/50">
            <table className="w-full text-[12px]">
              <thead>
                <tr className="border-b border-border/50 bg-accent/20 text-left text-[11px] text-muted-foreground">
                  <th className="px-3 py-2 font-medium">名称</th>
                  <th className="px-3 py-2 font-medium">最低档位</th>
                  <th className="px-3 py-2 font-medium">说明</th>
                  <th className="px-3 py-2 font-medium">口径</th>
                </tr>
              </thead>
              <tbody>
                {skills.map(s => (
                  <tr key={s.name} className="border-b border-border/30 last:border-0 align-top">
                    <td className="px-3 py-2 font-mono text-[11px] whitespace-nowrap">
                      {s.name}
                      {s.slow && (
                        <span className="ml-1.5 text-[10px] text-amber-600" title="慢接口">
                          slow
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <Badge variant={tierBadgeVariant(s.tier_min)}>{s.tier_min}</Badge>
                    </td>
                    <td className="px-3 py-2 text-muted-foreground max-w-md">
                      <span className="line-clamp-2">{s.description || '—'}</span>
                    </td>
                    <td className="px-3 py-2 text-[11px] text-muted-foreground max-w-[180px]">
                      {s.caliber || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* 在线调试台 */}
      <SkillPlayground skills={skills} />

      {/* 错误码 */}
      <section className="border-t border-border/40 pt-4 md:pt-5 mt-4">
        <SectionHead icon={AlertTriangle} title="错误码表" hint="含示例响应与退避建议" />
        <div className="space-y-3">
          {ERROR_ROWS.map(row => (
            <div key={row.code} className="rounded-lg border border-border/50 bg-accent/10 p-3">
              <div className="flex flex-wrap items-center gap-2 mb-1.5">
                <span className="font-num text-[14px] font-bold text-foreground">{row.code}</span>
                <span className="text-[13px] font-medium text-foreground">{row.title}</span>
              </div>
              <p className="text-[12px] text-foreground/80 leading-relaxed mb-2">{row.desc}</p>
              <CodeBlock code={row.example} />
              <p className="mt-2 flex items-start gap-1.5 text-[11px] text-muted-foreground">
                <KeyRound className="h-3.5 w-3.5 mt-0.5 shrink-0 opacity-60" />
                <span>
                  <span className="font-medium text-foreground/70">退避建议：</span>
                  {row.advice}
                </span>
              </p>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
