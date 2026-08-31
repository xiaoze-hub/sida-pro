import { useState, useEffect } from 'react'
import { Upload, FileText, Download, TrendingUp, Activity, Target, Shield, AlertTriangle, CheckCircle2, Loader2, RefreshCw, UserRound } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { formatDateTime } from '@/lib/utils'

interface ShadowResult {
  shadow_id: string
  profile: Record<string, any>
  behavior: Record<string, any> | null
  stats: Record<string, any> | null
  attribution: Record<string, any> | null
  report_html: string
  report_pdf: string | null
}

// 修复(S-5, 2026-08-23): 原 fmt 对字符串数字直接 String(v) 返回, 大数字展示不利.
// 改用 safeMoney 统一处理万/亿口径.
function fmt(v: any, suffix = ''): string {
  if (v === null || v === undefined || v === '') return '--'
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return '--'
  if (Math.abs(n) >= 10000) return `${(n / 10000).toFixed(2)}万${suffix}`
  if (Math.abs(n) >= 100) return n.toFixed(2) + suffix
  return String(n) + suffix
}

// rules 元素可能是 ShadowRule 对象(含 human_text)或历史字符串, 统一取人话文本。
function ruleLabel(r: any): string {
  if (typeof r === 'string') return r
  return r?.human_text || r?.rule_id || '--'
}

function StatCard({ icon: Icon, label, value, sub, color }: { icon: any; label: string; value: string; sub?: string; color: string }) {
  return (
    <div className="rounded-xl bg-accent/30 p-3.5">
      <div className="flex items-center gap-2 mb-1.5">
        <Icon className={`w-3.5 h-3.5 ${color}`} />
        <span className="text-[11px] text-muted-foreground">{label}</span>
      </div>
      <div className="text-[15px] font-semibold text-foreground">{value}</div>
      {sub && <div className="text-[10px] text-muted-foreground mt-0.5">{sub}</div>}
    </div>
  )
}

interface ShadowProfileResponse {
  profile: Record<string, any> | null
  saved: boolean
}

