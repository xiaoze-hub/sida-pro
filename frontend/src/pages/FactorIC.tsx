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
import { factorICApi } from '@sida/api'
import type { FactorIC, FactorICHistoryResp, FactorICResp } from '@sida/api'
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

/**
 * 因子中文名 + 一句人话口径(2026-11-11 可读性)。
 *
 * 命名**沿用仓库既有叫法**, 不自造: `src/core/signal_explain.py` 的 FACTOR_LABELS 与
 * `components/FactorWeightsPanel.tsx` 的映射一致(选股α/催化/计划质量/风险/拥挤度),
 * 这里只补 IC 表里出现、旧映射里没有的 `final_score` 与 `source_bonus`。
 *
 * 口径描述**只复述后端已有语义**(`src/core/strategy_engine.py` 的分项构成), 不写公式细节;
 * 拿不准的一律中性表述("口径以后端为准"), 不编造。
 */
interface FactorMeta { label: string; hint: string }

const FACTOR_META: Record<string, FactorMeta> = {
  alpha_score: {
    label: '选股α',
    hint: '个股基础打分叠加相对强度修正后的选股分，越高代表越被看好。',
  },
  catalyst_score: {
    label: '催化',
    hint: '事件驱动与形态信号（突破/回踩/超跌、新闻事件强度、涨跌幅区间）累起来的强度。',
  },
  quality_score: {
    label: '计划质量',
    hint: '这条建议本身的可执行性（计划质量分等），分越高计划越完整。',
  },
  risk_penalty: {
    label: '风险',
    hint: '扣分项：风险等级高、波动过大、计划质量不足等；分越高越该回避，所以 IC 预期为负。',
  },
  crowd_penalty: {
    label: '拥挤度',
    hint: '扣分项：资金抱团与过热程度（涨幅过热、量比、成交额）；分越高越拥挤，IC 预期同样为负。',
  },
  final_score: {
    label: '综合得分',
    hint: '各项因子加权后的最终分（口径以后端为准）。',
  },
  source_bonus: {
    label: '来源加成',
    hint: '信号来源（哪个智能体/哪套策略）带来的加成；当前 IC 表未单列。',
  },
}

/** 因子显示名: 未收录的代号**原样返回**, 不猜、不报错。 */
const factorLabel = (code: string): string => FACTOR_META[code]?.label ?? code

/** 因子列: 中文名为主, 英文代号以小号 mono 弱化跟在后面; 未知代号原样显示。 */
function FactorName({ code }: { code: string }) {
  const meta = FACTOR_META[code]
  if (!meta) {
    return (
      <span className="font-mono text-muted-foreground" title={`未收录中文名，原样显示代号 ${code}`}>
        {code}
      </span>
    )
  }
  return (
    // min-w 落在内层 div 上（td 的 min-width 在表格自动布局里会被忽略）: 给中文名列一个地板宽,
    // 列被挤窄时中文名 truncate + 悬停 title, 不撑破表格。
    <div className="flex min-w-[176px] items-baseline gap-1.5" title={`${meta.label}（${code}）：${meta.hint}`}>
      <span className="min-w-0 truncate text-foreground">{meta.label}</span>
      <span className="shrink-0 font-mono text-muted-foreground">{code}</span>
    </div>
  )
}

/** 「这些指标怎么读」—— 白话说清 8 个列名, 不改任何计算与阈值。 */
const METRIC_GUIDE: { term: string; text: string }[] = [
  { term: 'IC', text: '横截面秩相关（Spearman）按快照日各算一次再取均值。越接近 0 说明「分高的、后来收益并不更好」，预测力越弱；正负号只代表方向。' },
  { term: 't', text: 'IC 的 t 统计量，看这个 IC 像不像随机噪声；|t| 越大越不像偶然（一般 |t| ≥ 2 才谈得上显著）。' },
  { term: 'IR', text: 'IC 均值 ÷ IC 标准差，衡量稳定性：同样的 IC，波动越小 IR 越高。' },
  { term: '样本外 IC', text: '只用后 30% 交易日单独再算一遍；明显低于样本内 IC 时，要怀疑过拟合。' },
  { term: '参考值', text: 'pooled Spearman —— 所有样本混在一起算，混入时序变异；只作对照，判定看横截面 IC 与样本外 IC。' },
  { term: '样本', text: '参与评估的因子快照条数（信号 × 快照日）。' },
  { term: '期数', text: '参与 IC 计算的截面交易日数 / 样本外期数；不足时该格显示 --（不是 0）。' },
  { term: '近 30 日', text: '每日快照累积的 IC 走势，只连已算出的点。' },
]

