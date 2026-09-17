import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Info, Loader2, RefreshCw, Search } from 'lucide-react'
import { caliberCompareApi, type CaliberCompareResponse, type CaliberSource } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { safeMoney, safePercent, safeInt } from '@/lib/format'

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
          <h1 className="text-[17px] font-semibold text-foreground flex items-center gap-2">
            <Search className="w-4 h-4 text-primary" /> 口径对照
          </h1>
          <p className="text-[11px] text-muted-foreground mt-1">
            同一只票同一时刻, 明盘 L2 / 暗盘逐笔 / 东财四档 并排看 —— 数字不一样是**口径不一样**,
            不合成单一“权威数字”。
          </p>
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
              {data.differences.map((d) => (
                <div key={d.topic} className="border-l-2 border-border/50 pl-3">
                  <div className="text-[12px] text-foreground">{d.topic}</div>
                  <div className="text-[11px] text-muted-foreground leading-relaxed">{d.detail}</div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
