import { describe, it, expect } from 'vitest'
import {
  cnStockDataEnabled,
  mapSnapshot,
  visibleSnapshotCells,
  type SnapshotCell,
} from '@panwatch/biz-ui/components/workbench/HeaderBand'

/**
 * 带1 快照行纯函数单测(任务3 + 复审修复: 补 spec §1.2 缺的 流通市值/PE(动)/PE(TTM)/PB/股息率/连板;
 * v0.6.0 遗留⑤: 再补 spec §1.2 明列而此前缺失的 **成交量 / 振幅 / 封单额**)。
 *
 * 合成对象的 key 一律用**后端真实返回字段名**(非计划书示意的 total_mv/amount/current_price 旧名):
 *  - GET /quotes/{symbol}          → src/web/api/quotes.py::_quote_to_response
 *      current_price / change_pct / open_price / high_price / low_price / turnover(成交额, 元)
 *      / **volume(成交量, 手)** / **prev_close(昨收, 元 —— 振幅分母)**
 *      (volume 单位=手 的证据: vendors/tencent.py:95「成交量(手)」+ intraday_monitor.py:1072)
 *  - GET /quotes/{symbol}/more-info → src/core/marketdata_client.py::md_more_info:208-245
 *      turnover_rate(换手率%) / volume_ratio(量比) / total_market_value(总市值, 亿)
 *      / circulating_market_value(流通市值, 亿; tq.py:210 ← Ltsz)
 *  - GET /stocks/{symbol}/l2        → src/core/stock_l2.py::fetch_more:75-98 / fetch_snapshot:64-79
 *      more.zt_price / pe_dynamic / pe_ttm / pb / dividend_yield / ever_zt_count
 *      / **more.fcamo(封单额, 元; 后端已把 FCAmo 万元×1e4, 可为负=跌停封单, 0=未封板真值)**
 *      / **snapshot.high / low / last_close**(振幅的 CN 回退源)
 *      (Ruling A: HeaderBand 自取后作为第 3/4 参传入)
 * 换手率/量比/市值取自 more-info(与 quote 同名时以 more-info 为准)。
 * **振幅公式**: (最高 − 最低) / 昨收 × 100, 三值必须同源(见 `amplitudePct` 头注)。
 */

const byKey = (rows: SnapshotCell[]): Record<string, string> =>
  Object.fromEntries(rows.map((r) => [r.key, r.value]))

/** 19 cell 的固定 key(顺序即 spec §1.2 快照行顺序; price/change_pct 由顶行渲染, 这里仍产出)。 */
const ALL_KEYS = [
  'price',
  'change_pct',
  'open',
  'high',
  'low',
  'volume',
  'amount',
  'amplitude',
  'turnover',
  'volume_ratio',
  'market_cap',
  'float_market_cap',
  'pe_dynamic',
  'pe_ttm',
  'pb',
  'dividend_yield',
  'limit_price',
  'seal_amount',
  'limit_boards',
]

