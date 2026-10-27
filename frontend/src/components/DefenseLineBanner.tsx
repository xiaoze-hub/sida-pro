import { useCallback, useEffect, useState } from 'react'
import { fetchAPI } from '@sida/api'
import { AlertOctagon, Loader2, ShieldAlert, ShieldPlus } from 'lucide-react'
import { safeFixed } from '@/lib/format'
import { Button } from '@sida/base-ui/components/ui/button'
import { Input } from '@sida/base-ui/components/ui/input'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@sida/base-ui/components/ui/dialog'

/**
 * 持仓防线击穿与止损预警条 (DefenseLineBanner)
 *
 * 核心职能:
 * 1. 展示**用户自己设的**防守线(positions.defense_price, 绝对价), 盘中跌破即强提醒;
 * 2. 用户没设 → 只给一条静态提示和"设置"入口, **不告警**。
 *
 * 方案 B 裁决(2026-10-23): 系统不再拿"成本价 × (1-8%)"冒充用户的纪律位。
 * 原因: 那条 -8% 是系统臆造的, 一旦对所有人告警就等于把"系统的假设"说成
 * "用户的纪律", 用户既没认过它、也没法改它。现在默认值只出现在"参考"文案里
 * (成本 × -8%), 且必须由用户点一下才落库。
 *
 * 取价口径:
 * - 所有价格经 safeFixed 格式化(禁直接 toFixed, 遵守 R6 门禁);
 * - 缺现价时**显式降级**成一行说明, 绝不 return null 静默消失 ——
 *   "看不见的告警条"会被用户读成"我的防线没被击穿"。
 */

interface QuoteData {
  price?: number | null
  open?: number | null
  high?: number | null
  low?: number | null
  last_close?: number | null
}

/** `/portfolio/summary` 里持仓行的最小子集(本组件只消费这几个字段)。 */
interface PositionRow {
  id: number
  symbol: string
  cost_price: number
  defense_price?: number | null
}

interface PortfolioSummaryLike {
  accounts?: { positions?: PositionRow[] }[]
}

interface PositionState {
  status: 'loading' | 'ok' | 'error'
  id: number | null
  /** null = 用户确实没设; 与 status='error' 时的 null 语义不同, 不可混用。 */
  defensePrice: number | null
}

interface QuoteState {
  status: 'loading' | 'ok' | 'error'
  price: number | null
}

/** 距防线价多近才提示(相对**防线价**, 不是成本价)。 */
const NEAR_LINE_PCT = 3
/** 未设置时的参考预设: 仅把成本价折算成绝对价给用户点选, 不落库、不告警。 */
const PRESETS = [
  { pct: -5, label: '-5%' },
  { pct: -8, label: '-8%' },
  { pct: -10, label: '-10%' },
] as const

function presetPrice(cost: number, pct: number): number {
  return Math.round(cost * (1 + pct / 100) * 100) / 100
}

