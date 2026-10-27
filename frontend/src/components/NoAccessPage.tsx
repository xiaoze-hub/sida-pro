import { Link } from 'react-router-dom'
import { ArrowLeft, ShieldAlert } from 'lucide-react'
import { ProApplyCTA } from '@/components/ProApplyCTA'

/**
 * 无权限页（2026-10-27）。
 *
 * 此前 `PermGuard` 对无权限用户是 `<Navigate to="/" replace />` —— **静默弹回首页**：
 * 用户点了导航却"回到首页"，既不知道发生了什么，也不知道怎么拿到权限。
 * 现在改成明确的三段式：告诉我缺什么 → 我能申请 → 或者先回去。
 *
 * 权限中文名只做「常见项」映射，其余直接显示权限码 —— 宁可露出代码，
 * 也不要编一个听起来合理但其实不对的中文名（那会让用户拿着错的权限名去申请）。
 */
const PERM_LABELS: Record<string, string> = {
  view_dashboard: '首页看板',
  view_quotes: '行情与数智参谋',
  view_opportunities: '市场扫描与集合竞价',
  view_heatmap: '板块热力',
  view_reports: '报告中心',
  view_positions: '持仓',
  manage_users: '用户管理',
  manage_settings: '系统设置',
}

export function permLabel(perm: string): string {
  return PERM_LABELS[perm] ?? perm
}

export function NoAccessPage({ perm, title }: { perm: string; title?: string }) {
  const human = permLabel(perm)
  return (
    <div className="mx-auto flex min-h-[60vh] max-w-xl flex-col items-center justify-center px-4 text-center" data-no-access={perm}>
      <span className="mb-3 inline-flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
        <ShieldAlert className="h-6 w-6" />
      </span>
      <h2 className="text-[16px] font-semibold text-foreground">{title ?? '这个页面需要更高权限'}</h2>
      <p className="mt-2 text-[14px] leading-relaxed text-muted-foreground">
        你的账号当前没有「{human}」的访问权限
        <span className="mx-1">·</span>
        权限码 <code className="rounded bg-muted px-1 py-0.5 text-[12px]">{perm}</code>
      </p>

      <ProApplyCTA
        variant="block"
        className="mt-5 w-full text-left"
        label={`申请开通「${human}」权限`}
        reason={`权限不足：${human}（${perm}）`}
      />

      <Link
        to="/"
        className="mt-4 inline-flex items-center gap-1 text-[12px] text-muted-foreground underline-offset-4 hover:underline"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        先回首页
      </Link>
    </div>
  )
}

export default NoAccessPage
