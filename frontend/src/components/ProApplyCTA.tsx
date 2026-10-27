import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, Check, Loader2, Lock } from 'lucide-react'
import { applyPro, fetchProApplyStatus, isAuthenticated, type ProApplyStatus } from '@panwatch/api'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { cn } from '@/lib/utils'

/**
 * 「申请 Pro」转化组件（2026-10-27）。
 *
 * 背景：普通用户碰到权限门时，页面上只有一行「需要更高权限，升级后可见」的静态提示，
 * 没有出路 —— 用户知道被挡住了，但不知道去哪申请。这个组件就是把那行提示变成**可点的动作**。
 *
 * 三条纪律：
 *  ① **状态如实**：申请中 / 审核中 / 已开通 / 被拒，都按后端 `GET /api/pro/apply/status` 显示，
 *     取不到状态就不猜（退回可申请态，让用户点）。
 *  ② **一步到位**：已登录用户点一下就地提交申请，不跳页面；未登录才引导注册（转化 ≤2 步）。
 *  ③ **不抢主次**：`inline` 是跟在提示后面的小药丸，`block` 才是有标题说明的卡片块。
 */

export type ProApplyCTAProps = {
  /** 记进申请原因上下文，例如「权限不足：市场扫描」。会被后端审计，不要放敏感信息。 */
  reason?: string
  /** 未申请时的按钮文案 */
  label?: string
  variant?: 'inline' | 'block'
  className?: string
  /** 提交成功后的回调（调用方可用来刷新自身数据） */
  onApplied?: () => void
  /**
   * 内联在"权限不足"提示后面时传 true：已开通账号**不显示**这枚芯片。
   * 否则会出现「需要更高权限 … 已开通 Pro」并排的自相矛盾（2026-10-27 自查发现）。
   * 这时的正确语义是"权限没授予"，属于管理员配置问题，不该引导去申请 Pro。
   */
  hideWhenApproved?: boolean
}

const BTN_INLINE =
  'inline-flex shrink-0 items-center gap-1 rounded-md bg-primary px-2 py-0.5 text-[12px] font-semibold text-primary-foreground ' +
  'transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ' +
  'disabled:cursor-not-allowed disabled:opacity-60'
const BTN_BLOCK =
  'inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-2 text-[14px] font-semibold text-primary-foreground ' +
  'transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ' +
  'disabled:cursor-not-allowed disabled:opacity-60'

export function ProApplyCTA({ reason = '权限不足页面申请', label = '申请开通 Pro', variant = 'inline', className, onApplied, hideWhenApproved = false }: ProApplyCTAProps) {
  const [loggedIn] = useState(() => {
    try { return isAuthenticated() } catch { return false }
  })
  const [status, setStatus] = useState<ProApplyStatus | null>(null)
  const [applying, setApplying] = useState(false)
  const { toast } = useToast()

  useEffect(() => {
    if (!loggedIn) return
    let cancelled = false
    fetchProApplyStatus()
      .then(next => { if (!cancelled) setStatus(next) })
      // 取不到状态不猜：保持可申请态，用户点了会得到真实结果
      .catch(() => { if (!cancelled) setStatus(null) })
    return () => { cancelled = true }
  }, [loggedIn])

  const submit = useCallback(async () => {
    setApplying(true)
    try {
      await applyPro(reason)
      toast('已提交 Pro 申请，等待管理员开通', 'success')
      const next = await fetchProApplyStatus().catch(() => null)
      if (next) setStatus(next)
      onApplied?.()
    } catch (error) {
      toast(error instanceof Error ? `申请失败：${error.message}` : '申请失败，请稍后再试', 'error')
    } finally {
      setApplying(false)
    }
  }, [reason, toast, onApplied])

  // 未登录：先去注册/登录，回来就能一键申请
  if (!loggedIn) {
    return (
      <Link
        to="/login?mode=register"
        className={cn(variant === 'block' ? BTN_BLOCK : BTN_INLINE, className)}
        data-pro-apply-cta="register"
      >
        注册后申请 Pro
        <ArrowRight className="h-3.5 w-3.5" />
      </Link>
    )
  }

  const appStatus = status?.application?.status
  const role = status?.role
  // 后端 `submit_application` 对 owner/pro 直接 400 拒绝，前端必须先认这两个角色，
  // 否则 owner 点了会拿到一条"owner 无需申请 Pro"的错误 toast（2026-10-27 实测 admin 的 role 就是 owner）
  const approved = appStatus === 'approved' || role === 'pro' || role === 'owner' || role === 'admin'

  if (approved && hideWhenApproved) return null

  const button = approved ? (
    <span
      className={cn(
        variant === 'block'
          ? 'inline-flex items-center gap-1.5 rounded-lg border border-border px-3.5 py-2 text-[14px] font-medium text-muted-foreground'
          : 'inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-0.5 text-[12px] text-muted-foreground',
        className,
      )}
      data-pro-apply-cta="approved"
    >
      <Check className="h-3.5 w-3.5" />
      已开通 Pro
    </span>
  ) : (
    <button
      type="button"
      onClick={submit}
      disabled={applying || appStatus === 'pending'}
      className={cn(variant === 'block' ? BTN_BLOCK : BTN_INLINE, className)}
      data-pro-apply-cta={appStatus === 'pending' ? 'pending' : 'apply'}
      title={appStatus === 'pending' ? '申请已提交，管理员审核后自动开通' : reason}
    >
      {applying ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : appStatus === 'pending' ? null : <ArrowRight className="h-3.5 w-3.5" />}
      {applying ? '提交中…' : appStatus === 'pending' ? '申请审核中' : appStatus === 'rejected' ? '重新申请' : label}
    </button>
  )

  if (variant === 'inline') return button

  return (
    <div className={cn('card-surface flex flex-wrap items-center justify-between gap-3 rounded-xl bg-card px-4 py-3', className)} data-pro-apply-panel>
      <div className="min-w-0">
        <div className="flex items-center gap-1.5 text-[14px] font-medium text-foreground">
          <Lock className="h-3.5 w-3.5 text-muted-foreground" />
          这部分内容属于 Pro 权限
        </div>
        <div className="mt-0.5 text-[12px] leading-relaxed text-muted-foreground">
          {approved ? '你的账号已开通 Pro，可直接使用。' : '提交申请后由管理员审核，开通即刻生效，无需重新登录。'}
        </div>
      </div>
      {button}
    </div>
  )
}

export default ProApplyCTA