describe('mapSnapshot(带1 快照行)', () => {
  it('缺值一律 --, 不编(空对象与 undefined 四参双跑)', () => {
    const b = byKey(mapSnapshot({}, {}, {}, {}))
    expect(b.price).toBe('--')
    expect(b.change_pct).toBe('--')
    expect(b.open).toBe('--')
    expect(b.high).toBe('--')
    expect(b.low).toBe('--')
    expect(b.amount).toBe('--')
    expect(b.turnover).toBe('--')
    expect(b.volume_ratio).toBe('--')
    expect(b.market_cap).toBe('--')
    // 复审新增 6 cell: 缺值同样必须 --(不编造 PE/PB/股息率/连板)
    expect(b.float_market_cap).toBe('--')
    expect(b.pe_dynamic).toBe('--')
    expect(b.pe_ttm).toBe('--')
    expect(b.pb).toBe('--')
    expect(b.dividend_yield).toBe('--')
    expect(b.limit_price).toBe('--')
    expect(b.limit_boards).toBe('--')
    // 遗留⑤ 新增 3 cell: 缺值同样必须 --(不把缺失的成交量/振幅/封单额渲染成 0)
    expect(b.volume).toBe('--')
    expect(b.amplitude).toBe('--')
    expect(b.seal_amount).toBe('--')

    expect(() => mapSnapshot(undefined, undefined, undefined, undefined)).not.toThrow()
    expect(() => mapSnapshot()).not.toThrow()
    const u = byKey(mapSnapshot(undefined, undefined, undefined, undefined))
    expect(u.turnover).toBe('--')
    expect(u.limit_price).toBe('--')
    expect(u.float_market_cap).toBe('--')
    expect(u.pe_ttm).toBe('--')
    expect(u.dividend_yield).toBe('--')
    expect(u.limit_boards).toBe('--')
    expect(u.volume).toBe('--')
    expect(u.amplitude).toBe('--')
    expect(u.seal_amount).toBe('--')
  })

  it('行情字段用真实 key: current_price/change_pct/open_price/high_price/low_price/turnover', () => {
    const b = byKey(
      mapSnapshot(
        {
          current_price: 82.46,
          change_pct: 7.86,
          open_price: 77.0,
          high_price: 83.2,
          low_price: 76.5,
          turnover: 123456789,
        },
        {},
        {},
      ),
    )
    expect(b.price).toBe('82.46')
    expect(b.change_pct).toBe('+7.86%')
    expect(b.open).toBe('77')
    expect(b.high).toBe('83.2')
    expect(b.low).toBe('76.5')
    // 成交额: more-info 无成交额字段, 取 quote.turnover(元) 经 fmtAmount → 亿/万
    expect(b.amount).toBe('1.23亿')
  })

  it('跌时带 - 号(涨跌幅符号不丢)', () => {
    expect(byKey(mapSnapshot({ current_price: 10, change_pct: -3.5 }, {}, {})).change_pct).toBe(
      '-3.50%',
    )
    expect(byKey(mapSnapshot({ current_price: 10, change_pct: 0 }, {}, {})).change_pct).toBe('+0.00%')
  })

  it('more-info 真实 key 落位: turnover_rate/volume_ratio/total_market_value/circulating_market_value(亿)', () => {
    const b = byKey(
      mapSnapshot(
        { current_price: 10 },
        {
          turnover_rate: 3.2,
          volume_ratio: 1.4,
          total_market_value: 1234.56,
          circulating_market_value: 456.78,
        },
        {},
      ),
    )
    expect(b.turnover).toBe('3.2%')
    expect(b.volume_ratio).toBe('1.4')
    expect(b.market_cap).toBe('1234.56亿')
    // 流通市值与总市值同单位(亿, Ltsz 原值未换算), 同一格式
    expect(b.float_market_cap).toBe('456.78亿')
  })

  it('/stocks/{s}/l2 的 more 真实 key 落位: zt_price/pe_dynamic/pe_ttm/pb/dividend_yield/ever_zt_count', () => {
    const b = byKey(
      mapSnapshot(
        {},
        {},
        {
          zt_price: 11.22,
          pe_dynamic: 28.56,
          pe_ttm: 31.2,
          pb: 4.5,
          dividend_yield: 1.85,
          ever_zt_count: 3,
        },
      ),
    )
    expect(b.limit_price).toBe('11.22')
    expect(b.pe_dynamic).toBe('28.56')
    // 数值不补尾 0(与 safePrice 的既有一致口径)
    expect(b.pe_ttm).toBe('31.2')
    expect(b.pb).toBe('4.5')
    expect(b.dividend_yield).toBe('1.85%')
    // 连板为整数(不带「板」后缀, 单位由 label 承载)
    expect(b.limit_boards).toBe('3')
  })

  it('PE/PB 为负原样透传(亏损股真实口径, 不取绝对值、不归零)', () => {
    const b = byKey(mapSnapshot({}, {}, { pe_dynamic: -12.3, pe_ttm: -15, pb: -0.8 }))
    expect(b.pe_dynamic).toBe('-12.3')
    expect(b.pe_ttm).toBe('-15')
    expect(b.pb).toBe('-0.8')
  })

  it('PG DECIMAL→JSON 字符串数字不崩、不渲染 NaN', () => {
    const b = byKey(
      mapSnapshot(
        { current_price: '82.46', change_pct: '7.86', turnover: '123456789' },
        {
          turnover_rate: '3.2',
          volume_ratio: '1.4',
          total_market_value: '1234.56',
          circulating_market_value: '456.78',
        },
        {
          zt_price: '11.22',
          pe_dynamic: '28.56',
          pe_ttm: '31.2',
          pb: '4.5',
          dividend_yield: '1.85',
          ever_zt_count: '3',
        },
      ),
    )
    expect(b.price).toBe('82.46')
    expect(b.change_pct).toBe('+7.86%')
    expect(b.amount).toBe('1.23亿')
    expect(b.turnover).toBe('3.2%')
    expect(b.market_cap).toBe('1234.56亿')
    expect(b.float_market_cap).toBe('456.78亿')
    expect(b.pe_dynamic).toBe('28.56')
    expect(b.pe_ttm).toBe('31.2')
    expect(b.pb).toBe('4.5')
    expect(b.dividend_yield).toBe('1.85%')
    expect(b.limit_price).toBe('11.22')
    expect(b.limit_boards).toBe('3')
  })

  it('脏值(NaN/非数字串/空串)走 -- 而非 NaN 文案', () => {
    const b = byKey(
      mapSnapshot(
        { current_price: 'abc', change_pct: NaN },
        { total_market_value: 'x', circulating_market_value: '' },
        { pe_dynamic: 'abc', pe_ttm: NaN, pb: '', dividend_yield: '--', ever_zt_count: 'x' },
      ),
    )
    expect(b.price).toBe('--')
    expect(b.change_pct).toBe('--')
    expect(b.market_cap).toBe('--')
    expect(b.float_market_cap).toBe('--')
    expect(b.pe_dynamic).toBe('--')
    expect(b.pe_ttm).toBe('--')
    expect(b.pb).toBe('--')
    // 股息率/连板: 脏值 → --, 不带 % 后缀、不渲染 NaN
    expect(b.dividend_yield).toBe('--')
    expect(b.limit_boards).toBe('--')
  })

  it('cell 顺序与 key 固定(防漂移, 顺序对齐 spec §1.2 快照行)', () => {
    const rows = mapSnapshot({}, {}, {}, {})
    expect(rows.map((r) => r.key)).toEqual(ALL_KEYS)
    expect(rows.every((r) => r.label.length > 0)).toBe(true)
    // label 面向用户, 逐个锚定(PE 动/TTM 大小写、流通市值不可写成总市值)
    expect(rows.map((r) => r.label)).toEqual([
      '现价',
      '涨跌幅',
      '今开',
      '最高',
      '最低',
      '成交量',
      '成交额',
      '振幅',
      '换手率',
      '量比',
      '总市值',
      '流通市值',
      'PE(动)',
      'PE(TTM)',
      'PB',
      '股息率',
      '涨停价',
      '封单额',
      '连板',
    ])
  })

  // ---- 遗留⑤: 成交量 / 振幅 / 封单额 ----

  it('成交量: /quotes 的 volume(单位=手)→ 手/万手/亿手 紧凑; 0 是真值不是缺值', () => {
    expect(byKey(mapSnapshot({ volume: 8653 }, {}, {}, {})).volume).toBe('8653手')
    expect(byKey(mapSnapshot({ volume: 1_234_567 }, {}, {}, {})).volume).toBe('123.46万手')
    expect(byKey(mapSnapshot({ volume: 256_000_000 }, {}, {}, {})).volume).toBe('2.56亿手')
    // 未开盘/停牌: volume=0 是真值(不许渲染成 '--', 也不许编造成别的数)
    expect(byKey(mapSnapshot({ volume: 0 }, {}, {}, {})).volume).toBe('0手')
    // 字符串数字(PG DECIMAL → JSON)不崩
    expect(byKey(mapSnapshot({ volume: '1234567' }, {}, {}, {})).volume).toBe('123.46万手')
    // 脏值 → --
    expect(byKey(mapSnapshot({ volume: 'abc' }, {}, {}, {})).volume).toBe('--')
    // **不回退** /l2 的 snapshot.volume: 通达信 Volume 单位在本仓无实测证据 ⇒ 单位不明就不画
    expect(byKey(mapSnapshot({}, {}, {}, { volume: 999_999 })).volume).toBe('--')
  })

  it('振幅 = (最高 − 最低) / 昨收 × 100(取 /quotes 三值同源)', () => {
    // (83.2 − 76.5) / 77.4 × 100 = 8.6563…% → 8.66%
    const b = byKey(
      mapSnapshot({ high_price: 83.2, low_price: 76.5, prev_close: 77.4 }, {}, {}, {}),
    )
    expect(b.amplitude).toBe('8.66%')
    // 一字板(高=低)→ 0.00% 是真值
    expect(byKey(mapSnapshot({ high_price: 11.55, low_price: 11.55, prev_close: 10.5 }, {}, {}, {})).amplitude).toBe('0.00%')
  })

  it('振幅守卫: 昨收为 0(除零)/三值任一缺失/脏值 → --, 不返回 0 也不渲染 NaN/Infinity', () => {
    // 除零
    expect(byKey(mapSnapshot({ high_price: 10, low_price: 9, prev_close: 0 }, {}, {}, {})).amplitude).toBe('--')
    // 缺昨收
    expect(byKey(mapSnapshot({ high_price: 10, low_price: 9 }, {}, {}, {})).amplitude).toBe('--')
    // 缺最低
    expect(byKey(mapSnapshot({ high_price: 10, prev_close: 9.5 }, {}, {}, {})).amplitude).toBe('--')
    // 脏值
    expect(
      byKey(mapSnapshot({ high_price: 'abc', low_price: 9, prev_close: 9.5 }, {}, {}, {})).amplitude,
    ).toBe('--')
    // 全空也不许出现 NaN/Infinity 文案
    const txt = mapSnapshot({}, {}, {}, {}).map((c) => c.value).join('|')
    expect(txt).not.toContain('NaN')
    expect(txt).not.toContain('Infinity')
  })

  it('振幅回退: /quotes 三值不全时整体回退 /l2 的 snapshot(同源), 绝不跨源拼数', () => {
    // quotes 缺昨收 ⇒ 整体回退 l2 snapshot: (10.8 − 9.9) / 10.2 × 100 = 8.8235…% → 8.82%
    const b = byKey(
      mapSnapshot({ high_price: 83.2, low_price: 76.5 }, {}, {}, { high: 10.8, low: 9.9, last_close: 10.2 }),
    )
    expect(b.amplitude).toBe('8.82%')
    // **跨源混用禁止**: quotes 只给 high(缺 low/prev_close)时, 不得用 quotes.high 配 l2.low/last_close
    // ⇒ 该情形下 quotes 侧三值不全、l2 侧三值齐全才回退; 若 l2 也不全 → --
    expect(
      byKey(mapSnapshot({ high_price: 83.2 }, {}, {}, { low: 9.9, last_close: 10.2 })).amplitude,
    ).toBe('--')
    // l2 昨收为 0(除零)→ --
    expect(
      byKey(mapSnapshot({}, {}, {}, { high: 10.8, low: 9.9, last_close: 0 })).amplitude,
    ).toBe('--')
  })

  it('封单额: /l2 的 more.fcamo(元)→ 亿/万; 负值=跌停封单带 -; 0=未封板真值', () => {
    expect(byKey(mapSnapshot({}, {}, { fcamo: 812_000_000 }, {})).seal_amount).toBe('8.12亿')
    expect(byKey(mapSnapshot({}, {}, { fcamo: 23_000_000 }, {})).seal_amount).toBe('2300万')
    // 跌停封单(FCAmo < 0): 符号必须保留(取绝对值分档后补 -)
    expect(byKey(mapSnapshot({}, {}, { fcamo: -18_000_000 }, {})).seal_amount).toBe('-1800万')
    // 未封板: 0 是真值, 不是缺值
    expect(byKey(mapSnapshot({}, {}, { fcamo: 0 }, {})).seal_amount).toBe('0')
    // 字符串数字 / 脏值
    expect(byKey(mapSnapshot({}, {}, { fcamo: '23000000' }, {})).seal_amount).toBe('2300万')
    expect(byKey(mapSnapshot({}, {}, { fcamo: 'abc' }, {})).seal_amount).toBe('--')
  })
})

