/**
 * 连板晋级树页(创新 1, 2026-10-24 · 交接备注 §10 P0)。
 *
 * 消费 market_replay_daily(每日 15:50 东财三池同源落库, 此前无任何页面消费):
 *   GET /api/market/replay/tree?date=   → 单日梯队晋级树(缺省最新一期)
 *   GET /api/market/replay/series?days= → 近 N 日三池序列(点行切日期)
 *
 * 诚实口径(specs/harness/replay_tree.md REPTREE-001..006):
 *  - 缺数据显式「无数据」, 绝不补 0; 晋级率带三件套(样本数/基准缺口/一字板缺口);
 *  - 东财三池(eastmoney_zt, 不含北交所)与 TQ 全A自算(tq_pool)同屏各自带标签, 禁止混算;
 *  - 封单额/流通市值比表内未存 → 显式无数据; 分层晋级率不做前端现算;
 *  - 去卡片化: hairline 分隔, 无 Card 包裹; 买红卖绿; 数字 tabular-nums; 金额走 toAmount。
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { GitBranch, RefreshCw } from 'lucide-react'
import {
  marketReplayApi,
  type ReplaySeriesResp,
  type ReplayTreeResp,
  type ReplayTreeResult,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Skeleton } from '@/components/Skeleton'
import ReplayPromotionTree from '@/components/ReplayPromotionTree'
import { useI18n } from '@/hooks/useI18n'
import { navBackState } from '@/lib/nav-back'
import { safeFixed, safeInt } from '@/lib/format'

const SERIES_DAYS = 10

function isTreeAvailable(r: ReplayTreeResult): r is ReplayTreeResp {
  return r.available === true
}

/** 情绪周期用中性色(情绪不是价格, 不占红绿; 走查 02 号 D1 的阶段色口径) */
function phaseCls(label: string | null): string {
  if (!label) return 'text-muted-foreground'
  if (/冰点|退潮/.test(label)) return 'text-amber-500'
  if (/修复/.test(label)) return 'text-sky-400'
  if (/启动|主升|高潮|发酵/.test(label)) return 'text-primary'
  return 'text-foreground'
}

function PageSkeleton() {
  return (
    <div className="sida-page-enter space-y-4" aria-busy aria-live="polite">
      <div className="border-b border-border pb-3">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="mt-2 h-3 w-72" />
      </div>
      {Array.from({ length: 5 }).map((_, i) => (
        <Skeleton key={i} className="h-10 w-full" />
      ))}
    </div>
  )
}

function SectionTitle({ text, hint }: { text: string; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-t border-border pt-2">
      <span className="text-[14px] font-medium text-foreground">{text}</span>
      {hint && <span className="text-[12px] text-muted-foreground">{hint}</span>}
    </div>
  )
}

