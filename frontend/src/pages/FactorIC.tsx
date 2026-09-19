/**
 * 因子有效性页(B9 数据资产 E1, 2026-09-19) —— 回答"哪些因子在这段行情里真的有用"。
 *
 * ## 口径(全部来自后端 `src/core/factor_eval.py`, 前端**只显示不计算**)
 * - **横截面 IC 是主口径**(按快照日做横截面 Spearman 后取均值);
 * - `ic_pooled` 是**参考值**(混入时序变异, 与真实选股能力可能背离) —— 页面显式标"参考";
 * - `ic` 需 **≥3 个截面期数**才算, 不足时后端给 `null` → 页面显示 `--` **并说明"期数不足"**;
 *   这里**不许**自己算 IC, 也不许把 `null` 当 0(那会读成"这因子无效");
 * - 样本外 IC(后 30% 交易日)单独一列 —— 它才是"过拟合没"的判据;
 * - 后端带 `error` 时**照实显示**, 不装作"没数据"。
 *
 * 页面性质: 研究/复核用(不是盘中决策页), 因此允许表格 + 说明, 但仍守终端字阶(≤6 档)。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Loader2, RefreshCw } from 'lucide-react'
import { factorICApi } from '@panwatch/api'
import type { FactorIC, FactorICHistoryResp, FactorICResp } from '@panwatch/api'
// 全仓纪律: 数字格式化走 @/lib/format 的 safe* 系列(null → fallback, 不抛不编)
import { safeFixed } from '@/lib/format'

/**
 * IC 时序迷你图。**诚实口径**: 只有 ≥2 个"算得出来"的点才画线; 点不够就显示 `--` +
 * 悬停说明"快照不足" —— 不许把 1 个点画成一条线, 也不许把 null 当 0 拉平。
 */
function Spark({ series, title }: { series: number[]; title: string }) {
  if (series.length < 2) {
    return <span className="text-muted-foreground" title={title || '快照不足(需 ≥2 天已算出 IC)'}>--</span>
  }
  // 坐标保留 1 位: 这是**几何**不是数字格式化, 所以不走 safe*(R6 门禁针对的是展示数字) ——
  // 用算术取整, 避免与"数字必须走 safe*"的纪律混淆。
  const r1 = (n: number) => Math.round(n * 10) / 10
  const w = 64, h = 16, pad = 2
  const min = Math.min(...series), max = Math.max(...series)
  const span = max - min || 1
  const pts = series.map((v, i) => {
    const x = pad + (i * (w - pad * 2)) / (series.length - 1)
    const y = h - pad - ((v - min) / span) * (h - pad * 2)
    return `${r1(x)},${r1(y)}`
  }).join(' ')
  const last = series[series.length - 1]
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={title}>
      <title>{title}</title>
      <line x1={pad} y1={h - pad - ((0 - min) / span) * (h - pad * 2)}
            x2={w - pad} y2={h - pad - ((0 - min) / span) * (h - pad * 2)}
            stroke="currentColor" strokeOpacity="0.25" strokeDasharray="2 2" strokeWidth="1" />
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.2"
                className={last >= 0 ? 'text-red-400' : 'text-emerald-400'} />
    </svg>
  )
}

/** 期数下限(与后端一致: ic 需 ≥3, 样本外 ≥2) */
const MIN_PERIODS = 3
const MIN_HOLDOUT = 2

function num(v: number | null | undefined, digits = 3): string {
  return safeFixed(v, digits, '--')
}

/** IC 单元格: 没到样本量 → `--` + 悬停说明(不是 0)。 */
function icCell(v: number | null, periods: number, minPeriods: number) {
  if (v === null || v === undefined) {
    return { text: '--', title: `期数不足(${periods} < ${minPeriods}), 后端不给 IC —— 不是"无效"`, cls: 'text-muted-foreground' }
  }
  const strong = Math.abs(v) >= 0.05
  return {
    text: num(v),
    title: `横截面 IC, 期数 ${periods}`,
    cls: strong ? (v > 0 ? 'text-red-400' : 'text-emerald-400') : 'text-foreground',
  }
}