export function DefenseLineBanner({
  symbol,
  cost,
  hasPosition,
}: {
  symbol: string
  cost: number | null
  hasPosition?: boolean
}) {
  const [quote, setQuote] = useState<QuoteState>({ status: 'loading', price: null })
  const [position, setPosition] = useState<PositionState>({
    status: 'loading',
    id: null,
    defensePrice: null,
  })
  const [dialogOpen, setDialogOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const active = !!hasPosition && !!cost && cost > 0

  // 防守线来自持仓本身(而非父组件 props): 只有持仓行知道"用户到底设没设过",
  // 用 `undefined` 表示"还没读到"会被误渲染成"未设置", 故用 status 三态区分。
  useEffect(() => {
    let cancelled = false
    if (!active) {
      setPosition({ status: 'ok', id: null, defensePrice: null })
      return
    }
    setPosition({ status: 'loading', id: null, defensePrice: null })
    fetchAPI<PortfolioSummaryLike>('/portfolio/summary?include_quotes=false')
      .then((res) => {
        if (cancelled) return
        const rows = (res?.accounts || []).flatMap((acc) => acc.positions || [])
        const hit = rows.find((p) => p.symbol === symbol) || null
        const raw = hit?.defense_price
        const defensePrice =
          typeof raw === 'number' && Number.isFinite(raw) && raw > 0 ? raw : null
        setPosition({
          status: hit ? 'ok' : 'error',
          id: hit?.id ?? null,
          defensePrice: hit ? defensePrice : null,
        })
      })
      .catch(() => {
        if (!cancelled) setPosition({ status: 'error', id: null, defensePrice: null })
      })
    return () => {
      cancelled = true
    }
  }, [symbol, active])

  useEffect(() => {
    let cancelled = false
    if (!active) {
      setQuote({ status: 'loading', price: null })
      return
    }
    setQuote({ status: 'loading', price: null })
    fetchAPI<QuoteData>(`/quotes/${encodeURIComponent(symbol)}`)
      .then((res) => {
        if (cancelled) return
        const p = res?.price
        if (typeof p === 'number' && Number.isFinite(p) && p > 0) {
          setQuote({ status: 'ok', price: p })
        } else {
          // 响应 200 但没有可用价: 与请求失败同义(都算"缺现价")。
          setQuote({ status: 'error', price: null })
        }
      })
      .catch(() => {
        if (!cancelled) setQuote({ status: 'error', price: null })
      })
    return () => {
      cancelled = true
    }
  }, [symbol, active])

  const saveDefense = useCallback(
    async (value: number | null) => {
      if (position.id == null) {
        setSaveError('持仓信息未就绪，无法保存')
        return
      }
      setSaving(true)
      setSaveError(null)
      try {
        const res = await fetchAPI<{ defense_price?: number | null }>(
          `/positions/${position.id}/defense`,
          { method: 'PUT', body: JSON.stringify({ defense_price: value }) },
        )
        const saved = res?.defense_price
        setPosition((prev) => ({
          ...prev,
          status: 'ok',
          defensePrice:
            typeof saved === 'number' && Number.isFinite(saved) && saved > 0 ? saved : null,
        }))
        setDialogOpen(false)
      } catch {
        setSaveError('保存失败，请重试')
      } finally {
        setSaving(false)
      }
    },
    [position.id],
  )

  if (!active) return null

  const costVal = cost as number
  const defense = position.defensePrice
  const price = quote.price
  const lossPct = price != null ? ((price - costVal) / costVal) * 100 : null
  // 距离按防线价算: 用户改的是这条线, 告警的"还剩多少"就该跟着它走。
  const distancePct = price != null && defense != null ? ((price - defense) / defense) * 100 : null
  const isBreached = defense != null && price != null && price <= defense
  const isNear =
    defense != null && price != null && !isBreached &&
    distancePct != null && distancePct <= NEAR_LINE_PCT

  const openDialog = (seed: number | null) => {
    setDraft(seed != null ? safeFixed(seed, 2, '') : '')
    setSaveError(null)
    setDialogOpen(true)
  }

  const submitDraft = () => {
    const v = Number(draft)
    if (!draft.trim() || !Number.isFinite(v) || v <= 0) {
      setSaveError('请输入大于 0 的绝对价格')
      return
    }
    void saveDefense(Math.round(v * 100) / 100)
  }

  const shell = 'mt-2 flex items-center justify-between gap-3 rounded border px-3 py-2 text-[12px]'

  const dialog = (
    <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>设置防守线</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 text-[12px]">
          <div className="text-muted-foreground">
            成本价 <span className="tabular-nums">{safeFixed(costVal, 2)}</span> 元。
            防守线是**你**定的绝对价，跌到即提示执行纪律；不设则不告警。
          </div>
          <div className="flex items-center gap-2">
            <Input
              type="number"
              step="0.01"
              inputMode="decimal"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={`如 ${safeFixed(presetPrice(costVal, -8), 2)}`}
              className="tabular-nums"
              data-testid="defense-line-input"
            />
            <span className="shrink-0 text-muted-foreground">元</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {PRESETS.map((p) => (
              <Button
                key={p.pct}
                type="button"
                size="sm"
                variant="outline"
                className="h-7 px-2 text-[12px] tabular-nums"
                onClick={() => setDraft(safeFixed(presetPrice(costVal, p.pct), 2, ''))}
              >
                {p.label} · {safeFixed(presetPrice(costVal, p.pct), 2)}
              </Button>
            ))}
          </div>
          {saveError && <div className="text-rose-400">{saveError}</div>}
          <div className="flex items-center justify-between gap-2 pt-1">
            <div>
              {defense != null && (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="h-7 px-2 text-[12px] text-muted-foreground"
                  disabled={saving}
                  onClick={() => void saveDefense(null)}
                >
                  清除防守线
                </Button>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-7 px-2 text-[12px]"
                onClick={() => setDialogOpen(false)}
              >
                取消
              </Button>
              <Button
                type="button"
                size="sm"
                className="h-7 px-2 text-[12px]"
                disabled={saving}
                onClick={submitDraft}
                data-testid="defense-line-save"
              >
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : '保存'}
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )

  // ① 用户没设防线: 静态提示行(不是警告条) + 设置入口。
  if (position.status === 'ok' && defense == null) {
    return (
      <>
        <div
          className={`${shell} border-border bg-s1 text-muted-foreground`}
          data-testid="defense-line-unset"
        >
          <div className="flex flex-wrap items-center gap-2">
            <ShieldPlus className="h-4 w-4 shrink-0 text-role-system" />
            <span className="font-medium text-foreground">未设置防守线</span>
            <span>
              成本 <span className="tabular-nums">{safeFixed(costVal, 2)}</span>，
              参考 -8% = <span className="tabular-nums">{safeFixed(presetPrice(costVal, -8), 2)}</span>
              <span className="ml-1 text-[12px] opacity-70">(系统参考值，非你的纪律位)</span>
            </span>
          </div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-7 shrink-0 px-2 text-[12px]"
            onClick={() => openDialog(presetPrice(costVal, -8))}
            data-testid="defense-line-set"
          >
            设置
          </Button>
        </div>
        {dialog}
      </>
    )
  }

  // ② 持仓读取失败: 说"未知", 不冒充"未设置"。
  if (position.status === 'error') {
    return (
      <div
        className={`${shell} border-border bg-s1 text-muted-foreground`}
        data-testid="defense-line-unknown"
      >
        <span>防守线状态未知（持仓读取失败）</span>
      </div>
    )
  }

  // ③ 设了防线但取不到现价: 显式降级, 不静默消失。
  if (quote.status === 'error') {
    return (
      <div
        className={`${shell} border-border bg-s1 text-role-watch`}
        data-testid="defense-line-degraded"
      >
        <div className="flex items-center gap-2">
          <ShieldAlert className="h-4 w-4 shrink-0" />
          <span>防守线无法计算（缺现价）</span>
          <span className="text-muted-foreground">
            防线 <span className="tabular-nums">{safeFixed(defense, 2)}</span>，
            成本 <span className="tabular-nums">{safeFixed(costVal, 2)}</span>
          </span>
        </div>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-7 shrink-0 px-2 text-[12px] text-muted-foreground"
          onClick={() => openDialog(defense)}
        >
          修改
        </Button>
      </div>
    )
  }

  // ④ 取价在途: 不提前下结论(既不说击穿也不说安全)。
  if (quote.status !== 'ok' || price == null) return null

  const tone = isBreached
    ? 'border-rose-500/60 bg-rose-950/25 text-rose-100'
    : isNear
      ? 'border-amber-500/50 bg-amber-950/20 text-amber-200'
      : 'border-border bg-s1 text-muted-foreground'

  return (
    <>
      <div className={`${shell} ${tone}`} data-testid="defense-line-banner">
        <div className="flex flex-wrap items-center gap-2">
          {isBreached ? (
            <AlertOctagon className="h-4 w-4 shrink-0 text-rose-400" />
          ) : (
            <ShieldAlert className={`h-4 w-4 shrink-0 ${isNear ? 'text-amber-400' : 'text-role-system'}`} />
          )}
          <span className="font-semibold">
            {isBreached ? '已击穿你的防守线' : isNear ? `接近你的防守线（${safeFixed(distancePct, 2)}%）` : '防守线'}
          </span>
          <span className="tabular-nums">
            防线 {safeFixed(defense, 2)} · 现价 {safeFixed(price, 2)}
            {lossPct != null ? ` · 浮亏 ${safeFixed(lossPct, 2)}%` : ''}
            {!isBreached && distancePct != null ? ` · 距防线 ${safeFixed(distancePct, 2)}%` : ''}
          </span>
          <span className="text-[12px] opacity-70">
            成本 <span className="tabular-nums">{safeFixed(costVal, 2)}</span>
          </span>
        </div>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-7 shrink-0 px-2 text-[12px] opacity-80"
          onClick={() => openDialog(defense)}
        >
          修改
        </Button>
      </div>
      {dialog}
    </>
  )
}