/**
 * 复审修复 1(Important): 个股专属 cell 在指数/板块必须**隐藏**(不渲染), 而非渲染成 `--`。
 * 绑定条款: "Stock-only bits hidden when type !== 'stock'"。
 * `mapSnapshot` 保持 type-agnostic(仍产出 19 cell), 分流在渲染侧 `visibleSnapshotCells`。
 */
describe('visibleSnapshotCells(带1 快照行渲染侧类型分流)', () => {
  /** 个股专属 8 格(复审点名 7 + 遗留⑤ 的封单额) —— 非个股必须整格消失。 */
  const EQUITY_ONLY = [
    'float_market_cap',
    'pe_dynamic',
    'pe_ttm',
    'pb',
    'dividend_yield',
    'limit_price',
    'seal_amount',
    'limit_boards',
  ]
  /** 三类型共享 9 格(非个股必须保留; 遗留⑤ 的 成交量/振幅 与 今开/最高/最低 同源自 /quotes)。 */
  const SHARED = ['open', 'high', 'low', 'volume', 'amount', 'amplitude', 'turnover', 'volume_ratio', 'market_cap']

  it('isStock=true: 等值 mapSnapshot 去掉顶行两格(price/change_pct), 17 格全保留', () => {
    const keys = visibleSnapshotCells({}, {}, {}, {}, true).map((c) => c.key)
    expect(keys).toEqual(ALL_KEYS.filter((k) => k !== 'price' && k !== 'change_pct'))
    expect(keys).toEqual([...SHARED, ...EQUITY_ONLY])
  })

  it('isStock=false: 8 个个股专属 cell 整格消失(不是渲染成 --), 共享 9 格保留', () => {
    const cells = visibleSnapshotCells({}, {}, {}, {}, false)
    const keys = cells.map((c) => c.key)
    expect(keys).toEqual(SHARED)
    // 逐格锚定: 一个都不许漏(防"改成 -- 也算过"的假修复)
    for (const k of EQUITY_ONLY) expect(keys).not.toContain(k)
    // 且渲染侧确实没有这些格 → 不存在 `--` 占位噪声
    expect(cells.some((c) => c.value === '--' && EQUITY_ONLY.includes(c.key))).toBe(false)
  })

  it('指数/板块结果一致(两者同一分流: 非 stock 即隐藏)', () => {
    const nonStock = visibleSnapshotCells({}, {}, {}, {}, false).map((c) => c.key)
    expect(nonStock).toEqual(SHARED)
  })

  it('有真值时同样隐藏(不是"因为值是 -- 才消失")', () => {
    const l2 = {
      zt_price: 11.22,
      pe_dynamic: 28.56,
      pe_ttm: 31.2,
      pb: 4.5,
      dividend_yield: 1.85,
      ever_zt_count: 3,
      // 遗留⑤: 封单额有真值(8.12亿) —— 非个股仍必须整格消失, 证明是**类型分流**而非缺值
      fcamo: 812_000_000,
    }
    const more = { turnover_rate: 3.2, volume_ratio: 1.4, total_market_value: 1234.56, circulating_market_value: 456.78 }
    const stockKeys = visibleSnapshotCells({ current_price: 10 }, more, l2, {}, true).map((c) => c.key)
    const indexKeys = visibleSnapshotCells({ current_price: 10 }, more, l2, {}, false).map((c) => c.key)
    expect(stockKeys).toContain('pe_ttm')
    expect(stockKeys).toContain('limit_price')
    expect(stockKeys).toContain('seal_amount')
    expect(indexKeys).not.toContain('pe_ttm')
    expect(indexKeys).not.toContain('limit_price')
    expect(indexKeys).not.toContain('seal_amount')
    expect(indexKeys).not.toContain('float_market_cap')
  })

  it('默认参数按个股处理(向后兼容, 不误伤既有调用)', () => {
    expect(visibleSnapshotCells({}, {}, {}).map((c) => c.key)).toEqual(
      ALL_KEYS.filter((k) => k !== 'price' && k !== 'change_pct'),
    )
  })
})

