import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Loader2, RefreshCw } from 'lucide-react'
import {
  decisionsApi,
  signalsReviewApi,
  kindLabel,
  type DecisionLogResponse,
  type DecisionStatsResponse,
  type DecisionBacktestResponse,
  type EntryOutcomesResponse,
  type SignalHitRateResponse,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import {
  contextSummary,
  hitRateText,
  isFilled,
  priceText,
  retClass,
  retText,
  sampleText,
  ratioPctText,
  pctValueText,
  numText,
  ratioValueText,
  phaseLabel,
  signalTypeLabel,
  insufficientHint,
} from '@/lib/decision-ledger'

/**
 * 决策账本 / 复盘中心(设计稿 v3.0 §八 / 开发计划 P1-3)。
 *
 * 回答一个问题: **"信号出过以后, 到底管不管用?"**
 * 后端(v175 `decision_log`)把每个信号连同**当时的证据与价格**留痕, 事后用真实 K 线回填 T+1/3/5。
 *
 * 审计(P2 合集, 2026-10-10)补齐三处「有功能没入口」+ 一处分页:
 *  ① **共振回测**(P2-1): `GET /api/decisions/backtest` —— 后端能力原全仓零调用; 结果**带 basis
 *     口径标记**(双指标/三指标), 明盘历史无源不能与官方四态直接比较 —— 一律显式透传, 不藏;
 *  ② **信号对账**(P2-2): `GET /api/signals/hit-rate` —— 按信号类型 T+1/T+5 胜率 + 官方基准对照;
 *  ③ **入场候选后验**(P2-3): `GET /api/decisions/entry-outcomes` —— 原仅 cron 写入无查询口;
 *  ④ **账本明细分页**(P2-4): `GET /api/decisions/log` 补 offset/日期过滤, 前端翻页。
 *
 * 四条不越界的口径(与后端一致, 前端只透传):
 *  ① **样本不足不给数字**: `--` + 原因(样本按自然日累积, T+5 要等一周);
 *  ② **未回填不是 0**: 收益/命中为 null → `--`, 颜色走中性(不许把 null 当 0 上"涨红"色);
 *  ③ **当时没取到价就是 `--`**(不拿今天的价冒充当时的价);
 *  ④ 命中率/胜率**只取后端值**, 前端不自己算百分比; 缺数据/降级一律显式, 不猜不编造。
 */
const LOG_PAGE = 20

export default function DecisionLedger() {
  const [stats, setStats] = useState<DecisionStatsResponse | null>(null)
  const [log, setLog] = useState<DecisionLogResponse | null>(null)
  const [hitRate, setHitRate] = useState<SignalHitRateResponse | null>(null)
  const [outcomes, setOutcomes] = useState<EntryOutcomesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  const [offset, setOffset] = useState(0)
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')

  const [btSymbols, setBtSymbols] = useState('002361')
  const [bt, setBt] = useState<DecisionBacktestResponse | null>(null)
  const [btLoading, setBtLoading] = useState(false)
  const [btErr, setBtErr] = useState<string | null>(null)

  const load = useCallback(
    async (nextOffset = offset, sd = startDate, ed = endDate) => {
      setLoading(true)
      setErr(null)
      try {
        // 核心两段(stats/log)失败 → 页面报错; 附带两段(对账/后验)单独兜底, 不让一段抖动带垮整页
        const [s, l] = await Promise.all([
          decisionsApi.stats(180, 30),
          decisionsApi.log(undefined, LOG_PAGE, nextOffset, sd || undefined, ed || undefined),
        ])
        setStats(s)
        setLog(l)
        setOffset(nextOffset)
      } catch (e) {
        setErr(e instanceof Error ? e.message : '加载失败')
      } finally {
        setLoading(false)
      }
      const [hr, oc] = await Promise.allSettled([
        signalsReviewApi.hitRate(undefined, 30),
        decisionsApi.entryOutcomes(30, 20),
      ])
      if (hr.status === 'fulfilled') setHitRate(hr.value)
      if (oc.status === 'fulfilled') setOutcomes(oc.value)
    },
    [offset, startDate, endDate],
  )

  useEffect(() => {
    void load(0, '', '')
    // 首屏只加载一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const runBacktest = useCallback(async () => {
    setBtLoading(true)
    setBtErr(null)
    try {
      const r = await decisionsApi.backtest({ symbols: btSymbols, holdDays: 5, successPct: 3 })
      setBt(r)
    } catch (e) {
      setBtErr(e instanceof Error ? e.message : '回测失败')
    } finally {
      setBtLoading(false)
    }
  }, [btSymbols])

  const rows = stats?.rows ?? []
  const items = log?.items ?? []
  const total = log?.total ?? items.length
  const hasMore = log?.has_more ?? false
  const allInsufficient =
    rows.length > 0 &&
    rows.every((r) => r.horizons.t1.insufficient && r.horizons.t3.insufficient && r.horizons.t5.insufficient)
  const hitTypes = Object.entries(hitRate?.by_type ?? {})
  const outcomeRows = outcomes?.rows ?? []

  return (
    <div className="mx-auto max-w-[1200px] p-3 md:p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-[20px] font-bold tracking-tight">决策账本</h1>
        <span className="text-[11px] text-muted-foreground">
          信号 → 结果的账 · 命中 = T+n 收益 &gt; 0(平盘记未命中)
        </span>
        <Button variant="ghost" size="sm" className="ml-auto" onClick={() => void load(offset, startDate, endDate)} disabled={loading}>
          {loading ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="mr-1 h-3.5 w-3.5" />}
          刷新
        </Button>
      </div>

      {err && (
        <div className="mt-3 flex items-center gap-2 rounded border border-border/60 px-2 py-1.5 text-[12px] text-muted-foreground">
          <AlertTriangle className="h-3.5 w-3.5" />
          加载失败：{err}
        </div>
      )}

      {/* ① 命中率 */}
      <section className="mt-4">
        <h2 className="text-[16px] font-semibold">命中率</h2>
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          样本按自然日累积(T+5 要等一周), 样本不足时不给数字 —— 拿 3 个样本算出的百分比会误导决策。
        </p>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-left text-[12px]">
            <thead>
              <tr className="border-b border-border/60 text-muted-foreground">
                <th className="px-2 py-1 font-medium">信号</th>
                <th className="px-2 py-1 text-right font-medium">信号数</th>
                <th className="px-2 py-1 text-right font-medium">T+1</th>
                <th className="px-2 py-1 text-right font-medium">T+3</th>
                <th className="px-2 py-1 text-right font-medium">T+5</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {rows.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-2 py-3 text-[12px] text-muted-foreground">
                    还没有留痕信号 —— 共振扫描每天盘后跑一次, 出信号后才会记进账本。
                  </td>
                </tr>
              )}
              {rows.map((r) => (
                <tr key={r.signal_kind}>
                  <td className="px-2 py-1">{kindLabel(r.signal_kind)}</td>
                  <td className="px-2 py-1 text-right font-mono">{sampleText(r.n_total)}</td>
                  {(['t1', 't3', 't5'] as const).map((k) => {
                    const c = hitRateText(r.horizons[k])
                    return (
                      <td
                        key={k}
                        title={c.title}
                        className={`px-2 py-1 text-right font-mono ${c.muted ? 'text-muted-foreground' : ''}`}
                      >
                        {c.text}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {allInsufficient && (
          <p className="mt-1.5 text-[11px] text-muted-foreground">
            目前全部信号都处在「样本不足」阶段(需要等未来的 K 线才算得出来)—— 这是账本刚启用的正常状态, 不是没数据。
          </p>
        )}
        {stats?.note && <p className="mt-1 text-[10px] text-muted-foreground/80">{stats.note}</p>}
      </section>

      {/* ② 信号对账(批次D hit-rate) */}
      <section className="mt-6" data-testid="signal-reconcile">
        <h2 className="text-[16px] font-semibold">信号对账</h2>
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          按信号类型统计 T+1/T+5 胜率(近 {hitRate?.days ?? 30} 天) · 只取后端值, 前端不自己算百分比;
          resonance 附官方基准对照(口径不完全一致, 仅参考)。
        </p>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-left text-[12px]">
            <thead>
              <tr className="border-b border-border/60 text-muted-foreground">
                <th className="px-2 py-1 font-medium">信号类型</th>
                <th className="px-2 py-1 text-right font-medium">T+1 胜率</th>
                <th className="px-2 py-1 text-right font-medium">T+1 均值</th>
                <th className="px-2 py-1 text-right font-medium">T+5 胜率</th>
                <th className="px-2 py-1 text-right font-medium">T+5 均值</th>
                <th className="px-2 py-1 text-right font-medium">官方基准</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {hitTypes.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-2 py-3 text-[12px] text-muted-foreground">
                    暂无对账数据 —— 每晚 18:30 对账作业写入后才有(样本按自然日累积)。
                  </td>
                </tr>
              )}
              {hitTypes.map(([t, v]) => (
                <tr key={t}>
                  <td className="px-2 py-1">{signalTypeLabel(t)}</td>
                  <td
                    className={`px-2 py-1 text-right font-mono ${v.t1.win_rate == null ? 'text-muted-foreground' : ''}`}
                    title={v.t1.win_rate == null ? insufficientHint(v.t1.n, undefined) : `n=${v.t1.n}`}
                  >
                    {v.t1.win_rate == null ? '--' : pctValueText(v.t1.win_rate, 2)}
                  </td>
                  <td className="px-2 py-1 text-right font-mono text-muted-foreground">{pctValueText(v.t1.avg_pct, 2)}</td>
                  <td
                    className={`px-2 py-1 text-right font-mono ${v.t5.win_rate == null ? 'text-muted-foreground' : ''}`}
                    title={v.t5.win_rate == null ? insufficientHint(v.t5.n, undefined) : `n=${v.t5.n}`}
                  >
                    {v.t5.win_rate == null ? '--' : pctValueText(v.t5.win_rate, 2)}
                  </td>
                  <td className="px-2 py-1 text-right font-mono text-muted-foreground">{pctValueText(v.t5.avg_pct, 2)}</td>
                  <td className="px-2 py-1 text-right text-muted-foreground" title={v.official_benchmark?.note ?? '官方基准仅 resonance 且有样本时对照'}>
                    {v.official_benchmark ? `${ratioValueText(v.official_benchmark.win_rate)}%` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* ③ 入场候选后验 */}
      <section className="mt-6" data-testid="entry-outcomes">
        <h2 className="text-[16px] font-semibold">入场候选后验</h2>
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          候选发出后按 T+n 后验: 胜率 = 后验收益 &gt; 0 的占比 · 样本不足(低于 {outcomes?.min_sample ?? 20})不给数字。
        </p>
        {outcomes && !outcomes.available && (
          <p className="mt-2 text-[12px] text-muted-foreground">查询失败：{outcomes.error ?? '未知原因'}(不推算)</p>
        )}
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-left text-[12px]">
            <thead>
              <tr className="border-b border-border/60 text-muted-foreground">
                <th className="px-2 py-1 font-medium">Horizon</th>
                <th className="px-2 py-1 font-medium">来源</th>
                <th className="px-2 py-1 text-right font-medium">样本</th>
                <th className="px-2 py-1 text-right font-medium">胜率</th>
                <th className="px-2 py-1 text-right font-medium">均收益</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {outcomeRows.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-2 py-3 text-[12px] text-muted-foreground">
                    暂无可后验样本 —— 候选到期后由 cron 逐日评估, 尚无到期记录(空即空, 不补造)。
                  </td>
                </tr>
              )}
              {outcomeRows.map((r) => (
                <tr key={`${r.horizon_days}-${r.source}`}>
                  <td className="px-2 py-1 font-mono">T+{r.horizon_days}</td>
                  <td className="px-2 py-1">{r.source_label}</td>
                  <td className="px-2 py-1 text-right font-mono">{numText(r.total)}</td>
                  <td
                    className={`px-2 py-1 text-right font-mono ${r.insufficient ? 'text-muted-foreground' : ''}`}
                    title={r.insufficient ? insufficientHint(r.total, r.min_sample) : `胜 ${r.wins}/${r.total}`}
                  >
                    {r.insufficient ? '--' : pctValueText(r.win_rate, 2)}
                  </td>
                  <td className={`px-2 py-1 text-right font-mono ${r.insufficient ? 'text-muted-foreground' : retClass(r.avg_return_pct)}`}>
                    {r.insufficient ? '--' : retText(r.avg_return_pct == null ? null : r.avg_return_pct / 100)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {outcomes?.note && <p className="mt-1 text-[10px] text-muted-foreground/80">{outcomes.note}</p>}
      </section>

      {/* ④ 共振回测 */}
      <section className="mt-6" data-testid="resonance-backtest">
        <h2 className="text-[16px] font-semibold">三指标共振回测</h2>
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          逐日滚动重算(单股 O(n²), 池 ≤ 50、区间 ≤ 2 年)。明盘历史无源: 默认走「双指标」(缺资金维),
          与官方四态不可直接比较 —— 结果口径见下方 basis 标记。
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <input
            className="h-8 w-64 rounded-md border border-border bg-background px-2 font-mono text-[12px]"
            placeholder="股票池, 逗号分隔, 如 002361,600519"
            value={btSymbols}
            onChange={(e) => setBtSymbols(e.target.value)}
            aria-label="回测股票池"
          />
          <Button variant="outline" size="sm" onClick={() => void runBacktest()} disabled={btLoading}>
            {btLoading ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
            回测
          </Button>
        </div>
        {btErr && <p className="mt-2 text-[12px] text-muted-foreground">回测失败：{btErr}</p>}
        {bt && !bt.available && (
          <p className="mt-2 text-[12px] text-muted-foreground" data-testid="backtest-unavailable">
            回测不可用：{bt.error ?? '未跑出结果'}(口径 {bt.basis})
          </p>
        )}
        {bt?.available && (
          <div className="mt-2" data-testid="backtest-result">
            <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
              <span className="rounded border border-border/60 px-2 py-0.5" data-testid="backtest-basis">
                口径：{bt.basis}
              </span>
              <span>样本: {numText(bt.sample?.signals)} 信号 / {numText(bt.sample?.symbols)} 股</span>
              {bt.generated_at && <span>生成于 {bt.generated_at}</span>}
            </div>
            <div className="mt-2 overflow-x-auto">
              <table className="w-full text-left text-[12px]">
                <thead>
                  <tr className="border-b border-border/60 text-muted-foreground">
                    <th className="px-2 py-1 font-medium">共振态</th>
                    <th className="px-2 py-1 text-right font-medium">样本</th>
                    <th className="px-2 py-1 text-right font-medium">上涨概率</th>
                    <th className="px-2 py-1 text-right font-medium">盈亏比</th>
                    <th className="px-2 py-1 text-right font-medium">官方基准</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/40">
                  {(['向好', '拐点', '分歧', '走坏'] as const).map((p) => {
                    const agg = bt.by_phase?.[p]
                    const off = bt.official?.[p]
                    return (
                      <tr key={p}>
                        <td className="px-2 py-1">{phaseLabel(p)}</td>
                        <td className="px-2 py-1 text-right font-mono">{numText(agg?.count)}</td>
                        <td className={`px-2 py-1 text-right font-mono ${agg?.win_rate == null ? 'text-muted-foreground' : ''}`}>
                          {agg?.win_rate == null ? '--' : ratioPctText(agg.win_rate, 2)}
                        </td>
                        <td className="px-2 py-1 text-right font-mono">{ratioValueText(agg?.profit_ratio ?? null)}</td>
                        <td className="px-2 py-1 text-right text-muted-foreground">
                          {off ? `${ratioValueText(off.win_rate)}% / ${ratioValueText(off.pl_ratio)}` : '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            {bt.note && <p className="mt-1 text-[10px] text-muted-foreground/80">{bt.note}</p>}
          </div>
        )}
      </section>

      {/* ⑤ 明细(分页) */}
      <section className="mt-6">
        <h2 className="text-[16px] font-semibold">信号明细</h2>
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-[11px] text-muted-foreground">
            共 {numText(total)} 条 · 当前第 {numText(offset + 1)}–{numText(offset + items.length)} 条
          </p>
          <div className="ml-auto flex items-center gap-2">
            <input
              className="h-7 w-32 rounded-md border border-border bg-background px-2 font-mono text-[11px]"
              placeholder="起 YYYY-MM-DD"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              aria-label="起始日期"
            />
            <input
              className="h-7 w-32 rounded-md border border-border bg-background px-2 font-mono text-[11px]"
              placeholder="止 YYYY-MM-DD"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              aria-label="结束日期"
            />
            <Button variant="outline" size="sm" onClick={() => void load(0, startDate, endDate)} disabled={loading}>
              过滤
            </Button>
            <Button variant="outline" size="sm" onClick={() => void load(Math.max(0, offset - LOG_PAGE), startDate, endDate)} disabled={loading || offset <= 0}>
              上一页
            </Button>
            <Button variant="outline" size="sm" onClick={() => void load(offset + LOG_PAGE, startDate, endDate)} disabled={loading || !hasMore}>
              下一页
            </Button>
          </div>
        </div>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-left text-[11px]">
            <thead>
              <tr className="border-b border-border/60 text-muted-foreground">
                <th className="px-2 py-1 font-medium">日期</th>
                <th className="px-2 py-1 font-medium">信号</th>
                <th className="px-2 py-1 font-medium">标的</th>
                <th className="px-2 py-1 text-right font-medium">当时价</th>
                <th className="px-2 py-1 font-medium">证据</th>
                <th className="px-2 py-1 text-right font-medium">T+1</th>
                <th className="px-2 py-1 text-right font-medium">T+3</th>
                <th className="px-2 py-1 text-right font-medium">T+5</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {items.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-2 py-3 text-[12px] text-muted-foreground">
                    暂无明细(当前页为空 —— 可能已翻到末页或过滤条件过窄)。
                  </td>
                </tr>
              )}
              {items.map((it, i) => (
                <tr key={`${it.signal_kind}-${it.symbol}-${it.trade_date}-${i}`} className={isFilled(it) ? '' : 'text-muted-foreground'}>
                  <td className="px-2 py-1 font-mono">{it.trade_date}</td>
                  <td className="px-2 py-1">{kindLabel(it.signal_kind)}</td>
                  <td className="px-2 py-1 font-mono">{it.symbol}</td>
                  <td className="px-2 py-1 text-right font-mono">{priceText(it.price_at_signal)}</td>
                  <td className="px-2 py-1 text-muted-foreground" title={it.context}>
                    {contextSummary(it.context) || '—'}
                  </td>
                  {(['t1', 't3', 't5'] as const).map((k) => {
                    const o = it.outcomes?.[k]
                    return (
                      <td key={k} className={`px-2 py-1 text-right font-mono ${retClass(o?.ret ?? null)}`}>
                        {retText(o?.ret ?? null)}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {log?.note && <p className="mt-1 text-[10px] text-muted-foreground/80">{log.note}</p>}
      </section>
    </div>
  )
}
