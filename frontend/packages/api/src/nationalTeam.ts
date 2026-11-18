/**
 * 国家队动向 API 客户端（2026-11-18）。
 *
 * **契约来源是代码，不是猜**：`src/web/api/national_team.py`（前缀 `/api/national-team`）
 *  + `src/core/national_team.py` 模块 docstring。
 *
 *  · `GET /overview?report_date&kind` → 主体汇总 + 报告期列表 + 未在榜主体 + 采集时点
 *  · `GET /holdings?report_date&kind&holder&q&sort&desc&limit&offset` → 明细（`count` 是过滤后总数）
 *  · `GET /changes?report_date&kind&limit` → 环比（new / increase / decrease / exit）
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

export interface NationalTeamGroup {
  holder_key: string
  holder_name: string
  holder_group: string
  group_key: string
  stocks: number
  /** 持股数（股） */
  hold_num: number
  /** 持股市值（元） */
  market_cap: number
  /** 源端缺市值的行数（>0 表示合计是下限） */
  market_cap_missing: number
  /** 相对上一报告期持股数变化（含新进/退出）的只数 */
  changed_stocks: number
}

export interface NationalTeamAbsentHolder {
  holder_key: string
  holder_name: string
  holder_group: string
  group_key: string
  note: string
}

export interface NationalTeamOverview {
  available: boolean
  report_date: string | null
  report_kind: NationalTeamReportKind
  report_kind_label?: string
  prev_report_date: string | null
  periods: string[]
  groups: NationalTeamGroup[]
  absent_holders: NationalTeamAbsentHolder[]
  totals: { stocks: number; market_cap: number; hold_num: number; market_cap_missing: number }
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
  holder_key: string
  holder_name: string
  holder_group: string
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
}

export interface NationalTeamChangesResponse {
  available: boolean
  report_date: string | null
  prev_report_date: string | null
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
    params: { report_date?: string; kind?: NationalTeamReportKind; limit?: number } = {},
  ): Promise<NationalTeamChangesResponse> {
    return fetchAPI<NationalTeamChangesResponse>(
      withQuery('/national-team/changes', {
        report_date: params.report_date,
        kind: params.kind,
        limit: params.limit,
      }),
      { cacheMode: 'reload' },
    )
  },

  /** owner 手动采集（幂等）。非 owner → 后端 403，页面照实提示。 */
  refresh(periods?: string): Promise<NationalTeamRefreshReceipt> {
    return fetchAPI<NationalTeamRefreshReceipt>(withQuery('/national-team/refresh', { periods }), {
      method: 'POST',
      cacheMode: 'reload',
    })
  },
}
