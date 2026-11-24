/**
 * 国家队 / 机构持仓 API 客户端（2026-11-18）。
 *
 * **契约来源是代码，不是猜**：`src/web/api/national_team.py`（前缀 `/api/national-team`）
 *  + `src/core/national_team.py` 模块 docstring。
 *
 *  · `GET /overview?report_date&kind` → 主体汇总 + 报告期列表 + 未在榜主体 + 采集时点
 *  · `GET /holdings?report_date&kind&holder&q&sort&desc&limit&offset` → 明细（`count` 是过滤后总数）
 *  · `GET /changes?report_date&kind&holder&change&limit&prev_report_date` → 环比/同比（new / increase / decrease / exit）
 *  · `POST /refresh?periods=` → owner 手动采集（幂等），返回 receipt（ok/errors/written）
 *
 * 口径（后端写死，前端只转述，不得改写）：
 *  - 季报口径（`report_date` = 报告期末），不是实时持仓；
 *  - 只含进入十大股东/十大流通股东的持仓，第 11 名以后上游不提供；
 *  - **不含宽基 ETF**（汇金的 ETF 重仓只在基金半年报/年报「前十大持有人」披露，无公开汇总 API）；
 *  - `market_cap` 单位 **元**（源端 HOLDER_MARKET_CAP），A+H 混行未拆分 —— 展示用 safeMoney 换算，前端不再乘除。
 */
import { fetchAPI } from './client'

export type NationalTeamReportKind = 'float' | 'total'

/** 机构组汇总（组内多账户合并：只数按股票去重、持股与市值相加）。 */
export interface NationalTeamGroup {
  group_key: string
  group_label: string
  group_desc: string
  /** 组内命中的股票只数（去重） */
  stocks: number
  /** 组内命中的持有人账户数（社保组合 / 险资产品账户各算一个） */
  holders: number
  /** 持股数（股，组内相加） */
  hold_num: number
  /** 持股市值（元，组内相加） */
  market_cap: number
  /** 源端缺市值的行数（>0 表示合计是下限） */
  market_cap_missing: number
  /** 相对上一报告期持股数变化（含新进/退出）的只数 */
  changed_stocks: number
}

export interface NationalTeamAbsentHolder {
  group_key: string
  group_label: string
  group_desc: string
  note: string
}

export interface NationalTeamOverview {
  available: boolean
  /** ok / empty_table（还没采集）/ unknown_period（没有这个报告期） */
  status?: string
  report_date: string | null
  report_kind: NationalTeamReportKind
  report_kind_label?: string
  prev_report_date: string | null
  periods: string[]
  groups: NationalTeamGroup[]
  absent_holders: NationalTeamAbsentHolder[]
  totals: {
    /** 全口径**去重**股票只数（同一只票被多组持有只算一只） */
    stocks: number
    market_cap: number
    hold_num: number
    market_cap_missing: number
    /** 各组只数之和（>stocks 说明有跨组重复持有） */
    group_stocks_summed?: number
  }
  snapshot_at: string | null
  source: string
  caliber_note: string
  holder_scope_note?: string
  reason?: string
}

export interface NationalTeamHolding {
  stock_code: string
  stock_name: string | null
  holder_key: string
  holder_name: string
  holder_group: string
  group_key: string
  hold_num: number
  hold_ratio: number | null
  market_cap: number | null
  holder_rank: number | null
  change_type: string | null
  shares_type: string | null
  snapshot_at: string | null
}

export interface NationalTeamHoldingsResponse {
  available: boolean
  report_date: string | null
  report_kind: NationalTeamReportKind
  count: number
  limit: number
  offset: number
  items: NationalTeamHolding[]
  caliber_note: string
  reason?: string
}

export type NationalTeamChangeKind = 'new' | 'increase' | 'decrease' | 'exit' | 'unchanged'

