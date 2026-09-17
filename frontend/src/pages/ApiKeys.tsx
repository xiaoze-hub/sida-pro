import { useMemo, useState } from 'react'
import {
  KeyRound,
  Plus,
  Copy,
  RefreshCw,
  Trash2,
  AlertTriangle,
  Terminal,
  FileJson,
  Loader2,
  BarChart3,
  ShieldCheck,
  Zap,
} from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { formatDateTime } from '@/lib/utils'
import { useApiQuery } from '@/hooks/useApiQuery'
import { DevPageLayout, Section, InfoCard, CodeBlock, EmptyState, type SideMenuItem } from '@/components/dev/DevPageLayout'
import { ErrorState } from '@/components/ErrorState'
import { SkeletonTable } from '@/components/Skeleton'
import { useI18n } from '@/hooks/useI18n'

/**
 * API Key 控制台 v2(2026-09-16)。
 * 侧边栏导航 + 章节化布局 + 用量统计 + 智能体安装。
 */

interface MyApiKey {
  id: number
  key_prefix: string
  owner_label: string
  tier: string
  status: string
  daily_limit: number
  frozen_reason: string
  expires_at: string | null
  created_at: string | null
  last_used_at: string | null
}

interface KeyUsage {
  key_id: number
  key_prefix: string
  tier: string
  daily_limit: number
  used_today: number
  remaining_today: number
  used_7d: number
  used_30d: number
}

interface CreateKeyResp {
  api_key: string
  key: MyApiKey
}

const TIER_LABEL: Record<string, string> = { free: '免费', trial: '试用', pro: 'Pro' }
const TIER_COLOR: Record<string, string> = {
  free: 'bg-slate-500/10 text-slate-400 border-slate-500/20',
  trial: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  pro: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
}

const MENU: SideMenuItem[] = [
  { id: 'keys', label: '我的 Key', icon: <KeyRound className="h-3.5 w-3.5" />, anchor: 'sec-keys' },
  { id: 'create', label: '创建 Key', icon: <Plus className="h-3.5 w-3.5" />, anchor: 'sec-create' },
  { id: 'usage', label: '用量统计', icon: <BarChart3 className="h-3.5 w-3.5" />, anchor: 'sec-usage' },
  { id: 'agent', label: '智能体安装', icon: <Terminal className="h-3.5 w-3.5" />, anchor: 'sec-agent' },
]

