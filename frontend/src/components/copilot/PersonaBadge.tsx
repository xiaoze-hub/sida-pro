import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'

/**
 * 标的画像徽标（2026-10-22）。
 *
 * 回答里已经会写「本票为情绪妖股（S级，9连板）」，但页面上看不到 —— 用户在点开参谋之前
 * 就该知道"这是妖股/左侧票"，因为两种票的分析方法完全不同（妖股不看估值与支撑位）。
 *
 * 铁律：数据不足就不显示（不显示"未知"占位）；徽标只是标注，不做卡片、不加边框。
 */

export interface PersonaResp {
  available: boolean
  kind?: string
  label?: string
  score?: number | null
  grade?: string | null
  reasons?: string[]
  recipe?: string
  reason?: string
}

/** 画像 → 语义色点（不靠动画/渐变，弱化到只做区分） */
const DOT: Record<string, string> = {
  demon: 'bg-rose-500',
  left: 'bg-sky-500',
  leader: 'bg-violet-500',
  trend: 'bg-emerald-500',
  normal: 'bg-muted-foreground/40',
}

const CACHE = new Map<string, PersonaResp>()

export async function fetchPersona(symbol: string, market = 'CN'): Promise<PersonaResp | null> {
  const key = `${market}:${symbol}`
  const hit = CACHE.get(key)
  if (hit) return hit
  try {
    const r = await fetchAPI<PersonaResp>(
      `/copilot/persona/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}`,
    )
    if (r && typeof r === 'object') {
      CACHE.set(key, r)
      return r
    }
    return null
  } catch {
    return null
  }
}

export function PersonaBadge({
  symbol,
  market = 'CN',
  data,
  className = '',
}: {
  symbol: string
  market?: string
  /** 上层已取到画像时直接传，避免重复请求 */
  data?: PersonaResp | null
  className?: string
}) {
  const [resp, setResp] = useState<PersonaResp | null>(data ?? null)

  useEffect(() => {
    if (data !== undefined) {
      setResp(data)
      return
    }
    let alive = true
    void fetchPersona(symbol, market).then((r) => {
      if (alive) setResp(r)
    })
    return () => {
      alive = false
    }
  }, [symbol, market, data])

  // 没画像 / 接口失败 / 普通个股 → 不显示（普通票不需要徽标）
  if (!resp || !resp.available || !resp.kind || resp.kind === 'normal') return null

  const label = resp.label || resp.kind
  const grade = resp.grade ? ` ${resp.grade}` : ''
  const title = Array.isArray(resp.reasons) && resp.reasons.length
    ? resp.reasons.join('\n')
    : `${label}${grade}`

  return (
    <span
      className={`inline-flex items-center gap-1.5 text-[11px] text-muted-foreground ${className}`}
      title={title}
      data-persona={resp.kind}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${DOT[resp.kind] || DOT.normal}`} aria-hidden />
      <span>{label}</span>
      {grade && <span className="font-mono text-[10px] text-muted-foreground">{grade.trim()}</span>}
    </span>
  )
}

export default PersonaBadge
