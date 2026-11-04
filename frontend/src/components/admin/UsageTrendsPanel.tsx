/**
 * 管理后台 · API Key 用量监控增强视图(2026-11-04)。
 *
 * 数据源: `GET /admin/analytics/usage-trends?days=7|30`(管理面鉴权走 fetchAPI 既有 JWT/Cookie)。
 * 覆盖三件事:
 *   ① 实时 QPS(10 秒 / 60 秒均值)与窗口峰值;
 *   ② 每日新增 Key 与全局总调用走势;
 *   ③ Top Skills(后端给中文 display_name)与 Top Client IP。
 *
 * 诚实性约束(见 docs/UI_GUIDELINES §6):
 *   - 后端字段缺失(`qps.peak_per_sec` / `daily_trend` / `top_ips` 在旧版本不存在)一律显示 `--` 并说明原因,
 *     **不补 0** —— 0 是"确实没有调用", 缺字段是"我们不知道", 两者不能混。
 *   - 后端显式给的 0 照实显示(那是真实读数), 只有 null/undefined 才降级成 `--`。
 *   - 刷新失败但仍有上次成功数据时, 保留数据并标注"本次刷新失败", 不清空成错误页。
 *
 * 字段名以 `src/web/api/skills_gateway.py::get_admin_usage_analytics` 的真实响应为准,
 * 契约由 `tests/fixtures/usage-trends.sample.json`(后端实测 payload)钉住
 * (前端 `admin-usage-trends-contract.test.tsx` + 后端 `test_usage_trends_contract.py`)。
 */
import { useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Activity, ArrowRight, BarChart3, Clock3, Globe2, KeyRound, Loader2, RefreshCw, Timer } from 'lucide-react'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@sida/base-ui/components/ui/tabs'
import { Button } from '@sida/base-ui/components/ui/button'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { useApiQuery } from '@/hooks/useApiQuery'

/** 用量监控在管理后台分类里的 section 值(与 MENU anchor 一致)。 */
export const USAGE_SECTION_ID = 'sec-usage'

// ── 接口原始形状(全部可选: 旧后端可能只返回一部分) ──
export interface UsageTrendsRaw {
  success?: boolean
  days?: number
  /** 后端字段名是 `peak_per_sec`(最近 60 秒内单秒最高调用数), 不是 `peak_qps`。 */
  qps?: {
    current_10s?: number | string | null
    avg_60s?: number | string | null
    peak_per_sec?: number | string | null
    peak_second?: string | null
    samples_10s?: number | string | null
    samples_60s?: number | string | null
  }
  daily_trend?: { date?: string; new_keys?: number | string | null; total_calls?: number | string | null }[]
  top_skills?: {
    skill_name?: string
    display_name?: string | null
    /** 后端标注该接口是否有中文映射; 未收录的名字会自带「（未收录）」后缀。 */
    known?: boolean
    count?: number | string | null
  }[]
  top_ips?: { ip?: string; count?: number | string | null }[]
  /** 按 API Key 聚合的来源 IP：回答"这把 Key 被哪些 IP 用过"（共用/盗用判据）。 */
  keys_with_ips?: {
    api_key_id?: number | string | null
    key_prefix?: string
    owner_label?: string
    total_calls?: number | string | null
    ips?: { ip?: string; count?: number | string | null }[]
  }[]
  total_calls_in_window?: number | string | null
}

export interface UsageTrends {
  days: number | null
  qps: { current10s: number | null; avg60s: number | null; peak: number | null; peakSecond: string | null }
  /** 仅保留日期合法的行; 行内数值缺失 → null(渲染成 `--`) */
  daily: { date: string; newKeys: number | null; totalCalls: number | null }[]
  skills: { skillName: string; displayName: string | null; known: boolean | null; count: number | null }[]
  ips: { ip: string; count: number | null }[]
  /** 每把 Key 的来源 IP 明细；后端未返回时为空数组（不补假数据）。 */
  keysWithIps: {
    apiKeyId: number | null
    keyPrefix: string
    ownerLabel: string
    totalCalls: number | null
    ips: { ip: string; count: number | null }[]
  }[]
  windowCalls: number | null
}

function toNum(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  }
  return null
}

