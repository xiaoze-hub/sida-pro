import { useEffect, useMemo, useState } from 'react'
import {
  KeyRound,
  ShieldCheck,
  Gauge,
  Play,
  AlertTriangle,
  Terminal,
  Rocket,
  ListTree,
  Bug,
  Loader2,
  Database,
} from 'lucide-react'
import { fetchAPI, getToken } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@panwatch/base-ui/components/ui/select'
import { DevPageLayout, Section, InfoCard, CodeBlock, type SideMenuItem } from '@/components/dev/DevPageLayout'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'

/**
 * 开发者文档页 v2(2026-09-16)。
 * 侧边栏导航 + 章节化布局 + 在线调试台。
 */

// ── 目录类型 ──
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
  tiers: Record<string, TierLimit>
  guest?: { daily_limit: number }
}

// ── 菜单 ──
const MENU: SideMenuItem[] = [
  { id: 'quickstart', label: '快速开始', icon: <Rocket className="h-3.5 w-3.5" />, anchor: 'sec-quickstart' },
  { id: 'auth', label: '鉴权方式', icon: <ShieldCheck className="h-3.5 w-3.5" />, anchor: 'sec-auth' },
  { id: 'ratelimit', label: '限流说明', icon: <Gauge className="h-3.5 w-3.5" />, anchor: 'sec-ratelimit' },
  { id: 'catalog', label: 'Skill 目录', icon: <ListTree className="h-3.5 w-3.5" />, anchor: 'sec-catalog' },
  { id: 'playground', label: '在线调试', icon: <Play className="h-3.5 w-3.5" />, anchor: 'sec-playground' },
  { id: 'datasources', label: '数据来源', icon: <Database className="h-3.5 w-3.5" />, anchor: 'sec-datasources' },
  { id: 'errors', label: '错误码', icon: <Bug className="h-3.5 w-3.5" />, anchor: 'sec-errors' },
]

// 主要数据源与口径标签(合规: 资金类指标必须声明方向语义)
const DATA_SOURCES: { name: string; caliber: string; note: string }[] = [
  { name: '腾讯行情', caliber: 'tick', note: '逐笔成交/主动买卖方向, 主力意图判定唯一采信口径' },
  { name: '同花顺 DDE', caliber: 'ths', note: 'DDE 资金流, 适合大单净流入等横截面对比' },
  { name: '通达信 TQ', caliber: 'tick', note: 'L2 盘口主力净流入, 聚合逐笔后按方向汇总' },
  { name: '东方财富', caliber: 'eastmoney4', note: '按单金额四档归类(超大/大/中/小), 非逐笔' },
]

const TIER_BADGE: Record<string, string> = {
  free: 'bg-slate-500/10 text-muted-foreground dark:text-slate-400 border-slate-500/20',
  trial: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  pro: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
}

