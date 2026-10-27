import { Link, useSearchParams } from 'react-router-dom'
import { GithubMark } from '@/components/icons/GithubMark'
import { usePublicSeo } from '@/hooks/use-public-seo'
import { PublicSiteFrame } from '@/components/public/PublicSiteChrome'
import { PROJECT_REPOSITORY, COMMUNITY_REPOSITORY, COMMUNITY_DEPLOYMENT, COMMUNITY_LICENSE } from '@/lib/product-links'
import { StatusChip } from '@sida/base-ui/components/ui/status-chip'
import { useEffect, useMemo, useState } from 'react'
import {
  ArrowUpRight,
  Server,
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
  BookOpen,
} from 'lucide-react'
import { fetchAPI, getMyPermissions, getToken, isAuthenticated } from '@sida/api'
import { Button } from '@sida/base-ui/components/ui/button'
import { Input } from '@sida/base-ui/components/ui/input'
import { Label } from '@sida/base-ui/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@sida/base-ui/components/ui/select'
import { DevPageLayout, Section, InfoCard, CodeBlock, type SideMenuItem } from '@/components/dev/DevPageLayout'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { useI18n } from '@/hooks/useI18n'
import CaliberBadge, { caliberOf } from '@sida/biz-ui/components/CaliberBadge'

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
  { id: 'api-ref', label: 'API 参考', icon: <BookOpen className="h-3.5 w-3.5" />, anchor: 'sec-api-ref' },
  { id: 'datasources', label: '数据来源', icon: <Database className="h-3.5 w-3.5" />, anchor: 'sec-datasources' },
  { id: 'selfhost', label: '开源与部署', icon: <Server className="h-3.5 w-3.5" />, anchor: 'sec-selfhost' },
  { id: 'errors', label: '错误码', icon: <Bug className="h-3.5 w-3.5" />, anchor: 'sec-errors' },
]

