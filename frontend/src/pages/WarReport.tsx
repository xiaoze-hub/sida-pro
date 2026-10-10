/**
 * 主力资金战报页(规格 §4.4『主力资金战报』, 2026-10-10)。
 *
 * 当日主力动向汇总:
 *   - 全市场大单净流入 TOP(吸筹)/ TOP(派发) —— thsdk DDE 大单口径(万元);
 *   - 个股主力净额变化 = 当日 DDE 采样区间增量(无采样 → --);
 *   - 行业分布(TQ SUPAMO 板块主力资金, 亿元);
 *   - 拆单/对倒计数(委托号级 .tck; 对倒需账户信息 → 不可识别)。
 *
 * 集成路径:
 *   GET  /api/war-report/daily    → WarReportResp
 *   POST /api/war-report/refresh  → 手动触发(同步 ~16s, owner)
 *
 * 诚实口径(AGENTS): 缺源显式「无数据」, 不显示 0、不编造; 资金面必须可见口径标签。
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, RefreshCw, ShieldAlert, TrendingUp } from 'lucide-react'
import {
  warReportApi,
  type WarReportResp,
  type WarReportRow,
  type WarReportUnavailable,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Card } from '@panwatch/base-ui/components/ui/card'
import CaliberBadge from '@panwatch/biz-ui/components/CaliberBadge'
import { Skeleton } from '@/components/Skeleton'
import { useI18n } from '@/hooks/useI18n'
import { navBackState } from '@/lib/nav-back'
import { toAmountFromWan } from '@/lib/format'

function isUnavailable(r: WarReportResp): r is WarReportUnavailable {
  return r.available === false
}

function WarReportSkeleton() {
  return (
    <div className="sida-page-enter space-y-4" aria-busy aria-live="polite">
      <div className="border-b border-border/40 pb-3">
        <Skeleton className="h-5 w-48" />
        <Skeleton className="mt-2 h-3 w-72" />
      </div>
      {Array.from({ length: 6 }).map((_, i) => (
        <Skeleton key={i} className="h-10 w-full" />
      ))}
    </div>
  )
}

function RowTable({ rows, onPick }: { rows: WarReportRow[]; onPick: (s: string) => void }) {
  const { t } = useI18n()
  if (!rows.length) {
    return <div className="px-3 py-3 text-[12px] text-muted-foreground">{t('common.dataAbnormal')}</div>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[12px]">
        <thead className="bg-muted/60">
          <tr className="border-b border-border/60 text-[11px] text-muted-foreground">
            <th className="px-3 py-2 font-medium">{t('warReport.rank')}</th>
            <th className="px-3 py-2 font-medium">{t('warReport.code')}</th>
            <th className="px-3 py-2 font-medium">{t('warReport.name')}</th>
            <th className="px-3 py-2 text-right font-medium">{t('warReport.mainNet')}</th>
            <th className="px-3 py-2 text-right font-medium" title={t('warReport.netChangeHint')}>
              {t('warReport.netChange')}
            </th>
            <th className="px-3 py-2 text-right font-medium" title={t('warReport.samplesHint')}>
              {t('warReport.samples')}
            </th>
            <th className="px-3 py-2 font-medium">{t('common.source')}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/40">
          {rows.map((r, i) => {
            const main = r.main_net_wan
            const chg = r.net_change_wan
            return (
              <tr
                key={`${r.symbol}-${i}`}
                className="cursor-pointer hover:bg-accent/20"
                onClick={() => onPick(r.symbol)}
                title={`${r.symbol}`}
              >
                <td className="px-3 py-2 font-mono text-muted-foreground">{i + 1}</td>
                <td className="px-3 py-2 font-mono text-primary">{r.symbol}</td>
                <td className="px-3 py-2 text-muted-foreground">{r.name || '-'}</td>
                <td className={`px-3 py-2 text-right font-mono ${main > 0 ? 'text-stock-up' : main < 0 ? 'text-stock-down' : ''}`}>
                  {toAmountFromWan(main)}
                </td>
                <td className={`px-3 py-2 text-right font-mono ${chg != null && chg > 0 ? 'text-stock-up' : chg != null && chg < 0 ? 'text-stock-down' : 'text-muted-foreground'}`}>
                  {toAmountFromWan(chg)}
                </td>
                <td className="px-3 py-2 text-right font-mono text-muted-foreground">{r.samples ?? '--'}</td>
                <td className="px-3 py-2 font-mono text-[10px] text-muted-foreground">{r.source ?? '--'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default function WarReportPage() {
  const navigate = useNavigate()
  const { t } = useI18n()
  const [data, setData] = useState<WarReportResp | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setData(await warReportApi.daily())
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
    setError('')
    try {
      const r = await warReportApi.refresh()
      setData(r)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('warReport.refreshFailed'))
    } finally {
      setRefreshing(false)
    }
  }, [t])

  const pick = useCallback(
    (symbol: string) => navigate(`/stocks/${encodeURIComponent(symbol)}`, { state: navBackState('/war-report', '主力战报') }),
    [navigate],
  )

  if (loading) return <WarReportSkeleton />
  if (error) return <div className="p-8 text-center text-[12px] text-rose-500">{error}</div>
  if (!data) return <div className="p-8 text-center text-[12px] text-muted-foreground">{t('warReport.empty')}</div>

  if (isUnavailable(data)) {
    return (
      <div className="space-y-4">
        <Card className="p-6 text-center">
          <AlertTriangle className="mx-auto h-8 w-8 text-amber-500" />
          <div className="mt-3 text-[13px] font-medium text-foreground">{t('warReport.noSnapshot')}</div>
          <p className="mt-1.5 whitespace-pre-wrap text-[12px] text-muted-foreground">{data.note}</p>
          <div className="mt-4">
            <Button onClick={refresh} disabled={refreshing} size="sm" className="min-h-[44px]">
              <RefreshCw className={`mr-1.5 h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
              {t('warReport.scanNow')}
            </Button>
          </div>
        </Card>
      </div>
    )
  }

  const industry = data.industry
  const split = data.split_wash

  return (
    <div className="sida-page-enter space-y-4" data-testid="war-report">
      <div className="border-b border-border/40 pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <TrendingUp className="h-4 w-4 text-stock-up" />
              <h2 className="text-[13px] font-medium text-foreground">{t('warReport.title')}</h2>
            </div>
            <p className="mt-1 text-[11px] text-muted-foreground">
              {t('common.snapshotDate')} <span className="font-mono">{data.snapshot_date}</span>
              {' · '}{t('warReport.universe')} <span className="font-mono">{data.universe ?? '-'}</span>
              {' · '}{t('warReport.computed')} <span className="font-mono">{data.computed ?? '-'}</span>
              {' · '}{t('common.source')} <span className="font-mono">thsdk_dde</span>({t('warReport.sourceNote')})
              {' · '}
              <CaliberBadge caliber="eastmoney4" label={t('warReport.caliberTag')} title={t('warReport.caliberHint')} />
              {' · '}{t('common.updatedAt')} <span className="font-mono">{data.updated_at ?? '-'}</span>
            </p>
          </div>
          <Button onClick={refresh} disabled={refreshing} size="sm" variant="outline" className="min-h-[44px] min-w-[44px]">
            <RefreshCw className={`mr-1.5 h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
            {refreshing ? t('common.scanning') : t('warReport.rescan')}
          </Button>
        </div>
      </div>

      {/* 大单净流入 TOP */}
      <section className="overflow-hidden rounded-xl border border-border/60 bg-card">
        <div className="border-b border-border/40 px-3 py-2 text-[13px] font-medium text-foreground">
          {t('warReport.topInflow')}
        </div>
        <RowTable rows={data.top_inflow || []} onPick={pick} />
      </section>

      {/* 大单净流出 TOP */}
      <section className="overflow-hidden rounded-xl border border-border/60 bg-card">
        <div className="border-b border-border/40 px-3 py-2 text-[13px] font-medium text-foreground">
          {t('warReport.topOutflow')}
        </div>
        <RowTable rows={data.top_outflow || []} onPick={pick} />
      </section>

      {/* 行业分布 */}
      <section className="rounded-xl border border-border/60 bg-card">
        <div className="flex items-center justify-between border-b border-border/40 px-3 py-2">
          <span className="text-[13px] font-medium text-foreground">{t('warReport.industryTitle')}</span>
          <span className="text-[10px] text-muted-foreground">{t('warReport.industryHint')}</span>
        </div>
        {industry?.available && (industry.top?.length ?? 0) > 0 ? (
          <ul className="divide-y divide-border/40">
            {(industry.top || []).map((row) => (
              <li key={row.code} className="flex items-center justify-between px-3 py-2 text-[12px]">
                <span className="text-foreground">{row.name}</span>
                <span className={`font-mono ${row.fund_net_yi > 0 ? 'text-stock-up' : row.fund_net_yi < 0 ? 'text-stock-down' : 'text-muted-foreground'}`}>
                  {row.fund_net_yi > 0 ? '+' : ''}{row.fund_net_yi}亿
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="px-3 py-3 text-[12px] text-muted-foreground">
            {t('warReport.industryEmpty')}{industry?.note ? ` · ${industry.note}` : ''}
          </div>
        )}
      </section>

      {/* 拆单 / 对倒计数 */}
      <section className="rounded-xl border border-border/60 bg-card">
        <div className="flex items-center gap-2 border-b border-border/40 px-3 py-2">
          <ShieldAlert className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="text-[13px] font-medium text-foreground">{t('warReport.splitWashTitle')}</span>
        </div>
        {split?.available ? (
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-3 py-3 text-[12px]">
            <span className="text-muted-foreground">
              {t('warReport.splitCount')}: <span className="font-mono text-foreground">{split.split_count ?? '--'}</span>
            </span>
            <span className="text-muted-foreground">
              {t('warReport.washCount')}: <span className="font-mono text-foreground">{t('warReport.washNoSource')}</span>
            </span>
            <span className="text-[10px] text-muted-foreground">{split.note}</span>
          </div>
        ) : (
          <div className="px-3 py-3 text-[12px] text-muted-foreground">
            {t('warReport.splitWashEmpty')}{split?.note ? ` · ${split.note}` : ''}
          </div>
        )}
      </section>

      <div className="border-t border-border/50 px-3 py-1.5 text-[11px] text-muted-foreground">
        {t('warReport.footerLegend')}{' · '}
        <a href="/api/war-report/daily" className="text-primary hover:underline min-h-[44px] inline-flex items-center">
          /api/war-report/daily
        </a>
      </div>
    </div>
  )
}
