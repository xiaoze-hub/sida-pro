import { useEffect, useState, type ReactNode } from 'react'
import { fetchAPI } from '@panwatch/api'
import { useInsight } from '@panwatch/biz-ui/components/insight/context'
import { FundamentalsPanel } from '@panwatch/biz-ui/components/insight/FundamentalsPanel'
import { CompanyTab } from '@panwatch/biz-ui/components/insight/CompanyTab'
import AddPositionCalculator from '@panwatch/biz-ui/components/add-position-calculator'
import { fmtShares } from '@panwatch/biz-ui/components/workbench/QuickRail'
import { safeNum, safePrice } from '@/lib/format'
import InsightProvider from '@/pages/workbench/InsightProvider'

/**
 * 工作台标签「基本面」(工作台 v2 三合一, Task 13)。
 *
 * 落位(spec §三 去重表 / 计划 Task 13):
 *  ① **财务 / 股本行** —— PE(动)/PE(TTM)/PB/股息率 · 流通股本/总股本 · 上市/次新;
 *  ② **龙虎榜 / 融资融券 / 股东户数** —— 复用恢复组件 `FundamentalsPanel`;
 *  ③ **公司简介 / 基本信息** —— 复用恢复组件 `CompanyTab`;
 *  ④ **加仓计算器** —— 仅持仓时(`hasPosition`), 复用 `add-position-calculator`。
 * **不渲染题材/板块** —— 归右栏 `QuickRail` 的 chips(去重表 #6「「基本面/简介」不再重复列板块」)。
 *
 * 取数(全部真接口, 无 mock、无编造):
 *  - `GET /stocks/{s}/fundamental`(股本 `gb.ltgb/zgb` + 上市 `listing.listing_date` + `sub_new`)
 *    与 `GET /stocks/{s}/l2` 的 `more`(`pe_dynamic`/`pe_ttm`/`pb`/`dividend_yield`)—— 本标签直连;
 *    字段口径与右栏 `QuickRail` 的「基本面/股本」卡**同一套映射**(PE/PB 走 `safePrice(safeNum(...))`,
 *    股本走 `QuickRail.fmtShares` 的亿/万双档, 该函数由本任务从 QuickRail **导出复用**, 未复制实现);
 *    `dividend_yield`/`pe_dynamic` 与带1 `HeaderBand.mapSnapshot` 的取值/格式化口径一致。
 *    **CN-only 闸门**: 这两个端点都是通达信 CN 源(`/l2` 是 TQ RPC), 非 `market === 'CN'` **不发**
 *    (与 `HeaderBand.cnStockDataEnabled`/`QuickRail.cnDataEnabled` 同一条纪律, 避免把 CN 口径
 *    画到别的标的上); 非 CN 时该行全部 `--` 并给一行中性说明。
 *  - `GET /market-data/fundamentals-detail/{s}`(龙虎榜/两融/股东户数/分红/事件日历)与
 *    `GET /quotes/{s}/company`(公司简介/基本信息)—— 经 `InsightProvider` 的 context 消费。
 *
 * 复用 vs 自建(brief 硬要求「REUSE them (don't re-implement)」):
 *  ①②③ 的**渲染**全部复用恢复组件(本文件零重写其内部逻辑); ④ 复用既有 `AddPositionCalculator`。
 *  本文件自建的只有「财务/股本行」的取值映射与降级文案(该行在恢复组件里不存在对应组件)。
 *
 * 取数键与两处 provider 接线(**只传 `keys`, 无任何 `setTab` / 直调取数**):
 *  1. `keys={['fundamentals']}` —— 门控 `/market-data/fundamentals-detail`(spec §4.3 惰性);
 *  2. `keys={['company']}` —— 门控 `/quotes/{s}/company`(公司简介/基本信息)。
 *  说明(Task 13 复审已把两处历史遗留一并解耦): 本标签**最初**的写法是 `keys={['fundamentals']}`
 *  + 挂载时 `setTab('fundamentals')`(当时 `useInsightData` 里该端点的取数条件是
 *  `isResourceEnabled(keys,'fundamentals') && tab === 'fundamentals'` 两条件的**与**, 而工作台没有
 *  旧模态的标签栏, 内部 `tab` 恒为 `'overview'` ⇒ 只传 `keys` 时屏上恒为「暂无基本面数据」),
 *  公司数据则因为**没有任何资源键**而在挂载时直调 `loadCompany()`。两处都是**绕过** Provider 门控的
 *  变通: `setTab` 会改写 Provider 的共享内部状态(并顺带点亮 `refreshForAuto` 的**别的** tab 分支);
 *  直调取数不受任何键约束。现 Provider 已把 `deep`/`fundamentals`/`company` 三个键**解耦**内部
 *  `tab`(只按键判定), 故本标签只声明键集即可, 不碰 Provider 的任何内部状态、不直调任何取数函数。
 *  3. `key={symbol}` 挂在 `InsightProvider` 上 —— 换标的时整棵 provider 重挂载: 否则
 *     `fundamentalsLoaded`(无 `core` 键时不会被重置)与 `companyInfo`(`loadCompany` 有
 *     `if (companyInfo) return` 早退)会把**上一只票**的基本面/公司数据画到新标的上。
 *
 * 降级(never fabricate): 任一字段缺失/脏值 → `--`(不走裸 `.toFixed`, 全走 `@/lib/format` safe*);
 *  取数**失败**与「后端真的没数据」分开陈述(失败给中性文案且**不猜原因**, 后端 `note` 原样展示);
 *  非 CN 不取数 → `--` + 中性说明。`FundamentalsPanel` 自身「失败/无数据 → 暂无基本面数据」的
 *  静默降级是恢复组件的既有语义(见报告 concern: provider 未暴露该端点的失败位, 本标签无法区分二者)。
 *
 * 「加仓计算器」的两条额外纪律(见下 `AddPositionSection`):
 *  - 仅 `hasPosition` 时挂载(**未持仓不渲染**, 也不发 `/portfolio/summary`);
 *  - 但**还要求真实持仓数**: 复用组件的 `currentQuantity/currentCost` 一旦传 0 就会把标题写成
 *    「当前空仓 · 建仓测算」—— 对真有持仓的标的即**假陈述**, 故本标签用 provider 暴露的
 *    `loadHoldingAgg()`(1 个端点)取真实数量/成本; 取不到时**不代填 0**, 只给一行中性说明。
 *    现价(`currentPrice`)不传: 现价归带1 `HeaderBand`(去重表 #9 邻近条款), 复用组件的加仓价
 *    输入框在留空时本就可手填。
 *
 * 归属 `src/pages/workbench/tabs/`: 与 Task 11/12 同层(标签自带 Provider, 由 Task 17 的
 * `TabPanel` 按 `?tab=` 挂载); 不新增 biz-ui 依赖方向。
 */

