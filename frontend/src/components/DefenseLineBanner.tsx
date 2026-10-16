import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { AlertOctagon, ShieldAlert } from 'lucide-react'
import { safeFixed } from '@/lib/format'

/**
 * 持仓防线击穿与止损预警条 (DefenseLineBanner)
 *
 * 核心职能:
 * 1. 针对持有仓位的标的，自动计算关键最后防线 (如预设硬防线或浮亏达到警戒水位)；
 * 2. 盘中一旦跌破最后防线，触发强提醒警报，杜绝散户\"再等等看\"的侥幸心魔，督促冷血止损。
 *
 * 口径:
 * - 价格均通过 safeFixed 格式化，禁直接 toFixed() (遵守 R6 门禁)；
 * - 缺数据时显式 null，不编造防线。
 */

interface QuoteData {
  price?: number | null
  open?: number | null
  high?: number | null
  low?: number | null
  last_close?: number | null
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
  const [currentPrice, setCurrentPrice] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    if (!hasPosition || !cost || cost <= 0) {
      setCurrentPrice(null)
      return
    }

    fetchAPI<QuoteData>(`/quotes/${encodeURIComponent(symbol)}`)
      .then((res) => {
        if (cancelled) return
        if (res && typeof res.price === 'number' && Number.isFinite(res.price) && res.price > 0) {
          setCurrentPrice(res.price)
        }
      })
      .catch(() => {
        if (!cancelled) setCurrentPrice(null)
      })

    return () => {
      cancelled = true
    }
  }, [symbol, cost, hasPosition])

  if (!hasPosition || !cost || cost <= 0 || !currentPrice) {
    return null
  }

  // 计算硬防线: 默认买入成本的 -8% 预设止损防线 (A股纪律执行标线)
  const defaultDefense = cost * 0.92
  const isBreached = currentPrice <= defaultDefense
  const lossPct = ((currentPrice - cost) / cost) * 100

  // 只有当浮亏较大 (>5%) 或跌破 -8% 防线时展示警报
  if (!isBreached && lossPct > -5) {
    return null
  }

  return (
    <div
      className={`mt-2 flex items-center justify-between gap-3 rounded border px-3 py-2 text-[12px] ${
        isBreached
          ? 'border-red-600/80 bg-red-950/40 text-red-100 animate-pulse'
          : 'border-amber-500/50 bg-amber-950/20 text-amber-200'
      }`}
      data-testid="defense-line-banner"
    >
      <div className="flex items-center gap-2">
        {isBreached ? (
          <AlertOctagon className="h-4 w-4 shrink-0 text-red-400" />
        ) : (
          <ShieldAlert className="h-4 w-4 shrink-0 text-amber-400" />
        )}
        <span className="font-bold">
          {isBreached ? '🚨【触发硬防线警报 · 立即执行纪律】' : '⚠️【接近持仓防线警戒】'}
        </span>
        <span>
          成本价 <span className="font-mono font-medium">{safeFixed(cost, 2)}</span> 元，当前价{' '}
          <span className="font-mono font-medium">{safeFixed(currentPrice, 2)}</span> 元 (浮亏{' '}
          <span className="font-mono font-medium">{safeFixed(lossPct, 2)}%</span>)。
          {isBreached
            ? ` 已跌破预设硬防线 (${safeFixed(defaultDefense, 2)} 元)，请无条件执行纪律止损/减仓，不听任何故事！`
            : ` 距离硬防线 (${safeFixed(defaultDefense, 2)} 元) 仅剩不到 3%，手绑背后严禁逆势补仓！`}
        </span>
      </div>
      <div className="shrink-0 text-[11px] font-semibold text-rose-300">
        纪律高于预测
      </div>
    </div>
  )
}
