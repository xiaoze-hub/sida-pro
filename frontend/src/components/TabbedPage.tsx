import type { ReactNode } from 'react'
import { Navigate, useSearchParams } from 'react-router-dom'

import PageTabs, { type PageTabItem } from './PageTabs'

/**
 * 路由级 Tab 容器(设计稿 §4.3 Tab 合并通用件)。
 *
 * Tab 状态写进 URL 的 `?tab=`, 因此可分享 / 可刷新 / 可后退。
 * 旧路由由 App.tsx 统一重定向到 `xxx?tab=key`, 不破坏既有书签与推送链接。
 *
 * 权限过滤: `perm` 无该权限点隐藏, `ownerOnly` 非 owner 隐藏。
 * Tab 未挂 `render` 时显式渲染「待接入」占位(不编造内容)。
 */
export interface TabDef extends PageTabItem {
  /** Tab 内容; 不传 → 渲染「待接入」占位 */
  render?: () => ReactNode
}

export default function TabbedPage({
  tabs,
  defaultTab,
  myPerms,
  isOwner,
  className = '',
}: {
  tabs: TabDef[]
  /** URL 无 ?tab= 时落到哪个 Tab; 未传或不可见则取第一个可见 Tab */
  defaultTab?: string
  /** 当前用户权限点集合; null(未加载)时不做权限过滤, 避免闪隐 */
  myPerms?: Set<string> | null
  /** owner 判定回调(用于 ownerOnly) */
  isOwner?: () => boolean
  className?: string
}) {
  const [params, setParams] = useSearchParams()

  const visible = tabs.filter((t) => {
    if (t.ownerOnly && isOwner && !isOwner()) return false
    // myPerms 为 null(拉取失败/未加载) → 不隐藏, 回退角色判断, 不误伤
    if (t.perm && myPerms && !myPerms.has(t.perm)) return false
    return true
  })

  const fallback =
    defaultTab && visible.some((t) => t.key === defaultTab)
      ? defaultTab
      : visible[0]?.key ?? ''

  const requested = params.get('tab')
  const resolved =
    requested && visible.some((t) => t.key === requested) ? requested : fallback

  // URL 带了非法/不可见的 tab → 规范化到 fallback(replace, 不留历史垃圾)
  // 仅当 URL 显式带 tab 时才重定向, 避免无 tab 时反复重定向
  if (requested && requested !== resolved) {
    const next = new URLSearchParams(params)
    if (fallback) next.set('tab', fallback)
    else next.delete('tab')
    return <Navigate to={{ search: next.toString() }} replace />
  }

  const onChange = (key: string) => {
    const next = new URLSearchParams(params)
    next.set('tab', key)
    setParams(next) // push: 可后退
  }

  if (visible.length === 0) {
    return (
      <div className="py-8 text-center text-[13px] text-muted-foreground">
        无可用页签(权限不足)
      </div>
    )
  }

  const activeTab = visible.find((t) => t.key === resolved)

  return (
    <div className="space-y-3">
      <PageTabs
        tabs={visible.map(({ key, label, icon, hint }) => ({ key, label, icon, hint }))}
        value={resolved}
        onChange={onChange}
        className={className}
      />
      <div className="min-w-0">
        {activeTab?.render ? (
          activeTab.render()
        ) : (
          <div className="py-8 text-center">
            <div className="text-[13px] font-medium text-foreground">
              「{activeTab?.label ?? resolved}」待接入
            </div>
            <p className="mt-1 text-[12px] text-muted-foreground">
              该页签尚未挂组件, 接入后此处自动渲染(不编造内容)
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
