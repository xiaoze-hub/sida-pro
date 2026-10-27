import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  NavigationType,
  UNSAFE_LocationContext,
  UNSAFE_NavigationContext,
  type Location,
  type NavigateOptions,
  type To,
} from 'react-router-dom'

import { TabActiveProvider } from '@sida/base-ui'

import { tabKeyOf } from '@/lib/tab-shell'

/**
 * 页签面板(Tab keep-alive 的核心件, 2026-10-15)。
 *
 * 用户新裁决(原话): 「需要支持同时存在多个页签，点击哪个页签就进入里面的内容」——
 * 即**真 keep-alive**: 每个已开页签的页面组件**同时挂载**, 点谁显谁, 状态不丢。
 *
 * 为什么不用嵌套 `<MemoryRouter>`: react-router v6 的 `<Router>` 有硬不变量
 * 「不能在 `<Router>` 里再套 `<Router>`」, 而页签壳本身活在 App 的 BrowserRouter 里。
 * 于是这里**直接用底层上下文**给每个页签造一套独立的软路由:
 *   · `UNSAFE_NavigationContext` = 本页签自己的 `navigator`(导航逻辑我们说了算);
 *   · `UNSAFE_LocationContext`  = 本页签自己的地址(隐藏页签的 `useLocation`/`useSearchParams`
 *     读到的是**它自己**的地址, 不会被活动页签的地址串味 —— 这是"多组件并存"能成立的关键)。
 *
 * 路由语义(与「路由是唯一事实源」一致):
 *   · 本页签内导航(同 key: 换 `?tab=l2`/同票换参数) → 在本页签内改地址; 若本页签是**活动的**,
 *     同时把地址同步给外层路由 → 地址栏即"当前活动页签的地址", 刷新/收藏/后退都能回到原处;
 *     隐藏页签内导航**不碰地址栏**。
 *   · 跨页签/公开面导航(目标 tabKey ≠ 本页签) → 不在本页签里渲染别的功能页, 一律**交给外层路由**
 *     (外层据此开新页签或切到已有页签)。这保证了「在首页点一只股票 → 开个股页签」不变成
 *     「在首页页签里长出个股工作台」。
 *   · 外层地址变化(菜单/命令面板/后退) → 若指向本页签且本页签是活动的, 采纳为新地址; 否则不动。
 *
 * 隐藏方式: `hidden` + `display:none` —— **不卸载**, 组件状态(输入框、展开态、滚动)原样保留。
 */
export interface TabPaneProps {
  /** 本页签身份(= tab-shell 的 tabKeyOf) */
  tabKey: string
  /** 初址(页签被打开/恢复时的地址; 只在挂载时用一次) */
  initialPath: string
  /** 是否活动(活动=可见; 非活动=隐藏但保留) */
  active: boolean
  /** 外层路由的当前地址(来自 WorkspaceTabShell 的 useLocation) */
  outerLocation: Location
  /** 本页签内导航且本页签活动 → 同步到地址栏 */
  onInnerNavigate: (path: string) => void
  /** 跨页签导航 → 交给外层路由(开/切页签) */
  onOuterNavigate: (path: string) => void
  /** `navigate(-1)` 一类前进后退 → 交给外层路由(它才有真历史) */
  onOuterGo: (delta: number) => void
  children: ReactNode
}

/** 把 react-router 的 `To` 归一成 URL 字符串(useNavigate 传进来的已是解析过的 Path 对象) */
function urlOf(to: To): string {
  if (typeof to === 'string') return to || '/'
  let url = to.pathname ?? ''
  if (to.search) url += to.search.startsWith('?') ? to.search : `?${to.search}`
  if (to.hash) url += to.hash.startsWith('#') ? to.hash : `#${to.hash}`
  return url || '/'
}

/** 拆 URL → pathname / search / hash(与 tab-shell.parseTabPath 同口径, 但保留 hash) */
function splitUrl(url: string): { pathname: string; search: string; hash: string } {
  let rest = url || '/'
  let hash = ''
  const h = rest.indexOf('#')
  if (h >= 0) {
    hash = rest.slice(h)
    rest = rest.slice(0, h)
  }
  let search = ''
  const q = rest.indexOf('?')
  if (q >= 0) {
    search = rest.slice(q)
    rest = rest.slice(0, q)
  }
  return { pathname: rest || '/', search, hash }
}