export default function ApiKeysPage() {
  const { toast } = useToast()
  const { t } = useI18n()
  const [activeSection, setActiveSection] = useState('keys')
  const [plaintext, setPlaintext] = useState<string | null>(null)
  const [expandedUsage, setExpandedUsage] = useState<number | null>(null)
  const [creating, setCreating] = useState(false)
  const [resettingId, setResettingId] = useState<number | null>(null)
  const [deletingId, setDeletingId] = useState<number | null>(null)

  const { data: keys, isLoading: keysLoading, isError: keysError, refetch } = useApiQuery<MyApiKey[]>(['my-api-keys'], '/keys/my')
  const keyList = useMemo((): MyApiKey[] => {
    if (Array.isArray(keys)) return keys
    if (keys && typeof keys === 'object') {
      const k = keys as any
      if (Array.isArray(k.data)) return k.data
      if (Array.isArray(k.keys)) return k.keys
      if (Array.isArray(k.items)) return k.items
    }
    return []
  }, [keys])

  // ── 操作 ──
  const createKey = async () => {
    setCreating(true)
    try {
      const r = await fetchAPI<CreateKeyResp>('/keys/my', { method: 'POST', body: JSON.stringify({}) })
      setPlaintext(r.api_key)
      toast('Key 已创建，请立即复制保存', 'success')
      void refetch()
    } catch (e: any) {
      toast(e?.message || '创建失败', 'error')
    } finally {
      setCreating(false)
    }
  }

  const resetKey = async (id: number) => {
    if (!confirm('重置后旧 Key 立即失效，确定继续？')) return
    setResettingId(id)
    try {
      const r = await fetchAPI<CreateKeyResp>(`/keys/my/${id}/reset`, { method: 'POST' })
      setPlaintext(r.api_key)
      toast('Key 已重置，请复制新 Key', 'success')
      void refetch()
    } catch (e: any) {
      toast(e?.message || '重置失败', 'error')
    } finally {
      setResettingId(null)
    }
  }

  const deleteKey = async (id: number) => {
    if (!confirm('删除后该 Key 立即失效，确定删除？')) return
    setDeletingId(id)
    try {
      await fetchAPI(`/keys/my/${id}`, { method: 'DELETE' })
      toast('Key 已删除', 'success')
      void refetch()
    } catch (e: any) {
      toast(e?.message || '删除失败', 'error')
    } finally {
      setDeletingId(null)
    }
  }

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text)
    toast('已复制', 'success')
  }

  // ── 智能体安装命令 ──
  const installCmd = useMemo(() => {
    const key = plaintext || 'YOUR_API_KEY'
    return `curl -sL ${window.location.origin}/api/skills/install.sh | bash -s -- ${key}`
  }, [plaintext])

  const configJson = useMemo(() => {
    return JSON.stringify({
      base_url: window.location.origin,
      api_key: plaintext || 'sk_...',
      skills: ['get_stock_quote', 'get_technical_analysis', 'get_capital_flow', 'get_market_news'],
    }, null, 2)
  }, [plaintext])

  return (
    <DevPageLayout
      title={t('apiKeys.title')}
      subtitle={t('apiKeys.subtitle')}
      badge={`${keyList.length} 个 Key`}
      menu={MENU}
      activeId={activeSection}
      onNav={setActiveSection}
      headerExtra={
        <Button size="sm" className="h-8 shrink-0" onClick={createKey} disabled={creating}>
          {creating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
          <span className="hidden sm:inline">创建 Key</span>
        </Button>
      }
    >
      {/* 明文 Key 提示 */}
      {plaintext && (
        <div className="mb-6 rounded-xl border border-amber-600/30 dark:border-amber-500/30 bg-amber-500/5 p-4">
          <div className="mb-2 flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-400" />
            <span className="text-[13px] font-semibold text-amber-700 dark:text-amber-300">请立即复制保存你的 API Key</span>
          </div>
          <p className="mb-3 text-[11px] text-amber-700/80 dark:text-amber-400/70">此 Key 仅显示一次，关闭或刷新后将无法再次查看。</p>
          <div className="flex items-center gap-2">
            <code className="flex-1 overflow-x-auto rounded-lg bg-muted/60 dark:bg-black/30 px-3 py-2 font-mono text-[12px] text-primary dark:text-cyan-300">
              {plaintext}
            </code>
            <Button size="sm" variant="outline" className="h-8 shrink-0" onClick={() => copyText(plaintext)}>
              <Copy className="h-3.5 w-3.5" />
            </Button>
            <Button size="sm" variant="ghost" className="h-8 shrink-0" onClick={() => setPlaintext(null)}>
              我已保存
            </Button>
          </div>
        </div>
      )}

      {/* ── 我的 Key ── */}
      <Section
        id="sec-keys"
        title="我的 API Key"
        description="每个 Key 独立计量，可随时重置或删除"
        icon={<KeyRound className="h-4 w-4" />}
      >
        {keysLoading ? (
          <SkeletonTable rows={3} />
        ) : keysError ? (
          <ErrorState
            type="server"
            title="API Key 列表加载失败"
            onRetry={() => void refetch()}
            compact
          />
        ) : keyList.length === 0 ? (
          <EmptyState
            icon={<KeyRound className="h-8 w-8" />}
            title="还没有 API Key"
            desc="创建一个 Key 即可开始调用 Skill API"
            compact
            action={
              <Button size="sm" onClick={createKey} disabled={creating}>
                <Plus className="h-3.5 w-3.5" /> 创建 Key
              </Button>
            }
          />
        ) : (
          <div className="space-y-2">
            {keyList.map(k => (
              <div key={k.id} className="rounded-xl border border-border/50 dark:border-white/5 bg-card/60 dark:bg-white/[0.02] p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <code className="font-mono text-[13px] text-primary dark:text-cyan-300">{k.key_prefix}...</code>
                      <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] ${TIER_COLOR[k.tier] || TIER_COLOR.free}`}>
                        {TIER_LABEL[k.tier] || k.tier}
                      </span>
                      {k.status !== 'active' && (
                        <span className="inline-flex items-center rounded-full bg-red-500/10 border border-red-500/20 px-2 py-0.5 text-[10px] text-red-600 dark:text-red-400">
                          {k.status === 'frozen' ? '已冻结' : '已禁用'}
                        </span>
                      )}
                    </div>
                    <div className="mt-1 flex flex-wrap gap-3 text-[10px] text-muted-foreground dark:text-slate-600">
                      <span>日限 {k.daily_limit}</span>
                      {k.created_at && <span>创建 {formatDateTime(k.created_at)}</span>}
                      {k.last_used_at && <span>最后使用 {formatDateTime(k.last_used_at)}</span>}
                      {k.expires_at && <span>到期 {formatDateTime(k.expires_at)}</span>}
                    </div>
                    {k.frozen_reason && (
                      <p className="mt-1 text-[10px] text-red-600/80 dark:text-red-400/70">冻结原因: {k.frozen_reason}</p>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    <Button size="sm" variant="outline" className="h-7 px-2 text-[11px]" onClick={() => setExpandedUsage(expandedUsage === k.id ? null : k.id)}>
                      <BarChart3 className="h-3 w-3" /> 用量
                    </Button>
                    <Button size="sm" variant="outline" className="h-7 px-2 text-[11px]" onClick={() => resetKey(k.id)} disabled={resettingId === k.id}>
                      {resettingId === k.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />} 重置
                    </Button>
                    <Button size="sm" variant="outline" className="h-7 px-2 text-[11px] hover:text-red-400 hover:border-red-500/30" onClick={() => deleteKey(k.id)} disabled={deletingId === k.id}>
                      {deletingId === k.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />} 删除
                    </Button>
                  </div>
                </div>
                {/* 用量展开 */}
                {expandedUsage === k.id && <KeyUsagePanel keyId={k.id} />}
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* ── 用量统计 ── */}
      <Section
        id="sec-usage"
        title="用量总览"
        description="所有 Key 的汇总用量"
        icon={<BarChart3 className="h-4 w-4" />}
      >
        <UsageOverview keys={keyList} />
      </Section>

      {/* ── 智能体安装 ── */}
      <Section
        id="sec-agent"
        title="智能体一键接入"
        description="将 SIDA Skill 集成到你的 AI 助手中"
        icon={<Terminal className="h-4 w-4" />}
      >
        <div className="space-y-4">
          <InfoCard>
            <div className="mb-2 flex items-center gap-2">
              <Zap className="h-4 w-4 text-primary dark:text-cyan-400" />
              <span className="text-[13px] font-medium text-foreground dark:text-white">方式一：Shell 脚本</span>
            </div>
            <p className="mb-3 text-[11px] text-muted-foreground dark:text-slate-500">复制以下命令到终端执行，自动配置环境变量和客户端。</p>
            <CodeBlock code={installCmd} language="bash" />
          </InfoCard>

          <InfoCard>
            <div className="mb-2 flex items-center gap-2">
              <FileJson className="h-4 w-4 text-primary dark:text-cyan-400" />
              <span className="text-[13px] font-medium text-foreground dark:text-white">方式二：配置 JSON</span>
            </div>
            <p className="mb-3 text-[11px] text-muted-foreground dark:text-slate-500">适用于支持 JSON 配置的 AI 框架（LangChain、AutoGPT 等）。</p>
            <CodeBlock code={configJson} language="json" />
          </InfoCard>

          <InfoCard>
            <div className="mb-2 flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-primary dark:text-cyan-400" />
              <span className="text-[13px] font-medium text-foreground dark:text-white">安全提示</span>
            </div>
            <ul className="space-y-1 text-[11px] text-muted-foreground dark:text-slate-500">
              <li>• 不要将 API Key 提交到代码仓库</li>
              <li>• 不要在客户端 JavaScript 中暴露 Key</li>
              <li>• 怀疑 Key 泄露时立即重置</li>
              <li>• 生产环境建议使用 Pro 档位获得更高配额</li>
            </ul>
          </InfoCard>
        </div>
      </Section>
    </DevPageLayout>
  )
}

/* ── 单 Key 用量面板 ── */
function KeyUsagePanel({ keyId }: { keyId: number }) {
  const { data: usage } = useApiQuery<KeyUsage>(['key-usage', String(keyId)], `/keys/my/${keyId}/usage`)
  if (!usage) return <div className="mt-3 text-[11px] text-slate-600">加载用量中...</div>

  const pct = usage.daily_limit > 0 ? Math.round((usage.used_today / usage.daily_limit) * 100) : 0
  return (
    <div className="mt-3 grid grid-cols-2 gap-3 border-t border-white/5 pt-3 sm:grid-cols-4">
      <div>
        <div className="text-[10px] text-slate-600">今日已用</div>
        <div className="font-mono text-[16px] font-semibold text-white">{usage.used_today}</div>
        <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-white/5">
          <div className="h-full rounded-full bg-cyan-500 transition-all" style={{ width: `${Math.min(pct, 100)}%` }} />
        </div>
      </div>
      <div>
        <div className="text-[10px] text-slate-600">今日剩余</div>
        <div className="font-mono text-[16px] font-semibold text-cyan-400">{usage.remaining_today}</div>
      </div>
      <div>
        <div className="text-[10px] text-slate-600">近 7 天</div>
        <div className="font-mono text-[16px] font-semibold text-white">{usage.used_7d}</div>
      </div>
      <div>
        <div className="text-[10px] text-slate-600">近 30 天</div>
        <div className="font-mono text-[16px] font-semibold text-white">{usage.used_30d}</div>
      </div>
    </div>
  )
}

/* ── 用量总览 ── */
function UsageOverview({ keys }: { keys: MyApiKey[] }) {
  const activeKeys = keys.filter(k => k.status === 'active')
  const totalLimit = activeKeys.reduce((s, k) => s + k.daily_limit, 0)

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <InfoCard>
        <div className="text-[10px] text-slate-600">活跃 Key</div>
        <div className="font-mono text-[20px] font-semibold text-white">{activeKeys.length}</div>
      </InfoCard>
      <InfoCard>
        <div className="text-[10px] text-slate-600">总日限</div>
        <div className="font-mono text-[20px] font-semibold text-cyan-400">{totalLimit}</div>
      </InfoCard>
      <InfoCard>
        <div className="text-[10px] text-slate-600">已冻结</div>
        <div className="font-mono text-[20px] font-semibold text-amber-400">{keys.filter(k => k.status === 'frozen').length}</div>
      </InfoCard>
      <InfoCard>
        <div className="text-[10px] text-slate-600">已禁用</div>
        <div className="font-mono text-[20px] font-semibold text-red-400">{keys.filter(k => k.status === 'disabled').length}</div>
      </InfoCard>
    </div>
  )
}
