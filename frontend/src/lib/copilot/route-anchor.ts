/**
 * 停靠面板的**页面锚点**：当前路由是不是一张 A 股个股/指数工作台（2026-11-18）。
 *
 * 为什么要这一层：`AiCopilot` 过去只认两种锚点来源 —— URL 的 `?symbol=` 和
 * `CopilotCapsule` 派发的 `sida-open-chat` 事件。用户直接点右下角悬浮球打开面板、
 * 或者在面板开着的时候换股，锚点就一直是空的：请求体只有 `{content}`，
 * 参谋既不知道你在看哪只票，图上的那份快照也没有可归属的标的。
 *
 * 口径（全部来自路由本身，**不猜**）：
 *  - 路径必须是 `/stocks/:symbol`，且 symbol 是 6 位 ASCII 代码（A 股个股/指数同码，
 *    000001 可能是平安银行也可能是指数 —— 所以类型只认 `?type=`，不从代码推）；
 *  - `?type=board` 是板块（代码形态不同），`?type=index` 才是指数；
 *  - `?market=HK|US` 的页面不产出锚点：本锚点只服务 A 股 6 位码，
 *    把港股代码当 A 股锚点发出去，后端只会拿到一份对不上的身份；
 *  - 市场给 `CN`（工作台页面的口径，`?market=CN` 就是它）；交易所级 `SH/SZ/BJ`
 *    由后端按代码证明，页面不编。
 *
 * 纯函数：不读 `window`、不依赖 react-router —— 调用方（工作区）把
 * `location.pathname` / `location.search` 传进来，单测可以直接喂字符串。
 */

export interface RouteAnchor {
  symbol: string
  /** 市场口径（工作台页面就是 `CN`；HK/US 页面不产出锚点） */
  market: 'CN'
  /** 标的类型：只由 `?type=` 声明，`000001` 这种同码永远不从代码猜 */
  instrument: 'stock' | 'index'
}

/** 6 位 A 股代码（含北交所 4/8 开头），与 `SYMBOL_RE` 同口径 */
const ROUTE_SYMBOL_RE = /^[03468]\d{5}$/
const STOCK_PATH_RE = /^\/stocks\/([^/]+)\/?$/

export function readStockRouteAnchor(pathname: string, search: string): RouteAnchor | null {
  const hit = String(pathname ?? '').match(STOCK_PATH_RE)
  if (!hit) return null
  const raw = decodeURIComponent(hit[1] ?? '').trim()
  // 路由里可能带内部前缀（竞价表跳进来是 /stocks/USHT603922）：不是干净的 6 位码就不认，
  // 这里不替调用方"清洗" —— 清洗是页面的职责（`stripInternalPrefix`），锚点只接受可证身份。
  if (!ROUTE_SYMBOL_RE.test(raw)) return null
  const sp = new URLSearchParams(search ?? '')
  const market = (sp.get('market') || '').trim().toUpperCase()
  if (market === 'HK' || market === 'US') return null
  const type = (sp.get('type') || '').trim().toLowerCase()
  if (type === 'board') return null
  return { symbol: raw, market: 'CN', instrument: type === 'index' ? 'index' : 'stock' }
}
