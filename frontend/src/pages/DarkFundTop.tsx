/**
 * 暗盘资金 TOP 榜页面 (v2.0 §6.1 + v0.4.50 接入).
 *
 * 设计稿要求 (v0.4.50 集成邮件):
 *   - 后端: src/core/dark_fund_scan.scan_dark_fund_top()
 *     (thsdk DDE 批量 200只/批,全市场 5223 只约 16s)
 *   - 口径: main_net_wan = 同花顺官方主力净流入(万元,真实资金流)
 *           tck_dark_net_wan = .tck 委托号级拆单簇暗盘(持仓股才有)
 *   - 数据源: source="thsdk_dde" 不冒充暗盘;.tck 并列对照
 *   - 占位: 无快照 → 显示 available:false + note,不编造榜单
 *
 * 集成路径:
 *   GET  /api/market-scan/dark-fund-top → DarkFundTopResp
 *   POST /api/market-scan/dark-fund-top/refresh → 手动触发扫描(同步 ~16s)
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, RefreshCw, TrendingUp } from 'lucide-react'
import {
  marketScanApi,
  type DarkFundTopRow,
  type DarkFundTopResp,
  type DarkFundTopSnapshot,
  type DarkFundTopUnavailable,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'

/** 输入: 万元; 输出: 万元 或 亿 自动选.
 *  - |wan| < 1e4 万(1亿) → "+X.XX万"
 *  - |wan| >= 1e4 万     → "+X.XX亿"
 *  与 Quote/L2Orderbook.toAmount(元口径, 1e8 阈值)同口径, 符号恒显(同后端 _fmt_amount)。
 */
const toAmountFromWan = (wan: number | null | undefined): string => {
  if (wan == null) return '-'
  // wan 已是万元. 换算: 1 亿 = 1e4 万
  const sign = wan > 0 ? '+' : wan < 0 ? '-' : ''
  if (Math.abs(wan) >= 1e4) return `${sign}${(Math.abs(wan) / 1e4).toFixed(2)}亿`
  return `${sign}${Math.abs(wan).toFixed(2)}万`
}
// 旧名 toWan 保留(代码里调用点多, 改为兼容)
const toWan = toAmountFromWan

function isSnapshot(r: DarkFundTopResp): r is DarkFundTopSnapshot {
  return r.available === true
}

function isUnavailable(r: DarkFundTopResp): r is DarkFundTopUnavailable {
  return r.available === false
}

// 简约 loading / error / empty panel (与 Quote.tsx 同款, 不引入新组件)
function SimpleLoading({ text }: { text: string }) {
  return <div className="p-8 text-center text-[12px] text-muted-foreground">{text}</div>
}
function SimpleError({ text }: { text: string }) {
  return <div className="p-8 text-center text-[12px] text-rose-500">{text}</div>
}
function SimpleEmpty({ text }: { text: string }) {
  return <div className="p-8 text-center text-[12px] text-muted-foreground">{text}</div>
}

