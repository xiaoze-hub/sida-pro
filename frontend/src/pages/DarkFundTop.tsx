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
 *
 * P2 (2026-09-18): i18n + Card 组件 + 窄屏表格转卡片 + 骨架屏
 */
import { ANPAN, MINGPAN } from '@panwatch/biz-ui'
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
import { Card } from '@panwatch/base-ui/components/ui/card'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { Skeleton } from '@/components/Skeleton'
import { safeFixed, toAmountFromWan, toAmountFromWanUnsigned } from '@/lib/format'
import { useI18n } from '@/hooks/useI18n'

function isSnapshot(r: DarkFundTopResp): r is DarkFundTopSnapshot {
  return r.available === true
}

function isUnavailable(r: DarkFundTopResp): r is DarkFundTopUnavailable {
  return r.available === false
}

function DarkFundTopSkeleton() {
  return (
    <div className="sida-page-enter space-y-4" aria-busy aria-live="polite">
      <div className="border-b border-border/40 pb-3">
        <Skeleton className="h-5 w-48" />
        <Skeleton className="mt-2 h-3 w-72" />
      </div>
      <div className="space-y-2">
        {Array.from({ length: 8 }).map((_, i) => (
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    </div>
  )
}

export default function DarkFundTopPage() {
  const navigate = useNavigate()
  const { t } = useI18n()
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

  const hasTckData = useMemo(
    () => (data && isSnapshot(data) ? (data.top || []).some((r) => r.tck_dark_net_wan != null) : false),
    [data],
  )
  const rowCount = data && isSnapshot(data) ? (data.top?.length ?? 0) : 0

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const r = await marketScanApi.darkFundTop()
      setData(r)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    load()
  }, [load])

  const refresh = useCallback(async () => {
    setRefreshing(true)
    try {
      const r = await marketScanApi.refreshDarkFundTop({ top_n: 20 })
      setData(r)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('darkFundTop.refreshFailed'))
    } finally {
      setRefreshing(false)
    }
  }, [t])

  if (loading) return <DarkFundTopSkeleton />
  if (error) return <div className="p-8 text-center text-[12px] text-rose-500">{error}</div>

  if (!data) {
    return <div className="p-8 text-center text-[12px] text-muted-foreground">{t('darkFundTop.empty')}</div>
  }

  if (isUnavailable(data)) {
    return (
      <div className="space-y-4">
        <Card className="p-6 text-center">
          <AlertTriangle className="mx-auto h-8 w-8 text-amber-500" />
          <div className="mt-3 text-[13px] font-medium text-foreground">{t('darkFundTop.noSnapshot')}</div>
          <p className="mt-1.5 whitespace-pre-wrap text-[12px] text-muted-foreground">{data.note}</p>
          <div className="mt-4">
            <Button onClick={refresh} disabled={refreshing} size="sm" className="min-h-[44px]">
              <RefreshCw className={`mr-1.5 h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
              {t('darkFundTop.scanNow')}
            </Button>
          </div>
        </Card>
      </div>
    )
  }

  if (isSnapshot(data)) {
    return (
      <div className="sida-page-enter space-y-4">
        {/* 顶部摘要 + 操作栏 */}
        <div className="border-b border-border/40 pb-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <TrendingUp className="h-4 w-4 text-stock-up" />
                <h2 className="text-[13px] font-medium text-foreground">{t('darkFundTop.title')}</h2>
              </div>
              <p className="mt-1 text-[11px] text-muted-foreground">
                {t('common.snapshotDate')} <span className="font-mono">{data.snapshot_date}</span>
                {' · '}{t('darkFundTop.universe')} <span className="font-mono">{data.universe ?? '-'}</span>
                {' · '}{t('darkFundTop.computed')} <span className="font-mono">{data.computed ?? '-'}</span>
                {' · '}{t('darkFundTop.topCount')} <span className="font-mono">{data.top?.length ?? 0}</span>
                {' · '}{t('common.source')} <span className="font-mono">thsdk_dde</span>({t('darkFundTop.sourceNote')})
                {' · '}<Badge
                  variant="outline"
                  className="cursor-help underline decoration-dotted underline-offset-2"
                  title={t('darkFundTop.caliberHint')}
                >{t('darkFundTop.caliberTag')}</Badge>
                {' · '}{t('common.updatedAt')} <span className="font-mono">{data.updated_at ?? '-'}</span>
              </p>
            </div>
            <Button onClick={refresh} disabled={refreshing} size="sm" variant="outline" className="min-h-[44px] min-w-[44px]">
              <RefreshCw className={`mr-1.5 h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
              {refreshing ? t('common.scanning') : t('darkFundTop.rescan')}
            </Button>
          </div>
        </div>

        <div className="overflow-hidden">
          {/* 桌面端: 表格 */}
          <div className="hidden md:block overflow-x-auto">
            <table className="w-full text-left text-[12px]">
              <thead className="sticky top-0 bg-muted/80 backdrop-blur">
                <tr className="border-b border-border/60 text-[11px] text-muted-foreground">
                  <th className="px-3 py-2 font-medium">{t('darkFundTop.rank')}</th>
                  <th className="px-3 py-2 font-medium">{t('darkFundTop.code')}</th>
                  <th className="px-3 py-2 font-medium">{t('darkFundTop.name')}</th>
                  <th
                    className="px-3 py-2 text-right font-medium cursor-pointer select-none hover:text-foreground min-h-[44px]"
                    onClick={() => toggleSort('main')}
                    title={`${t('darkFundTop.mainNetSort')}\n本列 = 同花顺官方主力净流入；与明盘（${MINGPAN.oneLine}）不是同一口径，也不冒充暗盘（${ANPAN.oneLine}）`}
                  >
                    {t('darkFundTop.mainNet')}{sortMark('main')}
                  </th>
                  <th
                    className="px-3 py-2 text-right font-medium cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort('ratio')}
                    title={t('darkFundTop.mainNetRatioHint')}
                  >
                    {t('darkFundTop.mainNetRatio')}{sortMark('ratio')}
                  </th>
                  <th
                    className="px-3 py-2 text-right font-medium cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort('amount')}
                    title={t('darkFundTop.totalAmountSort')}
                  >
                    {t('darkFundTop.totalAmount')}{sortMark('amount')}
                  </th>
                  {hasTckData ? (
                    <th
                      className="px-3 py-2 text-right font-medium"
                      title={t('darkFundTop.tckHint')}
                    >
                      {t('darkFundTop.tckDark')}<span className="font-normal text-muted-foreground">{t('darkFundTop.tckOnlyHeld')}</span>
                    </th>
                  ) : null}
                  <th className="px-3 py-2 font-medium">{t('common.source')}</th>
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
                      onClick={() => navigate(`/stocks/${encodeURIComponent(r.symbol)}`)}
                      title={`${r.symbol} → ${t('nav.stocks')}`}
                    >
                      <td className="px-3 py-2 font-mono text-muted-foreground">{i + 1}</td>
                      <td className="px-3 py-2 font-mono">
                        <span className="text-primary">{r.symbol}</span>
                      </td>
                      <td className="px-3 py-2 text-muted-foreground">{r.name ?? '-'}</td>
                      <td
                        className={`px-3 py-2 text-right font-mono ${
                          positive ? 'text-stock-up' : negative ? 'text-stock-down' : 'text-muted-foreground'
                        }`}
                      >
                        {toAmountFromWan(r.main_net_wan)}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-muted-foreground">
                        {safeFixed(r.main_net_ratio, 0, '-')}
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-muted-foreground">
                        {toAmountFromWanUnsigned(r.total_amount_wan)}
                      </td>
                      {hasTckData ? (
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
                              {toAmountFromWan(r.tck_dark_net_wan)}
                            </span>
                          ) : (
                            <span className="text-muted-foreground/50">--</span>
                          )}
                        </td>
                      ) : null}
                      <td className="px-3 py-2 font-mono text-[10px] text-muted-foreground">
                        {r.source}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          {/* 移动端: 卡片列表 */}
          <div className="md:hidden space-y-2">
            {sortedTop.map((r: DarkFundTopRow, i: number) => {
              const positive = (r.main_net_wan ?? 0) > 0
              const negative = (r.main_net_wan ?? 0) < 0
              return (
                <Card
                  key={`${r.symbol}-${i}`}
                  variant="hover"
                  className="p-3 min-h-[44px] cursor-pointer"
                  onClick={() => navigate(`/stocks/${encodeURIComponent(r.symbol)}`)}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-[10px] text-muted-foreground">#{i + 1}</span>
                        <span className="font-mono text-[13px] text-primary font-medium">{r.symbol}</span>
                        <span className="text-[12px] text-foreground truncate">{r.name ?? '-'}</span>
                      </div>
                      <div className="mt-1 text-[10px] text-muted-foreground">{r.source}</div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className={`font-mono text-[13px] font-semibold ${positive ? 'text-stock-up' : negative ? 'text-stock-down' : 'text-muted-foreground'}`}>
                        {toAmountFromWan(r.main_net_wan)}
                      </div>
                      <div className="text-[10px] text-muted-foreground font-mono">
                        {toAmountFromWanUnsigned(r.total_amount_wan)}
                      </div>
                    </div>
                  </div>
                  {hasTckData && r.tck_dark_net_wan != null && (
                    <div className="mt-1.5 text-[10px] text-muted-foreground">
                      {t('darkFundTop.tckDark')}: <span className={`font-mono ${r.tck_dark_net_wan > 0 ? 'text-stock-up' : r.tck_dark_net_wan < 0 ? 'text-stock-down' : ''}`}>{toAmountFromWan(r.tck_dark_net_wan)}</span>
                    </div>
                  )}
                </Card>
              )
            })}
          </div>

          <div className="border-t border-border/50 px-3 py-1.5 text-[11px] text-muted-foreground">
            {t('darkFundTop.footerLegend')} ·{' '}
            {hasTckData
              ? t('darkFundTop.tckHasData')
              : t('darkFundTop.tckHidden', { n: rowCount })}
            {' · '}{t('darkFundTop.amountMissing')}{' '}
            <a href="/api/market-scan/dark-fund-top" className="text-primary hover:underline min-h-[44px] inline-flex items-center">
              /api/market-scan/dark-fund-top
            </a>
          </div>
        </div>
      </div>
    )
  }

  return <div className="p-8 text-center text-[12px] text-muted-foreground">{t('common.dataAbnormal')}</div>
}
