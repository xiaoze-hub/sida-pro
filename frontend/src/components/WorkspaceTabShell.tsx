import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { useToast } from '@sida/base-ui/components/ui/toast'

import TabPane from './TabPane'
import WorkspaceTabs from './WorkspaceTabs'
import { useI18n } from '@/hooks/useI18n'
import {
  MAX_TABS,
  closeOtherTabs,
  closeTab,
  closeTabsToRight,
  loadTabs,
  pickEvictionTarget,
  routeTab,
  saveTabs,
  tabLabel,
  upsertTab,
  type WorkspaceTab,
} from '@/lib/tab-shell'

/**
 * 页签式工作台壳(Tab Shell, 2026-10-15 → 真 keep-alive 2026-10-15)。
 *
 * 用户诉求原话:「还需要做功能菜单的页签，这样每次关闭页签，而不是把整个功能全关掉」,
 * 后续新裁决:「需要支持同时存在多个页签，点击哪个页签就进入里面的内容」。
 *
 * 怎么接线的:
 *   · **路由是唯一事实源**: 打开/切换页签都靠"路由变了 → 查页签集"。所以
 *     左侧菜单(以及任何 NavLink/命令面板/推送链接的跳转)**不用改一行**。
 *   · **每个页签一个常驻面板**(`TabPane`): 已开页签的页面组件**同时挂载**, 非活动页签
 *     `display:none` 隐藏但**不卸载** ⇒ 切走再切回, 输入框/展开态/滚动位置原样还在。
 *     同一 `key`(= tabKeyOf) 复用同一组件实例, 不会重复创建。
 *   · **地址栏 = 活动页签的地址**: 活动页签内的导航同步到地址栏(刷新/收藏/后退回到原处);
 *     隐藏页签内的导航不动地址栏。跨页签导航一律交给外层路由(开新页签或切到已有页签)。
 *   · 关闭: `closeTab` 给出"相邻页签"落点 —— 只关这一个功能页, 首页('/')常驻, 永不"全关掉"。
 *     关当前页签用 `replace` 导航: 浏览器后退**不会把刚关掉的页签复活**。
 *   · **上限 8(LRU)**: 超限挤掉最久未访问的**非首页**页签, 并**明确提示**(不静默丢用户的页), 防内存跑飞。
 *   · 持久化: 任何一次开/切/关都写 localStorage, 刷新后原样恢复(标题由路由现算, 不存文案)。
 *   · 非页签路由(公开文档面/老别名/未知)**不占页签**: 在页签面板之外单独渲染(整页)。
 *
 * `children` 是全部路由(`<Routes>`), 会被每个页签面板各渲染一份 —— 面板内的 `useLocation`
 * 读到的是**该页签自己的地址**(见 `TabPane` 的软路由), 所以每个页签各显示各自的功能页。
 * 壳层 chrome(如顶栏心跳条)经 `header` 传入, **只渲染一次**, 不随页签复制。
 */