/**
 * 门控键: 模块级常量 —— 每帧新建数组会换引用(Provider 内部虽按内容签名记忆化, 此为防御性收敛)。
 * `fundamentals` = `/market-data/fundamentals-detail/{s}`; `company` = `/quotes/{s}/company`
 * (Task 13 复审新增的键, 见文件头注 §「取数键」)。
 */
const FUNDAMENTAL_TAB_KEYS = ['fundamentals', 'company'] as const

/* ------------------------------------------------------------------ *
 * 后端契约(仅取本页用到的字段; 缺失一律可选, 由渲染层走 '--')
 * 字段证据: src/core/stock_l2.py::fetch_more(pe_dynamic=DynaPE / pe_ttm=StaticPE_TTM /
 * pb=PB_MRQ / dividend_yield=DYRatio, 原值不换算) ·
 * src/web/api/stocks.py::get_stock_fundamental({gb:{date,ltgb,zgb}, listing:{name,listing_date},
 * sub_new, note}, 源不可用 → 字段 None + note)。
 * ------------------------------------------------------------------ */

interface L2More {
  /** PE(动) — DynaPE */
  pe_dynamic?: number | null
  /** PE(TTM) — StaticPE_TTM */
  pe_ttm?: number | null
  /** PB(市净率) — PB_MRQ */
  pb?: number | null
  /** 股息率(%) — DYRatio */
  dividend_yield?: number | null
}

interface L2Resp {
  more?: L2More | null
  /** 源不可用时的降级说明(后端原文, 不编) */
  note?: string | null
}

interface FundamentalResp {
  /** 最近一条股本 {date, ltgb(流通股本, 股), zgb(总股本, 股)} */
  gb?: { date?: string; ltgb?: number | null; zgb?: number | null } | null
  /** 上市信息(name 属带1 的行情名, 本标签不渲染) */
  listing?: { name?: string; listing_date?: string } | null
  /** 上市 < 365 天 = 次新; null = 判不出(不猜) */
  sub_new?: boolean | null
  note?: string | null
}