export default function FactorIC() {
  const [days, setDays] = useState(90)
  const [horizon, setHorizon] = useState(5)
  const [data, setData] = useState<FactorICResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  const [hist, setHist] = useState<Record<string, number[]>>({})
  const [histDays, setHistDays] = useState(0)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await factorICApi.evaluate(days, horizon))
      setErr('')
    } catch (e) {
      setErr(e instanceof Error ? `取数失败：${e.message}` : '取数失败')
    } finally {
      setLoading(false)
    }
  }, [days, horizon])

  useEffect(() => { void load() }, [load])

  // IC 时序(B9 收口): 与当前 horizon 对齐; 取不到就空着(不编历史)
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const r: FactorICHistoryResp = await factorICApi.history(horizon, 30)
        if (!alive) return
        const by: Record<string, number[]> = {}
        for (const p of r.items) {
          if (p.ic === null || p.ic === undefined) continue   // null=那天没算出来, 不进曲线
          ;(by[p.factor_code] = by[p.factor_code] || []).push(p.ic)
        }
        setHist(by)
        setHistDays(r.items.length)
      } catch {
        if (alive) { setHist({}); setHistDays(0) }
      }
    })()
    return () => { alive = false }
  }, [horizon])

  const rows = useMemo(() => {
    const f = data?.factors ?? {}
    return Object.entries(f)
      .map(([code, v]) => ({ code, ...(v as FactorIC) }))
      .sort((a, b) => (Math.abs(b.ic ?? -1) - Math.abs(a.ic ?? -1)))
  }, [data])

  const insufficient = rows.filter((r) => r.ic === null).length

  return (
    <div className="mx-auto max-w-[1200px] p-4">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <h1 className="text-[16px] font-semibold">因子有效性</h1>
        <span className="text-[11px] text-muted-foreground">
          横截面 IC 为主口径 · 样本外 IC 看是否过拟合 · 参考值仅对照
        </span>
        <div className="ml-auto flex items-center gap-2 text-[11px]">
          <label className="text-muted-foreground">回看天数</label>
          <input
            type="number" min={7} max={365} value={days}
            onChange={(e) => setDays(Math.max(7, Math.min(365, Number(e.target.value) || 90)))}
            className="w-16 rounded border border-border/60 bg-background px-1.5 py-0.5 font-mono"
          />
          <label className="text-muted-foreground">持有期</label>
          <input
            type="number" min={1} max={60} value={horizon}
            onChange={(e) => setHorizon(Math.max(1, Math.min(60, Number(e.target.value) || 5)))}
            className="w-14 rounded border border-border/60 bg-background px-1.5 py-0.5 font-mono"
          />
          <button
            type="button" onClick={() => void load()} disabled={loading}
            className="inline-flex items-center gap-1 rounded border border-border/60 px-2 py-0.5 hover:text-foreground disabled:opacity-60"
          >
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />} 刷新
          </button>
        </div>
      </div>

      {err && <div className="mb-3 rounded border border-border/60 p-3 text-[12px] text-muted-foreground">{err}</div>}

      {/* 后端计算失败: 照实显示错误, 不显示成"没数据" */}
      {data?.error && (
        <div className="mb-3 flex items-start gap-2 rounded border border-amber-500/40 p-3 text-[12px]">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
          <div>
            <div className="font-medium">后端计算失败（原样显示，不代表因子无效）</div>
            <div className="mt-0.5 font-mono text-muted-foreground">{data.error}</div>
          </div>
        </div>
      )}

      {loading && !data && (
        <div className="flex items-center gap-2 p-6 text-[12px] text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> 正在计算…
        </div>
      )}

      {data && !data.error && rows.length === 0 && (
        <div className="rounded border border-border/60 p-6 text-[12px] text-muted-foreground">
          这段区间还没有可评估的因子快照（需要"策略信号快照 + 已评估的前向收益"配对）。
          把回看天数调大一些再试。
        </div>
      )}

      {data && rows.length > 0 && (
        <>
          <div className="overflow-x-auto rounded border border-border/60">
            <table className="w-full min-w-[860px]">
              <thead className="border-b border-border/60 bg-accent/20">
                <tr className="text-[11px] text-muted-foreground">
                  <th className="px-2 py-1.5 text-left font-medium">因子</th>
                  <th className="px-2 py-1.5 text-right font-medium" title="横截面 IC 均值(主口径)">IC</th>
                  <th className="px-2 py-1.5 text-right font-medium" title="IC 的 t 统计量">t</th>
                  <th className="px-2 py-1.5 text-right font-medium" title="IC 时序的 mean/std">IR</th>
                  <th className="px-2 py-1.5 text-right font-medium" title="样本外段(后 30% 交易日)的横截面 IC">样本外 IC</th>
                  <th className="px-2 py-1.5 text-right font-medium" title="pooled Spearman, 混入时序变异 —— 只作对照, 不作决策口径">参考值</th>
                  <th className="px-2 py-1.5 text-right font-medium" title="样本量(因子快照条数)">样本</th>
                  <th className="px-2 py-1.5 text-right font-medium" title="参与 IC 计算的截面期数 / 样本外期数">期数</th>
                  <th className="px-2 py-1.5 text-right font-medium" title="每日快照的 IC 走势(近 30 天; 只连已算出的点)">近 30 日</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const ic = icCell(r.ic, r.ic_periods, MIN_PERIODS)
                  const ho = icCell(r.ic_holdout, r.holdout_periods, MIN_HOLDOUT)
                  return (
                    <tr key={r.code} className="border-b border-border/40 last:border-0 text-[12px]">
                      <td className="px-2 py-1.5 font-mono">{r.code}</td>
                      <td className={`px-2 py-1.5 text-right font-mono ${ic.cls}`} title={ic.title}>{ic.text}</td>
                      <td className="px-2 py-1.5 text-right font-mono text-muted-foreground" title="|t| ≥ 2 才谈得上显著">
                        {num(r.ic_t, 2)}
                      </td>
                      <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">{num(r.ir, 2)}</td>
                      <td className={`px-2 py-1.5 text-right font-mono ${ho.cls}`} title={ho.title}>{ho.text}</td>
                      <td className="px-2 py-1.5 text-right font-mono text-muted-foreground/70">{num(r.ic_pooled)}</td>
                      <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">{r.sample_size}</td>
                      <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">
                        {r.ic_periods} / {r.holdout_periods}
                      </td>
                      <td className="px-2 py-1.5 text-right">
                        <Spark
                          series={hist[r.code] || []}
                          title={
                            (hist[r.code] || []).length >= 2
                              ? `近 30 天有 ${(hist[r.code] || []).length} 天算出 IC（共 ${histDays} 条快照）`
                              : histDays === 0
                                ? '还没有 IC 快照（每日快照任务尚未产出数据）'
                                : '快照不足（需 ≥2 天算出 IC 才画线）'
                          }
                        />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          <div className="mt-3 space-y-1 text-[11px] text-muted-foreground">
            <div>
              口径：回看 {data.days} 天 · 持有期 {data.horizon} 个交易日 · 市场 {data.market || '全部'} ·
              样本外占比 {safeFixed(data.holdout_ratio * 100, 0, '--')}%。
            </div>
            <div>
              「参考值」是 pooled Spearman（混入时序变异），**只作对照**；判定以横截面 IC 与样本外 IC 为准。
            </div>
            <div>
              IC 需 ≥{MIN_PERIODS} 个截面期数、样本外 IC 需 ≥{MIN_HOLDOUT} 个期数，不足时显示 `--`（不是 0）。
              {insufficient > 0 ? ` 当前有 ${insufficient} 个因子期数不足。` : ''}
            </div>
            <div>颜色仅表示 |IC| ≥ 0.05 的方向（红=正相关、绿=负相关），不代表买卖建议。</div>
          </div>
        </>
      )}
    </div>
  )
}