/** 归一化: 缺数组/缺字段一律降级为 null 或空数组, 不做 0 兜底。 */
export function normalizeUsageTrends(raw: UsageTrendsRaw | null | undefined): UsageTrends {
  const daily = (Array.isArray(raw?.daily_trend) ? raw!.daily_trend! : [])
    .map((row) => ({
      date: typeof row?.date === 'string' ? row.date : '',
      newKeys: toNum(row?.new_keys),
      totalCalls: toNum(row?.total_calls),
    }))
    .filter((row) => row.date !== '')

  const skills = (Array.isArray(raw?.top_skills) ? raw!.top_skills! : [])
    .map((row) => ({
      skillName: typeof row?.skill_name === 'string' ? row.skill_name : '',
      displayName: typeof row?.display_name === 'string' && row.display_name.trim() !== '' ? row.display_name : null,
      // known 是后端显式标记; 旧后端没有该字段时退回 null(不知道, 不冒充已收录)
      known: typeof row?.known === 'boolean' ? row.known : null,
      count: toNum(row?.count),
    }))
    .filter((row) => row.skillName !== '')

  const ips = (Array.isArray(raw?.top_ips) ? raw!.top_ips! : [])
    .map((row) => ({
      ip: typeof row?.ip === 'string' ? row.ip : '',
      count: toNum(row?.count),
    }))
    .filter((row) => row.ip !== '')

  // 每把 Key 的来源 IP：Key 标识缺失的行丢弃(没有归属的 IP 明细没有意义)
  const keysWithIps = (Array.isArray(raw?.keys_with_ips) ? raw!.keys_with_ips! : [])
    .map((row) => ({
      apiKeyId: toNum(row?.api_key_id),
      keyPrefix: typeof row?.key_prefix === 'string' ? row.key_prefix : '',
      ownerLabel: typeof row?.owner_label === 'string' ? row.owner_label : '',
      totalCalls: toNum(row?.total_calls),
      ips: (Array.isArray(row?.ips) ? row!.ips! : [])
        .map((x) => ({ ip: typeof x?.ip === 'string' ? x.ip : '', count: toNum(x?.count) }))
        .filter((x) => x.ip !== ''),
    }))
    .filter((row) => row.keyPrefix !== '' || row.apiKeyId !== null)

  return {
    days: toNum(raw?.days),
    qps: {
      current10s: toNum(raw?.qps?.current_10s),
      avg60s: toNum(raw?.qps?.avg_60s),
      // 后端真实字段: peak_per_sec。此前误写成 peak_qps → 新后端下峰值永远 `--`。
      peak: toNum(raw?.qps?.peak_per_sec),
      peakSecond: typeof raw?.qps?.peak_second === 'string' && raw.qps.peak_second !== '' ? raw.qps.peak_second : null,
    },
    daily,
    skills,
    ips,
    keysWithIps,
    windowCalls: toNum(raw?.total_calls_in_window),
  }
}

/** 缺失显示 `--`(不写 0); 千分位用 toLocaleString, 避开 toFixed 棘轮。 */
function fmtCount(v: number | null): string {
  return v === null ? '--' : v.toLocaleString('zh-CN')
}

function fmtQps(v: number | null): string {
  if (v === null) return '--'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** 峰值 QPS = 单秒调用数(后端给整数), 不补两位小数假装更精确。 */
function fmtPeak(v: number | null): string {
  return v === null ? '--' : v.toLocaleString('zh-CN')
}

/** `2026-11-04 20:18:53` / `2026-11-04T20:18:53` → `20:18:53`(解析不了原样显示)。 */
function fmtPeakSecond(v: string | null): string | null {
  if (!v) return null
  const m = /(\d{2}:\d{2}:\d{2})/.exec(v)
  return m ? m[1] : v
}

/** 权限类失败要说清差在哪一层, 不能只显示 `HTTP 403`。 */
function describeError(error: unknown): string {
  const err = error as { status?: number; message?: string } | null
  if (err?.status === 401) return '登录已过期，请重新登录管理端'
  if (err?.status === 403) return '需要管理员权限（owner 账号 + 管理端登录）'
  const msg = String(err?.message ?? '').trim()
  return msg === '' ? '未知错误' : msg
}

/**
 * 窗口内的合计: 只累加后端**真的给了**的读数。
 * 一行都没有、或全为 null(字段缺失)时返回 null → 页面显示 `--`, 不把缺失当 0 累加。
 */
function sumOrNull(values: (number | null)[]): number | null {
  const known = values.filter((v): v is number => v !== null)
  if (known.length === 0) return null
  return known.reduce((acc, v) => acc + v, 0)
}

function Metric({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[12px] text-muted-foreground">{label}</dt>
      <dd className="mt-1 font-mono text-[20px] font-semibold text-foreground">{value}</dd>
      <p className="mt-1 text-[12px] text-muted-foreground">{hint}</p>
    </div>
  )
}

function QueryError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  return <ErrorState compact error={error} title="用量数据加载失败" onRetry={onRetry} />
}

