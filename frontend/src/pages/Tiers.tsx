/**
 * 档位对比页(P2-4, 2026-09-18) —— **公开面**。
 *
 * 三条口径(设计稿 v3.0 §三 公开面规范 + 老板拍板"计费先内部"):
 *  1. **不显示价格**: 内测期不收费, 后端 `billing_enabled=false` 且不返回任何金额字段 ——
 *     页面据此隐藏价格区(而不是写"￥0"); 有价格才显示价格, 这是数据驱动而非话术;
 *  2. **权限清单来自后端** `GET /api/tiers`(后端从 `permissions` 现读现拼), 前端**不硬编码**
 *     —— 否则权限一改, 页面就撒谎;
 *  3. **免费档上限用实时值**(自选 10 / 预警 3 / 试用 3 次每日, 由管理员在设置页可调),
 *     读不到时如实说明, 不拿文档里的旧数字充数。
 *
 * 公开面字阶只用 5 档(12/13/16/20/28) —— ui-rules R10 公开面限 7 档。
 */
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, Check, Loader2 } from 'lucide-react'
import { applyPro, fetchProApplyStatus, fetchTiers, isAuthenticated } from '@panwatch/api'
import type { ProApplyStatus, TierRow, TiersResp } from '@panwatch/api'

function GroupList({ row, otherItems }: { row: TierRow; otherItems: Set<string> }) {
  return (
    <div className="space-y-3">
      {row.groups.map((g) => (
        <div key={g.group}>
          <div className="mb-1 text-[12px] font-semibold text-muted-foreground">{g.group}</div>
          <ul className="space-y-1">
            {g.items.map((it) => {
              const only = !otherItems.has(it)
              return (
                <li key={it} className="flex items-start gap-1.5 text-[13px] leading-relaxed">
                  <Check className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${only ? 'text-primary' : 'text-muted-foreground/50'}`} />
                  <span className={only ? 'font-medium text-foreground' : 'text-muted-foreground'}>{it}</span>
                  {only && <span className="ml-1 shrink-0 rounded bg-primary/10 px-1 text-[12px] text-primary">更高档位独有</span>}
                </li>
              )
            })}
          </ul>
        </div>
      ))}
    </div>
  )
}

export default function Tiers() {
  const [data, setData] = useState<TiersResp | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [loggedIn] = useState(() => isAuthenticated())
  const [status, setStatus] = useState<ProApplyStatus | null>(null)
  const [applying, setApplying] = useState(false)
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    try {
      setData(await fetchTiers())
      setErr('')
    } catch {
      setErr('档位信息暂时取不到（接口不可用）。请稍后再试，或直接注册后在内查看。')
    } finally {
      setLoading(false)
    }
    if (!loggedIn) return
    try {
      setStatus(await fetchProApplyStatus())
    } catch {
      setStatus(null)   // 取不到就不显示状态, 不猜
    }
  }, [loggedIn])

  useEffect(() => { void load() }, [load])

  async function onApply() {
    setApplying(true)
    setMsg('')
    try {
      await applyPro('来自档位页申请')
      setMsg('申请已提交，等待管理员开通。')
      setStatus(await fetchProApplyStatus())
    } catch (e) {
      setMsg(e instanceof Error ? `提交失败：${e.message}` : '提交失败，请稍后再试。')
    } finally {
      setApplying(false)
    }
  }

  const tiers = data?.tiers ?? []
  const free = tiers.find((t) => t.key === 'member')
  const pro = tiers.find((t) => t.key === 'pro')
  const proOnly = new Set((pro?.groups ?? []).flatMap((g) => g.items))
  const freeItems = new Set((free?.groups ?? []).flatMap((g) => g.items))
  const appStatus = status?.application?.status
  const statusText = appStatus === 'pending' ? '申请审核中' : appStatus === 'approved' ? '已开通' : appStatus === 'rejected' ? '未通过（可再次申请）' : ''

  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto max-w-3xl px-6 py-16">
        <h1 className="text-[28px] font-bold tracking-tight">档位</h1>
        <p className="mt-3 text-[13px] leading-relaxed text-muted-foreground">
          免费档可直接注册使用；Pro 面向需要完整决策链路的用户，目前为内测邀请制。
        </p>
        {data && (
          <p className="mt-2 text-[13px] font-medium text-primary">{data.note}</p>
        )}

        {loading && (
          <div className="mt-10 flex items-center gap-2 text-[13px] text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> 正在读取档位信息…
          </div>
        )}
        {err && <div className="mt-10 rounded-lg border border-border/60 p-4 text-[13px] text-muted-foreground">{err}</div>}

        {data && (
          <>
            <div className="mt-10 grid gap-6 md:grid-cols-2">
              {[free, pro].map((t, i) => t && (
                <div key={t.key} className={`rounded-xl border p-5 ${i === 1 ? 'border-primary/40 bg-primary/[0.03]' : 'border-border/60'}`}>
                  <div className="flex items-baseline justify-between">
                    <div className="text-[20px] font-bold">{t.label}</div>
                    <div className="text-[12px] text-muted-foreground">{t.count} 项能力</div>
                  </div>
                  {/* 内测期不收费: 后端不返回价格字段 → 这里只说明, 不写 ￥0 */}
                  <div className="mt-1 text-[12px] text-muted-foreground">
                    {data.billing_enabled ? '' : '内测期不收费'}
                  </div>
                  <div className="mt-4">
                    <GroupList row={t} otherItems={i === 0 ? proOnly : freeItems} />
                  </div>
                </div>
              ))}
            </div>

            {data.member_limits && (
              <div className="mt-6 rounded-lg border border-border/60 p-4">
                <div className="text-[13px] font-semibold">免费档当前上限</div>
                <ul className="mt-2 space-y-1 text-[13px] text-muted-foreground">
                  <li>自选股：{data.member_limits.watchlist_max} 只</li>
                  <li>价格预警：{data.member_limits.alert_max} 条</li>
                  <li>试用类功能：每日 {data.member_limits.trial_daily_limit} 次</li>
                </ul>
                <div className="mt-2 text-[12px] text-muted-foreground/80">{data.limits_note}</div>
              </div>
            )}
            {!data.member_limits && (
              <div className="mt-6 rounded-lg border border-border/60 p-4 text-[13px] text-muted-foreground">
                {data.limits_note}
              </div>
            )}

            <div className="mt-6 text-[12px] text-muted-foreground/80">{data.demo_note}</div>

            {/* 转化 ≤2 步: 未登录 → 注册(1 步) → 申请(2 步); 已登录 → 直接申请 */}
            <div className="mt-10 flex flex-wrap items-center gap-3">
              {!loggedIn && (
                <Link to="/login?mode=register" className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-[13px] font-semibold text-primary-foreground">
                  免费注册 <ArrowRight className="h-3.5 w-3.5" />
                </Link>
              )}
              {loggedIn && appStatus !== 'approved' && (
                <button
                  type="button"
                  onClick={onApply}
                  disabled={applying || appStatus === 'pending'}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-[13px] font-semibold text-primary-foreground disabled:opacity-60"
                >
                  {applying ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                  {appStatus === 'pending' ? '申请审核中' : '申请 Pro'}
                </button>
              )}
              {loggedIn && statusText && (
                <span className="text-[13px] text-muted-foreground">当前状态：{statusText}</span>
              )}
              <Link to="/developers" className="text-[13px] text-muted-foreground underline-offset-4 hover:underline">
                接口文档
              </Link>
              <Link to="/" className="text-[13px] text-muted-foreground underline-offset-4 hover:underline">
                返回首页
              </Link>
            </div>
            {msg && <div className="mt-3 text-[13px] text-muted-foreground">{msg}</div>}
          </>
        )}
      </div>
    </div>
  )
}
