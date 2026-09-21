/**
 * 返回入口(2026-09-20 用户报: "从持仓页面选择股票, 进入行情页, 但是没有返回按钮, 返回持仓页,
 * 不太方便")。
 *
 * 背景: 工作台改版时按 spec §1.3 把旧页骨架(返回/标题/刷新)并进带1 `HeaderBand`, 但**返回**这一项
 * 没被接过去 —— 于是所有列表页(持仓/机会/首页/暗盘/热力图)跳进行情页后, 只能靠浏览器后退。
 *
 * 做法: 跳转时把"来路"放进 router state, 行情页据此渲染返回按钮。
 * 三级降级(每一级都诚实, 不假装知道来路):
 *   ① 有 state(站内列表跳来的) → 「← 返回持仓」, 点击回那个**具体页面**(不是 history 退一步,
 *      所以即使中途又点了别的股票, 也能一步回到持仓页);
 *   ② 没 state 但确实是从站内某处跳来的(`location.key !== 'default'`) → 「← 返回」= history 退一步;
 *   ③ 直接输入网址/刷新/收藏进入(首条 history) → **不渲染**(此处"返回"会退到站外或无处可退,
 *      不如不给)。
 */
import { useLocation, useNavigate } from 'react-router-dom'

/** 「来路」: 路径 + 给用户看的名字(按钮文案「返回X」)。 */
export interface BackTarget {
  path: string
  label: string
}

/** 传给 `navigate` 的 state: `navigate(to, { state: navBackState('/portfolio', '持仓') })`。 */
export interface NavBackState {
  navBack?: BackTarget
}

export function navBackState(path: string, label: string): NavBackState {
  return { navBack: { path, label } }
}

/**
 * 行情页(个股/指数/板块)的返回入口。无可用来路时返回 `null` —— **不渲染**按钮。
 * 注意: 本 hook 内部按固定顺序调用 hooks, 不做条件调用。
 */
export function useBackTarget(): { label: string; onBack: () => void } | null {
  const location = useLocation()
  const navigate = useNavigate()
  const state = location.state as NavBackState | null
  const from = state?.navBack
  if (from?.path) {
    return { label: `返回${from.label}`, onBack: () => navigate(from.path) }
  }
  // React Router 对"会话里第一条历史"固定给 key='default' ⇒ 用来区分"站内跳来的"和"直接打开的"
  if (location.key !== 'default') {
    return { label: '返回', onBack: () => navigate(-1) }
  }
  return null
}
