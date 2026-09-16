import { useCallback, useMemo, useState } from 'react'
import {
  KeyRound,
  Plus,
  Copy,
  Check,
  RefreshCw,
  Trash2,
  ChevronDown,
  ChevronRight,
  AlertTriangle,
  Terminal,
  FileJson,
  Eye,
  EyeOff,
  Loader2,
} from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { formatDateTime } from '@/lib/utils'
import { useApiQuery } from '@/hooks/useApiQuery'

/**
 * API Key 控制台(2026-09-16)。
 * - 列出当前用户全部 Key(prefix/tier/status/创建/最后使用)
 * - 操作: 复制(仅明文) / 重置 / 删除(均二次确认)
 * - 用量: 展开查看今日/本周/本月
 * - 智能体一键安装: curl 命令 + 配置 JSON, 一键复制
 * 明文 key 只在创建/重置响应出现一次, 组件 state 暂存, 刷新即丢。
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
  note?: string
}

interface SkillsConfig {
  base_url: string
  api_key_prefix: string | null
  api_key: string | null
  tier: string
  daily_limit: number
  skills: string[]
  endpoint: string
  usage_endpoint: string
  install_command: string
  docs_url: string
}

const TIER_LABEL: Record<string, string> = { free: '免费', trial: '试用', pro: 'Pro' }
const STATUS_LABEL: Record<string, string> = {
  active: '正常',
  frozen: '已冻结',
  disabled: '已禁用',
}

const PUBLIC_BASE = 'https://www.sida.hengsheng-elec.com'

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* fall through */
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.left = '-9999px'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

function tierBadgeClass(tier: string): string {
  if (tier === 'pro') return 'bg-amber-500/15 text-amber-700 dark:text-amber-400 border-amber-500/30'
  if (tier === 'trial') return 'bg-primary/10 text-primary border-primary/30'
  return 'bg-accent text-muted-foreground border-border/60'
}

function statusBadgeClass(status: string): string {
  if (status === 'active') return 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/30'
  if (status === 'frozen') return 'bg-amber-500/10 text-amber-700 dark:text-amber-400 border-amber-500/30'
  return 'bg-destructive/10 text-destructive border-destructive/30'
}