// ── API 参考数据 ──
const API_GROUPS = [
  {
    title: '账号与认证',
    apis: [
      { method: 'POST', path: '/api/auth/register', auth: '公开', desc: '邮箱注册(需验证码), 自动签发 API Key' },
      { method: 'POST', path: '/api/auth/login', auth: '公开', desc: '用户名/邮箱 + 密码登录' },
      { method: 'POST', path: '/api/auth/send-code', auth: '公开', desc: '发送邮箱验证码(5分钟过期)' },
      { method: 'POST', path: '/api/auth/login-by-email', auth: '公开', desc: '邮箱 + 验证码登录' },
      { method: 'POST', path: '/api/auth/logout', auth: '登录', desc: '登出(清除 Cookie)' },
    ],
  },
  {
    title: 'API Key 管理',
    apis: [
      { method: 'GET', path: '/api/keys/my', auth: '登录', desc: '列出我的 API Key' },
      { method: 'POST', path: '/api/keys/my', auth: '登录', desc: '创建新 Key; 可选 body {owner_label, trial}, owner_label 为备注名(留空默认用户名); Pro/Owner 建出的 Key 直接是 pro 档' },
      { method: 'POST', path: '/api/keys/my/{id}/reset', auth: '登录', desc: '重置 Key(旧 Key 立即失效)' },
      { method: 'DELETE', path: '/api/keys/my/{id}', auth: '登录', desc: '删除 Key(软删: status=disabled, 立即失效; 行与用量流水保留供审计)' },
      { method: 'GET', path: '/api/keys/my/{id}/usage', auth: '登录', desc: '查看单 Key 用量' },
    ],
  },
  {
    title: 'Pro 申请与审核',
    apis: [
      { method: 'POST', path: '/api/pro/apply', auth: '登录', desc: '提交 Pro 升级申请' },
      { method: 'GET', path: '/api/pro/apply/status', auth: '登录', desc: '查询申请状态' },
      { method: 'GET', path: '/api/pro/admin/applications', auth: 'owner', desc: '审核列表' },
      { method: 'POST', path: '/api/pro/admin/approve', auth: 'owner', desc: '批准申请' },
      { method: 'POST', path: '/api/pro/admin/reject', auth: 'owner', desc: '拒绝申请' },
    ],
  },
  {
    // B9(2026-09-19): 因子有效性 —— 后端早就有 IC/IR/分层回测, 但**文档里一直没列**,
    // 外部接 API 的人根本发现不了。这里补上, 并把口径写清(横截面 IC 为主口径 / pooled 仅参考)。
    title: '因子有效性 (IC)',
    apis: [
      { method: 'GET', path: '/api/recommendations/strategy-factor-ic?days=90&horizon=5', auth: '登录', desc: '各因子横截面 IC / t / IR / 样本外 IC(pooled 仅参考)' },
      { method: 'GET', path: '/api/factors/weights', auth: '登录', desc: '因子权重(按市场区分, 含最近标定 IC/IR)' },
      { method: 'POST', path: '/api/factors/weights/{factor_code}/{market}', auth: 'owner', desc: '调整因子权重 / 锁定 / 自动标定开关' },
      { method: 'GET', path: '/api/recommendations/strategy-factors/{signal_run_id}', auth: '登录', desc: '单次信号运行的因子明细' },
    ],
  },
  {
    title: '用户数据 (GDPR)',
    apis: [
      { method: 'GET', path: '/api/user/data/export', auth: '登录', desc: '导出我的全部数据(JSON)' },
      { method: 'POST', path: '/api/user/data/delete', auth: '登录', desc: '请求删除账号(30天缓冲)' },
      { method: 'GET', path: '/api/user/data/delete/status', auth: '登录', desc: '查询删除状态' },
    ],
  },
  {
    title: '管理后台 (owner)',
    apis: [
      { method: 'GET', path: '/api/users/admin/list', auth: 'owner', desc: '全部用户列表' },
      { method: 'POST', path: '/api/users/admin/{uid}/toggle-active', auth: 'owner', desc: '启用/禁用用户' },
      { method: 'POST', path: '/api/users/admin/{uid}/change-role', auth: 'owner', desc: '修改用户角色' },
      { method: 'GET', path: '/api/users/admin/stats', auth: 'owner', desc: '注册统计' },
      { method: 'GET', path: '/api/admin/skills/keys', auth: 'owner', desc: '全部 API Key' },
      { method: 'POST', path: '/api/admin/skills/keys/action', auth: 'owner', desc: '冻结/解冻/禁用 Key' },
      { method: 'GET', path: '/api/admin/skills/usage', auth: 'owner', desc: '用量报表' },
      { method: 'POST', path: '/api/admin/rotate-secrets', auth: 'owner', desc: '手动轮换 JWT 密钥' },
    ],
  },
  {
    title: '智能体接入',
    apis: [
      { method: 'GET', path: '/api/skills/catalog', auth: '公开', desc: 'Skill 目录(含限流配置)' },
      { method: 'GET', path: '/api/skills/install.sh', auth: '公开', desc: '智能体安装脚本' },
      { method: 'GET', path: '/api/skills/config', auth: '登录', desc: '我的 Skill 配置 JSON' },
      { method: 'POST', path: '/api/skills/{name}/run', auth: 'Key/JWT/游客', desc: '调用 Skill; 请求体 {"args": {参数...}, "format": "json"} —— 参数必须放在 args 内' },
    ],
  },
]

// 主要数据源与口径标签(合规: 资金类指标必须声明方向语义)
const DATA_SOURCES: { name: string; caliber: string; note: string }[] = [
  { name: '腾讯行情', caliber: 'tick', note: '逐笔成交/主动买卖方向, 主力意图判定唯一采信口径' },
  { name: '同花顺 DDE', caliber: 'ths', note: 'DDE 资金流, 适合大单净流入等横截面对比' },
  { name: '通达信 TQ', caliber: 'tick', note: 'L2 盘口主力净流入, 聚合逐笔后按方向汇总' },
  { name: '东方财富', caliber: 'eastmoney4', note: '按单金额四档归类(超大/大/中/小), 非逐笔' },
]

const TIER_BADGE: Record<string, string> = {
  free: 'bg-s2 text-muted-foreground',
  trial: 'bg-s2 text-foreground',
  pro: 'bg-s3 text-primary',
}