export default function ReplayTreePage() {
  const { t } = useI18n()
  const navigate = useNavigate()
  const [tree, setTree] = useState<ReplayTreeResult | null>(null)
  const [series, setSeries] = useState<ReplaySeriesResp | null>(null)
  const [treeDate, setTreeDate] = useState<string | undefined>(undefined)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const loadTree = useCallback(async (date?: string) => {
    setLoading(true)
    setError('')
    try {
      setTree(await marketReplayApi.tree(date))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'))
    } finally {
      setLoading(false)
    }
  }, [t])

  const loadSeries = useCallback(async () => {
    try {
      setSeries(await marketReplayApi.series({ days: SERIES_DAYS }))
    } catch {
      setSeries(null)
    }
  }, [])

  useEffect(() => {
    loadTree(treeDate)
  }, [loadTree, treeDate])

  useEffect(() => {
    loadSeries()
  }, [loadSeries])

  const pick = useCallback(
    (symbol: string) =>
      navigate(`/stocks/${encodeURIComponent(symbol)}`, {
        state: navBackState('/replay-tree', '连板晋级树'),
      }),
    [navigate],
  )

  if (loading && !tree) return <PageSkeleton />
  if (error && !tree)
    return <div className="p-8 text-center text-[12px] text-rose-500">{error}</div>
  if (!tree) return <div className="p-8 text-center text-[12px] text-muted-foreground">{t('replayTree.empty')}</div>

  if (!isTreeAvailable(tree)) {
    return (
      <div className="sida-page-enter space-y-4" data-testid="replay-tree-unavailable">
        <div className="border-b border-border pb-3">
          <div className="flex items-center gap-2">
            <GitBranch className="h-4 w-4 text-muted-foreground" />
            <h2 className="text-[16px] font-medium text-foreground">{t('replayTree.title')}</h2>
          </div>
        </div>
        <div className="py-6 text-center">
          <div className="text-[14px] text-foreground">{t('replayTree.noSnapshot')}</div>
          <p className="mt-1.5 text-[12px] text-muted-foreground">{tree.note}</p>
          {tree.latest_date && (
            <p className="mt-1 text-[12px] text-muted-foreground">
              {t('replayTree.latestIs')}{' '}
              <span className="font-mono">{tree.latest_date_iso}</span>
            </p>
          )}
          {tree.latest_date && (
            <div className="mt-3">
              <Button
                size="sm"
                variant="outline"
                className="min-h-[44px]"
                onClick={() => setTreeDate(tree.latest_date ?? undefined)}
              >
                {t('replayTree.viewLatest')}
              </Button>
            </div>
          )}
        </div>
      </div>
    )
  }

  const promo = tree.promotion
  const prev = tree.prev_comparison as {
    available?: boolean
    delta_limit_up?: number | null
    delta_broken?: number | null
    delta_limit_down?: number | null
    delta_break_rate_pp?: number | null
    prev_date?: string | null
  }
  const cc = tree.cross_check
  const seriesItems = series?.items ?? []

  return (
    <div className="sida-page-enter space-y-4" data-testid="replay-tree">
      {/* 页头 */}
      <div className="border-b border-border pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <GitBranch className="h-4 w-4 text-stock-up" />
              <h2 className="text-[16px] font-medium text-foreground">{t('replayTree.title')}</h2>
            </div>
            <p className="mt-1 flex flex-wrap items-center gap-x-1 text-[12px] text-muted-foreground">
              <span className="font-mono">{tree.trade_date_iso}</span>
              {' · '}{t('replayTree.sourceLabel')}{' '}
              <span className="font-mono">{tree.source ?? '无数据'}</span>
              ({t('replayTree.sourceNote')})
              {' · '}
              <span data-testid="replay-tree-caliber-em" title={t('replayTree.sourceNote')}>
                东财三池 <span className="font-mono">eastmoney_zt</span>
              </span>
              {tree.updated_at ? ` · ${t('replayTree.updated')} ${tree.updated_at}` : ''}
              {tree.quality && tree.quality !== 'ok' ? ` · ${t('replayTree.quality')} ${tree.quality}` : ''}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {treeDate && (
              <Button
                size="sm"
                variant="outline"
                className="min-h-[44px]"
                onClick={() => setTreeDate(undefined)}
              >
                {t('replayTree.viewLatest')}
              </Button>
            )}
            <Button
              size="sm"
              variant="outline"
              className="min-h-[44px] min-w-[44px]"
              onClick={() => {
                loadTree(treeDate)
                loadSeries()
              }}
            >
              <RefreshCw className={`mr-1.5 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              {t('common.refresh')}
            </Button>
          </div>
        </div>
      </div>

      {/* 三池概览(hairline 单行段, 无卡片) */}
      <section>
        <SectionTitle text={t('replayTree.overview')} hint={t('replayTree.caliberNote')} />
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 py-2 text-[12px]">
          <span className="text-muted-foreground">
            {t('replayTree.limitUp')}{' '}
            <span className="text-[14px] font-medium tabular-nums text-stock-up">
              {safeInt(tree.limit_up_count, '--')}
            </span>
            {prev?.available && prev.delta_limit_up != null && (
              <span className="ml-1 tabular-nums text-[12px] text-muted-foreground">
                ({prev.delta_limit_up >= 0 ? '+' : ''}
                {prev.delta_limit_up})
              </span>
            )}
          </span>
          <span className="text-muted-foreground">
            {t('replayTree.broken')}{' '}
            <span className="text-[14px] font-medium tabular-nums text-stock-down">
              {safeInt(tree.broken_count, '--')}
            </span>
            {tree.break_rate_pct != null && (
              <span className="ml-1 tabular-nums text-[12px] text-stock-down">
                {t('replayTree.breakRate')} {safeFixed(tree.break_rate_pct, 2, '--')}%
              </span>
            )}
            {tree.break_rate_pct == null && (
              <span className="ml-1 text-[12px] text-muted-foreground">
                {t('replayTree.breakRate')} {t('replayTree.noData')}
              </span>
            )}
          </span>
          <span className="text-muted-foreground">
            {t('replayTree.limitDown')}{' '}
            <span className="text-[14px] font-medium tabular-nums text-stock-down">
              {safeInt(tree.limit_down_count, '--')}
            </span>
          </span>
          <span className="text-muted-foreground">
            {t('replayTree.sealRate')}{' '}
            <span className="tabular-nums text-foreground">
              {tree.seal_rate_pct != null ? `${safeFixed(tree.seal_rate_pct, 2, '--')}%` : t('replayTree.noData')}
            </span>
          </span>
          <span className="text-muted-foreground">
            {t('replayTree.emotion')}{' '}
            <span className={`text-[12px] font-medium ${phaseCls(tree.phase_label)}`}>
              {tree.phase_label || t('replayTree.noData')}
            </span>
          </span>
          <span className="text-muted-foreground">
            {t('replayTree.maxStreak')}{' '}
            <span className="tabular-nums text-foreground">{safeInt(tree.max_streak, '--')}</span>
          </span>
        </div>
        {/* 晋级率 + 三件套(REPTREE-002) */}
        <div className="border-t border-border py-2 text-[12px]" data-testid="replay-tree-promotion">
          <span className="text-muted-foreground">
            {t('replayTree.promoRate')}{' '}
            {promo.available ? (
              <span className="text-[14px] font-medium tabular-nums text-stock-up">
                {safeFixed(promo.promo_rate_pct, 2, '--')}%
              </span>
            ) : (
              <span className="tabular-nums text-foreground">{t('replayTree.noData')}</span>
            )}
            {' · '}
            {t('replayTree.promoSample')}{' '}
            <span className="tabular-nums text-foreground">
              {promo.sample_count != null ? `n=${promo.sample_count}` : t('replayTree.noData')}
            </span>
            {promo.promoted != null && (
              <>
                {' · '}
                <span className="tabular-nums text-foreground">
                  {t('replayTree.promoted')} {promo.promoted}
                </span>
              </>
            )}
            {' · '}
            {t('replayTree.promoBaseline')}{' '}
            <span className="text-amber-500" title={promo.baseline_note}>
              {t('replayTree.promoBaselineMissing')}
            </span>
            {' · '}
            <span className="text-amber-500" title={promo.one_word_note}>
              {t('replayTree.promoOneWord')}
            </span>
            {' · '}
            {t('replayTree.oneWordCount')}{' '}
            <span className="tabular-nums text-foreground">{safeInt(promo.one_word_count, '--')}</span>
          </span>
        </div>
      </section>

      {/* 连板梯队晋级树 */}
      <section>
        <SectionTitle
          text={t('replayTree.ladder')}
          hint={`${t('replayTree.firstBoard')} ${safeInt(tree.first_board_count, '--')} · 2板+ ${safeInt(tree.ge2_count, '--')} · 3板+ ${safeInt(tree.ge3_count, '--')}`}
        />
        <ReplayPromotionTree
          levels={tree.levels}
          ladderAvailable={tree.ladder_available}
          maxStreak={tree.max_streak}
          detailNote={tree.detail_note}
          onPick={pick}
        />
      </section>

      {/* 近 N 日三池序列 */}
      <section>
        <SectionTitle text={t('replayTree.trend')} hint={t('replayTree.trendHint')} />
        {seriesItems.length === 0 ? (
          <div className="py-3 text-[12px] text-muted-foreground">{t('replayTree.noData')}</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12px]">
              <thead>
                <tr className="border-b border-border text-[12px] text-muted-foreground">
                  <th className="py-1.5 pr-3 font-medium">{t('replayTree.colDate')}</th>
                  <th className="py-1.5 pr-3 text-right font-medium">{t('replayTree.limitUp')}</th>
                  <th className="py-1.5 pr-3 text-right font-medium">{t('replayTree.broken')}</th>
                  <th className="py-1.5 pr-3 text-right font-medium">{t('replayTree.breakRate')}</th>
                  <th className="py-1.5 pr-3 text-right font-medium">{t('replayTree.limitDown')}</th>
                  <th className="py-1.5 pr-3 text-right font-medium">{t('replayTree.promoRate')}</th>
                  <th className="py-1.5 pr-3 text-right font-medium">{t('replayTree.maxStreak')}</th>
                  <th className="py-1.5 pr-3 font-medium">{t('replayTree.emotion')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/40">
                {[...seriesItems].reverse().map((it) => (
                  <tr
                    key={it.trade_date}
                    className={`cursor-pointer hover:bg-s2 ${it.trade_date === tree.trade_date ? 'bg-s2' : ''}`}
                    data-testid={`replay-series-row-${it.trade_date}`}
                    onClick={() => setTreeDate(it.trade_date)}
                  >
                    <td className="py-1.5 pr-3 font-mono text-[12px] text-muted-foreground">
                      {it.trade_date_iso}
                      {!it.available && (
                        <span className="ml-1 text-[12px]">{t('replayTree.missingDay')}</span>
                      )}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums text-stock-up">
                      {it.available ? safeInt(it.limit_up_count, '--') : '--'}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums text-stock-down">
                      {it.available ? safeInt(it.broken_count, '--') : '--'}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums text-stock-down">
                      {it.available && it.break_rate_pct != null
                        ? `${safeFixed(it.break_rate_pct, 2, '--')}%`
                        : '--'}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums text-stock-down">
                      {it.available ? safeInt(it.limit_down_count, '--') : '--'}
                    </td>
                    <td
                      className="py-1.5 pr-3 text-right tabular-nums text-foreground"
                      title={`${t('replayTree.promoSample')}${it.promo_prev_count != null ? ` n=${it.promo_prev_count}` : ''} · ${t('replayTree.promoBaseline')}${t('replayTree.promoBaselineMissing')} · ${t('replayTree.promoOneWord')}`}
                    >
                      {it.available && it.promo_rate_pct != null
                        ? `${safeFixed(it.promo_rate_pct, 2, '--')}%`
                        : '--'}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums text-foreground">
                      {it.available ? safeInt(it.max_streak, '--') : '--'}
                    </td>
                    <td className={`py-1.5 pr-3 text-[12px] ${phaseCls(it.phase_label)}`}>
                      {it.available ? it.phase_label || t('replayTree.noData') : '--'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* 口径与缺口 */}
      <section>
        <SectionTitle text={t('replayTree.caliberGaps')} />
        <ul className="divide-y divide-border/40 text-[12px] text-muted-foreground" data-testid="replay-tree-gaps">
          <li className="flex flex-wrap items-center gap-x-2 py-1.5">
            <span data-testid="replay-tree-cross-check">
              {t('replayTree.crossCheck')}:{' '}
              <span title={t('replayTree.sourceNote')}>
                东财三池 <span className="font-mono">eastmoney_zt</span>{' '}
                <span className="tabular-nums text-foreground">
                  {cc.eastmoney_zt != null ? safeInt(cc.eastmoney_zt, '--') : t('replayTree.noData')}
                </span>
              </span>
              {' vs '}
              <span title={t('replayTree.crossCheckTq')}>
                TQ 全A自算 <span className="font-mono">tq_pool</span>{' '}
                <span className="tabular-nums text-foreground">
                  {cc.tq_pool != null ? safeInt(cc.tq_pool, '--') : t('replayTree.noData')}
                </span>
              </span>
              {cc.diff != null && (
                <span className="tabular-nums">({t('replayTree.diff')} {cc.diff}{t('replayTree.diffUnit')})</span>
              )}
            </span>
          </li>
          <li className="py-1.5">
            {t('replayTree.sealMvRatio')}: <span className="text-foreground">{t('replayTree.noData')}</span>
            {' · '}{tree.seal_mv_ratio_note}
          </li>
          <li className="py-1.5">{promo.per_level_note}</li>
          {tree.missing_dims.length > 0 && (
            <li className="py-1.5">
              {t('replayTree.qualityMissing')}: {tree.missing_dims.join('、')}
            </li>
          )}
        </ul>
      </section>

      <div className="border-t border-border px-3 py-1.5 text-[12px] text-muted-foreground">
        {t('replayTree.footerLegend')}
      </div>
    </div>
  )
}
