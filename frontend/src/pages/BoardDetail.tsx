import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Layers, RefreshCw } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'

/**
 * 板块详情页(2026-08-20, v0.3.0) 路由 /boards/:blockCode。
 * 依赖:
 *   GET /api/boards/{block_code}             板块详情(今日 change_pct / fund_net / volume)
 *   GET /api/boards/{block_code}/constituents 成分股(thsdk 实时, 1h 缓存)
 *   GET /api/boards/rotation?days=5          板块轮动排序(取 Top5 横条 + 本板块 5 日涨幅)
 * 口径:
 *   - 今日涨跌幅 change_pct、资金净流入 fund_net、量能 volume 均来自 thsdk 板块日线。
 *   - 5 日涨幅 = 近 5 日日线复利累计涨幅(rotation 接口, 若命中本板块)。
 *   - 换手率接口未提供(仅 volume), 故用成交额/量能呈现并注明口径。
 * 成分股字段为 thsdk 原始列, 前端按语义模糊取列(代码/名称/涨速/资金), 缺字段显示 --。
 */

interface BoardToday {
  date?: string | null
  change_pct?: number | null
  fund_net?: number | null
  volume?: number | null
}

interface BoardDetailResp {
  block_code: string
  name: string
  board_type?: string
  today?: BoardToday | null
  has_daily?: boolean
  live?: boolean
  error?: string
}

interface BoardConstituent {
  count: number
  items: Record<string, unknown>[]
}

interface RotationItem {
  block_code: string
  name: string
  rotation_score: number
  change_5d?: number | null
  fund_net?: number | null
  consecutive_days?: number
}

interface RotationResp {
  days: number
  items: RotationItem[]
}

/** 从 thsdk 原始列 dict 按中文/英文语义取字段 */
function pickNum(row: Record<string, unknown>, ...keys: string[]): number | null {
  for (const k of keys) {
    const v = row[k]
    if (v == null) continue
    const n = typeof v === 'number' ? v : Number(String(v).replace(/[^\d.-]/g, ''))
    if (Number.isFinite(n)) return n
  }
  return null
}

function pickStr(row: Record<string, unknown>, ...keys: string[]): string {
  for (const k of keys) {
    const v = row[k]
    if (v != null && String(v).trim() !== '') return String(v).trim()
  }
  return ''
}