/* ------------------------------------------------------------------ *
 * 纯格式化(不写裸 .toFixed —— R6 棘轮对新文件直接失败)
 * ------------------------------------------------------------------ */

/**
 * 上市日期(通达信 `J_start`, 形如 `20100618`)→ `2010-06-18`。
 * 仅对**恰好 8 位数字**做补横线(纯重排, 不猜格式/不猜时区); 其它形态原样透传; 空值 `--`。
 */
function fmtListingDate(v?: string | null): string {
  const s = String(v ?? '').trim()
  if (!s) return '--'
  return /^\d{8}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s
}

/** 次新判定三态: true=次新 / false=非次新(后端判过上市满 365 天) / null=判不出 → `--`。 */
function subNewText(v?: boolean | null): string {
  if (v === true) return '次新'
  if (v === false) return '非次新'
  return '--'
}

/* ------------------------------------------------------------------ *
 * 展示原子(hairline 分节, 与 L2Tab/SuggestTab 同一视觉语言)
 * ------------------------------------------------------------------ */

function Section({
  id,
  title,
  hint,
  extra,
  children,
}: {
  id: string
  title: string
  hint?: string
  extra?: ReactNode
  children: ReactNode
}) {
  return (
    <section data-testid={`fundamental-section-${id}`} className="border-b border-border/40 pb-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[11px]">
        <span className="font-medium text-foreground">{title}</span>
        {hint ? (
          <span className="text-muted-foreground" title={hint}>
            {hint}
          </span>
        ) : null}
        {extra}
      </div>
      {children}
    </section>
  )
}