/**
 * 复审修复 2(Important): CN-only 数据面闸门。
 * `/quotes/{s}/more-info` 非 CN 后端 400, `/stocks/{s}/l2` 是 CN TQ RPC →
 * 只有 `个股 + CN` 才发, 防同代码非 CN 标的画出 CN 涨停价/PE/PB。
 */
describe('cnStockDataEnabled(CN-only 数据面闸门)', () => {
  it('个股 + CN → 放行', () => {
    expect(cnStockDataEnabled('stock', 'CN')).toBe(true)
  })

  it('个股 + 非 CN(US/HK/空串) → 拒发(核心回归: 同代码境外标的不得复用 CN 估值)', () => {
    expect(cnStockDataEnabled('stock', 'US')).toBe(false)
    expect(cnStockDataEnabled('stock', 'HK')).toBe(false)
    expect(cnStockDataEnabled('stock', '')).toBe(false)
  })

  it('指数/板块 + CN → 拒发(非个股无更多信息/无 L2)', () => {
    expect(cnStockDataEnabled('index', 'CN')).toBe(false)
    expect(cnStockDataEnabled('board', 'CN')).toBe(false)
  })

  it('指数/板块 + 非 CN → 拒发(双重不满足)', () => {
    expect(cnStockDataEnabled('index', 'US')).toBe(false)
    expect(cnStockDataEnabled('board', 'HK')).toBe(false)
  })

  it('默认值: type 缺省=stock、market 缺省=CN(与组件默认 props 同源) → 放行', () => {
    expect(cnStockDataEnabled()).toBe(true)
    expect(cnStockDataEnabled(undefined, undefined)).toBe(true)
    expect(cnStockDataEnabled('stock')).toBe(true)
    expect(cnStockDataEnabled(undefined, 'CN')).toBe(true)
    expect(cnStockDataEnabled(undefined, 'HK')).toBe(false)
  })
})