export default function DevelopersPage() {
  usePublicSeo('/developers')
  const { t } = useI18n()
  const [loggedIn] = useState(() => isAuthenticated())
  const [, setSearchParams] = useSearchParams()
  const [catalogQuery, setCatalogQuery] = useState('')
  // 2026-09-19(用户报障): 文档是**公开面**(匿名可见), 但里面列了 12 个 owner 专用端点
  // (用户列表/改角色/全部 API Key/轮换密钥/审核申请…) —— 那不是给普通用户看的内部面。
  // 处理: 按角色过滤, **非 owner 不渲染 owner 条目**; 匿名访客自然也只看到公开/登录级端点。
  const [isOwner, setIsOwner] = useState(false)
  useEffect(() => {
    let alive = true
    if (!isAuthenticated()) { setIsOwner(false); return }
    void (async () => {
      const perms = await getMyPermissions()
      if (alive) setIsOwner(perms?.role === 'owner')
    })()
    return () => { alive = false }
  }, [])

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
  const [runError, setRunError] = useState('')
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
  const visibleSkills = useMemo(() => skills.filter(skill => `${skill.name} ${skill.description}`.toLowerCase().includes(catalogQuery.trim().toLowerCase())), [skills, catalogQuery])
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
    setRunError('')
    if (authMode === 'apikey' && !apiKey.trim()) { setRunError('请先填写 API Key，或切换为游客模式。'); return }
    if (authMode === 'jwt' && !loggedIn) { setRunError('请先登录当前实例，或选择其他鉴权方式。'); return }
    setRunning(true)
    setRunResult(null)
    try {
      let params: Record<string, unknown>
      try { params = JSON.parse(paramsJson) } catch { setRunError('参数 JSON 格式错误'); return }

      const headers: Record<string, string> = { 'Content-Type': 'application/json' }
      if (authMode === 'apikey' && apiKey) headers['X-API-Key'] = apiKey
      else if (authMode === 'jwt') {
        const t = getToken()
        if (t) headers['Authorization'] = `Bearer ${t}`
      }

      const resp = await fetch(`${window.location.origin}/api/skills/${selectedSkill}/run`, {
        method: 'POST',
        headers,
        // 后端契约固定为 {"args": {...}, "format": "json"}; 裸传参数会被当成未知顶层字段
        body: JSON.stringify({ args: params, format: 'json' }),
      })
      const data = await resp.json()
      setRunResult(JSON.stringify(data, null, 2))
    } catch (e: any) {
      setRunError(`请求失败: ${e?.message || e}`)
    } finally {
      setRunning(false)
    }
  }

  const base = typeof window !== 'undefined' ? window.location.origin : ''

  return (
    <PublicSiteFrame enabled={!loggedIn}>
    <DevPageLayout
      publicView={!loggedIn}
      title={t('developers.title')}
      subtitle="从一条请求开始接入当前实例。查看鉴权方式、可用接口与数据口径，或了解社区版开源部署。"
      badge="Skill API"
      menu={MENU}
      activeId={activeSection}
      onNav={setActiveSection}
      headerExtra={<>
        <Button asChild className="h-11"><Link to={loggedIn ? '/api-keys' : '/login?mode=register'}><KeyRound size={16} />{loggedIn ? '管理 API Key' : '注册并获取 Key'}</Link></Button>
        <Button asChild variant="ghost" className="h-11"><a href={PROJECT_REPOSITORY} target="_blank" rel="noopener noreferrer"><GithubMark size={16} />项目仓库<ArrowUpRight size={16} /></a></Button>
      </>}
    >
      {/* ── 快速开始 ── */}
      <Section id="sec-quickstart" title="快速开始" description="三步接入 SIDA Skill API" icon={<Rocket className="h-4 w-4" />}>
        <div className="space-y-4">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            {[
              { step: '01', title: '注册账号', desc: '注册或登录当前实例', icon: <KeyRound className="h-4 w-4" /> },
              { step: '02', title: '获取 Key', desc: '在 API Key 管理页创建与管理密钥', icon: <ShieldCheck className="h-4 w-4" /> },
              { step: '03', title: '发起调用', desc: '一行 curl 即可验证连通', icon: <Terminal className="h-4 w-4" /> },
            ].map(s => (
              <div key={s.step} className="card-surface rounded-lg bg-s2 p-4">
                <div className="mb-3 inline-flex h-8 w-8 items-center justify-center rounded-lg bg-card text-primary">
                  {s.icon}
                </div>
                <div className="font-mono text-[12px] text-muted-foreground">{s.step}</div>
                <div className="text-[14px] font-medium text-foreground">{s.title}</div>
                <div className="mt-0.5 text-[12px] text-muted-foreground">{s.desc}</div>
              </div>
            ))}
          </div>

          <InfoCard>
            <div className="mb-2 text-[14px] font-medium text-foreground">第一次调用</div>
            <CodeBlock
              code={`curl -X POST "${base}/api/skills/get_stock_quote/run" \\
  -H "X-API-Key: sk_YOUR_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"args": {"symbol": "000001"}, "format": "json"}'`}
              language="bash"
            />
            <p className="mt-2 text-[12px] text-muted-foreground">请求体固定为 <code className="font-mono">{'{ "args": {...}, "format": "json" }'}</code>: skill 参数一律放在 args 里。旧版表单式请求体已向后兼容, 但新接入请按此契约传参。</p>
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
              code: `curl -X POST "${base}/api/skills/get_stock_quote/run" \\
  -H "X-API-Key: sk_YOUR_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"args": {"symbol": "000001"}, "format": "json"}'`,
              badge: '推荐',
            },
            {
              title: 'JWT Token',
              desc: '网页端登录后自动携带，与 API Key 共享配额',
              code: `curl -X POST "${base}/api/skills/get_stock_quote/run" \\
  -H "Authorization: Bearer YOUR_JWT" \\
  -H "Content-Type: application/json" \\
  -d '{"args": {"symbol": "000001"}, "format": "json"}'`,
              badge: '网页',
            },
            {
              title: '游客模式',
              desc: '无需认证，仅限 free 级 Skill；当前配额见限流说明',
              code: `curl -X POST "${base}/api/skills/get_stock_quote/run" \\
  -H "Content-Type: application/json" \\
  -d '{"args": {"symbol": "000001"}, "format": "json"}'`,
              badge: '受限',
            },
          ].map((a, i) => (
            <InfoCard key={i}>
              <div className="mb-1.5 flex items-center gap-2">
                <span className="text-[14px] font-medium text-foreground">{a.title}</span>
                <span className="rounded bg-s3 px-1.5 py-0.5 text-[12px] font-mono text-primary">{a.badge}</span>
              </div>
              <p className="mb-2 text-[12px] text-muted-foreground">{a.desc}</p>
              <CodeBlock code={a.code} language="bash" />
            </InfoCard>
          ))}
        </div>
      </Section>

      {/* ── 限流说明 ── */}
      <Section id="sec-ratelimit" title="限流说明" description="各档位的调用限制" icon={<Gauge className="h-4 w-4" />}>
        {catLoading ? (
          <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
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
          <div className="overflow-x-auto rounded-lg bg-s2 p-4">
            <table className="w-full min-w-[560px] text-left text-[14px]">
              <thead>
                <tr className="border-b border-border  text-muted-foreground">
                  <th className="pb-2 pr-4 font-medium">档位</th>
                  <th className="pb-2 pr-4 font-medium">日限</th>
                  <th className="pb-2 pr-4 font-medium">突发</th>
                  <th className="pb-2 font-medium">说明</th>
                </tr>
              </thead>
              <tbody>
                {catalog?.guest && (
                  <tr className="border-b border-border">
                    <td className="py-2 pr-4"><span className="rounded bg-s2 px-1.5 py-0.5 text-[12px] text-muted-foreground">游客</span></td>
                    <td className="py-2 pr-4 font-mono text-foreground">{catalog.guest.daily_limit}</td>
                    <td className="py-2 pr-4 font-mono text-muted-foreground">-</td>
                    <td className="py-2 text-muted-foreground">仅 free 级 Skill，按 IP 限流</td>
                  </tr>
                )}
                {Object.entries(catalog?.tiers ?? {}).map(([tier, limit]) => (
                  <tr key={tier} className="border-b border-border">
                    <td className="py-2 pr-4">
                      <span className={`rounded px-1.5 py-0.5 text-[12px] ${TIER_BADGE[tier] || TIER_BADGE.free}`}>
                        {tier}
                      </span>
                    </td>
                    <td className="py-2 pr-4 font-mono text-foreground">{limit.daily_limit}</td>
                    <td className="py-2 pr-4 font-mono text-muted-foreground">{limit.burst ?? '-'}</td>
                    <td className="py-2 text-muted-foreground">
                      {tier === 'free' && '注册即得，基础配额'}
                      {tier === 'trial' && `试用 ${limit.days ?? 7} 天`}
                      {tier === 'pro' && '以当前实例授权为准'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="card-surface mt-3 rounded-lg bg-s2 p-4">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-role-watch" />
            <div className="text-[12px] text-muted-foreground">
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
          <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
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
          <div className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3"><Input aria-label="搜索 Skill 目录" type="search" placeholder="搜索接口名称或用途" value={catalogQuery} onChange={event => setCatalogQuery(event.target.value)} className="h-11 max-w-md text-[16px] sm:text-[14px]" /><span className="text-[12px] text-muted-foreground">{visibleSkills.length} / {skills.length} 个接口</span></div>
            {visibleSkills.length === 0 && <div className="rounded-lg bg-s2 p-5 text-[14px] text-muted-foreground">没有匹配的接口。<Button variant="ghost" className="ml-2 h-11" onClick={() => setCatalogQuery('')}>清除搜索</Button></div>}
            {visibleSkills.map(s => (
              <div key={s.name} className="card-surface rounded-lg bg-s2 p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <code className="font-mono text-[12px] text-primary">{s.name}</code>
                  <span className={`rounded px-1.5 py-0.5 text-[12px] ${TIER_BADGE[s.tier_min] || TIER_BADGE.free}`}>
                    {s.tier_min}
                  </span>
                  {s.slow && <span className="rounded bg-orange-500/10 px-1.5 py-0.5 text-[12px] text-orange-600 dark:text-orange-400">慢</span>}
                  {s.caliber && <CaliberBadge caliber={caliberOf(s.caliber)} className="mr-1.5 align-middle" />}{false && <span className="rounded bg-blue-500/10 px-1.5 py-0.5 text-[12px] text-blue-600 dark:text-blue-400">{s.caliber}</span>}
                </div>
                <p className="mt-1 text-[12px] text-muted-foreground">{s.description}</p>
                <Button variant="ghost" className="mt-2 h-11 px-0" onClick={() => { setSelectedSkill(s.name); setSearchParams(previous => { const next = new URLSearchParams(previous); next.set('section', 'sec-playground'); return next }) }}><Play size={14} />调试此接口</Button>
                {Object.keys(s.params || {}).length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {Object.entries(s.params).map(([k, v]) => (
                      <span key={k} className="rounded bg-s2  px-1.5 py-0.5 font-mono text-[12px] text-muted-foreground">
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
                <Label htmlFor="dev-auth" className="text-[12px] text-muted-foreground">鉴权方式</Label>
                <Select value={authMode} onValueChange={(v: any) => setAuthMode(v)}>
                  <SelectTrigger id="dev-auth" className="mt-1 h-11 text-[16px] sm:text-[14px]"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="apikey">API Key</SelectItem>
                    <SelectItem value="jwt">JWT（需登录）</SelectItem>
                    <SelectItem value="guest">游客（受限）</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              {authMode === 'apikey' && (
                <div>
                  <Label htmlFor="dev-api-key" className="text-[12px] text-muted-foreground">API Key</Label>
                  <Input
                    id="dev-api-key" type="password"
                    aria-label="API Key" className="mt-1 h-11 font-mono text-[16px] sm:text-[14px]"
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
                <Label htmlFor="dev-skill" className="text-[12px] text-muted-foreground">选择 Skill</Label>
                <Select value={selectedSkill} onValueChange={setSelectedSkill}>
                  <SelectTrigger id="dev-skill" className="mt-1 h-11 text-[16px] sm:text-[14px]"><SelectValue placeholder="请选择" /></SelectTrigger>
                  <SelectContent>
                    {skills.map(s => (
                      <SelectItem key={s.name} value={s.name}>{s.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              {selected && (
                <div className="flex items-end">
                  <p className="text-[12px] text-muted-foreground">{selected.description}</p>
                </div>
              )}
            </div>
            <div className="mt-3">
              <Label htmlFor="dev-params" className="text-[12px] text-muted-foreground">参数 (JSON)</Label>
              <textarea
                id="dev-params" aria-label="参数 JSON" className="mt-1 h-36 w-full rounded-lg border border-input bg-s2 p-3 font-mono text-[16px] text-foreground sm:text-[14px]"
                value={paramsJson}
                onChange={e => setParamsJson(e.target.value)}
                spellCheck={false}
              />
            </div>
            <div className="mt-3 flex justify-end">
              <Button size="sm" className="h-11" onClick={runSkill} disabled={running || !selectedSkill}>
                {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                运行
              </Button>
            </div>
          </InfoCard>

          {runError && <p role="alert" className="rounded-lg bg-s2 p-4 text-[14px] text-foreground">{runError}</p>}
          {runResult && (
            <InfoCard>
              <div className="mb-2 text-[14px] font-medium text-foreground">响应结果</div>
              <CodeBlock code={runResult} language="json" />
            </InfoCard>
          )}
        </div>
      </Section>

      {/* ── API 参考 ── */}
      <Section
        id="sec-api-ref"
        title="API 参考"
        description="全部 REST 端点一览"
        icon={<BookOpen className="h-4 w-4" />}
      >
        <div className="space-y-6">
          {API_GROUPS
            .map((g) => ({ ...g, apis: isOwner ? g.apis : g.apis.filter((a) => a.auth !== 'owner') }))
            .filter((g) => g.apis.length > 0)
            .map((group, gi) => (
            <div key={gi}>
              <h3 className="mb-3 text-[14px] font-semibold text-foreground">{group.title}</h3>
              <div className="overflow-x-auto rounded-lg bg-s2 p-4">
                <table className="w-full min-w-[560px] text-left text-[14px]">
                  <thead>
                    <tr className="border-b border-border text-muted-foreground">
                      <th className="pb-2 pr-3 font-medium">方法</th>
                      <th className="pb-2 pr-3 font-medium">路径</th>
                      <th className="pb-2 pr-3 font-medium">鉴权</th>
                      <th className="pb-2 font-medium">说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {group.apis.map((api, ai) => (
                      <tr key={ai} className="border-b border-border">
                        <td className="py-2 pr-3">
                          <span className={`font-mono text-[12px] font-semibold ${
                            api.method === 'GET' ? 'text-emerald-500' :
                            api.method === 'POST' ? 'text-blue-500' :
                            api.method === 'DELETE' ? 'text-red-500' : 'text-amber-500'
                          }`}>{api.method}</span>
                        </td>
                        <td className="py-2 pr-3 font-mono text-[12px] text-foreground">{api.path}</td>
                        <td className="py-2 pr-3">
                          <span className={`rounded px-1.5 py-0.5 text-[12px] ${
                            api.auth === '公开' ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400' :
                            api.auth === 'owner' ? 'bg-red-500/10 text-red-600 dark:text-red-400' :
                            'bg-blue-500/10 text-blue-600 dark:text-blue-400'
                          }`}>{api.auth}</span>
                        </td>
                        <td className="py-2 text-muted-foreground">{api.desc}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
          {!isOwner && (
            <p className="mt-3 text-[12px] text-muted-foreground">
              管理员级端点（用户管理、密钥与用量、密钥轮换、申请审核等）仅对管理员账号显示。
            </p>
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
          <div className="overflow-x-auto rounded-lg bg-s2 p-4">
            <table className="w-full min-w-[560px] text-left text-[14px]">
              <thead>
                <tr className="border-b border-border  text-muted-foreground">
                  <th className="pb-2 pr-4 font-medium">数据源</th>
                  <th className="pb-2 pr-4 font-medium">口径</th>
                  <th className="pb-2 font-medium">说明</th>
                </tr>
              </thead>
              <tbody>
                {DATA_SOURCES.map(s => (
                  <tr key={s.name} className="border-b border-border">
                    <td className="py-2 pr-4 text-foreground">{s.name}</td>
                    <td className="py-2 pr-4">
                      <span className="rounded bg-blue-500/10 px-1.5 py-0.5 font-mono text-[12px] text-blue-400">
                        {s.caliber}
                      </span>
                    </td>
                    <td className="py-2 text-muted-foreground">{s.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="card-surface rounded-lg bg-s2 p-4">
            <div className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-role-watch" />
              <div className="space-y-1 text-[12px] text-muted-foreground">
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
            <div key={e.code} className="card-surface rounded-lg bg-s2 p-4">
              <div className="flex items-center gap-2">
                <span className={`font-mono text-[14px] font-bold ${
                  e.code === 401 ? 'text-amber-600 dark:text-role-watch' : e.code === 403 ? 'text-orange-600 dark:text-orange-400' : e.code === 429 ? 'text-red-600 dark:text-red-400' : 'text-red-600 dark:text-red-500'
                }`}>{e.code}</span>
                <span className="text-[14px] font-medium text-foreground">{e.title}</span>
              </div>
              <p className="mt-1 text-[12px] text-muted-foreground">{e.desc}</p>
              <p className="mt-1 text-[12px] text-foreground">→ {e.fix}</p>
            </div>
          ))}
        </div>

        <div className="mt-4">
          <div className="mb-2 text-[14px] font-medium text-foreground">429 退避示例</div>
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
      <Section id="sec-selfhost" title="开源与部署" description="社区开源版与当前实例分别维护" icon={<GithubMark size={18} />}>
        <div className="space-y-5">
          <div className="rounded-lg bg-s2 p-5"><h3 className="text-[16px] font-semibold">SIDA Pro 项目仓库</h3><p className="mt-2 text-[14px] leading-7 text-muted-foreground">当前项目的源码、版本记录与问题反馈，以项目仓库及其许可说明为准。</p><a href={PROJECT_REPOSITORY} target="_blank" rel="noopener noreferrer" className="mt-3 inline-flex min-h-11 max-w-full items-center gap-2 break-all text-[14px] font-medium"><GithubMark size={16} />github.com/xiaoze-hub/sida-pro<ArrowUpRight size={16} className="shrink-0" /></a></div>
          <div className="flex flex-wrap items-center gap-2"><StatusChip>社区版</StatusChip><StatusChip>AGPL-3.0</StatusChip></div>
          <h3 className="text-[20px] font-semibold">在自己的环境中部署 SIDA</h3>
          <p className="text-[14px] leading-7 text-muted-foreground">免费获取社区版源码，按仓库说明部署、配置模型和数据源。社区版功能与版本以仓库为准；本页 Skill API 文档对应当前实例，接入前请核对目标服务的接口目录。</p>
          <div className="rounded-lg bg-s2 p-5"><a href={COMMUNITY_REPOSITORY} target="_blank" rel="noopener noreferrer" className="inline-flex max-w-full items-start gap-2 break-all text-[14px] font-medium"><GithubMark size={18} className="mt-0.5 shrink-0" />xiaoze-hub/Stock-Intelligent-Data-Analytics<ArrowUpRight size={16} className="shrink-0" /></a></div>
          <CodeBlock code={`git clone ${COMMUNITY_REPOSITORY}.git`} language="bash" />
          <div className="flex flex-wrap gap-2"><Button asChild className="h-11"><a href={COMMUNITY_DEPLOYMENT} target="_blank" rel="noopener noreferrer"><Server size={16} />查看部署说明<ArrowUpRight size={16} /></a></Button><Button asChild variant="ghost" className="h-11"><a href={COMMUNITY_LICENSE} target="_blank" rel="noopener noreferrer">阅读开源许可<ArrowUpRight size={16} /></a></Button><Button asChild variant="ghost" className="h-11"><Link to="/tiers">查看账户方案</Link></Button></div>
          <p className="text-[12px] leading-6 text-muted-foreground">自行部署需准备运行环境，模型调用和外部数据服务可能需要单独配置与承担费用。当前 Pro 实例遵循自己的许可和权限规则。</p>
        </div>
      </Section>
    </DevPageLayout>
    </PublicSiteFrame>
  )
}