function Cell({
  testid,
  label,
  value,
  hint,
}: {
  testid: string
  label: string
  value: string
  hint?: string
}) {
  return (
    <div data-testid={testid} title={hint}>
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="font-mono text-foreground">{value}</div>
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * ① 财务 / 股本行(本标签直连的两个 CN 端点)
 * ------------------------------------------------------------------ */

interface CnSnapshot {
  l2: L2Resp | null
  fund: FundamentalResp | null
  /** `/stocks/{s}/l2` 取数失败(与「后端真的没数据」分开记) */
  l2Failed: boolean
  /** `/stocks/{s}/fundamental` 取数失败 */
  fundFailed: boolean
}

const EMPTY_SNAPSHOT: CnSnapshot = { l2: null, fund: null, l2Failed: false, fundFailed: false }

/**
 * 两个 CN 端点一次拉(`/stocks/{s}/l2` 的 `more` + `/stocks/{s}/fundamental`), 逐端点成败。
 *  - 非 CN / 空 symbol → 不发请求(CN-only 闸门);
 *  - 换标的 → 清旧值(不把上一只票的 PE/股本画到新标的上; 本组件亦被 `<InsightProvider key={symbol}>`
 *    包住, 换标的必然重挂载, 这里是双保险);
 *  - 失败 → 只置失败位, 值走 `--`(**不猜原因**, 不编造, 更不写 0);
 *  - **不传** `cacheMode`: `/l2` 与带1 `HeaderBand` 是同一 URL, `fetchAPI` 的 30s GET 缓存会命中
 *    带1 刚取的那份 ⇒ 同一数据点(PE/PB/股息)在带1 与本行**值一致且零额外 RPC**; 缓存过期即真取。
 */
function useCnSnapshot(symbol: string, market: string): CnSnapshot {
  const [snap, setSnap] = useState<CnSnapshot>(EMPTY_SNAPSHOT)
  const cn = market === 'CN'
  useEffect(() => {
    if (!symbol || !cn) {
      setSnap(EMPTY_SNAPSHOT)
      return
    }
    let alive = true
    setSnap(EMPTY_SNAPSHOT)
    const sym = encodeURIComponent(symbol)
    void Promise.allSettled([
      fetchAPI<L2Resp>(`/stocks/${sym}/l2`),
      fetchAPI<FundamentalResp>(`/stocks/${sym}/fundamental`),
    ]).then(([l2, fund]) => {
      if (!alive) return
      setSnap({
        l2: l2.status === 'fulfilled' ? (l2.value ?? null) : null,
        fund: fund.status === 'fulfilled' ? (fund.value ?? null) : null,
        l2Failed: l2.status === 'rejected',
        fundFailed: fund.status === 'rejected',
      })
    })
    return () => {
      alive = false
    }
  }, [symbol, cn])
  return snap
}

/** 财务/股本行单元格(key 稳定, 供测试锚定)。 */
function financeCells(snap: CnSnapshot) {
  const more = snap.l2?.more
  const gb = snap.fund?.gb
  const dy = safeNum(more?.dividend_yield)
  return [
    {
      key: 'pe_dynamic',
      label: 'PE(动)',
      value: safePrice(safeNum(more?.pe_dynamic), 2),
      hint: '动态市盈率(原值, 可为负 = 亏损股口径); 源: /stocks/{s}/l2 的 more.pe_dynamic',
    },
    {
      key: 'pe_ttm',
      label: 'PE(TTM)',
      value: safePrice(safeNum(more?.pe_ttm), 2),
      hint: '静态市盈率 TTM(原值); 源: more.pe_ttm',
    },
    { key: 'pb', label: 'PB', value: safePrice(safeNum(more?.pb), 2), hint: '市净率(原值); 源: more.pb' },
    {
      key: 'dividend_yield',
      label: '股息率',
      value: dy == null ? '--' : `${safePrice(dy, 2)}%`,
      hint: '年分红 / 股价(%); 源: more.dividend_yield',
    },
    {
      key: 'ltgb',
      label: '流通股本',
      value: fmtShares(gb?.ltgb),
      hint: '最近一条股本数据的流通股本(亿/万两档); 源: /stocks/{s}/fundamental 的 gb.ltgb',
    },
    {
      key: 'zgb',
      label: '总股本',
      value: fmtShares(gb?.zgb),
      hint: '最近一条股本数据的总股本(亿/万两档); 源: gb.zgb',
    },
    {
      key: 'listing',
      label: '上市',
      value: fmtListingDate(snap.fund?.listing?.listing_date),
      hint: '上市日期(通达信 get_stock_info 的 J_start); 非 8 位数字串原样透传',
    },
    {
      key: 'sub_new',
      label: '次新',
      value: subNewText(snap.fund?.sub_new),
      hint: '上市 < 365 天 = 次新; 后端判不出时为 --(不猜)',
    },
  ]
}

/** 降级文案(**不猜原因**, 后端 `note` 原样展示)。 */
function financeNotes(snap: CnSnapshot, cn: boolean): string[] {
  const notes: string[] = []
  if (!cn) {
    notes.push('非 CN 标的: 估值/股本/上市为 CN 专有端点, 未取数(显示 --)')
    return notes
  }
  if (snap.l2Failed) notes.push('估值取数失败(PE/PB/股息率显示 --, 不代表源无数据)')
  else if (snap.l2?.note) notes.push(snap.l2.note)
  if (snap.fundFailed) notes.push('股本/上市取数失败(显示 --, 不代表源无数据)')
  else if (snap.fund?.note) notes.push(snap.fund.note)
  return notes
}

function FinanceSection({ snap, cn }: { snap: CnSnapshot; cn: boolean }) {
  const notes = financeNotes(snap, cn)
  const gbDate = snap.fund?.gb?.date
  return (
    <Section
      id="finance"
      title="财务 / 股本"
      hint="估值: 通达信 get_more_info(L2) · 股本/上市/次新: GET /stocks/{s}/fundamental"
      extra={
        gbDate ? <span className="ml-auto font-mono text-[10px] text-muted-foreground">股本 {gbDate}</span> : null
      }
    >
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 md:grid-cols-4">
        {financeCells(snap).map((c) => (
          <Cell key={c.key} testid={`fundamental-finance-${c.key}`} label={c.label} value={c.value} hint={c.hint} />
        ))}
      </div>
      {notes.length > 0 ? (
        <div className="mt-1.5 space-y-0.5 text-[10px] text-muted-foreground/70">
          {notes.map((n) => (
            <div key={n}>{n}</div>
          ))}
        </div>
      ) : null}
    </Section>
  )
}

/* ------------------------------------------------------------------ *
 * ④ 加仓计算器(仅持仓时; 需**真实**持仓数, 见文件头注)
 * ------------------------------------------------------------------ */

function AddPositionSection({ symbol, market }: { symbol: string; market: string }) {
  const { loadHoldingAgg, holdingAgg, holdingLoaded, holdingLoadError } = useInsight()
  // 只有本组件被渲染(= hasPosition)时才会执行 ⇒ 未持仓不发 /portfolio/summary。
  useEffect(() => {
    void loadHoldingAgg()
  }, [loadHoldingAgg])

  if (!holdingLoaded) {
    return (
      <div data-testid="fundamental-add-position" className="text-[11px] text-muted-foreground">
        加仓测算: 读取当前持仓…
      </div>
    )
  }
  if (!holdingAgg) {
    // 取不到真实持仓数时**不代填 0** —— 复用组件在 0 持仓时会写「当前空仓 · 建仓测算」,
    // 对确有持仓的标的是假陈述。这里只陈述"没取到"这一事实, 不猜原因。
    return (
      <div
        data-testid="fundamental-add-position"
        data-state="no-holding-data"
        className="text-[11px] text-muted-foreground"
      >
        加仓测算: {holdingLoadError ? '持仓汇总取数失败' : '持仓汇总中无该标的'}, 未取到当前持仓数 ——
        需真实持仓数才能算, 不代填 0
      </div>
    )
  }
  return (
    <div data-testid="fundamental-add-position">
      <AddPositionCalculator
        symbol={symbol}
        market={market}
        currentQuantity={holdingAgg.quantity}
        currentCost={holdingAgg.unitCost}
        currentPrice={null}
      />
    </div>
  )
}

/* ------------------------------------------------------------------ *
 * 标签正文(在 Provider 内消费 useInsight; 见文件头注)
 * ------------------------------------------------------------------ */

function FundamentalTabBody({
  symbol,
  market,
  hasPosition,
}: {
  symbol: string
  market: string
  hasPosition?: boolean
}) {
  const { fundamentals, fundamentalsLoading, fundamentalsLoaded } = useInsight()
  const snap = useCnSnapshot(symbol, market)
  const cn = market === 'CN'

  return (
    <div className="mt-1 space-y-3 text-[12px]" data-testid="fundamental-tab">
      {/* 条: 口径说明(名称/现价/涨停价归带1; 题材/板块归右栏 —— 本标签一概不渲染, 去重表) */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/40 pb-2 text-[11px] text-muted-foreground">
        <span className="text-foreground">基本面</span>
        <span className="text-border/60">|</span>
        <span>财务/股本 · 龙虎榜/融资融券/股东户数 · 公司简介/基本信息{hasPosition ? ' · 加仓测算' : ''}</span>
      </div>

      <FinanceSection snap={snap} cn={cn} />

      <Section
        id="detail"
        title="龙虎榜 · 融资融券 · 股东户数"
        hint="GET /market-data/fundamentals-detail/{s}(含分红/事件日历; 无数据的段不渲染)"
      >
        {/* 首拉在途才显「加载中...」; 已有数据时不清屏(手动刷新/未来 tab 驱动的重取也走这条:
            后台重取不把屏上数据换成占位 —— 与 L2Tab 的 stale-on-error 同旨) */}
        <FundamentalsPanel
          data={fundamentals}
          loading={fundamentalsLoading && !fundamentals}
          loaded={fundamentalsLoaded}
        />
      </Section>

      <Section id="company" title="公司简介 · 基本信息" hint="GET /quotes/{s}/company(zhitu 公司简介, 1h 缓存)">
        {/* 去重表 #6: 「概念板块」chips 归右栏 QuickRail 的题材/板块卡, 本标签不重复列(故关掉该段) */}
        <CompanyTab showConcepts={false} />
      </Section>

      {hasPosition ? <AddPositionSection symbol={symbol} market={market} /> : null}
    </div>
  )
}

/**
 * 标签入口。`keys={['fundamentals','company']}`: 只启用 `/market-data/fundamentals-detail`(龙虎榜/
 * 两融/股东户数)与 `/quotes/{s}/company`(公司简介/基本信息)两个键 —— quote/moreInfo/darkFlowTq/
 * klineSummary/klines/portfolioSummary/watchlist/news/announcements/suggestions/reports/deep
 * 一个都不发; 本标签的另两个端点 `/stocks/{s}/l2`、`/stocks/{s}/fundamental` 是 CN 专有、按需直连
 * (不经 Provider 键表)。`key={symbol}`: 换标的整体重挂载(见头注第 3 条)。
 */
export default function FundamentalTab({
  symbol,
  market,
  hasPosition,
}: {
  symbol: string
  market: string
  hasPosition?: boolean
}) {
  // market 归一化(provider 内部亦归一化, 这里为了 CN 闸门与复用的计算器口径一致)
  const mkt = String(market || 'CN').trim().toUpperCase()
  return (
    <InsightProvider
      key={symbol}
      symbol={symbol}
      market={mkt}
      hasPosition={hasPosition}
      keys={FUNDAMENTAL_TAB_KEYS}
    >
      <FundamentalTabBody symbol={symbol} market={mkt} hasPosition={hasPosition} />
    </InsightProvider>
  )
}