/** 资金净额(元) -> 亿/万 紧凑带符号 */
function fmtWan(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--'
  const abs = Math.abs(v)
  const sign = v > 0 ? '+' : v < 0 ? '-' : ''
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)}亿`
  return `${sign}${(abs / 1e4).toFixed(0)}万`
}

function fmtPct(v: number | null | undefined, plus = true): string {
  if (v == null || !Number.isFinite(v)) return '--'
  return `${plus && v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

function pctColor(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return 'text-muted-foreground'
  return v > 0 ? 'text-rose-600 dark:text-rose-400' : v < 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-muted-foreground'
}

export default function BoardDetailPage() {
  const { blockCode } = useParams<{ blockCode: string }>()
  const navigate = useNavigate()
  const [detail, setDetail] = useState<BoardDetailResp | null>(null)
  const [constituents, setConstituents] = useState<BoardConstituent | null>(null)
  const [rotation, setRotation] = useState<RotationResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = async () => {
    if (!blockCode) return
    setLoading(true)
    setError('')
    try {
      const [d, c] = await Promise.all([
        fetchAPI<BoardDetailResp>(`/boards/${encodeURIComponent(blockCode)}`, { cacheMode: 'reload' }),
        fetchAPI<BoardConstituent>(`/boards/${encodeURIComponent(blockCode)}/constituents`, { cacheMode: 'reload' }).catch(() => null),
      ])
      if (d?.error) setError(d.error)
      else setDetail(d)
      setConstituents(c)
    } catch (e) {
      setError(e instanceof Error ? e.message : '板块详情加载失败')
    }
    // 板块轮动(全局, 独立加载, 失败静默)
    fetchAPI<RotationResp>('/boards/rotation?days=5', { cacheMode: 'reload' }).then(setRotation).catch(() => {})
    setLoading(false)
  }

  useEffect(() => { void load() }, [blockCode])

  const rotItemsTop = useMemo(() => (rotation?.items ?? []).slice(0, 5), [rotation])
  // 本板块 5 日涨幅(从轮动结果按 block_code 反查; 非本板块命中则为空)
  const selfRotation = useMemo(
    () => (rotation?.items ?? []).find((r) => r.block_code === blockCode) ?? null,
    [rotation, blockCode]
  )
  const fundNet = detail?.today?.fund_net ?? selfRotation?.fund_net
  // 轮动分数区间 → 颜色(涨红/跌绿, 强弱由 ± score 比例决定)
  const maxScore = Math.max(1, ...rotItemsTop.map((r) => r.rotation_score))

  const today = detail?.today ?? null

  // 顶栏关键指标
  const metrics = [
    { label: '今日涨跌幅', value: fmtPct(today?.change_pct), cls: pctColor(today?.change_pct), note: 'thsdk 日线' },
    { label: '5日涨幅', value: fmtPct(selfRotation?.change_5d), cls: pctColor(selfRotation?.change_5d), note: '轮动复利' },
    { label: '资金净流入', value: fmtWan(fundNet), cls: pctColor(fundNet), note: 'thsdk 日线' },
    { label: '量能', value: today?.volume != null && Number.isFinite(today.volume) ? `${(today.volume / 1e8).toFixed(2)}亿` : '--', cls: 'text-foreground', note: '换手率未提供' },
  ]

  const rotBarColor = (r: RotationItem): string => {
    const c = r.change_5d
    if (c == null || !Number.isFinite(c)) return '#60a5fa'
    return c > 0 ? '#ef4444' : c < 0 ? '#10b981' : '#94a3b8'
  }

  return (
    <div className="page-container pb-10">
      {/* 页头 */}
      <div className="flex items-center gap-3 mb-4">
        <Button variant="ghost" size="sm" className="h-8" onClick={() => navigate(-1)}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div>
          <h1 className="text-[20px] md:text-[22px] font-bold flex items-center gap-2">
            <Layers className="w-5 h-5 text-primary" />
            {detail?.name || '板块详情'}
          </h1>
          <div className="text-[11px] text-muted-foreground font-mono">
            {detail?.block_code || blockCode}
            {detail?.board_type ? ` · ${detail.board_type === 'concept' ? '概念' : '行业'}` : ''}
            {detail?.live ? ' · 实时' : detail?.has_daily ? ' · 日线' : ''}
          </div>
        </div>
        <div className="ml-auto">
          <Button variant="outline" size="sm" className="h-8" onClick={() => void load()} disabled={loading}>
            <RefreshCw className={`h-3.5 w-3.5 mr-1 ${loading ? 'animate-spin' : ''}`} /> 刷新
          </Button>
        </div>
      </div>

      {error && <div className="card p-3 mb-4 text-[12px] text-amber-700 dark:text-amber-500">{error}</div>}
      {loading && !detail ? (
        <div className="grid gap-3">
          <div className="h-[92px] rounded-xl border border-border/50 animate-pulse bg-accent/20" />
          <div className="h-[220px] rounded-xl border border-border/50 animate-pulse bg-accent/20" />
        </div>
      ) : (
        <>
          {/* 关键指标 */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
            {metrics.map((m) => (
              <div key={m.label} className="card p-3">
                <div className="text-[11px] text-muted-foreground">{m.label}</div>
                <div className={`text-[20px] font-bold mt-1 font-mono tabular-nums ${m.cls}`}>{m.value}</div>
                <div className="text-[10px] text-muted-foreground mt-1">{m.note}</div>
              </div>
            ))}
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            {/* 成分股表 */}
            <div className="lg:col-span-2 card p-3 md:p-4">
              <div className="flex items-center justify-between mb-2">
                <h2 className="text-[14px] font-semibold text-foreground">成分股</h2>
                <span className="text-[11px] text-muted-foreground">
                  共 {constituents?.count ?? 0} 只 · 口径 thsdk 实时
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-[12px]">
                  <thead>
                    <tr className="text-[11px] text-muted-foreground border-b border-border/60">
                      <th className="text-left py-1.5 pr-2 w-8">#</th>
                      <th className="text-left py-1.5 pr-2">代码</th>
                      <th className="text-left py-1.5 pr-2">名称</th>
                      <th className="text-right py-1.5 pr-2">涨速</th>
                      <th className="text-right py-1.5">资金</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(constituents?.items ?? []).map((row, i) => {
                      const code = pickStr(row, '代码', 'code', '证券代码')
                      const name = pickStr(row, '名称', 'name', '证券名称')
                      // 涨速: 语义匹配含"速"/"speed"的列
                      const speedKey = Object.keys(row).find((k) => k.includes('速') || k.toLowerCase().includes('speed'))
                      const speed = speedKey ? pickNum(row, speedKey) : null
                      // 资金: 语义匹配含"主力净"/"资金"/"净流入"/"fund"的列
                      const fundKey = Object.keys(row).find((k) => /主力净|资金|净流入|fund/i.test(k))
                      const fund = fundKey ? pickNum(row, fundKey) : null
                      return (
                        <tr key={code || i} className="border-b border-border/30 hover:bg-accent/40">
                          <td className="py-1 pr-2 text-[10px] text-muted-foreground">{i + 1}</td>
                          <td className="py-1 pr-2 font-mono text-muted-foreground">{code || '--'}</td>
                          <td className="py-1 pr-2 font-medium text-foreground">{name || '--'}</td>
                          <td className={`py-1 pr-2 text-right font-mono tabular-nums ${pctColor(speed)}`}>{fmtPct(speed)}</td>
                          <td className={`py-1 text-right font-mono tabular-nums ${pctColor(fund)}`}>{fmtWan(fund)}</td>
                        </tr>
                      )
                    })}
                    {!constituents && (
                      <tr>
                        <td colSpan={5} className="py-8 text-center text-[11px] text-muted-foreground">
                          成分股暂不可用(thsdk 未接入 / 拉取失败)
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            {/* 板块轮动 Top5 横条 */}
            <div className="card p-3 md:p-4">
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-[14px] font-semibold text-foreground">板块轮动 Top 5</h2>
                <span className="text-[10px] text-muted-foreground">近{rotation?.days ?? 5}日 · click进入详情</span>
              </div>
              {rotItemsTop.length === 0 ? (
                <div className="text-[11px] text-muted-foreground py-8 text-center">暂无轮动数据</div>
              ) : (
                <div className="space-y-2.5">
                  {rotItemsTop.map((r) => (
                    <button
                      key={r.block_code}
                      type="button"
                      className="w-full text-left group"
                      onClick={() => navigate(`/boards/${r.block_code}`)}
                      title={`${r.name} · 强度 ${r.rotation_score.toFixed(1)}`}
                    >
                      <div className="flex items-center justify-between text-[11px] mb-0.5">
                        <span className="truncate font-medium text-foreground group-hover:text-primary transition-colors">{r.name}</span>
                        <span className="font-mono ml-2 shrink-0">
                          <span className={pctColor(r.change_5d)}>{fmtPct(r.change_5d)}</span>
                          <span className="text-muted-foreground ml-1.5">{r.rotation_score.toFixed(0)}</span>
                        </span>
                      </div>
                      <div className="h-1.5 rounded-full bg-accent/40 overflow-hidden">
                        <div
                          className="h-full rounded-full transition-[width]"
                          style={{ width: `${(r.rotation_score / maxScore) * 100}%`, backgroundColor: rotBarColor(r) }}
                        />
                      </div>
                    </button>
                  ))}
                </div>
              )}
              <div className="mt-3 text-[10px] text-muted-foreground">强度分: 动量+资金+连续性 0-100 降序</div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