export default function ApiKeysPage() {
  const { toast } = useToast()
  const {
    data: keysResp,
    isLoading,
    error,
    refetch,
  } = useApiQuery<{ keys: MyApiKey[] }>(['api-keys', 'my'], '/keys/my')

  const { data: skillsConfig } = useApiQuery<SkillsConfig>(['api-keys', 'skills-config'], '/skills/config')

  const keys = keysResp?.keys ?? []

  // 明文 key: 创建/重置后暂存, 只显示一次
  const [plaintext, setPlaintext] = useState<{ keyId: number; value: string } | null>(null)
  const [showPlaintext, setShowPlaintext] = useState(true)
  const [creating, setCreating] = useState(false)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [usageMap, setUsageMap] = useState<Record<number, KeyUsage>>({})
  const [loadingUsage, setLoadingUsage] = useState<number | null>(null)
  const [copied, setCopied] = useState<string | null>(null)

  const markCopied = (id: string) => {
    setCopied(id)
    window.setTimeout(() => setCopied(c => (c === id ? null : c)), 1600)
  }

  const doCopy = async (text: string, id: string, tip = '已复制') => {
    const ok = await copyText(text)
    if (ok) {
      markCopied(id)
      toast(tip, 'success')
    } else {
      toast('复制失败, 请手动选中复制', 'error')
    }
  }

  const loadUsage = useCallback(async (keyId: number) => {
    setLoadingUsage(keyId)
    try {
      const u = await fetchAPI<KeyUsage>(`/keys/my/${keyId}/usage`, { cacheMode: 'reload' })
      setUsageMap(m => ({ ...m, [keyId]: u }))
    } catch (e) {
      toast(e instanceof Error ? e.message : '加载用量失败', 'error')
    } finally {
      setLoadingUsage(null)
    }
  }, [toast])

  const toggleExpand = (keyId: number) => {
    if (expandedId === keyId) {
      setExpandedId(null)
      return
    }
    setExpandedId(keyId)
    if (!usageMap[keyId]) void loadUsage(keyId)
  }

  const handleCreate = async () => {
    setCreating(true)
    try {
      const r = await fetchAPI<CreateKeyResp>('/keys/my', {
        method: 'POST',
        body: JSON.stringify({ trial: false }),
      })
      setPlaintext({ keyId: r.key.id, value: r.api_key })
      setShowPlaintext(true)
      toast('Key 已创建, 请立即保存明文(只显示一次)', 'success')
      await refetch()
    } catch (e) {
      toast(e instanceof Error ? e.message : '创建失败', 'error')
    } finally {
      setCreating(false)
    }
  }

  const handleReset = async (k: MyApiKey) => {
    if (!window.confirm(`确认重置 Key ${k.key_prefix}...？旧 Key 将立即失效, 且只能看到一次新明文。`)) return
    setBusyId(k.id)
    try {
      const r = await fetchAPI<CreateKeyResp>(`/keys/my/${k.id}/reset`, { method: 'POST' })
      setPlaintext({ keyId: r.key.id, value: r.api_key })
      setShowPlaintext(true)
      toast('Key 已重置, 请立即保存新明文(只显示一次)', 'success')
      await refetch()
    } catch (e) {
      toast(e instanceof Error ? e.message : '重置失败', 'error')
    } finally {
      setBusyId(null)
    }
  }

  const handleDelete = async (k: MyApiKey) => {
    if (!window.confirm(`确认删除 Key ${k.key_prefix}...？删除后无法恢复, 使用该 Key 的调用将立即 401。`)) return
    setBusyId(k.id)
    try {
      await fetchAPI(`/keys/my/${k.id}`, { method: 'DELETE' })
      if (plaintext?.keyId === k.id) setPlaintext(null)
      if (expandedId === k.id) setExpandedId(null)
      toast('Key 已删除', 'success')
      await refetch()
    } catch (e) {
      toast(e instanceof Error ? e.message : '删除失败', 'error')
    } finally {
      setBusyId(null)
    }
  }

  // 智能体安装: 优先用当前展示的明文, 否则占位
  const activePlaintext = plaintext?.value || ''
  const installCommand = useMemo(() => {
    const key = activePlaintext || 'YOUR_API_KEY'
    return `curl -sL ${PUBLIC_BASE}/api/skills/install.sh | bash -s -- ${key}`
  }, [activePlaintext])

  const configJson = useMemo(() => {
    const skills = skillsConfig?.skills?.length
      ? skillsConfig.skills
      : ['get_stock_quote', 'get_technical_analysis', 'get_main_intent', 'get_capital_flow', 'get_market_news']
    return JSON.stringify(
      {
        base_url: PUBLIC_BASE,
        api_key: activePlaintext || 'sk_xxxx',
        skills,
      },
      null,
      2,
    )
  }, [activePlaintext, skillsConfig?.skills])

  const bestPrefix = useMemo(() => {
    if (plaintext) {
      const hit = keys.find(k => k.id === plaintext.keyId)
      if (hit) return hit.key_prefix
    }
    const active = keys.find(k => k.status === 'active')
    return active?.key_prefix || keys[0]?.key_prefix || null
  }, [keys, plaintext])

  if (isLoading && !keysResp) {
    return (
      <div className="w-full h-[60vh] flex items-center justify-center">
        <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  return (
    <div className="sida-page-enter">
      {/* 页头 */}
      <div className="relative overflow-hidden border-b border-border/40 p-5 md:p-7">
        <div className="relative flex flex-col md:flex-row md:items-end md:justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-[16px] md:text-[18px] font-bold text-foreground flex items-center gap-2">
              <KeyRound className="w-5 h-5 text-primary" />
              API Key 控制台
            </h1>
            <p className="text-[12px] text-muted-foreground mt-1">
              管理 Skill Gateway 密钥 · 查看用量 · 一键安装到智能体
            </p>
          </div>
          <Button size="sm" className="h-8 shrink-0" onClick={handleCreate} disabled={creating}>
            {creating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
            创建新 Key
          </Button>
        </div>
      </div>

      <div className="mt-6 space-y-6">
        {/* 明文展示(只显示一次) */}
        {plaintext && (
          <section className="rounded-md border border-amber-500/40 bg-amber-500/10 p-4">
            <div className="flex items-start gap-2 mb-3">
              <AlertTriangle className="w-4 h-4 text-amber-600 dark:text-amber-500 mt-0.5 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-semibold text-amber-800 dark:text-amber-300">
                  请立即保存 API Key — 此明文只显示一次
                </div>
                <div className="text-[11px] text-amber-700/80 dark:text-amber-400/80 mt-0.5">
                  服务端只存 hash, 关闭或刷新本页后将无法再次查看完整 Key。
                </div>
              </div>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 shrink-0"
                onClick={() => setPlaintext(null)}
                aria-label="关闭"
              >
                <EyeOff className="w-3.5 h-3.5" />
              </Button>
            </div>
            <div className="flex items-center gap-2">
              <code className="flex-1 min-w-0 rounded border border-amber-500/30 bg-background/80 px-3 py-2 font-mono text-[12px] text-foreground break-all">
                {showPlaintext ? plaintext.value : `${plaintext.value.slice(0, 11)}${'•'.repeat(24)}`}
              </code>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 shrink-0"
                onClick={() => setShowPlaintext(v => !v)}
                aria-label={showPlaintext ? '隐藏' : '显示'}
              >
                {showPlaintext ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
              </Button>
              <Button
                size="sm"
                className="h-8 shrink-0"
                onClick={() => doCopy(plaintext.value, 'plaintext', 'API Key 已复制')}
              >
                {copied === 'plaintext' ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                复制
              </Button>
            </div>
          </section>
        )}

        {/* Key 列表 */}
        <section className="border-t border-border/40 pt-4 md:pt-5">
          <div className="flex items-center gap-2 mb-4">
            <KeyRound className="w-4 h-4 text-primary" />
            <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">我的 API Key</h3>
            <span className="text-[11px] text-muted-foreground">({keys.length})</span>
          </div>

          {error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-[12px] text-destructive mb-3">
              加载失败: {error instanceof Error ? error.message : '未知错误'}
              <button type="button" className="ml-2 underline" onClick={() => void refetch()}>
                重试
              </button>
            </div>
          )}

          {keys.length === 0 && !error ? (
            <div className="rounded-md border border-border/40 bg-accent/20 p-8 text-center">
              <KeyRound className="w-8 h-8 text-muted-foreground mx-auto mb-3 opacity-50" />
              <div className="text-[13px] text-foreground mb-1">还没有 API Key</div>
              <div className="text-[12px] text-muted-foreground mb-4">
                创建后即可通过 Skill Gateway 调用行情/技术/资金等开放能力
              </div>
              <Button size="sm" className="h-8" onClick={handleCreate} disabled={creating}>
                {creating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
                创建第一个 Key
              </Button>
            </div>
          ) : (
            <div className="space-y-2">
              {keys.map(k => {
                const expanded = expandedId === k.id
                const usage = usageMap[k.id]
                const busy = busyId === k.id
                return (
                  <div key={k.id} className="rounded-md border border-border/40 bg-accent/15 overflow-hidden">
                    {/* 主行 */}
                    <div className="flex flex-wrap items-center gap-2 md:gap-3 p-3.5">
                      <button
                        type="button"
                        className="flex items-center gap-1 text-muted-foreground shrink-0"
                        onClick={() => toggleExpand(k.id)}
                        aria-expanded={expanded}
                      >
                        {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                      </button>

                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 flex-wrap">
                          <code className="font-mono text-[13px] text-foreground font-medium">
                            {k.key_prefix}...
                          </code>
                          <Badge variant="outline" className={`text-[10px] h-5 ${tierBadgeClass(k.tier)}`}>
                            {TIER_LABEL[k.tier] || k.tier}
                          </Badge>
                          <Badge variant="outline" className={`text-[10px] h-5 ${statusBadgeClass(k.status)}`}>
                            {STATUS_LABEL[k.status] || k.status}
                          </Badge>
                          {k.frozen_reason && (
                            <span className="text-[10px] text-amber-600 dark:text-amber-500 truncate max-w-[180px]">
                              {k.frozen_reason}
                            </span>
                          )}
                        </div>
                        <div className="text-[11px] text-muted-foreground mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                          <span>日限 {k.daily_limit}</span>
                          <span>创建 {k.created_at ? formatDateTime(k.created_at) : '--'}</span>
                          <span>最后使用 {k.last_used_at ? formatDateTime(k.last_used_at) : '从未'}</span>
                        </div>
                      </div>

                      <div className="flex items-center gap-1 shrink-0">
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-[11px]"
                          title="复制完整 Key(需先创建/重置获取明文)"
                          disabled={!plaintext || plaintext.keyId !== k.id}
                          onClick={() => plaintext && plaintext.keyId === k.id && doCopy(plaintext.value, `copy-${k.id}`)}
                        >
                          {copied === `copy-${k.id}` ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                          复制
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-[11px]"
                          disabled={busy}
                          onClick={() => void handleReset(k)}
                        >
                          {busy ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : (
                            <RefreshCw className="w-3 h-3" />
                          )}
                          重置
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 px-2 text-[11px] text-destructive hover:text-destructive"
                          disabled={busy}
                          onClick={() => void handleDelete(k)}
                        >
                          <Trash2 className="w-3 h-3" />
                          删除
                        </Button>
                      </div>
                    </div>

                    {/* 用量展开 */}
                    {expanded && (
                      <div className="border-t border-border/40 bg-background/40 px-3.5 py-3">
                        {loadingUsage === k.id && !usage ? (
                          <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
                            <Loader2 className="w-3.5 h-3.5 animate-spin" /> 加载用量…
                          </div>
                        ) : usage ? (
                          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                            {[
                              { label: '今日', value: usage.used_today, sub: `剩余 ${usage.remaining_today}` },
                              { label: '近 7 天', value: usage.used_7d, sub: '本周用量' },
                              { label: '近 30 天', value: usage.used_30d, sub: '本月用量' },
                              {
                                label: '日配额',
                                value: usage.daily_limit,
                                sub: TIER_LABEL[usage.tier] || usage.tier,
                              },
                            ].map(tile => (
                              <div key={tile.label} className="rounded border border-border/40 bg-accent/20 p-2.5">
                                <div className="text-[10px] text-muted-foreground">{tile.label}</div>
                                <div className="text-[16px] font-semibold text-foreground mt-0.5">{tile.value}</div>
                                <div className="text-[10px] text-muted-foreground mt-0.5">{tile.sub}</div>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <button
                            type="button"
                            className="text-[12px] text-primary hover:underline"
                            onClick={() => void loadUsage(k.id)}
                          >
                            加载用量
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </section>

        {/* 智能体一键安装 */}
        <section className="border-t border-border/40 pt-4 md:pt-5">
          <div className="flex items-center gap-2 mb-1">
            <Terminal className="w-4 h-4 text-primary" />
            <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">智能体安装</h3>
          </div>
          <p className="text-[11px] text-muted-foreground mb-4">
            将 API Key 配置到本机环境, 供 Claude / Cursor / 自研智能体直接调用 SIDA Skill Gateway。
            {bestPrefix ? (
              <>
                {' '}
                当前 Key 前缀 <code className="font-mono">{bestPrefix}...</code>
                {plaintext ? ' (下方命令已填入明文, 可直接复制)' : ' (创建/重置后可自动填入明文)'}
              </>
            ) : (
              ' 请先创建一个 API Key。'
            )}
          </p>

          <div className="space-y-4">
            {/* curl 一键安装 */}
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <Labelish>一键安装命令</Labelish>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-[11px]"
                  onClick={() => doCopy(installCommand, 'install', '安装命令已复制')}
                >
                  {copied === 'install' ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                  复制命令
                </Button>
              </div>
              <pre className="overflow-x-auto rounded-md border border-border/40 bg-foreground/[0.03] p-3 font-mono text-[11px] leading-5 text-foreground whitespace-pre-wrap break-all">
                {installCommand}
              </pre>
              <p className="text-[10px] text-muted-foreground mt-1">
                脚本会写入 ~/.bashrc 与 ~/.zshrc 的 SIDA_API_KEY / SIDA_BASE_URL, 并自动校验 Key 有效性。
              </p>
            </div>

            {/* 配置 JSON */}
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <Labelish>
                  <FileJson className="w-3.5 h-3.5 inline -mt-0.5 mr-1" />
                  配置 JSON
                </Labelish>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-[11px]"
                  onClick={() => doCopy(configJson, 'config', '配置 JSON 已复制')}
                >
                  {copied === 'config' ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                  复制 JSON
                </Button>
              </div>
              <pre className="overflow-x-auto rounded-md border border-border/40 bg-foreground/[0.03] p-3 font-mono text-[11px] leading-5 text-foreground max-h-64">
                {configJson}
              </pre>
            </div>

            {/* 可用 skill 提示 */}
            {skillsConfig?.skills?.length ? (
              <div className="rounded-md border border-border/40 bg-accent/15 p-3">
                <div className="text-[11px] text-muted-foreground mb-2">
                  当前档位可用 skill({skillsConfig.skills.length}):
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {skillsConfig.skills.map(s => (
                    <code
                      key={s}
                      className="rounded bg-background/60 border border-border/40 px-1.5 py-0.5 font-mono text-[10px] text-foreground"
                    >
                      {s}
                    </code>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        </section>
      </div>
    </div>
  )
}

function Labelish({ children }: { children: React.ReactNode }) {
  return <span className="text-[11px] font-medium text-foreground">{children}</span>
}