let paneKeySeq = 0
const nextKey = () => `pane-${++paneKeySeq}`

export default function TabPane({
  tabKey,
  initialPath,
  active,
  outerLocation,
  onInnerNavigate,
  onOuterNavigate,
  onOuterGo,
  children,
}: TabPaneProps) {
  const [location, setLocation] = useState<Location>(() => {
    const { pathname, search, hash } = splitUrl(initialPath)
    // key='default' 对齐 react-router「会话第一条历史」语义 —— `useBackTarget` 据此判断
    // "站内跳来的" vs "直接打开的", 别让每个页签的初址都被当成可后退的站内跳转。
    return { pathname, search, hash, state: null, key: 'default' }
  })

  // 回调/最新值放 ref: navigator 只建一次(引用稳定), 免得每次渲染换 navigator 让子组件导航失效。
  const locationRef = useRef(location)
  locationRef.current = location
  const activeRef = useRef(active)
  activeRef.current = active
  const tabKeyRef = useRef(tabKey)
  tabKeyRef.current = tabKey
  const innerNavRef = useRef(onInnerNavigate)
  innerNavRef.current = onInnerNavigate
  const outerNavRef = useRef(onOuterNavigate)
  outerNavRef.current = onOuterNavigate
  const outerGoRef = useRef(onOuterGo)
  outerGoRef.current = onOuterGo

  const navigatePane = useCallback((to: To, state: unknown, replace: boolean) => {
    const url = urlOf(to)
    const { pathname, search, hash } = splitUrl(url)
    // 跨页签 → 不在本页签渲染别人的功能页; 交给外层(开/切到对应页签)
    if (tabKeyOf(pathname, search) !== tabKeyRef.current) {
      outerNavRef.current(`${pathname}${search}${hash}`)
      return
    }
    const cur = locationRef.current
    const same = cur.pathname === pathname && cur.search === search && cur.hash === hash
    if (same && state == null) return // 完全无变化: 不重渲染
    setLocation((prev) => ({
      pathname,
      search,
      hash,
      state: state ?? null,
      key: replace ? prev.key : nextKey(),
    }))
    // 活动页签: 地址栏跟着走(单一事实源 = 地址栏); 隐藏页签: 绝不改地址栏
    if (activeRef.current) innerNavRef.current(`${pathname}${search}${hash}`)
  }, [])

  const navigator = useMemo(
    () => ({
      createHref: (to: To) => urlOf(to),
      encodeLocation: (to: To) => splitUrl(urlOf(to)),
      go: (delta: number) => outerGoRef.current(delta),
      push: (to: To, state?: unknown, _opts?: NavigateOptions) => navigatePane(to, state, false),
      replace: (to: To, state?: unknown, _opts?: NavigateOptions) => navigatePane(to, state, true),
    }),
    [navigatePane],
  )

  // 外层地址(指向本页签时) → 采纳: 覆盖"刷新恢复 / 后退前进 / 菜单跳到同一页签的新地址"
  useEffect(() => {
    if (!active) return
    const { pathname, search, hash } = outerLocation
    if (tabKeyOf(pathname, search) !== tabKey) return
    const cur = locationRef.current
    if (cur.pathname === pathname && cur.search === search && cur.hash === hash) return
    setLocation({ pathname, search, hash, state: outerLocation.state ?? null, key: cur.key })
  }, [active, outerLocation, tabKey])

  const navigationContext = useMemo(
    () => ({ basename: '/', navigator, static: false, future: { v7_relativeSplatPath: false }, useTransitions: false }),
    [navigator],
  )
  const locationContext = useMemo(() => ({ location, navigationType: NavigationType.Pop }), [location])

  return (
    <div
      data-testid="workspace-tab-pane"
      data-tab-key={tabKey}
      data-active={active ? 'true' : 'false'}
      hidden={!active}
      className={active ? undefined : 'hidden'}
    >
      <TabActiveProvider active={active}>
        <UNSAFE_NavigationContext.Provider value={navigationContext}>
          <UNSAFE_LocationContext.Provider value={locationContext}>
            {children}
          </UNSAFE_LocationContext.Provider>
        </UNSAFE_NavigationContext.Provider>
      </TabActiveProvider>
    </div>
  )
}
