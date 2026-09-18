import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Loader2, RefreshCw } from 'lucide-react'
import { decisionsApi, kindLabel, type DecisionLogResponse, type DecisionStatsResponse } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import {
  contextSummary,
  hitRateText,
  isFilled,
  priceText,
  retClass,
  retText,
  sampleText,
} from '@/lib/decision-ledger'

/**
 * 决策账本(设计稿 v3.0 §八 / 开发计划 P1-3)。
 *
 * 回答一个问题: **"信号出过以后, 到底管不管用?"**
 * 后端(v175 `decision_log`)把每个信号连同**当时的证据与价格**留痕, 事后用真实 K 线回填 T+1/3/5。
 * 本页只做两件事: ①按信号类型给命中率; ②给明细(当时凭什么、拿到的价、回填了没)。
 *
 * 四条不越界的口径(与后端一致, 前端只透传):
 *  ① **样本不足不给数字**: `--` + 原因(样本按自然日累积, T+5 要等一周);
 *  ② **未回填不是 0**: 收益/命中为 null → `--`, 颜色走中性(不许把 null 当 0 上"涨红"色);
 *  ③ **当时没取到价就是 `--`**(不拿今天的价冒充当时的价);
 *  ④ 命中率**只取后端值**, 前端不自己算百分比。
 */
export default function DecisionLedger() {
  const [stats, setStats] = useState<DecisionStatsResponse | null>(null)
  const [log, setLog] = useState<DecisionLogResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const [s, l] = await Promise.all([decisionsApi.stats(180, 30), decisionsApi.log(undefined, 100)])
      setStats(s)
      setLog(l)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const rows = stats?.rows ?? []
  const items = log?.items ?? []
  const allInsufficient = rows.length > 0 && rows.every((r) => r.horizons.t1.insufficient && r.horizons.t3.insufficient && r.horizons.t5.insufficient)

  return (
    <div className="mx-auto max-w-[1200px] p-3 md:p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-[20px] font-bold tracking-tight">决策账本</h1>
        <span className="text-[11px] text-muted-foreground">
          信号 → 结果的账 · 命中 = T+n 收益 &gt; 0(平盘记未命中)
        </span>
        <Button variant="ghost" size="sm" className="ml-auto" onClick={() => void load()} disabled={loading}>
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

      {/* ② 明细 */}
      <section className="mt-6">
        <h2 className="text-[16px] font-semibold">信号明细</h2>
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          最近 {items.length} 条 · 含当时的证据与价格 —— 事后能回放"凭什么出这个信号"。
        </p>
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
                    暂无明细
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
