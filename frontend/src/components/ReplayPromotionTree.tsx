/**
 * 连板晋级树组件(创新 1, 2026-10-24)。
 *
 * 首板 → 2板 → … → 最高板 的横向梯队: 每板级一列(列头 = N板 + 家数),
 * 列内成员 = 名称/代码 + 首封时间 + 封单额(亿/万) + 炸板次数。
 *
 * 诚实口径(specs REPTREE-004/005):
 *  - 家数来自标量列/梯队计数(真实), 成员明细受采集 Top40 截断 → 不全时列内标注;
 *  - 分层晋级率表内未存 → 不渲染任何"每层晋级率"(口径缺口在页面说明区统一标注);
 *  - payload 未落库时整体显式「无数据」, 不用标量反推编造。
 */
import PromotionFunnel from './PromotionFunnel'
import { ChevronRight } from 'lucide-react'
import type { ReplayTreeLevel } from '@panwatch/api'
import { toAmountFromWanUnsigned } from '@/lib/format'

function MemberRow({
  m,
  onPick,
}: {
  m: ReplayTreeLevel['members'][number]
  onPick?: (symbol: string) => void
}) {
  const seal = m.seal_amount_wan
  return (
    <button
      type="button"
      className="block w-full border-t border-border px-2 py-1.5 text-left first:border-t-0 hover:bg-s2"
      data-testid={`replay-tree-member-${m.symbol ?? 'na'}`}
      onClick={() => m.symbol && onPick?.(m.symbol)}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate text-[12px] text-foreground" title={m.name ?? ''}>
          {m.name || '无数据'}
        </span>
        <span className="font-mono text-[12px] text-muted-foreground">{m.symbol}</span>
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px] text-muted-foreground">
        <span className="tabular-nums">首封 {m.first_seal || '--'}</span>
        <span className="tabular-nums">
          封单 {seal != null ? toAmountFromWanUnsigned(seal) : '--'}
        </span>
        <span className="tabular-nums">炸 {m.open_times ?? '--'}次</span>
        {m.pct != null && (
          <span className="tabular-nums text-stock-up">+{m.pct}%</span>
        )}
      </div>
    </button>
  )
}

export default function ReplayPromotionTree({
  levels,
  ladderAvailable,
  maxStreak,
  detailNote,
  onPick,
}: {
  levels: ReplayTreeLevel[]
  ladderAvailable: boolean
  maxStreak: number
  detailNote?: string | null
  /** 点击成员 → 跳个股工作台(可选) */
  onPick?: (symbol: string) => void
}) {
  if (!ladderAvailable) {
    return (
      <div
        className="border-t border-border py-3 text-[12px] text-muted-foreground"
        data-testid="replay-tree-missing"
      >
        梯队明细 无数据(采集 payload 未落库) —— 家数概览见上方三池概览, 不用标量反推。
      </div>
    )
  }
  if (!levels.length) {
    return (
      <div className="border-t border-border py-3 text-[12px] text-muted-foreground">
        当日无涨停梯队记录。
      </div>
    )
  }
  // 首板在左 → 最高板在右(晋级方向), 列间 hairline 分隔, 横向滚动
  const asc = [...levels].sort((a, b) => a.board - b.board)
  return (
    <div className="border-t border-border" data-testid="replay-tree-root">
      {/* 家数断崖图（2026-10-24 补）：四列文字 rhythms 里看不出「70→9」那一刀有多深 */}
      <PromotionFunnel levels={asc} />
      {detailNote && (
        <div className="py-1.5 text-[12px] text-muted-foreground" data-testid="replay-tree-detail-note">
          {detailNote}
        </div>
      )}
      <div className="flex items-start overflow-x-auto">
        {asc.map((lv, i) => (
          <div key={lv.board} className="flex items-start">
            {i > 0 && (
              <div
                className="flex flex-col items-center justify-center px-1 pt-4 text-muted-foreground"
                aria-hidden
                title="晋级方向(分层晋级率表内未存)"
              >
                <ChevronRight className="h-3.5 w-3.5" />
              </div>
            )}
            <div
              className="min-w-[150px] flex-1"
              data-testid={`replay-tree-level-${lv.board}`}
            >
              <div className="flex items-baseline justify-between gap-2 border-b border-border px-2 py-1.5">
                <span className="text-[14px] font-medium text-stock-up">
                  {lv.board === 1 ? '首板' : `${lv.board}板`}
                </span>
                <span className="text-[12px] tabular-nums text-muted-foreground">
                  {lv.count} 家
                </span>
              </div>
              {lv.members.length === 0 ? (
                <div className="px-2 py-2 text-[12px] text-muted-foreground">
                  {lv.members_truncated ? '明细未落库(仅采集 Top40)' : '无明细'}
                </div>
              ) : (
                <div className="max-h-[280px] overflow-y-auto">
                  {lv.members.map((m, j) => (
                    <MemberRow key={`${m.symbol ?? 'na'}-${j}`} m={m} onPick={onPick} />
                  ))}
                  {lv.members_truncated && (
                    <div className="border-t border-border px-2 py-1 text-[12px] text-muted-foreground">
                      明细不全(采集 Top40) · 家数以 {lv.count} 家为准
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
      <div className="border-t border-border py-1.5 text-[12px] text-muted-foreground">
        晋级方向 首板 → … → 最高板({maxStreak}板) · 红色=涨停/晋级 · 分层晋级率表内未存(口径缺口)
      </div>
    </div>
  )
}