/** 数据体上的"字段缺失"提示 —— 说明是**后端没给**而不是 0 次调用。 */
function MissingFieldNote({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-2 text-[12px] text-muted-foreground" role="status">
      {children}
    </p>
  )
}

function UsageRangeBody({ days }: { days: 7 | 30 }) {
  const { data, isLoading, isFetching, error, refetch, dataUpdatedAt } = useApiQuery<UsageTrendsRaw>(
    ['admin-usage-trends', days],
    `/admin/analytics/usage-trends?days=${days}`,
  )
  const view = useMemo(() => (data ? normalizeUsageTrends(data) : null), [data])

  const updated = dataUpdatedAt ? new Date(dataUpdatedAt).toLocaleTimeString('zh-CN', { hour12: false }) : ''

  if (isLoading && !view) {
    return (
      <div className="flex items-center justify-center gap-2 rounded-xl bg-s2 py-10 text-[12px] text-muted-foreground" role="status">
        <Loader2 className="h-4 w-4 animate-spin" />
        加载 {days} 天用量走势…
      </div>
    )
  }
  if (!view) {
    return <QueryError error={error} onRetry={() => void refetch()} />
  }

  const newKeysTotal = sumOrNull(view.daily.map((d) => d.newKeys))
  const callsTotal = sumOrNull(view.daily.map((d) => d.totalCalls))
  const maxCalls = view.daily.reduce<number>((max, row) => (row.totalCalls !== null && row.totalCalls > max ? row.totalCalls : max), 0)
  const peakMissing = view.qps.peak === null
  const peakAt = fmtPeakSecond(view.qps.peakSecond)
  const unmapped = view.skills.filter((row) => row.known === false)

  return (
    <div className="space-y-6">
      {/* 元信息行: 更新时间与刷新(单主操作) */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[12px] text-muted-foreground">
          {updated ? `读取于 ${updated}` : '尚未取到读数'}
          {isFetching ? ' · 刷新中…' : ''}
        </p>
        <Button size="sm" variant="outline" className="h-8" disabled={isFetching} onClick={() => void refetch()}>
          {isFetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          刷新用量
        </Button>
      </div>

      {/* 刷新失败但保留上次成功数据: 不把页面清空成错误页 */}
      {error ? (
        <div className="rounded-lg bg-s2 px-3 py-2 text-[12px] text-amber-500" role="alert">
          本次刷新失败（{describeError(error).slice(0, 80)}），以下为上次成功读取的结果。
        </div>
      ) : null}

      {/* ① 实时 QPS */}
      <div>
        <div className="mb-2 flex items-center gap-2 text-[12px] font-medium text-muted-foreground">
          <Timer className="h-3.5 w-3.5" />
          实时吞吐（QPS）
        </div>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 rounded-xl bg-s2 p-4 sm:grid-cols-4">
          <Metric label="当前 QPS（10 秒均值）" value={fmtQps(view.qps.current10s)} hint="后端过去 10 秒调用数 ÷ 10" />
          <Metric label="QPS（60 秒均值）" value={fmtQps(view.qps.avg60s)} hint="后端过去 60 秒调用数 ÷ 60" />
          <Metric
            label="峰值 QPS（单秒）"
            value={fmtPeak(view.qps.peak)}
            hint={peakMissing ? '接口未返回峰值口径' : peakAt ? `最近 60 秒峰值出现在 ${peakAt}` : '最近 60 秒内单秒最高调用数'}
          />
          <Metric label="窗口内总调用" value={fmtCount(view.windowCalls)} hint={`近 ${days} 天全部 Key 的调用条数`} />
        </dl>
        {peakMissing ? (
          <MissingFieldNote>后端本次未返回 `qps.peak_per_sec`（旧版本接口），峰值显示 `--`；不拿均值折算补齐。</MissingFieldNote>
        ) : null}
      </div>

      {/* ② 每日新增 Key / 总调用 */}
      <div>
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-[12px] font-medium text-muted-foreground">
            <BarChart3 className="h-3.5 w-3.5" />
            每日新增 Key 与总调用
          </div>
          <p className="text-[12px] text-muted-foreground">
            窗口合计：新增 Key {fmtCount(newKeysTotal)} · 调用 {fmtCount(callsTotal)}
          </p>
        </div>
        {view.daily.length === 0 ? (
          <EmptyState
            compact
            icon={<BarChart3 className="h-5 w-5" />}
            title="接口未返回逐日数据"
            description="本次响应里没有 daily_trend，无法画出走势"
            caliber="不用 0 补齐缺失的日期 —— 那会被读成“当天没有新增也没有调用”。"
          />
        ) : (
          <div className="overflow-x-auto rounded-xl bg-card">
            <table className="w-full min-w-[560px]">
              <thead className="border-b border-border">
                <tr>
                  <th className="whitespace-nowrap px-3 py-2 text-left text-[12px] font-medium text-muted-foreground">日期</th>
                  <th className="whitespace-nowrap px-3 py-2 text-left text-[12px] font-medium text-muted-foreground">新增 Key</th>
                  <th className="whitespace-nowrap px-3 py-2 text-left text-[12px] font-medium text-muted-foreground">总调用</th>
                  <th className="whitespace-nowrap px-3 py-2 text-left text-[12px] font-medium text-muted-foreground">调用分布</th>
                </tr>
              </thead>
              <tbody>
                {view.daily.map((row) => {
                  // 0 次调用就是空条(不给最小宽度), 否则"零"会被画成"有量"
                  const width =
                    row.totalCalls !== null && row.totalCalls > 0 && maxCalls > 0
                      ? Math.max(2, Math.round((row.totalCalls / maxCalls) * 100))
                      : 0
                  return (
                    <tr key={row.date} className="border-b border-border last:border-0">
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-[12px] text-foreground">{row.date}</td>
                      <td className="px-3 py-2 font-mono text-[12px] text-foreground">{fmtCount(row.newKeys)}</td>
                      <td className="px-3 py-2 font-mono text-[12px] text-foreground">{fmtCount(row.totalCalls)}</td>
                      <td className="w-[40%] px-3 py-2">
                        {row.totalCalls === null ? (
                          <span className="text-[12px] text-muted-foreground">未返回</span>
                        ) : (
                          <span className="block h-1.5 w-full rounded-full bg-s3">
                            <span className="block h-1.5 rounded-full bg-primary" style={{ width: `${width}%` }} />
                          </span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ③ Top Skills(中文名) + Top IP */}
      <div className="grid gap-6 lg:grid-cols-2">
        <div>
          <div className="mb-2 flex items-center gap-2 text-[12px] font-medium text-muted-foreground">
            <Activity className="h-3.5 w-3.5" />
            Top Skills（近 {days} 天调用最多）
          </div>
          {view.skills.length === 0 ? (
            <EmptyState compact icon={<Activity className="h-5 w-5" />} title="窗口内暂无调用记录" description="没有可排序的接口调用" />
          ) : (
            <div className="overflow-x-auto rounded-xl bg-card">
              <table className="w-full min-w-[360px]">
                <thead className="border-b border-border">
                  <tr>
                    <th className="px-3 py-2 text-left text-[12px] font-medium text-muted-foreground">接口</th>
                    <th className="px-3 py-2 text-left text-[12px] font-medium text-muted-foreground">识别名</th>
                    <th className="px-3 py-2 text-right text-[12px] font-medium text-muted-foreground">调用</th>
                  </tr>
                </thead>
                <tbody>
                  {view.skills.map((row) => (
                    <tr key={row.skillName} className="border-b border-border last:border-0">
                      <td className="px-3 py-2 text-[12px] text-foreground" title={row.skillName}>
                        {row.displayName ?? row.skillName}
                      </td>
                      <td className="px-3 py-2">
                        <code className="font-mono text-[12px] text-muted-foreground">{row.skillName}</code>
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-[12px] text-foreground">{fmtCount(row.count)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {unmapped.length > 0 ? (
            <MissingFieldNote>
              有 {unmapped.length} 个接口未收录中文映射（后端标记 known=false）：{unmapped.slice(0, 3).map((row) => row.skillName).join('、')}
              {unmapped.length > 3 ? ' 等' : ''}
              ，按「原名（未收录）」展示。
            </MissingFieldNote>
          ) : view.skills.some((row) => row.known === null && row.displayName === null) ? (
            <MissingFieldNote>部分接口缺少中文映射（display_name 为空，旧后端未给 known 标记），直接显示原始接口名。</MissingFieldNote>
          ) : null}
        </div>

        <div>
          <div className="mb-2 flex items-center gap-2 text-[12px] font-medium text-muted-foreground">
            <Globe2 className="h-3.5 w-3.5" />
            Top 客户端 IP（近 {days} 天）
          </div>
          {view.ips.length === 0 ? (
            <EmptyState
              compact
              icon={<Globe2 className="h-5 w-5" />}
              title="没有 IP 记录"
              description="本次响应未返回 top_ips，或窗口内没有带 IP 的调用"
            />
          ) : (
            <div className="overflow-x-auto rounded-xl bg-card">
              <table className="w-full min-w-[320px]">
                <thead className="border-b border-border">
                  <tr>
                    <th className="px-3 py-2 text-left text-[12px] font-medium text-muted-foreground">IP</th>
                    <th className="px-3 py-2 text-right text-[12px] font-medium text-muted-foreground">调用</th>
                  </tr>
                </thead>
                <tbody>
                  {view.ips.map((row) => (
                    <tr key={row.ip} className="border-b border-border last:border-0">
                      <td className="px-3 py-2 font-mono text-[12px] text-foreground">{row.ip}</td>
                      <td className="px-3 py-2 text-right font-mono text-[12px] text-foreground">{fmtCount(row.count)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div>
          <div className="mb-2 flex items-center gap-2 text-[12px] font-medium text-muted-foreground">
            <KeyRound className="h-3.5 w-3.5" />
            按 API Key 的来源 IP（近 {days} 天）
          </div>
          {view.keysWithIps.length === 0 ? (
            <EmptyState
              compact
              icon={<KeyRound className="h-5 w-5" />}
              title="没有 Key 级 IP 记录"
              description="本次响应未返回 keys_with_ips，或窗口内没有带 Key 归属的调用"
            />
          ) : (
            <div className="overflow-x-auto rounded-xl bg-card">
              <table className="w-full min-w-[520px]">
                <thead className="border-b border-border">
                  <tr>
                    <th className="px-3 py-2 text-left text-[12px] font-medium text-muted-foreground">Key</th>
                    <th className="px-3 py-2 text-left text-[12px] font-medium text-muted-foreground">归属</th>
                    <th className="px-3 py-2 text-right text-[12px] font-medium text-muted-foreground">调用</th>
                    <th className="px-3 py-2 text-left text-[12px] font-medium text-muted-foreground">来源 IP</th>
                  </tr>
                </thead>
                <tbody>
                  {view.keysWithIps.map((row) => (
                    <tr
                      key={row.keyPrefix || String(row.apiKeyId)}
                      className="border-b border-border last:border-0"
                    >
                      <td className="px-3 py-2">
                        <code className="font-mono text-[12px] text-foreground">{row.keyPrefix || '--'}</code>
                      </td>
                      <td className="px-3 py-2 text-[12px] text-muted-foreground" title={row.ownerLabel}>
                        {row.ownerLabel || '--'}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-[12px] text-foreground">
                        {fmtCount(row.totalCalls)}
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex flex-wrap gap-x-2 gap-y-1">
                          {row.ips.length === 0 ? (
                            <span className="text-[12px] text-muted-foreground">--</span>
                          ) : (
                            row.ips.map((x) => (
                              <span
                                key={x.ip}
                                className="font-mono text-[12px] text-muted-foreground"
                                title={`${x.ip} · ${x.count ?? '--'} 次`}
                              >
                                {x.ip}
                                <span className="ml-1 text-[12px] text-muted-foreground">
                                  {fmtCount(x.count)}
                                </span>
                              </span>
                            ))
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {view.keysWithIps.length > 0 ? (
            <MissingFieldNote>
              仅展示调用量最高的 20 把 Key；IP 只统计窗口内、且计量时能解析来源的调用。
            </MissingFieldNote>
          ) : null}
        </div>
      </div>
    </div>
  )
}

/** 用量监控页主视图: 7/30 天互斥切换(共享 Tabs), 一次只挂载一个窗口的面板。 */
export function UsageTrendsPanel() {
  return (
    <Tabs defaultValue="7">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-[14px] font-medium text-foreground">
            <Clock3 className="h-4 w-4 text-primary" />
            实时用量走势
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            QPS 为后端实时读数；每日序列按后端给的连续日期展示，缺字段显示 `--`。
          </p>
        </div>
        <TabsList aria-label="统计窗口">
          <TabsTrigger value="7">近 7 天</TabsTrigger>
          <TabsTrigger value="30">近 30 天</TabsTrigger>
        </TabsList>
      </div>
      <TabsContent value="7">
        <UsageRangeBody days={7} />
      </TabsContent>
      <TabsContent value="30">
        <UsageRangeBody days={30} />
      </TabsContent>
    </Tabs>
  )
}

/**
 * API Key 管理页的用量概览 + 入口。
 *
 * 放在 Key 页顶部: 管理员在这页就要能判断"Key 是不是被刷了/量在不在"，
 * 不必先猜用量监控在哪。完整明细仍在使用监控分类（按钮写入 `section=`，保留其他查询参数）。
 */
export function UsageTrendsOverview() {
  const [params, setParams] = useSearchParams()
  const { data, isLoading, error, refetch } = useApiQuery<UsageTrendsRaw>(
    ['admin-usage-trends', 7],
    '/admin/analytics/usage-trends?days=7',
  )
  const view = useMemo(() => (data ? normalizeUsageTrends(data) : null), [data])

  const openUsage = () => {
    const next = new URLSearchParams(params)
    next.set('section', USAGE_SECTION_ID)
    setParams(next)
    window.scrollTo({ top: 0 })
  }

  const newKeys7d = view ? sumOrNull(view.daily.map((d) => d.newKeys)) : null
  const calls7d = view ? sumOrNull(view.daily.map((d) => d.totalCalls)) : null

  return (
    <div className="rounded-xl bg-s2 p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-[14px] font-medium text-foreground">
            <Activity className="h-4 w-4 text-primary" />
            用量概览（近 7 天）
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            QPS 反映当前压力，近 7 天新增 Key / 调用量反映配额消耗趋势；逐日明细在「用量监控」。
          </p>
        </div>
        <Button size="sm" variant="outline" className="h-8 shrink-0" onClick={openUsage}>
          查看完整用量监控
          <ArrowRight className="h-3.5 w-3.5" />
        </Button>
      </div>

      {error && view ? (
        <p className="mb-2 text-[12px] text-amber-500" role="alert">
          本次刷新失败（{describeError(error).slice(0, 60)}），以下为上次成功读取的结果。
        </p>
      ) : null}

      {isLoading && !view ? (
        <div className="flex items-center gap-2 py-4 text-[12px] text-muted-foreground" role="status">
          <Loader2 className="h-4 w-4 animate-spin" />
          加载用量概览…
        </div>
      ) : !view ? (
        <div className="space-y-2">
          <p className="text-[12px] text-muted-foreground" role="alert">
            用量接口读取失败（{describeError(error).slice(0, 80)}），因此不展示 QPS 与新增 Key 读数。
          </p>
          <Button size="sm" variant="outline" className="h-8" onClick={() => void refetch()}>
            <RefreshCw className="h-3.5 w-3.5" />
            重试
          </Button>
        </div>
      ) : (
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
          <Metric label="当前 QPS（10 秒）" value={fmtQps(view.qps.current10s)} hint="过去 10 秒均值" />
          <Metric label="QPS（60 秒）" value={fmtQps(view.qps.avg60s)} hint="过去 60 秒均值" />
          <Metric label="7 天总调用" value={fmtCount(calls7d)} hint="窗口内全部 Key 调用条数" />
          <Metric label="7 天新增 Key" value={fmtCount(newKeys7d)} hint="窗口内新签发的 Key 数" />
        </dl>
      )}
    </div>
  )
}