export default function DevelopersPage() {
  const [activeSection, setActiveSection] = useState('quickstart')
  const [catalog, setCatalog] = useState<CatalogResp | null>(null)
  const [catLoading, setCatLoading] = useState(true)
  const [catError, setCatError] = useState<Error | null>(null)

  // 调试台状态
  const [authMode, setAuthMode] = useState<'jwt' | 'apikey' | 'guest'>('apikey')
  const [apiKey, setApiKey] = useState('')
  const [selectedSkill, setSelectedSkill] = useState('')
  const [paramsJson, setParamsJson] = useState('{}')
  const [runResult, setRunResult] = useState<string | null>(null)
  const [running, setRunning] = useState(false)

  const loadCatalog = () => {
    setCatLoading(true)
    setCatError(null)
    fetchAPI<CatalogResp>('/skills/catalog')
      .then(d => { setCatalog(d); setCatLoading(false) })
      .catch((e: unknown) => {
        setCatError(e instanceof Error ? e : new Error(String(e)))
        setCatLoading(false)
      })
  }

  useEffect(() => { loadCatalog() }, [])

  const skills = useMemo(() => catalog?.skills ?? [], [catalog])
  const selected = useMemo(() => skills.find(s => s.name === selectedSkill), [skills, selectedSkill])

  // 自动填默认参数
  useEffect(() => {
    if (!selected) return
    const defaults: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(selected.params || {})) {
      if (v.default !== undefined) defaults[k] = v.default
    }
    setParamsJson(JSON.stringify(defaults, null, 2))
  }, [selected])

  const runSkill = async () => {
    if (!selectedSkill) return
    setRunning(true)
    setRunResult(null)
    try {
      let params: Record<string, unknown>
      try { params = JSON.parse(paramsJson) } catch { setRunResult('参数 JSON 格式错误'); return }

      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (authMode === 'apikey' && apiKey) headers['X-API-Key'] = apiKey
      else if (authMode === 'jwt') {
        const t = getToken()
        if (t) headers['Authorization'] = `Bearer ${t}`
      }

      const resp = await fetch(`${window.location.origin}/api/skills/${selectedSkill}/run`, {
        method: 'POST',
        headers,
        body: JSON.stringify(params),
      })
      const data = await resp.json()
      setRunResult(JSON.stringify(data, null, 2))
    } catch (e: any) {
      setRunResult(`请求失败: ${e?.message || e}`)
    } finally {
      setRunning(false)
    }
  }

  const base = typeof window !== 'undefined' ? window.location.origin : ''

  return (
    <DevPageLayout
      title="开发者文档"
      subtitle="SIDA Skill API 接入指南 — 从零到第一次调用"
      badge="v1.0"
      menu={MENU}
      activeId={activeSection}
      onNav={setActiveSection}
      headerExtra={
        <Button size="sm" variant="outline" className="h-8 shrink-0" onClick={() => window.location.href = '/api-keys'}>
          <KeyRound className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">API Key</span>
        </Button>
      }
    >
      {/* ── 快速开始 ── */}
      <Section id="sec-quickstart" title="快速开始" description="三步接入 SIDA Skill API" icon={<Rocket className="h-4 w-4" />}>
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            {[
              { step: '01', title: '注册账号', desc: '邮箱注册，自动签发 API Key', icon: <KeyRound className="h-4 w-4" /> },
              { step: '02', title: '获取 Key', desc: '在控制台查看/管理你的 Key', icon: <ShieldCheck className="h-4 w-4" /> },
              { step: '03', title: '发起调用', desc: '一行 curl 即可验证连通', icon: <Terminal className="h-4 w-4" /> },
            ].map(s => (
              <div key={s.step} className="rounded-xl border border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02] p-4 text-center">
                <div className="mx-auto mb-2 inline-flex h-8 w-8 items-center justify-center rounded-full bg-primary/10 text-primary dark:bg-cyan-500/10 dark:text-cyan-400">
                  {s.icon}
                </div>
                <div className="font-mono text-[10px] text-primary/60 dark:text-cyan-500/50">{s.step}</div>
                <div className="text-[13px] font-medium text-foreground dark:text-white">{s.title}</div>
                <div className="mt-0.5 text-[11px] text-muted-foreground">{s.desc}</div>
              </div>
            ))}
          </div>

          <InfoCard>
            <div className="mb-2 text-[13px] font-medium text-foreground dark:text-white">第一次调用</div>
            <CodeBlock
              code={`curl -X POST "${base}/api/skills/get_stock_quote/run" \\
  -H "X-API-Key: sk_YOUR_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"symbol": "000001"}'`}
              language="bash"
            />
          </InfoCard>
        </div>
      </Section>

      {/* ── 鉴权方式 ── */}
      <Section id="sec-auth" title="鉴权方式" description="三种方式调用 Skill API" icon={<ShieldCheck className="h-4 w-4" />}>
        <div className="space-y-3">
          {[
            {
              title: 'API Key（推荐）',
              desc: '服务端调用首选，通过 X-API-Key 请求头传递',
              code: `curl -H "X-API-Key: sk_YOUR_KEY" "${base}/api/skills/get_stock_quote/run"`,
              badge: '推荐',
            },
            {
              title: 'JWT Token',
              desc: '网页端登录后自动携带，与 API Key 共享配额',
              code: `curl -H "Authorization: Bearer YOUR_JWT" "${base}/api/skills/get_stock_quote/run"`,
              badge: '网页',
            },
            {
              title: '游客模式',
              desc: '无需认证，仅限 free 级 Skill，每 IP 每天 10 次',
              code: `curl -X POST "${base}/api/skills/get_stock_quote/run" \\
  -H "Content-Type: application/json" \\
  -d '{"symbol": "000001"}'`,
              badge: '受限',
            },
          ].map((a, i) => (
            <InfoCard key={i}>
              <div className="mb-1.5 flex items-center gap-2">
                <span className="text-[13px] font-medium text-foreground dark:text-white">{a.title}</span>
                <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[9px] font-mono text-primary dark:bg-cyan-500/10 dark:text-cyan-400">{a.badge}</span>
              </div>
              <p className="mb-2 text-[11px] text-muted-foreground">{a.desc}</p>
              <CodeBlock code={a.code} language="bash" />
            </InfoCard>
          ))}
        </div>
      </Section>

      {/* ── 限流说明 ── */}
      <Section id="sec-ratelimit" title="限流说明" description="各档位的调用限制" icon={<Gauge className="h-4 w-4" />}>
        {catLoading ? (
          <div className="flex items-center gap-2 text-[12px] text-muted-foreground dark:text-slate-600">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> 加载中...
          </div>
        ) : catError ? (
          <ErrorState
            error={catError}
            type="server"
            title="限流信息加载失败"
            onRetry={loadCatalog}
            compact
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12px]">
              <thead>
                <tr className="border-b border-border/40 dark:border-white/10 text-muted-foreground">
                  <th className="pb-2 pr-4 font-medium">档位</th>
                  <th className="pb-2 pr-4 font-medium">日限</th>
                  <th className="pb-2 pr-4 font-medium">突发</th>
                  <th className="pb-2 font-medium">说明</th>
                </tr>
              </thead>
              <tbody>
                {catalog?.guest && (
                  <tr className="border-b border-border/40 dark:border-white/5">
                    <td className="py-2 pr-4"><span className="rounded bg-slate-500/10 px-1.5 py-0.5 text-[10px] text-muted-foreground dark:text-slate-400">游客</span></td>
                    <td className="py-2 pr-4 font-mono text-foreground dark:text-white">{catalog.guest.daily_limit}</td>
                    <td className="py-2 pr-4 font-mono text-muted-foreground dark:text-slate-500">-</td>
                    <td className="py-2 text-muted-foreground dark:text-slate-500">仅 free 级 Skill，按 IP 限流</td>
                  </tr>
                )}
                {Object.entries(catalog?.tiers ?? {}).map(([tier, limit]) => (
                  <tr key={tier} className="border-b border-border/40 dark:border-white/5">
                    <td className="py-2 pr-4">
                      <span className={`rounded border px-1.5 py-0.5 text-[10px] ${TIER_BADGE[tier] || TIER_BADGE.free}`}>
                        {tier}
                      </span>
                    </td>
                    <td className="py-2 pr-4 font-mono text-foreground dark:text-white">{limit.daily_limit}</td>
                    <td className="py-2 pr-4 font-mono text-muted-foreground dark:text-slate-500">{limit.burst ?? '-'}</td>
                    <td className="py-2 text-muted-foreground dark:text-slate-500">
                      {tier === 'free' && '注册即得，基础配额'}
                      {tier === 'trial' && `试用 ${limit.days ?? 7} 天`}
                      {tier === 'pro' && 'Pro 付费，全量开放'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-3 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-400" />
            <div className="text-[11px] text-amber-300/80">
              <strong>429 限流响应</strong> 会携带 <code className="font-mono">Retry-After</code> 头（秒）。
              建议实现指数退避 + 抖动：首次等待 Retry-After，后续每次翻倍，上限 60s。
            </div>
          </div>
        </div>
      </Section>

      {/* ── Skill 目录 ── */}
      <Section
        id="sec-catalog"
        title="Skill 目录"
        description={`共 ${skills.length} 个可用接口`}
        icon={<ListTree className="h-4 w-4" />}
      >
        {catLoading ? (
          <div className="flex items-center gap-2 text-[12px] text-muted-foreground dark:text-slate-600">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> 加载中...
          </div>
        ) : catError ? (
          <ErrorState
            error={catError}
            type="server"
            title="Skill 目录加载失败"
            description="无法获取接口列表，请检查网络后重试"
            onRetry={loadCatalog}
            compact
          />
        ) : skills.length === 0 ? (
          <EmptyState
            title="暂无可用 Skill"
            description="目录为空，请稍后刷新或联系管理员"
            compact
          />
        ) : (
          <div className="space-y-2">
            {skills.map(s => (
              <div key={s.name} className="rounded-lg border border-border/50 dark:border-white/5 bg-card/50 dark:bg-white/[0.02] p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <code className="font-mono text-[12px] text-primary dark:text-cyan-300">{s.name}</code>
                  <span className={`rounded border px-1.5 py-0.5 text-[9px] ${TIER_BADGE[s.tier_min] || TIER_BADGE.free}`}>
                    {s.tier_min}
                  </span>
                  {s.slow && <span className="rounded bg-orange-500/10 px-1.5 py-0.5 text-[9px] text-orange-600 dark:text-orange-400">慢</span>}
                  {s.caliber && <span className="rounded bg-blue-500/10 px-1.5 py-0.5 text-[9px] text-blue-600 dark:text-blue-400">{s.caliber}</span>}
                </div>
                <p className="mt-1 text-[11px] text-muted-foreground">{s.description}</p>
                {Object.keys(s.params || {}).length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {Object.entries(s.params).map(([k, v]) => (
                      <span key={k} className="rounded bg-muted/60 dark:bg-white/5 px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground">
                        {k}{v.default !== undefined ? `=${v.default}` : ''}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* ── 在线调试 ── */}
      <Section id="sec-playground" title="在线调试台" description="选择 Skill、填参数、一键运行" icon={<Play className="h-4 w-4" />}>
        <div className="space-y-4">
          <InfoCard>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <div>
                <Label className="text-[11px] text-muted-foreground dark:text-slate-400">鉴权方式</Label>
                <Select value={authMode} onValueChange={(v: any) => setAuthMode(v)}>
                  <SelectTrigger className="mt-1 h-8 text-[12px]"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="apikey">API Key</SelectItem>
                    <SelectItem value="jwt">JWT（需登录）</SelectItem>
                    <SelectItem value="guest">游客（受限）</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              {authMode === 'apikey' && (
                <div>
                  <Label className="text-[11px] text-muted-foreground dark:text-slate-400">API Key</Label>
                  <Input
                    type="password"
                    className="mt-1 h-8 font-mono text-[12px]"
                    placeholder="sk_..."
                    value={apiKey}
                    onChange={e => setApiKey(e.target.value)}
                    autoComplete="off"
                  />
                </div>
              )}
            </div>
          </InfoCard>

          <InfoCard>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <div>
                <Label className="text-[11px] text-muted-foreground dark:text-slate-400">选择 Skill</Label>
                <Select value={selectedSkill} onValueChange={setSelectedSkill}>
                  <SelectTrigger className="mt-1 h-8 text-[12px]"><SelectValue placeholder="请选择" /></SelectTrigger>
                  <SelectContent>
                    {skills.map(s => (
                      <SelectItem key={s.name} value={s.name}>{s.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              {selected && (
                <div className="flex items-end">
                  <p className="text-[11px] text-muted-foreground dark:text-slate-500">{selected.description}</p>
                </div>
              )}
            </div>
            <div className="mt-3">
              <Label className="text-[11px] text-muted-foreground dark:text-slate-400">参数 (JSON)</Label>
              <textarea
                className="mt-1 h-24 w-full rounded-lg border border-border/60 dark:border-white/10 bg-muted/40 dark:bg-[#0d0d18] p-2.5 font-mono text-[11px] text-foreground dark:text-cyan-100/80 focus:border-primary/40 dark:focus:border-cyan-500/30 focus:outline-none"
                value={paramsJson}
                onChange={e => setParamsJson(e.target.value)}
                spellCheck={false}
              />
            </div>
            <div className="mt-3 flex justify-end">
              <Button size="sm" className="h-8" onClick={runSkill} disabled={running || !selectedSkill}>
                {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                运行
              </Button>
            </div>
          </InfoCard>

          {runResult && (
            <InfoCard>
              <div className="mb-2 text-[13px] font-medium text-foreground dark:text-white">响应结果</div>
              <CodeBlock code={runResult} language="json" />
            </InfoCard>
          )}
        </div>
      </Section>

      {/* ── 数据来源与口径 ── */}
      <Section
        id="sec-datasources"
        title="数据来源与口径"
        description="主要数据源、口径标签与方向语义约定"
        icon={<Database className="h-4 w-4" />}
      >
        <div className="space-y-3">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12px]">
              <thead>
                <tr className="border-b border-border/40 dark:border-white/10 text-muted-foreground">
                  <th className="pb-2 pr-4 font-medium">数据源</th>
                  <th className="pb-2 pr-4 font-medium">口径</th>
                  <th className="pb-2 font-medium">说明</th>
                </tr>
              </thead>
              <tbody>
                {DATA_SOURCES.map(s => (
                  <tr key={s.name} className="border-b border-border/40 dark:border-white/5">
                    <td className="py-2 pr-4 text-foreground dark:text-white">{s.name}</td>
                    <td className="py-2 pr-4">
                      <span className="rounded bg-blue-500/10 px-1.5 py-0.5 font-mono text-[10px] text-blue-400">
                        {s.caliber}
                      </span>
                    </td>
                    <td className="py-2 text-muted-foreground dark:text-slate-500">{s.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-3">
            <div className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-400" />
              <div className="space-y-1 text-[11px] text-amber-300/80">
                <p>
                  <strong>方向语义必须声明。</strong>
                  资金类指标返回值须携带 <code className="font-mono">caliber</code>（tick / eastmoney4 / ths / unknown）
                  与 <code className="font-mono">direction_semantics</code>。
                  拿不到标签时按 unknown 处理，不得用于方向性判定。
                </p>
                <p>
                  两口径方向冲突时须说明差异（逐笔主动买卖 vs 按单金额归类），并
                  <strong>优先采信逐笔（tick）</strong>。
                  禁止混用口径做主力意图/吸筹派发判断。
                </p>
              </div>
            </div>
          </div>
        </div>
      </Section>

      {/* ── 错误码 ── */}
      <Section id="sec-errors" title="错误码" description="常见错误及处理方式" icon={<Bug className="h-4 w-4" />}>
        <div className="space-y-2">
          {[
            { code: 401, title: '未认证', desc: '缺少或无效的 API Key / JWT', fix: '检查 X-API-Key 头是否正确，或重新登录获取 JWT' },
            { code: 403, title: '权限不足', desc: '当前档位无法访问该 Skill', fix: '升级到更高档位（trial/pro），或选择 free 级 Skill' },
            { code: 429, title: '限流', desc: '超过当日/当分钟调用限制', fix: '读取 Retry-After 头，指数退避后重试；或升级档位' },
            { code: 500, title: '服务器错误', desc: '内部处理异常', fix: '稍后重试；若持续出现请联系管理员' },
          ].map(e => (
            <div key={e.code} className="rounded-lg border border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02] p-3">
              <div className="flex items-center gap-2">
                <span className={`font-mono text-[14px] font-bold ${
                  e.code === 401 ? 'text-amber-600 dark:text-amber-400' : e.code === 403 ? 'text-orange-600 dark:text-orange-400' : e.code === 429 ? 'text-red-600 dark:text-red-400' : 'text-red-600 dark:text-red-500'
                }`}>{e.code}</span>
                <span className="text-[13px] font-medium text-foreground dark:text-white">{e.title}</span>
              </div>
              <p className="mt-1 text-[11px] text-muted-foreground">{e.desc}</p>
              <p className="mt-1 text-[11px] text-primary/70 dark:text-cyan-400/60">→ {e.fix}</p>
            </div>
          ))}
        </div>

        <div className="mt-4">
          <div className="mb-2 text-[13px] font-medium text-foreground dark:text-white">429 退避示例</div>
          <CodeBlock
            code={`import time, random

def call_with_retry(fn, max_retries=3):
    for attempt in range(max_retries):
        try:
            return fn()
        except RateLimitError as e:
            wait = e.retry_after or (2 ** attempt + random.uniform(0, 1))
            time.sleep(min(wait, 60))
    raise Exception("Max retries exceeded")`}
            language="python"
          />
        </div>
      </Section>
    </DevPageLayout>
  )
}
