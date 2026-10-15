import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import WorkspaceTabs from './WorkspaceTabs'
import {
  closeTab,
  loadTabs,
  routeTab,
  saveTabs,
  upsertTab,
  type WorkspaceTab,
} from '@/lib/tab-shell'

/**
 * 页签式工作台壳(Tab Shell, 2026-10-15)。
 *
 * 用户诉求原话:「还需要做功能菜单的页签，这样每次关闭页签，而不是把整个功能全关掉」。
 *
 * 怎么接线的:
 *   · **路由是唯一事实源**: 打开/切换页签都靠"路由变了 → 查页签集"。所以
 *     左侧菜单(以及任何 NavLink/命令面板/推送链接的跳转)**不用改一行** —— 跳到没开过的
 *     功能页就新开页签, 跳到已开过的就切到它(`upsertTab` 按 key 去重)。
 *   · 页签集**在内容区外层**, 不碰任何页面内部(个股工作台 StockWorkbench 内部布局零改动)。
 *   · 关闭: `closeTab` 给出"相邻页签"落点 —— 只关这一个功能页, 首页('/')常驻, 永不"全关掉"。
 *     关当前页签用 `replace` 导航: 浏览器后退**不会把刚关掉的页签复活**。
 *   · 持久化: 任何一次开/切/关都写 localStorage, 刷新后原样恢复(标题由路由现算, 不存文案)。
 *   · 公开文档面(/terms、/tiers)不页签化: 整页渲染, 页签栏也不占位。
 */
export default function WorkspaceTabShell({ children }: { children: ReactNode }) {
  const location = useLocation()
  const navigate = useNavigate()
  const [tabs, setTabs] = useState<WorkspaceTab[]>(() => loadTabs())

  const spec = routeTab(location.pathname, location.search)
  const specKey = spec?.key ?? null
  const specPath = spec?.path ?? null

  // 路由 → 页签集: 未开过则新开(含刷新后从存储恢复的那些), 已开过则激活并记住本页签内的最新地址。
  // 依赖用两个**原始值**(不是每次渲染都新建的 spec 对象), 否则 effect 每轮都跑。
  useEffect(() => {
    if (!specKey || !specPath) return
    setTabs((prev) => {
      const next = upsertTab(prev, { key: specKey, path: specPath })
      if (next !== prev) saveTabs(next)
      return next
    })
  }, [specKey, specPath])

  const onActivate = useCallback(
    (tab: WorkspaceTab) => {
      if (tab.key === specKey) return
      navigate(tab.path)
    },
    [specKey, navigate],
  )

  const onClose = useCallback(
    (tab: WorkspaceTab) => {
      // 关页签只关**这一个**功能页: 相邻页签接管, 首页兜底 —— 不存在"把整个功能全关掉"。
      const { tabs: nextTabs, nextActiveKey } = closeTab(tabs, tab.key, specKey ?? '')
      setTabs(nextTabs)
      saveTabs(nextTabs)
      if (!nextActiveKey) return // 关的不是当前页签(或关闭被拒): 内容区不动
      const target = nextTabs.find((tb) => tb.key === nextActiveKey)
      if (target) navigate(target.path, { replace: true })
    },
    [tabs, specKey, navigate],
  )

  // 路由不是功能页(公开文档面/老别名/未知) → 不新开页签, 也不高亮任何一个:
  // 页签栏继续在(不闪), 已开的页签仍可见可切。
  return (
    <>
      <WorkspaceTabs tabs={tabs} activeKey={specKey} onActivate={onActivate} onClose={onClose} />
      {children}
    </>
  )
}