export interface NationalTeamChangeItem {
  kind: NationalTeamChangeKind
  group_key: string
  /** 机构组标签（如「险资」）—— 同组多账户已在后端按股票合并 */
  holder_group: string
  /** 同 holder_group（保留原字段名，便于旧消费者） */
  holder_name: string
  stock_code: string
  stock_name: string | null
  /** 本期持股数（退出为 0） */
  cur_num: number
  /** 上期持股数（新进为 null） */
  prev_num: number | null
  /** 本期 − 上期（股） */
  delta_num: number
  cur_ratio: number | null
  prev_ratio: number | null
  cur_cap: number | null
  prev_cap: number | null
  change_type: string | null
  /** 本期命中该股的账户数（组内） */
  cur_holders?: number
}

export interface NationalTeamChangesResponse {
  available: boolean
  /** ok / empty_table / unknown_period / insufficient_periods / zero_change / empty_result / prev_unavailable */
  status?: string
  report_date: string | null
  prev_report_date: string | null
  /** 后端真实比较口径：qoq 环比 / yoy 同比（去年同季）/ cross 跨期 */
  comparison?: 'qoq' | 'yoy' | 'cross'
  /** 本期与比较期相隔几个季度 */
  compare_gap?: number
  report_kind: NationalTeamReportKind
  counts: Record<NationalTeamChangeKind, number>
  count: number
  items: NationalTeamChangeItem[]
  truncated: boolean
  caliber_note: string
  reason?: string
}

export interface NationalTeamRefreshReceipt {
  ok: boolean
  periods: string[]
  report_kinds: string[]
  snapshot_at: string
  written: number
  deleted_stale: number
  per_holder: Record<string, { stocks?: number; rows?: number; dropped?: Record<string, number> }>
  empty: string[]
  errors: Array<{ target: string; error: string }>
  source: string
  caliber_note: string
}

function withQuery(path: string, params: Record<string, string | number | boolean | null | undefined>): string {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    qs.set(k, String(v))
  }
  const s = qs.toString()
  return s ? `${path}?${s}` : path
}

export const nationalTeamApi = {
  overview(params: { report_date?: string; kind?: NationalTeamReportKind } = {}): Promise<NationalTeamOverview> {
    return fetchAPI<NationalTeamOverview>(
      withQuery('/national-team/overview', { report_date: params.report_date, kind: params.kind }),
      { cacheMode: 'reload' },
    )
  },

  holdings(
    params: {
      report_date?: string
      kind?: NationalTeamReportKind
      holder?: string
      q?: string
      sort?: string
      desc?: boolean
      limit?: number
      offset?: number
    } = {},
  ): Promise<NationalTeamHoldingsResponse> {
    return fetchAPI<NationalTeamHoldingsResponse>(
      withQuery('/national-team/holdings', {
        report_date: params.report_date,
        kind: params.kind,
        holder: params.holder,
        q: params.q,
        sort: params.sort,
        desc: params.desc,
        limit: params.limit,
        offset: params.offset,
      }),
      { cacheMode: 'reload' },
    )
  },

  changes(
    params: {
      report_date?: string
      kind?: NationalTeamReportKind
      /** 机构组 key 或具体 holder_key，多个用逗号 —— 后端在截断前过滤 */
      holder?: string
      /** new/increase/decrease/exit，多个用逗号 —— 后端在截断前过滤 */
      change?: string
      /** 同比比较期（去年同季，YYYY-MM-DD）；缺省按严格相邻上一季算环比 */
      prev_report_date?: string
      limit?: number
    } = {},
  ): Promise<NationalTeamChangesResponse> {
    return fetchAPI<NationalTeamChangesResponse>(
      withQuery('/national-team/changes', {
        report_date: params.report_date,
        kind: params.kind,
        holder: params.holder,
        change: params.change,
        prev_report_date: params.prev_report_date,
        limit: params.limit,
      }),
      { cacheMode: 'reload' },
    )
  },

  /**
   * owner 手动采集（幂等）。非 owner → 后端 403，页面照实提示。
   * `mode='scan'` 走全市场扫描（数分钟，用于新报告期首采 / 认全社保与险资账户）。
   */
  refresh(params: { periods?: string; mode?: 'targeted' | 'scan' } = {}): Promise<NationalTeamRefreshReceipt> {
    return fetchAPI<NationalTeamRefreshReceipt>(
      withQuery('/national-team/refresh', { periods: params.periods, mode: params.mode }),
      { method: 'POST', cacheMode: 'reload' },
    )
  },
}