/** 各因子在看什么 —— 与 FACTOR_META 同源, 保证表里和说明里叫法一致。 */
const FACTOR_GLOSSARY_ORDER = [
  'alpha_score',
  'catalyst_score',
  'quality_score',
  'risk_penalty',
  'crowd_penalty',
  'final_score',
]

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

/**
 * IC 发散条（2026-10-24 L3-C）：IC 原来只是一个数字 + 颜色, 读者看不出「-0.04 是弱负相关还是有效」。
 * 改成以 0 为中轴的发散条：右侧红=正相关(A股红=好), 左侧绿=负相关, 长度按 |IC|/0.1 归一。
 * 只新增可视化, 不改任何 IC 计算与阈值。
 */
function IcBar({ value }: { value: number | null | undefined }) {
  if (value == null || !Number.isFinite(value)) return <span className="text-muted-foreground">--</span>
  const clamped = Math.max(-0.1, Math.min(0.1, value))
  const pct = (Math.abs(clamped) / 0.1) * 50
  return (
    <span
      className="ml-2 inline-flex items-center align-middle"
      role="img"
      aria-label={`IC 发散条: ${value}（中轴为 0，右正左负）`}
      title={`IC=${value}；中轴为 0，右侧红=正相关，左侧绿=负相关`}
    >
      <span className="relative inline-block h-1.5 w-16 rounded bg-s2">
        {value >= 0 ? (
          <span className="absolute left-1/2 top-0 h-1.5 rounded-r bg-stock-up" style={{ width: `${pct}%` }} />
        ) : (
          <span className="absolute right-1/2 top-0 h-1.5 rounded-l bg-stock-down" style={{ width: `${pct}%` }} />
        )}
        <span className="absolute left-1/2 top-[-2px] h-2.5 w-px bg-border" />
      </span>
    </span>
  )
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
    <div className="page-container space-y-4">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <h1 className="text-[16px] font-semibold">因子有效性</h1>
        <span className="text-[12px] text-muted-foreground">
          横截面 IC 为主口径 · 样本外 IC 看是否过拟合 · 参考值仅对照
        </span>
        <div className="ml-auto flex items-center gap-2 text-[12px]">
          <label className="text-muted-foreground">回看天数</label>
          <input
            type="number" min={7} max={365} value={days}
            onChange={(e) => setDays(Math.max(7, Math.min(365, Number(e.target.value) || 90)))}
            className="w-16 rounded border border-border bg-background px-1.5 py-0.5 font-mono"
          />
          <label className="text-muted-foreground">持有期</label>
          <input
            type="number" min={1} max={60} value={horizon}
            onChange={(e) => setHorizon(Math.max(1, Math.min(60, Number(e.target.value) || 5)))}
            className="w-14 rounded border border-border bg-background px-1.5 py-0.5 font-mono"
          />
          <button
            type="button" onClick={() => void load()} disabled={loading}
            className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 hover:text-foreground disabled:opacity-60"
          >
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />} 刷新
          </button>
        </div>
      </div>

      {err && <div className="card-surface bg-card mb-3 rounded p-3 text-[12px] text-muted-foreground">{err}</div>}

      {/* 后端计算失败: 照实显示错误, 不显示成"没数据" */}
      {data?.error && (
        <div className="card-surface bg-card mb-3 flex items-start gap-2 rounded p-3 text-[12px]">
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
        <div className="card-surface bg-card rounded p-6 text-[12px] text-muted-foreground">
          这段区间还没有可评估的因子快照（需要"策略信号快照 + 已评估的前向收益"配对）。
          把回看天数调大一些再试。
        </div>
      )}

      {data && rows.length > 0 && (
        <>
          {/* L3-C(2026-10-24): 一句话人话结论放在表前, 不让读者自己从 4 列指标里猜。
              只用已返回的 IC 与期数, 不算新指标、不编基准。 */}
          <p className="mb-2 text-[12px] text-muted-foreground">
            {(() => {
              const usable = rows.filter((r) => r.ic_periods >= MIN_PERIODS && typeof r.ic === 'number')
              if (usable.length === 0) return '暂无可评估因子（样本期数不足）；IC=预测力，越接近 0 越弱。'
              // IC 为 null = 期数不足, **不能当 0 参与排名**(会把"没数据"说成"最弱"),
              // 也不能显示成 IC 0。全窗口都空时如实说不排名。
              const scored = usable.filter((r) => r.ic != null)
              const best = scored.length
                ? [...scored].sort((a, b) => Math.abs(b.ic as number) - Math.abs(a.ic as number))[0]
                : null
              return best
                ? `可评估 ${usable.length} 个因子，最强 ${factorLabel(best.code)}（${best.code}，IC ${num(best.ic, 3)}）。IC 越接近 0 预测力越弱；样本外列用于看是否过拟合，非买卖指令。`
                : `可评估 ${usable.length} 个因子，但本窗口 IC 全部为空（期数不足），暂不排名。样本外列用于看是否过拟合，非买卖指令。`
            })()}
          </p>

          <div className="card-surface bg-card overflow-x-auto rounded ">
            <table className="w-full min-w-[860px]">
              <thead className="border-b border-border bg-s2">
                <tr className="text-[12px] text-muted-foreground">
                  <th className="min-w-[140px] px-2 py-1.5 text-left font-medium">因子</th>
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
                    <tr key={r.code} className="border-b border-border last:border-0 text-[12px]">
                      <td className="min-w-[140px] px-2 py-1.5">
                        <FactorName code={r.code} />
                      </td>
                      <td className={`px-2 py-1.5 text-right font-mono ${ic.cls}`} title={ic.title}>
                        {ic.text}
                        <IcBar value={r.ic_periods >= MIN_PERIODS && typeof r.ic === 'number' ? r.ic : null} />
                      </td>
                      <td className="px-2 py-1.5 text-right font-mono text-muted-foreground" title="|t| ≥ 2 才谈得上显著">
                        {num(r.ic_t, 2)}
                      </td>
                      <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">{num(r.ir, 2)}</td>
                      <td className={`px-2 py-1.5 text-right font-mono ${ho.cls}`} title={ho.title}>{ho.text}</td>
                      <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">{num(r.ic_pooled)}</td>
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

          {/* 可读性(2026-11-11): 表下补两块白话说明 —— 指标怎么读 + 每个因子在看什么。
              纯展示层: 不改任何 IC/IR 计算与阈值, 也不新增"买卖结论"。 */}
          <div className="mt-4 grid gap-4 border-t border-border pt-3 text-[12px] md:grid-cols-2">
            <section data-testid="factor-ic-metric-legend">
              <h2 className="mb-1 text-[12px] font-medium text-foreground">这些指标怎么读</h2>
              <dl className="space-y-1 text-muted-foreground">
                {METRIC_GUIDE.map((m) => (
                  <div key={m.term} className="flex gap-2">
                    <dt className="w-20 shrink-0 text-foreground">{m.term}</dt>
                    <dd className="min-w-0 flex-1">{m.text}</dd>
                  </div>
                ))}
              </dl>
            </section>

            <section data-testid="factor-ic-factor-glossary">
              <h2 className="mb-1 text-[12px] font-medium text-foreground">每个因子在看什么</h2>
              <dl className="space-y-1 text-muted-foreground">
                {FACTOR_GLOSSARY_ORDER.map((code) => {
                  const meta = FACTOR_META[code]
                  if (!meta) return null
                  return (
                    <div key={code} className="flex gap-2">
                      <dt className="w-28 shrink-0 text-foreground">
                        <span className="block truncate">{meta.label}</span>
                        <span className="block truncate font-mono text-muted-foreground">{code}</span>
                      </dt>
                      <dd className="min-w-0 flex-1">{meta.hint}</dd>
                    </div>
                  )
                })}
              </dl>
              <p className="mt-1 text-muted-foreground">
                正 IC = 分越高、后续收益越好；负 IC = 分越高、后续越差（惩罚项常见）。
              </p>
            </section>
          </div>

          <div className="mt-3 space-y-1 text-[12px] text-muted-foreground">
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
            {/* 诚实说明: IC 时序**不回溯** —— 历史那天的 IC 需要"那天当时看不见的"前向收益, 事后算出来
                再标成过去日期就是时间穿越。所以曲线从每日快照启用日往后累积。 */}
            <div>
              「近 30 日」是从每日快照启用（2026-09-19）起累积的：过去的日子**没有快照**，
              也不做回溯推算（那需要当时看不到的未来收益）。刚开始几天曲线会短，属正常。
            </div>
          </div>
        </>
      )}
    </div>
  )
}