export default function DarkFundTopPage() {
  const navigate = useNavigate()
  const [data, setData] = useState<DarkFundTopResp | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  // 2026-09-04: 列头排序(默认榜单顺序; 点同一列切方向)
  const [sort, setSort] = useState<{ key: 'main' | 'amount' | 'ratio' | null; dir: 1 | -1 }>({
    key: null,
    dir: -1,
  })
  const toggleSort = (key: 'main' | 'amount' | 'ratio') =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === -1 ? 1 : -1 } : { key, dir: -1 }))
  const sortedTop = useMemo(() => {
    if (!data || !isSnapshot(data)) return []
    const rows = [...(data.top || [])]
    if (!sort.key) return rows
    const val = (r: DarkFundTopRow) =>
      sort.key === 'main' ? r.main_net_wan ?? 0
      : sort.key === 'amount' ? r.total_amount_wan ?? 0
      : r.main_net_ratio ?? 0
    return rows.sort((a, b) => (val(a) - val(b)) * (sort.dir === -1 ? -1 : 1))
  }, [data, sort])
  const sortMark = (key: 'main' | 'amount' | 'ratio') =>
    sort.key === key ? (sort.dir === -1 ? ' ▼' : ' ▲') : ''

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const r = await marketScanApi.darkFundTop()
      setData(r)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const refresh = useCallback(async () => {
    setRefreshing(true)
    try {
      const r = await marketScanApi.refreshDarkFundTop({ top_n: 20 })
      setData(r)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : '刷新失败')
    } finally {
      setRefreshing(false)
    }
  }, [])

  if (loading) return <SimpleLoading text="加载暗盘资金 TOP 榜…" />
  if (error) return <SimpleError text={error} />

  if (!data) return <SimpleEmpty text="暂无数据" />

  // 场景 1: 无快照(盘后 15:30 cron 还没跑 / 首次部署 / 刷新失败)
  if (isUnavailable(data)) {
    return (
      <div className="space-y-4">
        <div className="p-6 text-center border-b border-border/40">
          <AlertTriangle className="mx-auto h-8 w-8 text-amber-500" />
          <div className="mt-3 text-[14px] font-medium text-foreground">暂无暗盘资金 TOP 快照</div>
          <p className="mt-1.5 whitespace-pre-wrap text-[12px] text-muted-foreground">{data.note}</p>
          <div className="mt-4">
            <Button onClick={refresh} disabled={refreshing} size="sm">
              <RefreshCw className={`mr-1.5 h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
              立即扫描(全市场约 16s)
            </Button>
          </div>
        </div>
      </div>
    )
  }

  // 场景 2: 有快照
  if (isSnapshot(data)) {
    return (
      <div className="space-y-4">
        {/* 顶部摘要 + 操作栏 */}
        <div className="border-b border-border/40 pb-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2">
                <TrendingUp className="h-4 w-4 text-stock-up" />
                <h2 className="text-[14px] font-medium text-foreground">暗盘资金 TOP 榜(全市场)</h2>
              </div>
              <p className="mt-1 text-[11px] text-muted-foreground">
                快照日 <span className="font-mono">{data.snapshot_date}</span>
                {' · '}宇宙股票 <span className="font-mono">{data.universe ?? '-'}</span> 只
                {' · '}实际计算 <span className="font-mono">{data.computed ?? '-'}</span> 只
                {' · '}TOP <span className="font-mono">{data.top?.length ?? 0}</span> 条
                {' · '}数据源 <span className="font-mono">thsdk_dde</span>(同花顺官方主力资金流)
                {' · '}更新 <span className="font-mono">{data.updated_at ?? '-'}</span>
              </p>
            </div>
            <Button onClick={refresh} disabled={refreshing} size="sm" variant="outline">
              <RefreshCw className={`mr-1.5 h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
              {refreshing ? '扫描中…' : '重新扫描'}
            </Button>
          </div>
        </div>

        <div className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12px]">
              <thead className="sticky top-0 bg-muted/80 backdrop-blur">
                <tr className="border-b border-border/60 text-[11px] text-muted-foreground">
                  <th className="px-3 py-2 font-medium">#</th>
                  <th className="px-3 py-2 font-medium">代码</th>
                  <th className="px-3 py-2 font-medium">名称</th>
                  <th
                    className="px-3 py-2 text-right font-medium cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort('main')}
                    title="按主力净流入排序"
                  >
                    主力净流入(万/亿){sortMark('main')}
                  </th>
                  <th
                    className="px-3 py-2 text-right font-medium cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort('ratio')}
                    title="同花顺主力净量(股数口径, 非百分比)"
                  >
                    主力净量{sortMark('ratio')}
                  </th>
                  <th
                    className="px-3 py-2 text-right font-medium cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort('amount')}
                    title="按总成交额排序"
                  >
                    总成交额(万/亿){sortMark('amount')}
                  </th>
                  <th className="px-3 py-2 text-right font-medium">.tck 暗盘对照</th>
                  <th className="px-3 py-2 font-medium">数据源</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/40">
                {sortedTop.map((r: DarkFundTopRow, i: number) => {
                  const positive = (r.main_net_wan ?? 0) > 0
                  const negative = (r.main_net_wan ?? 0) < 0
                  return (
                    <tr
                      key={`${r.symbol}-${i}`}
                      className="hover:bg-accent/20 cursor-pointer"
                      onClick={() => navigate(`/quote?type=stock&symbol=${r.symbol}`)}
                      title={`${r.symbol} → 行情`}
                    >
                      <td className="px-3 py-2 font-mono text-muted-foreground">{i + 1}</td>
                      <td className="px-3 py-2 font-mono">
                        <a
                          href={`/quote?type=stock&symbol=${r.symbol}`}
                          className="text-primary hover:underline"
                        >
                          {r.symbol}
                        </a>
                      </td>
                      <td className="px-3 py-2 text-muted-foreground">{r.name ?? '-'}</td>
                      <td
                        className={`px-3 py-2 text-right font-mono ${
                          positive ? 'text-stock-up' : negative ? 'text-stock-down' : 'text-muted-foreground'
                        }`}
                      >
                        {toWan(r.main_net_wan)}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-muted-foreground">
                        {r.main_net_ratio != null ? `${r.main_net_ratio.toFixed(0)}` : '-'}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-muted-foreground">
                        {toWan(r.total_amount_wan)}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-muted-foreground">
                        {r.tck_dark_net_wan != null ? (
                          <span
                            className={
                              r.tck_dark_net_wan > 0
                                ? 'text-stock-up'
                                : r.tck_dark_net_wan < 0
                                ? 'text-stock-down'
                                : ''
                            }
                          >
                            {toWan(r.tck_dark_net_wan)}
                          </span>
                        ) : (
                          <span className="text-muted-foreground/50">-</span>
                        )}
                      </td>
                      <td className="px-3 py-2 font-mono text-[10px] text-muted-foreground">
                        {r.source}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <div className="border-t border-border/50 px-3 py-1.5 text-[11px] text-muted-foreground">
            红 = 主力净流入(吸筹), 绿 = 主力净流出(派发) · .tck 暗盘对照仅持仓股有数据 ·{' '}
            完整榜单见{' '}
            <a href="/api/market-scan/dark-fund-top" className="text-primary hover:underline">
              /api/market-scan/dark-fund-top
            </a>
          </div>
        </div>
      </div>
    )
  }

  // 兜底(理论上 type narrowing 已穷尽)
  return <SimpleEmpty text="数据格式异常" />
}
