import { useCallback, useEffect, useMemo, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { fetchAPI, insightApi } from '@panwatch/api'
import { cn } from '@panwatch/base-ui'
import { TechnicalBadge, technicalToneFromSuggestionAction } from '@panwatch/biz-ui/components/technical-badge'
import { fmtAmount, fmtSignedAmount } from '@panwatch/biz-ui/lib/ladder-format'
import { buildKlineSuggestion } from '@/lib/kline-scorer'
import { safeFixed, safeNum, safePrice } from '@/lib/format'
import type { KlineSummaryData } from '@panwatch/biz-ui/components/kline-summary-dialog'
import { showStockOnly, type WorkbenchTab, type WorkbenchType } from '@/lib/workbench-tabs'

/**
 * 带1 顶部信息带(v0.5.96, 三合一 spec §4.1): 个股/指数/板块**共享**。
 *
 * 刷新语义(Task 7 复审 Finding 1, 控制器裁定「页面级刷新」): 刷新按钮除了刷**自己**的行情
 * (`tick`), 还回调 `onRefresh` —— 由页面(`StockWorkbench`)用 `refreshKey` 重挂载正文子树,
 * 让指数/板块正文与个股带2(`KlineChart`/`QuickRail`)一起重新取数。旧独立页的「刷新」只管
 * 自己(正文有各自按钮), 骨架并入带1 后若不外抛, 正文就**只能在挂载/换标的时取数**
 * (盘中手动刷新缺失 = 回归)。`onRefresh` 可选: 单独使用 `HeaderBand` 且不传时维持旧行为。
 *
 * 结构(**个股**): 顶行(名称 + 代码 + 现价 + 涨跌色 + 类型三按钮 + 刷新)
 *       + 快照行(今开/最高/最低/成交量/成交额/振幅/换手率/量比/总市值/流通市值/
 *                PE(动)/PE(TTM)/PB/股息率/涨停价/封单额/连板)
 *       + 技术指标买卖建议条(仅 type=stock; 点击跳「建议」标签)。
 *
 * **非个股(指数/板块)只渲染类型三按钮 + 刷新 + 中性代码标签** —— 见下「同码不同标的」闸门。
 *
 * 类型分流(spec 绑定条款): `type !== 'stock'` 时**隐藏**(非渲染成 `--`)8 个个股专属 cell ——
 * `float_market_cap`/`pe_dynamic`/`pe_ttm`/`pb`/`dividend_yield`/`limit_price`/`seal_amount`/`limit_boards`;
 * `mapSnapshot` 保持 type-agnostic（纯函数不动），分流在渲染侧 `visibleSnapshotCells`。
 *
 * **同码不同标的闸门(Important, 控制器裁定)**: `/stocks/000001?type=index` 的 `000001` 是
 * **上证指数**, 而同代码的 `GET /quotes/000001` 返回的是**个股 平安银行** —— 在带1 里顶行画
 * `quote.name/current_price/change_pct`、快照行画 `quote.open/high/low/turnover` 就是把**另一个
 * 标的**的数据当成本页标的画出来(本仓明令禁止的"同码不同标的"泄漏)。故 `type !== 'stock'` 时:
 *  ① **不发** `GET /quotes/{s}`(`isStock` 闸门, 与 CN-only 的 `cnStockDataEnabled` 同源);
 *  ② **不渲染**顶行的 `quote.*`(名称/现价/涨跌)与整条快照行(那些 cell 的值全部来自 `quote`/
 *     `more`/`l2`, 对指数/板块只会是 `--` 噪声);
 *  ③ 只保留类型三按钮 + 刷新 + **裸代码**(中性标签, **非** quote —— 纯 `symbol` 字符串, 无取数)。
 * 指数/板块的**名称与涨跌值由正文 `IndexBody`/`BoardBody` 拥有**(它们按正确端点取正确标的),
 * 带1 绝不代取。个股分支(`type === 'stock'`)渲染与取数**逐字节不变**。
 *
 * 数据面(全部真数据, 缺值 `--`, 绝不编造):
 *  - GET /quotes/{symbol}            → current_price/change_pct/open_price/high_price/low_price/turnover
 *                                      + **volume(成交量, 手)/prev_close(昨收, 振幅分母)**(遗留⑤)
 *                                      (**仅个股** —— 指数/板块同代码返回的是另一标的, 不发)
 *  - GET /quotes/{symbol}/more-info  → turnover_rate/volume_ratio/total_market_value(亿)/circulating_market_value(亿)
 *  - GET /stocks/{symbol}/l2         → more.zt_price(涨停价) + more.pe_dynamic/pe_ttm/pb/dividend_yield/ever_zt_count
 *                                      + **more.fcamo(封单额, 元)** + **snapshot.high/low/last_close(振幅的 CN 回退源)**
 *                                      (遗留⑤: 都是**同一条**响应里的字段 ⇒ 未新增任何请求; 自包含, 不等兄弟组件回喂)
 *  - GET /klines/{symbol}/summary    → buildKlineSuggestion(技术面建议)
 * **封单额归属(遗留⑤ 去重裁定)**: `DATA_OWNERSHIP.seal_amount = 'band1.snapshot'` ⇒ 本快照行是
 * 全站**唯一**拥有面; 右栏 `QuickRail` 的「盘口速览」封单行**已删**(该卡只留 主力净额 + 五档)。
 * **快照时钟(KI-059 方案 B, 2026-09-18)**: 封单额/涨停价/连板等来自 `/l2` 的读数在带1 **无 30s 轮询**
 * (取数只在挂载/换标的/手动刷新时跑) ⇒ 盘中封单额可能秒级剧变但屏上不动。方案 B = **不轮询**,
 * 在快照行尾显式标 `快照 HH:MM:SS`(`as_of` 取数时刻) —— 让用户知道这些数是什么时候的,
 * 不把首屏时刻伪装成实时。`as_of` 缺失则不渲染时钟(不编时间)。
 * **CN-only**: more-info 与 /l2 只在 `个股 + CN` 时发(`cnStockDataEnabled`)—— more-info 对非 CN 后端 400,
 * /l2 是 CN TQ RPC; 否则同代码的境外标的会把 CN 涨停价/PE/PB 画成自己的。
 * 任一接口失败**保留旧值**(stale-on-error), 不把失败渲染成 0/编造值。
 */

/** 快照行单元格: key 稳定(测试/去重表锚点), label 面向用户, value 一律已格式化字符串。 */
export interface SnapshotCell {
  key: string
  label: string
  value: string
}

/** GET /quotes/{symbol} 响应(字段名对齐 src/web/api/quotes.py::_quote_to_response)。 */
export interface QuoteSnapshot {
  name?: string | null
  /** 现价(元) */
  current_price?: number | string | null
  /** 涨跌幅(%) */
  change_pct?: number | string | null
  /** 今开(元) */
  open_price?: number | string | null
  /** 最高(元) */
  high_price?: number | string | null
  /** 最低(元) */
  low_price?: number | string | null
  /** 昨收(元) —— 振幅的分母(遗留⑤) */
  prev_close?: number | string | null
  /**
   * 成交量(**手**) —— 遗留⑤ 新增 cell 的数据面。
   * 单位证据: `packages/marketdata/src/marketdata/vendors/tencent.py:95`(「最新价/成交量(手)/成交额(元)」)
   * + `src/agents/intraday_monitor.py:1072`(同一 quote 字段渲染成「成交量：{volume} 手」)
   * + `types.py:51-52`(内/外盘亦标「手」)。
   */
  volume?: number | string | null
  /** 成交额(元); 单位证据见 packages/marketdata .../types.py::Quote.turnover */
  turnover?: number | string | null
}

/**
 * more-info 侧字段(packages/marketdata types.py::MoreInfo 透传)。
 * 注意 total_market_value / circulating_market_value 单位都是**亿**(Zsz/Ltsz 原值未换算),
 * 展示必须带「亿」, 不可再过 fmtAmount(元口径)。
 */
export interface MoreInfoSnapshot {
  /** 换手率(%) — fHSL */
  turnover_rate?: number | string | null
  /** 量比 — fLianB */
  volume_ratio?: number | string | null
  /** 总市值(亿) — Zsz */
  total_market_value?: number | string | null
  /** 流通市值(亿) — Ltsz */
  circulating_market_value?: number | string | null
}

/**
 * `/stocks/{symbol}/l2` 的 `more` 段(spec §1.2 快照行后半段的真数据面; 仅个股/CN 可用)。
 * 字段证据: `src/core/stock_l2.py::fetch_more`(**:80-100**; 2026-09-14 复审订正行号, 原写 75-98) + 源键
 * packages/marketdata/.../vendors/tq.py::_parse_more_info:203-219(ZTPrice/DynaPE/StaticPE_TTM/PB_MRQ/DYRatio/EverZTCount)。
 * 单位: 上述几个字段是 `_f()` **原值透传**(不换算); 但 `fetch_more` 整体**并非**都不换算 ——
 * 其 docstring 明写「万元→元 仅 FCAmo/OpenAmo」, 即下方 `fcamo`(封单额)**已 ×1e4 成元**(见该字段注)。
 */
export interface L2MoreSnapshot {
  /** 涨停价(元) — ZTPrice */
  zt_price?: number | string | null
  /** PE(动) — DynaPE */
  pe_dynamic?: number | string | null
  /** PE(TTM) — StaticPE_TTM */
  pe_ttm?: number | string | null
  /** PB(市净率) — PB_MRQ */
  pb?: number | string | null
  /** 股息率(%) — DYRatio */
  dividend_yield?: number | string | null
  /** 连板天数(个) — EverZTCount */
  ever_zt_count?: number | string | null
  /**
   * 封单额(**元**) — FCAmo(遗留⑤: 去重表 `seal_amount → band1.snapshot`, 右栏「盘口速览」的
   * 封单行已删, 本 cell 是全站唯一拥有面)。后端已把万元换算成元:
   * `src/core/stock_l2.py::fetch_more`(`"fcamo": (fcamo * 1e4)`); **可为负**(FCAmo<0 = 跌停封单),
   * `0` 是真值(未封板)不是缺值 ⇒ 用带符号的 `fmtSignedAmount` 渲染。
   */
  fcamo?: number | string | null
}

/**
 * `/stocks/{symbol}/l2` 的 `snapshot` 段(遗留⑤: 振幅的 CN 回退数据面)。
 * 字段证据: `src/core/stock_l2.py::fetch_snapshot`(Max→high / Min→low / LastClose→last_close /
 * Volume→volume, 均 `_f()` 原值透传, 缺字段为 `None`)。
 *
 * 注: 本带**只用** high/low/last_close 三个价格(振幅), **不用** `snapshot.volume` ——
 * 通达信 `get_market_snapshot` 的 `Volume` 单位在本仓无实测证据(stock_l2.py 原值透传、无单位注),
 * 单位不明就不画(never fabricate); 成交量走 `/quotes/{s}.volume`(单位=手, 证据见 `QuoteSnapshot.volume`)。
 */
export interface L2QuoteSnapshot {
  /** 最高(元) — Max */
  high?: number | string | null
  /** 最低(元) — Min */
  low?: number | string | null
  /** 昨收(元) — LastClose */
  last_close?: number | string | null
  /** 成交量(单位未证实, 见接口头注 —— 本带不渲染它) — Volume */
  volume?: number | string | null
}

/**
 * 成交量(**手**)→ 紧凑显示(遗留⑤)。单位=手 的证据见 `QuoteSnapshot.volume`。
 * 缺值/脏值 → `--`(不编, 不当 0); `0` 是真值(停牌/未开盘)⇒ 渲染 `0手`。
 * R6: 小数位一律走 `@/lib/format` 的 `safeFixed`(本文件零裸调用)。
 */
export function fmtVolumeHands(v: unknown): string {
  const n = safeNum(v)
  if (n == null) return '--'
  const abs = Math.abs(n)
  if (abs >= 1e8) return `${safeFixed(n / 1e8, 2)}亿手`
  if (abs >= 1e4) return `${safeFixed(n / 1e4, 2)}万手`
  return `${safeFixed(n, 0)}手`
}

/**
 * 振幅(%) = **(最高 − 最低) / 昨收 × 100**(遗留⑤; 公式即 A 股通行口径)。
 *
 * **同源纪律**: 三个入参必须来自**同一个响应** —— 优先 `/quotes/{s}` 的
 * `high_price`/`low_price`/`prev_close`; 三者任一缺失时整体回退 `/stocks/{s}/l2` 的
 * `snapshot.high`/`low`/`last_close`(通达信同源快照, CN-only)。
 * **绝不跨源混用**(如 quotes.high 配 l2.last_close): 两个源的取值时刻不同, 拼出来的振幅是假数。
 *
 * 守卫: 昨收缺失或为 0(除零)、任一价缺失、结果非有限 ⇒ `null`(渲染层出 `--`), 不猜、不返回 0。
 */
export function amplitudePct(
  q?: QuoteSnapshot | null,
  l2snap?: L2QuoteSnapshot | null,
): number | null {
  const amp = (high: unknown, low: unknown, lastClose: unknown): number | null => {
    const h = safeNum(high)
    const l = safeNum(low)
    const c = safeNum(lastClose)
    if (h == null || l == null || c == null) return null
    // **非正价格守卫**(复审 Minor 2): 停牌/未开盘时腾讯源给 `high="0.00"`/`low="0.00"`,
    // `_to_float` 返回 **0.0 而不是 None** ⇒ `(0-0)/c*100 = 0` 会在屏上渲染成「振幅 0.00%」,
    // 把"没有数据"伪装成"今天零波动"(一个**算出来的**假读数, 比直显 0 更容易被当真)。
    // 与本仓 vendor 纪律同源: `packages/marketdata/.../vendors/tencent.py:4`「解析层对缺失/空字段
    // 一律保留 None, 绝不回退 0(0 价参与算术会伪造假暴跌)」。c<=0 同时兼掉除零与负价脏数据。
    if (h <= 0 || l <= 0 || c <= 0) return null
    const v = ((h - l) / c) * 100
    return Number.isFinite(v) ? v : null
  }
  const fromQuote = amp(q?.high_price, q?.low_price, q?.prev_close)
  if (fromQuote != null) return fromQuote
  return amp(l2snap?.high, l2snap?.low, l2snap?.last_close)
}

/** 快照行纯函数: 缺值/脏值一律 `--`(不编, 不渲染 NaN)。格式化一律走 @/lib/format 的 safe* 系列(R6)。 */
export function mapSnapshot(
  q?: QuoteSnapshot | null,
  more?: MoreInfoSnapshot | null,
  l2?: L2MoreSnapshot | null,
  /**
   * `/stocks/{s}/l2` 的 `snapshot` 段(遗留⑤ 新增第 4 参, 可选 ⇒ 既有调用方逐字不变)。
   * 目前只被 振幅 用作 CN 回退源(见 `amplitudePct` 的同源纪律)。
   */
  l2snap?: L2QuoteSnapshot | null,
): SnapshotCell[] {
  const quote = q ?? {}
  const info = more ?? {}
  const l2m = l2 ?? {}
  const changeNum = safeNum(quote.change_pct)
  const turnoverRate = safeNum(info.turnover_rate)
  const marketCap = safeNum(info.total_market_value)
  const floatCap = safeNum(info.circulating_market_value)
  const dividendYield = safeNum(l2m.dividend_yield)
  const boards = safeNum(l2m.ever_zt_count)
  const amplitude = amplitudePct(q, l2snap)
  return [
    { key: 'price', label: '现价', value: safePrice(quote.current_price, 2) },
    {
      key: 'change_pct',
      label: '涨跌幅',
      value: changeNum == null ? '--' : `${changeNum >= 0 ? '+' : ''}${safeFixed(changeNum, 2)}%`,
    },
    { key: 'open', label: '今开', value: safePrice(quote.open_price, 2) },
    { key: 'high', label: '最高', value: safePrice(quote.high_price, 2) },
    { key: 'low', label: '最低', value: safePrice(quote.low_price, 2) },
    // 成交量(手): 只取 /quotes 的 volume(单位有实测证据); 不回退 /l2 的 snapshot.volume(单位未证实)
    { key: 'volume', label: '成交量', value: fmtVolumeHands(quote.volume) },
    { key: 'amount', label: '成交额', value: fmtAmount(safeNum(quote.turnover)) },
    // 振幅 = (最高 − 最低) / 昨收 × 100(公式与同源/除零守卫见 amplitudePct)
    {
      key: 'amplitude',
      label: '振幅',
      value: amplitude == null ? '--' : `${safeFixed(amplitude, 2)}%`,
    },
    {
      key: 'turnover',
      label: '换手率',
      value: turnoverRate == null ? '--' : `${safePrice(turnoverRate, 2)}%`,
    },
    { key: 'volume_ratio', label: '量比', value: safePrice(safeNum(info.volume_ratio), 2) },
    {
      key: 'market_cap',
      label: '总市值',
      value: marketCap == null ? '--' : `${safePrice(marketCap, 2)}亿`,
    },
    {
      key: 'float_market_cap',
      label: '流通市值',
      value: floatCap == null ? '--' : `${safePrice(floatCap, 2)}亿`,
    },
    // 估值三件套: 原样数值(PE/PB 可为负 = 亏损股真实口径), 缺值 `--`
    { key: 'pe_dynamic', label: 'PE(动)', value: safePrice(safeNum(l2m.pe_dynamic), 2) },
    { key: 'pe_ttm', label: 'PE(TTM)', value: safePrice(safeNum(l2m.pe_ttm), 2) },
    { key: 'pb', label: 'PB', value: safePrice(safeNum(l2m.pb), 2) },
    {
      key: 'dividend_yield',
      label: '股息率',
      value: dividendYield == null ? '--' : `${safePrice(dividendYield, 2)}%`,
    },
    { key: 'limit_price', label: '涨停价', value: safePrice(l2m.zt_price, 2) },
    // 封单额(元, 可为负 = 跌停封单; 0 = 未封板的真值): 去重表 seal_amount → band1.snapshot,
    // 右栏「盘口速览」的封单行已删 ⇒ 本 cell 是全站唯一拥有面(遗留⑤)。
    { key: 'seal_amount', label: '封单额', value: fmtSignedAmount(l2m.fcamo) },
    // 连板: 整数天(safeFixed(...,0) 四舍五入, 不加千分位——连板数上限个位数)
    { key: 'limit_boards', label: '连板', value: safeFixed(boards, 0) },
  ]
}

/** 现价/涨跌幅在顶行已醒目呈现 → 快照行去除, 守住「同一数据点只出现一处」(spec §一)。 */
const TOP_ROW_KEYS = new Set(['price', 'change_pct'])

/**
 * 个股专属 cell(仅 `type === 'stock'` 显示): 涨停价/连板来自 CN-only 的 `/stocks/{s}/l2`,
 * 流通市值/PE/PB/股息率是个股估值口径 —— 指数/板块无此概念, 渲染成 `--` 只是噪声,
 * 故**隐藏**而非占位。与 CN-only 数据面闸门(`cnStockDataEnabled`)同源:
 * 非同 CN 标的这些格本就取不到值, 一并消失比留 7 个 `--` 更诚实。
 * 过滤在**渲染侧**(不在 `mapSnapshot` 内做类型分流, 保持纯函数 type-agnostic)。
 */
const EQUITY_ONLY_KEYS = new Set([
  'float_market_cap',
  'pe_dynamic',
  'pe_ttm',
  'pb',
  'dividend_yield',
  'limit_price',
  // 遗留⑤: 封单额来自 CN-only 的 `/stocks/{s}/l2`.more.fcamo(且封单是涨停/跌停个股概念) ——
  // 指数/板块既取不到也无此概念, 与 涨停价/连板 同处置(**隐藏**, 不留 `--` 噪声)。
  'seal_amount',
  'limit_boards',
])

/**
 * 渲染侧可见 cell(纯函数, 供单测锚定): 顶行去重 + 个股专属 cell 的**类型分流**。
 * 非个股直接**不渲染** 8 个估值/涨停/封单额/连板 cell(而非渲染成 `--`)。
 * `mapSnapshot` 保持 type-agnostic 不变。参数顺序与 `mapSnapshot` 对齐(数据四参在前, `isStock` 收尾)。
 */
export function visibleSnapshotCells(
  q?: QuoteSnapshot | null,
  more?: MoreInfoSnapshot | null,
  l2?: L2MoreSnapshot | null,
  l2snap?: L2QuoteSnapshot | null,
  isStock = true,
): SnapshotCell[] {
  return mapSnapshot(q, more, l2, l2snap).filter(
    (c) => !TOP_ROW_KEYS.has(c.key) && (isStock || !EQUITY_ONLY_KEYS.has(c.key)),
  )
}

/**
 * CN-only 数据面闸门(纯函数, 供单测锚定): `/quotes/{s}/more-info` 对非 CN 后端直接 400,
 * `/stocks/{s}/l2` 是 CN TQ RPC —— 只有「个股 + CN」才允许发这两个请求,
 * 否则同代码的境外标的会把 CN 涨停价/PE/PB 画成自己的。
 */
export function cnStockDataEnabled(t?: WorkbenchType, market?: string): boolean {
  return showStockOnly(t ?? 'stock') && (market ?? 'CN') === 'CN'
}

const TYPE_OPTIONS: { id: WorkbenchType; label: string }[] = [
  { id: 'stock', label: '个股' },
  { id: 'index', label: '指数' },
  { id: 'board', label: '板块' },
]

interface L2Resp {
  more?: L2MoreSnapshot | null
  /** 遗留⑤: 同一份 `/l2` 响应的 `snapshot` 段(振幅的 CN 回退源) —— 不新增请求。 */
  snapshot?: L2QuoteSnapshot | null
  /** KI-059B: 取数时刻 ISO 串(后端 `/l2` 顶层字段, 与 QuickRail 同源)。 */
  as_of?: string | null
}

/** `as_of` ISO 串 → `HH:MM:SS`; 缺值/格式不符 → null(不猜、不编时间)。与 QuickRail::asOfClock 同口径。 */
export function asOfClock(iso?: string | null): string | null {
  if (!iso || iso.length < 19) return null
  const clock = iso.slice(11, 19)
  return /^\d{2}:\d{2}:\d{2}$/.test(clock) ? clock : null
}

export interface HeaderBandProps {
  symbol: string
  market?: string
  type?: WorkbenchType
  hasPosition?: boolean
  /**
   * 持仓态未知(T19): 真实持仓判定源在途/失败时由页面传 `true`。此时 `hasPosition` 的 `false`
   * **不代表"未持仓"**, 只是"按未持仓口径算评分"; 本带据此在建议条旁显式标注「持仓态未知」,
   * 不把未知说成未持仓。
   */
  positionUnknown?: boolean
  onTypeChange?: (t: WorkbenchType) => void
  onGotoTab?: (tab: WorkbenchTab) => void
  /**
   * 页面级刷新回调(Finding 1): 刷新按钮点火后, 页面借此重挂载正文子树
   * (指数/板块正文 + 个股带2), 使它们的挂载取数重新执行。不传 = 只刷本带行情。
   */
  onRefresh?: () => void
}

export default function HeaderBand({
  symbol,
  market = 'CN',
  type = 'stock',
  hasPosition = false,
  positionUnknown = false,
  onTypeChange,
  onGotoTab,
  onRefresh,
}: HeaderBandProps) {
  const isStock = showStockOnly(type)
  /** CN-only 数据面闸门: `/stocks/{s}/l2` 是 CN TQ RPC, more-info 对非 CN 直接 400 —— 同代码的境外标的绝不能发, 否则会把 CN 涨停价/PE/PB 画到别的标的上。 */
  const cnStock = cnStockDataEnabled(type, market)
  const [quote, setQuote] = useState<QuoteSnapshot | null>(null)
  const [more, setMore] = useState<MoreInfoSnapshot | null>(null)
  const [l2More, setL2More] = useState<L2MoreSnapshot | null>(null)
  /** 遗留⑤: `/stocks/{s}/l2` 的 `snapshot` 段(与 `l2More` **同一条响应**, 不新增请求)。 */
  const [l2Snap, setL2Snap] = useState<L2QuoteSnapshot | null>(null)
  /** KI-059B: `/l2` 取数时刻(快照时钟; 无轮询, 只在挂载/刷新时更新)。 */
  const [l2AsOf, setL2AsOf] = useState<string | null>(null)
  const [summary, setSummary] = useState<KlineSummaryData | null>(null)
  const [busy, setBusy] = useState(false)
  const [tick, setTick] = useState(0)

  // 换股/换类型必须先清上一标的的旧值(否则会把"A股的涨停价"画到"指数"上);
  // 手动刷新(tick)不清, 以保留 stale-on-error 的旧值语义。
  useEffect(() => {
    setQuote(null)
    setMore(null)
    setL2More(null)
    setL2Snap(null)
    setL2AsOf(null)
    setSummary(null)
  }, [symbol, market, isStock])

  useEffect(() => {
    if (!symbol) return
    let alive = true
    setBusy(true)
    const tasks: Promise<unknown>[] = []
    // `isStock` 闸门(同码不同标的): 指数/板块**绝不**发 `/quotes/{s}` —— 同代码的个股报价是
    // 另一标的(000001: 指数=上证指数 vs 个股=平安银行), 画进本页即"同码不同标的"泄漏。
    if (isStock) {
      tasks.push(
        insightApi.quote<QuoteSnapshot>(symbol, market).then((r) => {
          if (alive) setQuote(r ?? null)
        }),
      )
    }
    if (cnStock) {
      // CN-only 数据面(非同 CN 标的不得发): more-info 对非 CN 后端 400;
      // /stocks/{s}/l2 是 CN TQ RPC → 涨停价/PE/PB/股息率/连板 只对 CN 个股有意义。
      tasks.push(
        insightApi.moreInfo<MoreInfoSnapshot>(symbol, market).then((r) => {
          if (alive) setMore(r ?? null)
        }),
      )
      // Ruling A: 涨停价/PE/PB/股息率/连板 自包含取 /stocks/{s}/l2(不能依赖兄弟组件回喂)
      // 遗留⑤: 同一条响应里顺手取 `more.fcamo`(封单额)与 `snapshot`(振幅的 CN 回退源) —— **不新增请求**。
      tasks.push(
        fetchAPI<L2Resp>(`/stocks/${encodeURIComponent(symbol)}/l2`).then((r) => {
          if (!alive) return
          setL2More(r?.more ?? null)
          setL2Snap(r?.snapshot ?? null)
          setL2AsOf(r?.as_of ?? null)
        }),
      )
    }
    if (isStock) {
      tasks.push(
        insightApi.klineSummary<KlineSummaryData>(symbol, market).then((r) => {
          if (alive) setSummary(r ?? null)
        }),
      )
    }
    // 失败保留旧值(stale-on-error), 不编造、不清零
    void Promise.allSettled(tasks).then(() => {
      if (alive) setBusy(false)
    })
    return () => {
      alive = false
    }
  }, [symbol, market, isStock, cnStock, tick])

  // 刷新 = 本带行情重取(`tick`)+ 页面级广播(`onRefresh`, 让正文/带2 也重新取数)。见头注 Finding 1。
  const refresh = useCallback(() => {
    setTick((t) => t + 1)
    onRefresh?.()
  }, [onRefresh])

  const suggestion = useMemo(
    () => (isStock && summary ? buildKlineSuggestion(summary, hasPosition) : null),
    [isStock, summary, hasPosition],
  )

  const changeNum = safeNum(quote?.change_pct)
  const toneClass =
    changeNum == null
      ? 'text-muted-foreground'
      : changeNum > 0
        ? 'text-[--stock-up]'
        : changeNum < 0
          ? 'text-[--stock-down]'
          : 'text-muted-foreground'
  // 顶行去重(price/change_pct) + 个股专属 cell 的**类型分流**:
  // index/board 直接不渲染估值/涨停/封单额/连板(它们是 -- 噪声, 且指数/板块无此概念), 而非渲染成 `--`。
  const cells = visibleSnapshotCells(quote, more, l2More, l2Snap, isStock)
  const scoreText = suggestion ? `${suggestion.score >= 0 ? '+' : ''}${suggestion.score}` : '--'

  return (
    <div className="sticky top-0 z-20 rounded border border-border/60 bg-background/95 px-3 py-2 backdrop-blur">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {isStock ? (
          // 个股: 顶行(名称 + 代码 + 现价 + 涨跌色)逐字节不变
          <>
            <span className="text-[15px] font-semibold">{quote?.name || symbol}</span>
            <span className="font-mono text-[11px] text-muted-foreground">{symbol}</span>
            <span className={cn('font-mono text-[18px] font-semibold', toneClass)}>
              {safePrice(quote?.current_price, 2)}
            </span>
            <span className={cn('font-mono text-[12px]', toneClass)}>
              {changeNum == null ? '--' : `${changeNum >= 0 ? '+' : ''}${safeFixed(changeNum, 2)}%`}
            </span>
          </>
        ) : (
          // 指数/板块: 只给**裸代码**中性标签 —— 不渲染 quote 的名称/现价/涨跌
          // (同代码的个股报价是**另一标的**; 名称与数值由正文 IndexBody/BoardBody 拥有)。
          <span className="font-mono text-[11px] text-muted-foreground">{symbol}</span>
        )}

        <div className="ml-auto flex items-center gap-1">
          <div className="flex items-center gap-0.5" role="group" aria-label="类型切换">
            {TYPE_OPTIONS.map((o) => (
              <button
                key={o.id}
                type="button"
                aria-pressed={type === o.id}
                onClick={() => {
                  if (type !== o.id) onTypeChange?.(o.id)
                }}
                className={cn(
                  'rounded px-1.5 py-0.5 text-[11px] transition-colors',
                  type === o.id
                    ? 'bg-accent text-foreground'
                    : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {o.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={refresh}
            title="刷新"
            aria-label="刷新"
            className="rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', busy && 'animate-spin')} />
          </button>
        </div>
      </div>

      {/* 快照行**仅个股** —— 其 cell 的值全部来自 `/quotes`(个股)/more-info/l2, 对指数/板块
          既是 `--` 噪声, 又(若发了 /quotes)会把另一标的的今开/最高/最低/成交额画出来。 */}
      {isStock ? (
        <div className="mt-1.5 flex flex-wrap items-baseline gap-x-4 gap-y-1">
          {cells.map((c) => (
            <div key={c.key} className="flex items-baseline gap-1 text-[11px]">
              <span className="text-muted-foreground">{c.label}</span>
              <span className="font-mono">{c.value}</span>
            </div>
          ))}
          {/* KI-059B: /l2 派生读数(封单额/涨停价/连板)无轮询, 标取数时刻防误读成实时。
              as_of 缺失则整块不渲染(不编时间); 与 QuickRail「快照 HH:MM:SS」同口径。 */}
          {(() => {
            const clock = asOfClock(l2AsOf)
            if (!clock) return null
            return (
              <span
                data-testid="band1-l2-snapshot-clock"
                title="封单额/涨停价/连板 等 /l2 读数的取数时刻; 本带无 30s 轮询, 点刷新可重取"
                className="font-mono text-[9px] text-muted-foreground"
              >
                快照 {clock}
              </span>
            )
          })()}
        </div>
      ) : null}

      {suggestion ? (
        <div className="mt-1.5 flex items-center gap-2">
          <button
            type="button"
            onClick={() => onGotoTab?.('suggest')}
            className="flex min-w-0 flex-1 items-center gap-2 rounded border border-border/60 px-2 py-1 text-left hover:bg-accent/40"
          >
            <TechnicalBadge
              label={`建议·${suggestion.action_label}`}
              tone={technicalToneFromSuggestionAction(suggestion.action, suggestion.action_label)}
              size="sm"
            />
            <span className="font-mono text-[11px] text-muted-foreground">评分 {scoreText}</span>
            <span className="truncate text-[11px]">{suggestion.signal}</span>
          </button>
          {/* T19: 持仓态未知时显式标注 —— 评分按**未持仓**口径算, 不把"未知"说成"未持仓"。 */}
          {positionUnknown ? (
            <span
              data-testid="position-unknown"
              title="持仓态未知: 持仓判定源在途或取数失败, 评分暂按未持仓口径"
              className="shrink-0 text-[10px] text-muted-foreground/80"
            >
              持仓态未知
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
