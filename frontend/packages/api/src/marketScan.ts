// 前端暗盘资金 TOP 榜 API 客户端(v0.4.50 后端已有,前端补齐)
// 来源: src/web/api/market_scan.py 的 GET /api/market-scan/dark-fund-top
//        + POST /api/market-scan/dark-fund-top/refresh
import { fetchAPI } from './client'

type QueryValue = string | number | boolean | null | undefined

function withQuery(path: string, params: Record<string, QueryValue>): string {
  const q = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v === undefined || v === null) return
    const sv = String(v).trim()
    if (!sv) return
    q.set(k, sv)
  })
  const s = q.toString()
  return s ? `${path}?${s}` : path
}

/** 暗盘资金 TOP 榜行(由后端 thsdk DDE 实时扫 / 盘后 15:30 cron 落库) */
export interface DarkFundTopRow {
  symbol: string
  name?: string
  ths_code?: string
  /** 同花顺官方主力净流入(元); 后端转万元 = 元 / 1e4 */
  main_net_wan: number
  /** 主力净量占比(总成交里主力买入占比) */
  main_net_ratio?: number | null
  /** 总成交额(万元) */
  total_amount_wan?: number | null
  /** 持仓股才有的字段: 委托号级精确暗盘净额(万元) */
  tck_dark_net_wan?: number | null
  /** 数据源标签(thsdk_dde / tck_dark) */
  source: string
}

export interface DarkFundTopSnapshot {
  available: true
  snapshot_date: string
  market: string
  updated_at: string | null
  /** 后端 payload 里的字段,典型有: top / universe / computed / generated_at */
  universe?: number
  computed?: number
  generated_at?: string | null
  top: DarkFundTopRow[]
}

export interface DarkFundTopUnavailable {
  available: false
  note: string
}

export type DarkFundTopResp = DarkFundTopSnapshot | DarkFundTopUnavailable

export const marketScanApi = {
  /** 读最新暗盘资金 TOP 快照(无快照 → available:false)。 */
  darkFundTop: (params?: { market?: string }) =>
    fetchAPI<DarkFundTopResp>(
      withQuery('/market-scan/dark-fund-top', { market: params?.market })
    ),

  /** 手动触发暗盘资金 TOP 扫描(同步,全市场约 16s;需登录 owner)。 */
  refreshDarkFundTop: (params?: { top_n?: number; with_tck?: boolean; positions_symbols?: string[] }) => {
    const body: Record<string, unknown> = {}
    if (params?.top_n != null) body.top_n = params.top_n
    if (params?.with_tck != null) body.with_tck = params.with_tck
    if (params?.positions_symbols?.length) body.positions_symbols = params.positions_symbols
    return fetchAPI<DarkFundTopResp>('/market-scan/dark-fund-top/refresh', {
      method: 'POST',
      body: JSON.stringify(body),
    })
  },
}