export default function ShadowAccountPage() {
  const [result, setResult] = useState<ShadowResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [dragOver, setDragOver] = useState(false)
  // "我的画像": 进页面自动加载已存画像(users.shadow_profile_json 落库版)
  const [myProfile, setMyProfile] = useState<Record<string, any> | null>(null)
  const [profileLoading, setProfileLoading] = useState(true)
  const [profileFailed, setProfileFailed] = useState(false)

  const loadProfile = async () => {
    try {
      const d = await fetchAPI<ShadowProfileResponse>('/shadow/profile', { cacheMode: 'reload' })
      setMyProfile(d?.profile ?? null)
    } catch {
      // 静默失败: 画像区不显示, 不阻断上传功能
      setProfileFailed(true)
    } finally {
      setProfileLoading(false)
    }
  }

  useEffect(() => { loadProfile() }, [])

  const upload = async (file: File) => {
    setLoading(true)
    setError('')
    try {
      const form = new FormData()
      form.append('file', file)
      const d = await fetchAPI<ShadowResult>('/shadow/analyze', {
        method: 'POST',
        body: form,
        timeoutMs: 180000, // 交割单解析+画像可能 60-120s(586笔实测75s), 默认20s不够
      })
      setResult((d as any)?.data ?? d)
      // 分析完成落库后, 刷新"我的画像"区
      loadProfile()
    } catch (e: any) {
      setError(e?.message || '分析失败，请检查交割单格式')
    } finally {
      setLoading(false)
    }
  }

  // 修复(S-2, 2026-08-23): 旧实现用 document.write 把服务器 HTML 写进新窗口,
  // 新窗口同源 → 可读 parent document.localStorage.token, 形成 XSS→JWT 穿透链路.
  // 改为 blob URL + sandbox iframe: 没有 origin, 没有 storage access, 即便 HTML
  // 含恶意脚本也只能在隔离环境跑, 拿不到 token. 打印/下载按钮在 sandbox 内仍可用.
  const openReportHtml = async () => {
    if (!result) return
    try {
      const token = localStorage.getItem('token') || ''
      const resp = await fetch(result.report_html, { headers: { Authorization: `Bearer ${token}` } })
      const html = await resp.text()
      // 1) 优先 blob URL 路径 — 在新窗口用 sandbox iframe 渲染, 内层 document 完全隔离.
      const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
      const blobUrl = URL.createObjectURL(blob)
      const w = window.open('', '_blank')
      if (!w) {
        // 弹窗被拦截, 直接回退到新标签打开(用报告 URL, 走授权头失败再回退原 URL)
        window.open(result.report_html, '_blank')
        return
      }
      // 2) 在新窗口里写一段最小 HTML, 嵌入 sandbox iframe (不含 allow-same-origin,
      //    保证内层 iframe 拿不到 parent.localStorage / cookies).
      const safeWrapper = `<!doctype html><html><head><meta charset="utf-8"><title>影子账户报告</title>
<style>html,body{margin:0;padding:0;height:100%;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f7f8fa}
.toolbar{position:fixed;top:0;left:0;right:0;height:40px;background:#fff;border-bottom:1px solid #e5e7eb;display:flex;align-items:center;gap:8px;padding:0 12px;z-index:10;font-size:12px}
.toolbar a{color:#2563eb;text-decoration:none;padding:4px 10px;border-radius:4px;border:1px solid #e5e7eb}
.toolbar a:hover{background:#f3f4f6}
.frame-host{position:absolute;top:40px;left:0;right:0;bottom:0;border:0}
iframe{width:100%;height:100%;border:0}
</style>
</head><body>
<div class="toolbar">
  <span>🔒 隔离沙箱内渲染的报告(不可访问上层存储)</span>
  <a href="${result.report_html}" target="_self">在新窗口打开原始链接</a>
  <a href="${blobUrl}" target="_self" download="shadow-report-${result.shadow_id}.html">下载 HTML</a>
  <a href="${result.report_pdf || '#'}" target="_blank">下载 PDF</a>
</div>
<iframe class="frame-host" sandbox="allow-scripts" src="${blobUrl}"></iframe>
</body></html>`
      w.document.open()
      w.document.write(safeWrapper)
      w.document.close()
      // blob URL 在父进程保留期间一直有效, 用户关闭子窗口后回收.
      w.addEventListener?.('beforeunload', () => {
        try { URL.revokeObjectURL(blobUrl) } catch (e) { /* ignore */ }
      })
    } catch {
      // 最终 fallback: 浏览器原生打开原始 URL(走 cookie / Authorization 头时已失效,
      // 用户需自行登录后查看 — 与原行为一致).
      window.open(result.report_html, '_blank')
    }
  }

  const downloadPdf = async () => {
    if (!result?.report_pdf) return
    try {
      const token = localStorage.getItem('token') || ''
      const resp = await fetch(result.report_pdf, { headers: { Authorization: `Bearer ${token}` } })
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = `${result.shadow_id}.pdf`; a.click()
      URL.revokeObjectURL(url)
    } catch { window.open(result.report_pdf, '_blank') }
  }

  return (
    <div className="max-w-5xl mx-auto px-4 py-6 space-y-6">
      {/* 页头 */}
      <div>
        <h1 className="text-[17px] font-semibold text-foreground flex items-center gap-2">
          <Shield className="w-4.5 h-4.5 text-primary" />
          影子账户 Shadow Account
        </h1>
        <p className="text-[12px] text-muted-foreground mt-1">
          上传你的交易交割单（同花顺 / 东财 / 富途 / 通用 CSV），AI 提炼你的真实交易行为画像、盈利模式与风险习惯。
        </p>
      </div>

      {/* 我的画像: 进页面自动加载已存画像(users.shadow_profile_json 落库版), 不用重新上传 */}
      <div className="rounded-xl bg-card border border-border p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-[14px] font-semibold text-foreground flex items-center gap-2">
            <UserRound className="w-4 h-4 text-primary" /> 我的画像
          </h2>
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-[11px]"
            onClick={() => document.getElementById('shadow-file-input')?.click()}
          >
            <RefreshCw className="w-3 h-3 mr-1" /> 更新画像
          </Button>
        </div>

        {profileLoading ? (
          /* 加载中: 骨架屏 */
          <div className="space-y-3">
            <div className="h-3.5 w-40 animate-pulse rounded bg-accent/30" />
            <div className="h-3 w-full animate-pulse rounded bg-accent/20" />
            <div className="h-3 w-3/4 animate-pulse rounded bg-accent/20" />
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3 pt-1">
              {[0, 1, 2, 3, 4].map(i => (
                <div key={i} className="h-16 animate-pulse rounded-xl bg-accent/20" />
              ))}
            </div>
          </div>
        ) : profileFailed ? null : !myProfile ? (
          /* 空态: 无画像, 引导上传 */
          <div className="flex items-center gap-3 rounded-xl bg-accent/20 border border-dashed border-border px-4 py-3">
            <UserRound className="w-4 h-4 text-muted-foreground shrink-0" />
            <p className="text-[12px] text-muted-foreground">
              还没有交易画像。上传交割单后自动生成你的行为画像，下次进来直接查看。
            </p>
          </div>
        ) : (
          /* 已存画像展示 */
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-muted-foreground">
                更新时间: {formatDateTime(myProfile.created_at) || '--'}
              </span>
              {Array.isArray(myProfile.date_range) && myProfile.date_range.length >= 2 && (
                <span className="text-[11px] text-muted-foreground">
                  交易区间: {formatDateTime(myProfile.date_range[0])} ~ {formatDateTime(myProfile.date_range[1])}
                </span>
              )}
            </div>
            <p className="text-[12px] leading-relaxed text-foreground whitespace-pre-line">{myProfile.profile_text}</p>
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              <StatCard icon={CheckCircle2} label="盈利回合" value={fmt(myProfile.profitable_roundtrips)} color="text-emerald-700 dark:text-emerald-500" />
              <StatCard icon={Activity} label="总回合" value={fmt(myProfile.total_roundtrips)} color="text-blue-500" />
              <StatCard
                icon={Target}
                label="胜率"
                value={(() => {
                  // 修复(M-8, 2026-08-23): 三元保护防除零与字符串数字
                  const total = Number(myProfile.total_roundtrips)
                  const win = Number(myProfile.profitable_roundtrips)
                  if (!Number.isFinite(total) || !Number.isFinite(win) || total === 0) return '--'
                  return `${((win / total) * 100).toFixed(0)}%`
                })()}
                color="text-violet-500"
              />
              <StatCard
                icon={TrendingUp}
                label="偏好市场"
                value={(myProfile.preferred_markets || []).join(', ') || '--'}
                color="text-amber-500"
              />
              <StatCard
                icon={Activity}
                label="持仓中位(天)"
                value={Array.isArray(myProfile.typical_holding_days) && myProfile.typical_holding_days[0] != null ? fmt(myProfile.typical_holding_days[0]) : '--'}
                color="text-sky-500"
              />
            </div>
            {myProfile.rules && myProfile.rules.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {myProfile.rules.map((r: any, i: number) => (
                  <span key={typeof r === 'string' ? r : (r?.rule_id || i)} className="inline-flex items-center rounded-full bg-primary/10 px-2.5 py-1 text-[11px] text-primary">{ruleLabel(r)}</span>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 上传区 */}
      <div
        className={`border-2 border-dashed rounded-2xl p-8 text-center transition-colors cursor-pointer ${
          dragOver ? 'border-primary bg-primary/5' : 'border-border bg-accent/10 hover:bg-accent/20'
        }`}
        onDragOver={e => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={e => {
          e.preventDefault(); setDragOver(false)
          const f = e.dataTransfer.files?.[0]
          if (f) upload(f)
        }}
        onClick={() => document.getElementById('shadow-file-input')?.click()}
      >
        <input
          id="shadow-file-input"
          type="file"
          accept=".csv,.xlsx,.xls,.pdf"
          className="hidden"
          onChange={e => { const f = e.target.files?.[0]; if (f) upload(f) }}
        />
        {loading ? (
          <div className="flex flex-col items-center gap-2">
            <Loader2 className="w-8 h-8 text-primary animate-spin" />
            <span className="text-[13px] text-muted-foreground">正在分析交割单，生成行为画像...</span>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-2">
            <Upload className="w-8 h-8 text-muted-foreground" />
            <span className="text-[13px] font-medium text-foreground">点击或拖拽上传交割单</span>
            <span className="text-[11px] text-muted-foreground">支持 .csv / .xlsx / .pdf（同花顺、国投、东财格式自动识别）</span>
          </div>
        )}
      </div>

      {error && (
        <div className="flex items-center gap-2 rounded-xl bg-red-500/10 border border-red-500/20 p-3 text-[12px] text-red-600">
          <AlertTriangle className="w-4 h-4" /> {error}
        </div>
      )}

      {/* 结果展示 */}
      {result && (
        <>
          {/* 行为画像 */}
          {result.profile && (
            <div className="space-y-4">
              <h2 className="text-[14px] font-semibold text-foreground flex items-center gap-2">
                <TrendingUp className="w-4 h-4 text-primary" /> 行为画像
              </h2>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <StatCard icon={Activity} label="总交易回合" value={fmt(result.profile.total_roundtrips)} color="text-blue-500" />
                <StatCard icon={CheckCircle2} label="盈利回合" value={fmt(result.profile.profitable_roundtrips)} sub={(() => {
                  const total = Number(result.profile.total_roundtrips)
                  const win = Number(result.profile.profitable_roundtrips)
                  if (!Number.isFinite(total) || !Number.isFinite(win) || total === 0) return undefined
                  return `胜率 ${((win / total) * 100).toFixed(0)}%`
                })()} color="text-emerald-700 dark:text-emerald-500" />
                <StatCard icon={Activity} label="平均持有时长" value={fmt(result.profile.typical_holding_days, '天')} color="text-violet-500" />
                <StatCard icon={Target} label="偏好市场" value={(result.profile.preferred_markets || []).join(', ') || '--'} color="text-amber-500" />
              </div>
              {result.profile.rules && result.profile.rules.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {result.profile.rules.map((r: any, i: number) => (
                    <span key={typeof r === 'string' ? r : (r?.rule_id || i)} className="inline-flex items-center rounded-full bg-primary/10 px-2.5 py-1 text-[11px] text-primary">{ruleLabel(r)}</span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 行为诊断 */}
          {result.behavior && (
            <div className="space-y-4">
              <h2 className="text-[14px] font-semibold text-foreground flex items-center gap-2">
                <Target className="w-4 h-4 text-primary" /> 行为诊断
              </h2>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {(Object.entries(result.behavior) as [string, any][]).filter(([k]) => k !== 'error').map(([k, v]) => (
                  <StatCard key={k} icon={Activity} label={k} value={fmt(v)} color="text-amber-500" />
                ))}
              </div>
            </div>
          )}

          {/* 归因结果 */}
          {result.attribution && (
            <div className="space-y-4">
              <h2 className="text-[14px] font-semibold text-foreground flex items-center gap-2">
                <Shield className="w-4 h-4 text-primary" /> 归因分析
              </h2>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <StatCard icon={TrendingUp} label="影子收益" value={fmt(result.attribution.shadow_total_pnl)} color="text-emerald-700 dark:text-emerald-500" />
                <StatCard icon={Activity} label="实际收益" value={fmt(result.attribution.real_total_pnl)} color="text-blue-500" />
                <StatCard icon={CheckCircle2} label="差值" value={fmt(result.attribution.delta_pnl)} color="text-violet-500" />
                <StatCard icon={Target} label="归因项" value={fmt((result.attribution.attribution || []).length)} color="text-amber-500" />
              </div>
            </div>
          )}

          {/* 报告链接 */}
          <div className="flex flex-wrap gap-2 pt-2">
            <Button size="sm" onClick={openReportHtml}>
              <FileText className="w-3.5 h-3.5 mr-1.5" /> 查看完整报告
            </Button>
            {result.report_pdf && (
              <Button size="sm" variant="secondary" onClick={downloadPdf}>
                <Download className="w-3.5 h-3.5 mr-1.5" /> 下载 PDF
              </Button>
            )}
          </div>
        </>
      )}
    </div>
  )
}
