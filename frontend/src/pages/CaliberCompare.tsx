import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Info, Loader2, RefreshCw, Search } from 'lucide-react'
import {
  caliberCompareApi,
  caliberDriftApi,
  type CaliberCompareResponse,
  type CaliberDriftResponse,
  type CaliberSource,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { safeMoney, safePercent, safeInt, safeFixed } from '@/lib/format'
// 口径词典(单一来源, 见 packages/biz-ui/src/lib/caliber-glossary.ts): 官方投教口径, 各页共用
import { ANPAN, GS_SIGNAL, MINGPAN, glossaryTooltip } from '@panwatch/biz-ui'

/**
 * 口径对照页(2026-09-18, 老板需求 A2 第一步)。
 *
 * 同一只票、同一时刻, 把 **明盘 L2(TQ/同花顺口径) / 暗盘(腾讯逐笔 v6) / 东财四档** 三套"主力资金"
 * 并排摆出来, 每套都带口径说明与基准日 —— 目的是**消歧**, 不是合成一个权威数字。
 *
 * 硬规则(全仓诚实口径): 某源没有数据就照实显示「无数据」+ 原因, **绝不显示 0 冒充**;
 * 涨红跌绿按 A 股惯例(净流入=红, 净流出=绿); 单位为元, 显示时自动折 亿/万。
 */
function valueColor(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return 'text-muted-foreground'
  if (v > 0) return 'text-[var(--stock-up)]'
  if (v < 0) return 'text-[var(--stock-down)]'
  return 'text-foreground'
}

function FieldRow({ label, value, unit }: { label: string; value: number | null; unit?: string }) {
  const isNumber = value !== null && Number.isFinite(value)
  const text = !isNumber
    ? '--'
    : unit === '%'
      ? safePercent(value as number, 1)
      : unit === '笔'
        ? safeInt(value as number)
        : safeMoney(value as number)
  return (
    <div className="flex items-baseline justify-between gap-3 py-1 border-b border-border/30 last:border-0">
      <span className="text-[11px] text-muted-foreground">{label}</span>
      <span className={`text-[13px] tabular-nums ${unit === '%' || unit === '笔' ? '' : valueColor(value)}`}>
        {text}
        {unit && unit !== '%' && unit !== '笔' && <span className="text-[10px] text-muted-foreground ml-1">{unit}</span>}
      </span>
    </div>
  )
}

function SourceColumn({ s }: { s: CaliberSource }) {
  return (
    <div className="min-w-0 border-l border-border/40 pl-3 first:border-0 first:pl-0">
      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-[12px] font-semibold text-foreground truncate" title={s.name}>
          {s.name}
        </span>
        {s.available ? (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-500">可用</span>
        ) : (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-500">无数据</span>
        )}
      </div>
      <div className="text-[10px] text-muted-foreground leading-relaxed mb-2">{s.caliber}</div>

      {s.available ? (
        <div>
          {s.fields.map((f) => (
            <FieldRow key={f.label} label={f.label} value={f.value} unit={f.unit || s.unit} />
          ))}
        </div>
      ) : (
        /* 诚实口径: 无数据就写"无数据 + 原因", 不画 0 / 不画空表 */
        <div className="text-[11px] text-amber-500/90 py-2 leading-relaxed">
          无数据{s.note ? ` —— ${s.note}` : ''}
        </div>
      )}

      {s.available && s.note && (
        <div className="text-[10px] text-amber-500/90 mt-2 leading-relaxed flex gap-1">
          <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
          <span>{s.note}</span>
        </div>
      )}
    </div>
  )
}


/** 源的短名(与对照页一致, 便于同一屏里对照) */
const SOURCE_SHORT: Record<string, string> = {
  thsdk_l2: '明盘 L2',
  tencent_dark: '暗盘逐笔',
  eastmoney_flow: '东财四档',
}

/**
 * 口径漂移(B5, 2026-09-18): 逐日留痕 + 跨源差异。
 *
 * 硬规则:
 * - 每源一条序列, **不取平均、不互相校准、不合成单一"权威数字"**;
 * - 没留痕的日期显示「该日未留痕」, 不插值、不补 0;
 * - 跨源差异必须写明**比的是哪两个字段**, 并标注"口径差异不是误差"(字段含义本来就不同)。
 */
function DriftSection({ symbol }: { symbol: string }) {
  const [days, setDays] = useState(30)
  const [data, setData] = useState<CaliberDriftResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    let alive = true
    setLoading(true)
    setErr('')
    caliberDriftApi
      .get(symbol, days)
      .then((d) => {
        if (alive) setData(d)
      })
      .catch((e: unknown) => {
        if (alive) setErr(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [symbol, days])

  const series = data?.series ?? []
  const sources = Object.keys(data?.field_by_source ?? {})

  return (
    <div className="mt-6 border-t border-border/40 pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
        <h2 className="text-[13px] font-semibold text-foreground flex items-center gap-2">
          <Info className="w-3.5 h-3.5 text-primary" /> 口径漂移（逐日留痕）
        </h2>
        <div className="flex items-center gap-1">
          {[7, 30, 90].map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDays(d)}
              data-drift-days={d}
              className={`text-[11px] px-2 py-0.5 rounded border ${
                days === d
                  ? 'border-primary/50 text-foreground bg-primary/10'
                  : 'border-border/50 text-muted-foreground hover:text-foreground'
              }`}
            >
              近 {d} 天
            </button>
          ))}
          {loading && <Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground" />}
        </div>
      </div>

      <p className="text-[11px] text-muted-foreground leading-relaxed mb-3">
        逐日留痕来自收盘后定时采集（交易日 15:55）。每源各说各的 —— 这里<span className="text-foreground">不取平均、不互相校准</span>；
        跨源差异是<span className="text-foreground">口径差异，不是误差</span>（两家对"主力"的定义本来就不同）。
      </p>

      {err && <div className="text-[12px] text-red-500 mb-2">漂移数据加载失败：{err}</div>}

      {!err && series.length === 0 && !loading && (
        <div data-drift-empty className="rounded-md border border-dashed border-border/60 bg-muted/10 px-3 py-4">
          <div className="text-[12px] text-muted-foreground">暂无留痕（近 {days} 天）</div>
          <div className="text-[11px] text-muted-foreground/80 mt-1">
            交易日 15:55 自动采集三源口径；采集后次日即可在此对比。留痕缺失时这里不会用 0 或推算值填补。
          </div>
        </div>
      )}

      {series.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-[12px] tabular-nums">
            <thead>
              <tr className="text-[10px] text-muted-foreground">
                <th className="text-left font-normal py-1 pr-3">交易日</th>
                {sources.map((k) => (
                  <th key={k} className="text-right font-normal py-1 px-3">
                    {SOURCE_SHORT[k] ?? k}
                    <span className="block text-[10px] text-muted-foreground/70">
                      {data?.field_by_source?.[k]}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {series.map((row) => (
                <tr key={row.trade_date} className="border-t border-border/30">
                  <td className="py-1 pr-3 text-muted-foreground">{row.trade_date}</td>
                  {sources.map((k) => {
                    const pt = row.sources?.[k]
                    const ok = pt?.available && pt.value !== null && Number.isFinite(pt.value)
                    return (
                      <td key={k} className="py-1 px-3 text-right">
                        {ok ? (
                          <span className={valueColor(pt.value as number)}>
                            {safeMoney(pt.value as number)}
                            {pt.quality === 'suspect' && (
                              <span className="ml-1 text-[10px] text-amber-500" title="该源自标数据可疑">
                                ⚠
                              </span>
                            )}
                          </span>
                        ) : (
                          <span className="text-muted-foreground" title={pt?.reason || '该日未留痕'}>
                            {pt && pt.value === null && !pt.available ? '该日未留痕' : '--'}
                          </span>
                        )}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(data?.comparisons?.length ?? 0) > 0 && (
        <div className="mt-3 space-y-1.5">
          <div className="text-[11px] text-muted-foreground">
            跨源差异（只统计两端都有留痕的天；样本不足时如实显示 0 天）
          </div>
          {data!.comparisons.map((c) => (
            <div
              key={`${c.left.source}-${c.right.source}`}
              className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[11px]"
            >
              <span className="text-foreground">
                {SOURCE_SHORT[c.left.source] ?? c.left.source}「{c.left.field}」
                <span className="text-muted-foreground"> vs </span>
                {SOURCE_SHORT[c.right.source] ?? c.right.source}「{c.right.field}」
              </span>
              <span className="text-muted-foreground">· 两端都有 {c.both_available_days} 天</span>
              {c.mean_abs_diff !== null ? (
                <span className="text-muted-foreground">
                  · 平均绝对差 <span className="text-foreground">{safeMoney(c.mean_abs_diff)}</span>
                  {c.max_abs_diff !== null && <> · 最大 {safeMoney(c.max_abs_diff)}</>}
                </span>
              ) : (
                <span className="text-muted-foreground">· 样本不足，暂不给差异统计</span>
              )}
              <span className="text-muted-foreground/70 w-full">{c.note}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function CaliberComparePage() {
  const [code, setCode] = useState('002361')
  const [data, setData] = useState<CaliberCompareResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(async (symbol: string) => {
    const s = (symbol || '').trim()
    if (!/^\d{6}$/.test(s)) {
      setErr('请输入 6 位 A 股代码')
      return
    }
    setLoading(true)
    setErr('')
    try {
      const d = await caliberCompareApi.get(s)
      setData(d)
    } catch (e: any) {
      setData(null)
      setErr(e?.message || '取数失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load('002361')
  }, [load])

  return (
    <div className="sida-page-enter max-w-6xl mx-auto px-4 py-6">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h1 className="text-[16px] font-semibold text-foreground flex items-center gap-2">
            <Search className="w-4 h-4 text-primary" /> 口径对照
          </h1>
          <p className="text-[11px] text-muted-foreground mt-1">
            同一只票同一时刻, 明盘 L2 / 暗盘逐笔 / 东财四档 并排看 —— 数字不一样是「口径不一样」,
            不合成单一“权威数字”。
          </p>
        </div>
      {/* 口径词典(2026-09-18 官方投教口径): 这三个词在页面上反复出现, 就地解释一次,
          避免读者把明盘/暗盘当成同一件事的两个说法。文案来自 caliber-glossary(单一来源)。 */}
      <div className="mt-3 rounded border border-border/60 px-3 py-2" data-testid="caliber-glossary">
        <div className="text-[11px] font-medium text-muted-foreground">口径词典</div>
        <div className="mt-1 space-y-0.5 text-[11px]">
          <div title={glossaryTooltip(MINGPAN)}>
            <span className="text-foreground">{MINGPAN.name}</span>
            <span className="text-muted-foreground"> —— {MINGPAN.oneLine}（单笔 &gt;30 万，散户也计入，不代表真正主力）</span>
          </div>
          <div title={glossaryTooltip(ANPAN)}>
            <span className="text-foreground">{ANPAN.name}</span>
            <span className="text-muted-foreground"> —— {ANPAN.oneLine}（大单拆成 30 万以下 → 明盘看不到，同一账户买入记为暗盘）</span>
          </div>
          <div title={glossaryTooltip(GS_SIGNAL)}>
            <span className="text-foreground">{GS_SIGNAL.name}</span>
            <span className="text-muted-foreground"> —— {GS_SIGNAL.oneLine} G=机会+防卖飞 · S=震荡或下跌+避深套</span>
          </div>
        </div>
      </div>

        <div className="flex items-center gap-2">
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void load(code)}
            aria-label="股票代码"
            placeholder="6 位代码"
            className="h-8 w-28 rounded border border-border/60 bg-transparent px-2 text-[12px] text-foreground"
          />
          <Button size="sm" className="h-8 text-[11px]" disabled={loading} onClick={() => void load(code)}>
            {loading ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <RefreshCw className="w-3 h-3 mr-1" />}
            查询
          </Button>
        </div>
      </div>

      {err && <div className="text-[12px] text-red-500 mb-3">{err}</div>}

      {data && (
        <>
          <div className="flex items-center gap-3 text-[11px] text-muted-foreground mb-3">
            <span>{data.symbol}（{data.market}）</span>
            <span>取数时间 {data.as_of.replace('T', ' ').slice(0, 19)}</span>
            <span className={data.available_count > 0 ? 'text-emerald-500' : 'text-amber-500'}>
              {data.available_count}/3 个源有数据
            </span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
            {data.sources.map((s) => (
              <SourceColumn key={s.key} s={s} />
            ))}
          </div>

          <div>
            <h2 className="text-[13px] font-semibold text-foreground mb-2 flex items-center gap-2">
              <Info className="w-3.5 h-3.5 text-primary" /> 为什么三个数字不一样
            </h2>
            <div className="space-y-2">
              {/* P2-1(2026-09-18): 成对差异 + 归因 —— 三源并排能"看到差", 这块回答"这个差正不正常" */}
              {(data.pair_diffs ?? []).length > 0 && (
                <div className="mt-4">
                  <div className="mb-1.5 flex flex-wrap items-center gap-2">
                    <span className="text-[13px] font-medium text-foreground">差异归因</span>
                    {data.diff_conclusion && (
                      <span
                        data-testid="diff-conclusion"
                        className={`rounded px-1.5 py-0.5 text-[11px] ${
                          data.diff_conclusion.level === 'alert'
                            ? 'bg-destructive/10 text-destructive'
                            : data.diff_conclusion.level === 'warn'
                              ? 'bg-amber-500/10 text-amber-600'
                              : 'bg-muted text-muted-foreground'
                        }`}
                      >
                        {data.diff_conclusion.level === 'ok'
                          ? '差在预期带内'
                          : data.diff_conclusion.level === 'warn'
                            ? '略出预期带'
                            : data.diff_conclusion.level === 'alert'
                              ? '远离预期带 / 方向冲突'
                              : '数据不足'}
                      </span>
                    )}
                  </div>
                  {data.diff_conclusion && (
                    <p className="mb-2 text-[11px] text-muted-foreground">{data.diff_conclusion.hint}</p>
                  )}
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-[11px]">
                      <thead>
                        <tr className="border-b border-border/60 text-muted-foreground">
                          <th className="px-2 py-1 font-medium">对比</th>
                          <th className="px-2 py-1 text-right font-medium">差值</th>
                          <th className="px-2 py-1 text-right font-medium">相对差</th>
                          <th className="px-2 py-1 text-right font-medium">比值</th>
                          <th className="px-2 py-1 font-medium">判定与归因</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border/40">
                        {(data.pair_diffs ?? []).map((d) => (
                          <tr key={`${d.a}-${d.b}`}>
                            <td className="px-2 py-1 whitespace-nowrap">
                              {d.label_a} <span className="text-muted-foreground">vs</span> {d.label_b}
                            </td>
                            <td className="px-2 py-1 text-right font-mono">{safeMoney(d.abs_diff)}</td>
                            <td className="px-2 py-1 text-right font-mono">
                              {d.rel_diff == null ? '--' : `${safeFixed(d.rel_diff * 100, 1)}%`}
                            </td>
                            <td className="px-2 py-1 text-right font-mono">
                              {d.ratio == null ? '--' : `${safeFixed(d.ratio, 2)}×`}
                            </td>
                            <td className="px-2 py-1">
                              <span
                                className={`mr-1 ${
                                  d.level === 'alert'
                                    ? 'text-destructive'
                                    : d.level === 'warn'
                                      ? 'text-amber-600'
                                      : 'text-muted-foreground'
                                }`}
                              >
                                {d.level === 'ok' ? '预期' : d.level === 'warn' ? '留意' : d.level === 'alert' ? '需核对' : '缺数据'}
                              </span>
                              <span className="text-muted-foreground">{d.note}</span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {data.differences.map((d) => (
                <div key={d.topic} className="border-l-2 border-border/50 pl-3">
                  <div className="text-[12px] text-foreground">{d.topic}</div>
                  <div className="text-[11px] text-muted-foreground leading-relaxed">{d.detail}</div>
                </div>
              ))}
            </div>
          </div>

          <DriftSection symbol={data.symbol} />
        </>
      )}
    </div>
  )
}