export default function WorkspaceTabShell({
  children,
  header,
}: {
  children: ReactNode
  /** 页签栏与内容之间的壳层(只渲染一次, 不进页签面板) */
  header?: ReactNode
}) {
  const location = useLocation()
  const navigate = useNavigate()
  const { t } = useI18n()
  const { toast } = useToast()
  const [tabs, setTabs] = useState<WorkspaceTab[]>(() => loadTabs())
  // 用 ref 取"最新页签集"与文案: 路由 effect 只依赖路由值, 但要在**副作用里**算出被上限挤掉的页签并提示。
  const tabsRef = useRef(tabs)
  tabsRef.current = tabs
  const tRef = useRef(t)
  tRef.current = t
  const toastRef = useRef(toast)
  toastRef.current = toast
  // LRU 访问顺序(最旧在前): 与页签数组顺序解耦 —— 数组顺序=打开顺序(视觉稳定),
  // 淘汰按「最久没被访问」, 避免用户在 A/B 之间反复切时 A 被当成"最早的"挤掉。
  const touchRef = useRef<string[]>([])

  const spec = routeTab(location.pathname, location.search)
  const specKey = spec?.key ?? null
  const specPath = spec?.path ?? null
  const outerPath = `${location.pathname}${location.search}`

  // 路由 → 页签集: 未开过则新开(含刷新后从存储恢复的那些), 已开过则激活并记住本页签内的最新地址。
  // 依赖用两个**原始值**(不是每次渲染都新建的 spec 对象), 否则 effect 每轮都跑。
  useEffect(() => {
    if (!specKey || !specPath) return
    const prev = tabsRef.current
    const touch = touchRef.current.filter((key) => key !== specKey)
    // 只在"真要新开一个且已到上限"时才算淘汰目标(切换已有页签不该触发淘汰)
    const isNewTab = !prev.some((tab) => tab.key === specKey)
    const evictKey =
      isNewTab && prev.length >= MAX_TABS ? pickEvictionTarget(prev, touch, specKey) : undefined
    const next = upsertTab(prev, { key: specKey, path: specPath }, MAX_TABS, evictKey)
    touchRef.current = [...touch, specKey]
    if (next === prev) return
    saveTabs(next)
    setTabs(next)
    // 上限把最早的**非首页**页签挤掉了(append 成功但总数没涨) ⇒ 明确告知, 不静默丢页。
    if (next.length === prev.length) {
      const evicted = prev.filter((p) => !next.some((n) => n.key === p.key))
      if (evicted.length > 0) {
        const tr = tRef.current
        const name = evicted.map((e) => tabLabel(e.path, tr)).join('、')
        toastRef.current(tr('tabs.limitToast', { name, max: String(MAX_TABS) }), 'info')
      }
    }
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

  // 右键菜单: 关闭其他 / 关闭右侧。落点规则与 onClose 同源 —— 当前页签被关掉才导航。
  const settleAfterBulk = useCallback(
    (nextTabs: WorkspaceTab[], preferred?: string) => {
      setTabs(nextTabs)
      saveTabs(nextTabs)
      if (!specKey || nextTabs.some((tb) => tb.key === specKey)) return
      const target =
        nextTabs.find((tb) => tb.key === preferred) ?? nextTabs[nextTabs.length - 1] ?? nextTabs[0]
      if (target) navigate(target.path, { replace: true })
    },
    [specKey, navigate],
  )

  const onCloseOthers = useCallback(
    (tab: WorkspaceTab) => {
      settleAfterBulk(closeOtherTabs(tabs, tab.key), tab.key)
    },
    [tabs, settleAfterBulk],
  )

  const onCloseRight = useCallback(
    (tab: WorkspaceTab) => {
      settleAfterBulk(closeTabsToRight(tabs, tab.key), tab.key)
    },
    [tabs, settleAfterBulk],
  )

  const onInnerNavigate = useCallback(
    (path: string) => {
      if (path === outerPath) return // 与外层已一致: 不产生多余历史/重渲染
      navigate(path)
    },
    [navigate, outerPath],
  )

  const onOuterNavigate = useCallback((path: string) => navigate(path), [navigate])
  const onOuterGo = useCallback((delta: number) => navigate(delta), [navigate])

  // 路由不是功能页(公开文档面/老别名/未知) → 不新开页签, 也不高亮任何一个:
  // 页签栏继续在(不闪), 已开的页签仍可见可切; 内容区走"页签之外"的整页渲染。
  return (
    <>
      <WorkspaceTabs
        tabs={tabs}
        activeKey={specKey}
        onActivate={onActivate}
        onClose={onClose}
        onCloseOthers={onCloseOthers}
        onCloseRight={onCloseRight}
      />
      {header}
      {tabs.map((tab) => (
        <TabPane
          key={tab.key}
          tabKey={tab.key}
          initialPath={tab.path}
          active={tab.key === specKey}
          outerLocation={location}
          onInnerNavigate={onInnerNavigate}
          onOuterNavigate={onOuterNavigate}
          onOuterGo={onOuterGo}
        >
          {children}
        </TabPane>
      ))}
      {spec ? null : <div data-testid="workspace-ephemeral">{children}</div>}
    </>
  )
}
