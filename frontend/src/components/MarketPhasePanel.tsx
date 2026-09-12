/** 市场情绪周期面板(2026-09-12, 借鉴 tick-stock-panel 阶段体系)。
 *
 * 展示三块: 当前阶段+历史分位徽标 / 阶段规律(段数·时长·去向概率) / 阶段时间轴色带。
 * 数据来自 /api/market/phase/{segments,stats}(由 limit_up_events 回填+每日 sync 落库)。
 * 空态为"可操作空态": 无历史时给回填入口(owner 限定), 而不是一句"暂无数据"。
 */
import { useCallback, useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { getJwtRole } from '@/lib/jwt'
import { Gauge } from 'lucide-react'
import {
  PHASE_LABEL,
  currentPhaseRule,
  phaseBandClass,
  phaseTextClass,
  type PhaseKey,
  type PhaseSegment,
  type PhaseStats,
} from '@/lib/market-phase'

interface SegmentsResp {
  available: boolean
  segments: PhaseSegment[]
  total_days: number
}
interface StatsResp {
  available: boolean
  stats: Record<string, PhaseStats>
  percentiles: Record<string, number | null> | null
  total_days: number
}

const PCT_ITEMS: { key: string; label: string }[] = [
  { key: 'first_board', label: '首板' },
  { key: 'ge2_count', label: '2板+' },
  { key: 'max_height', label: '高度' },
  { key: 'promo_rate', label: '晋级率' },
  { key: 'completeness', label: '梯队完整度' },
]

export function MarketPhasePanel() {
  const [segs, setSegs] = useState<SegmentsResp | null>(null)
  const [stats, setStats] = useState<StatsResp | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const isOwner = getJwtRole() === 'owner'

  const load = useCallback(async () => {
    try {
      const [s, t] = await Promise.all([
        fetchAPI<SegmentsResp>('/market/phase/segments?days=400'),
        fetchAPI<StatsResp>('/market/phase/stats?days=400'),
      ])
      setSegs(s)
      setStats(t)
    } catch {
      /* 保留旧值; 空态会给出引导 */
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const segments = segs?.segments ?? []
  const current = segments.length ? segments[segments.length - 1] : null
  const rule = currentPhaseRule(current?.phase ?? null, stats?.stats ?? null)
  const pcts = stats?.percentiles ?? null

  const doBackfill = async () => {
    setBusy(true)
    setMsg('')
    try {
      const r = await fetchAPI<{ ok: boolean; note?: string; reason?: string }>(
        '/market/phase/backfill',
        { method: 'POST' },
      )
      setMsg(r.ok ? (r.note ?? '回填完成') : (r.reason ?? '回填失败'))
      if (r.ok) await load()
    } catch (e) {
      setMsg(`回填失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded border border-border/60 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Gauge className="h-4 w-4 text-muted-foreground" strokeWidth={1.8} />
        <span className="text-[13px] font-semibold">市场情绪周期</span>
        {current ? (
          <span
            className={`rounded px-1.5 py-0.5 text-[12px] font-medium ${phaseTextClass(current.phase)}`}
          >
            {current.label} · 第 {current.days} 天
          </span>
        ) : null}
        <span className="ml-auto text-[10px] text-muted-foreground">
          口径: 连板梯队 EMA α=1/3 + 2 日确认; 阈值用自有历史分位标定
        </span>
      </div>

      {!segs?.available ? (
        <div className="grid place-items-center px-6 py-8 text-center">
          <Gauge className="h-8 w-8 text-muted-foreground" strokeWidth={1.5} />
          <p className="mt-3 text-[13px] text-foreground">尚无阶段历史</p>
          <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">
            阶段需从涨停事件回填历史后才能判定(EMA 与 2 日确认依赖完整序列)。
          </p>
          {isOwner ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => void doBackfill()}
              className="mt-3 rounded bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
            >
              {busy ? '回填中…' : '回填历史阶段'}
            </button>
          ) : (
            <p className="mt-2 text-[11px] text-muted-foreground">需管理员在后台触发回填</p>
          )}
          {msg ? <p className="mt-2 text-[11px] text-muted-foreground">{msg}</p> : null}
        </div>
      ) : (
        <div className="space-y-2">
          {pcts ? (
            <div className="flex flex-wrap gap-1.5">
              {PCT_ITEMS.map((it) => {
                const v = pcts[it.key]
                return (
                  <span
                    key={it.key}
                    title={`${it.label} 当前值在近 ${stats?.total_days ?? 0} 天历史中的百分位`}
                    className="rounded bg-accent/40 px-1.5 py-0.5 text-[11px] text-foreground"
                  >
                    {it.label} <span className="font-mono">p{v == null ? '--' : v}</span>
                  </span>
                )
              })}
            </div>
          ) : null}

          {rule && current ? (
            <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
              <span className={phaseTextClass(current.phase)}>
                {PHASE_LABEL[current.phase]} 已持续 {current.days} 天
              </span>
              <span>· 历史 {rule.count} 段</span>
              <span>· 平均 {rule.avg_days} 天</span>
              <span>· 最长 {rule.max_days} 天</span>
              {rule.next.length ? (
                <span className="flex items-center gap-1">
                  · 历史去向:
                  {rule.next.map(([label, p]) => (
                    <span key={label} className="rounded bg-accent/40 px-1 py-0.5 text-foreground">
                      {label} {Math.round(p * 100)}%
                    </span>
                  ))}
                </span>
              ) : null}
            </div>
          ) : null}

          <div>
            <div className="flex h-4 w-full overflow-hidden rounded">
              {segments.map((s) => (
                <div
                  key={`${s.start}-${s.phase}`}
                  title={`${s.label} ${s.start} ~ ${s.end} · ${s.days} 天 · 均高 ${s.avg_height} 板`}
                  className={phaseBandClass(s.phase)}
                  style={{ flexGrow: s.days, flexBasis: 0 }}
                />
              ))}
            </div>
            <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-muted-foreground">
              {(Object.keys(PHASE_LABEL) as PhaseKey[]).map((k) => (
                <span key={k} className="flex items-center gap-1">
                  <span className={`inline-block h-2 w-2 rounded-sm ${phaseBandClass(k)}`} />
                  {PHASE_LABEL[k]}
                </span>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
