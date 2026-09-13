// 侧栏/底栏导航项"选中态"纯函数(2026-09-13 修复: 「行情」项永不选中)。
//
// 背景: navItems 的「行情」项现指向 `/stocks/000001?type=index`(个股工作台,
// 默认上证指数)。旧判定是 `location.pathname.startsWith(to)`, 而 pathname 永不含
// query → 含 `?` 的 to 恒不命中, 「行情」在桌面侧栏与移动底栏都不会高亮。
//
// 规则(三条, 从简):
//   1. `to === '/'` → 仅 pathname 恰为 `/` 时选中(保持原特殊处理不变);
//   2. 取 `to` 的 pathname 部分(`to.split('?')[0]`, 无 query 时即 `to` 自身);
//   3. 该 pathname 落在 `/stocks` 下(等于 `/stocks` 或以 `/stocks/` 开头)时,
//      视为**整个 /stocks 分区**命中 —— `/stocks/:symbol` 是同一工作台的不同
//      symbol(000001 / 002636 / 600519 ...), 单看前缀无法覆盖其它 symbol;
//      其余项一律沿用原前缀语义 `pathname.startsWith(toPath)`, 行为零变化。
export function isNavItemActive(to: string, pathname: string): boolean {
  if (to === '/') return pathname === '/'
  const toPath = to.split('?')[0]
  if (toPath === '/stocks' || toPath.startsWith('/stocks/')) {
    return pathname === '/stocks' || pathname.startsWith('/stocks/')
  }
  return pathname.startsWith(toPath)
}
