import React from 'react'
import { Crosshair, ArrowUpRight, TrendingUp, TrendingDown, Bell } from 'lucide-react'
import { safeFixed } from '@/lib/format'

/** 罗盘顶部的自评徽标文案：模型自评 ≠ 统计置信度/准确率，单点定义供 AnswerCard 复用。 */
export const MODEL_SELF_RATING_LABEL = '模型自评'

/**
 * 模型自评的合法区间（原始 x/10）。越界、非有限、非数字一律视为"没有"。
 *
 * 为什么必须在这里卡死：历史缺陷是调用方把 8/10 放大成 80 再传进来，
 * 组件直接拼 `{value}%` 就变成了"置信度 80%"这种没有样本支撑的伪统计口径。
 * 现在单位在入参名上写明是 x/10，越界值宁可隐藏也不做任何换算。
 */
const SELF_RATING_MIN = 0
const SELF_RATING_MAX = 10

export function normalizeSelfRating(value: number | null | undefined): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  if (value < SELF_RATING_MIN || value > SELF_RATING_MAX) return null
  return value
}

/** 原样保留模型给的 x/10：8 → "8"、8.5 → "8.5"，不取整、不补零、不补成百分比。 */
function formatSelfRating(value: number): string {
  return String(value)
}

export interface DecisionCompassProps {
  ticker?: string
  tickerName?: string
  currentPrice?: number
  costPrice?: number
  supportPrice: number
  resistancePrice: number
  stopLossPrice?: number
  targetPrice?: number
  biasLabel?: string
  /**
   * 模型自评分值，**原始 x/10**（与回答正文「置信度 8/10」同一个数）。
   *
   * 不是统计置信度、不是准确率：没有样本与回测支撑，所以只按 x/10 原样展示。
   * 缺失 / 非有限 / 越界（不在 0~10）→ 徽标隐藏，不补默认值。
   */
  modelConfidence?: number
  tactics?: string[]
  onDrawOnChart?: () => void
  onSetAlert?: (price: number) => void
  onCopy?: () => void
}

/**
 * 数智裁决指标罗盘组件 (SIDA Pro 顶级金融终端版)
 *
 * 将散落的文本支撑、阻力与盈亏信息结构化为直观的水平价位罗盘轴，
 * 支持一键在 K 线图叠加画线与设置支撑破位告警。
 * 纯真实数据驱动：无默认伪造价格或虚构置信度。
 * 严守口径：只把明确标注的支撑与阻力作为对应坐标与画线，不将止损或目标混淆为支撑阻力。
 */
export const DecisionCompassCard: React.FC<DecisionCompassProps> = ({
  ticker,
  tickerName,
  currentPrice,
  costPrice,
  supportPrice,
  resistancePrice,
  stopLossPrice,
  targetPrice,
  biasLabel = '多空裁决',
  modelConfidence,
  tactics = [],
  onDrawOnChart,
  onSetAlert,
  onCopy,
}) => {
  // 计算各关键点位在区间内的百分比位置
  const validPrices: number[] = [supportPrice, resistancePrice]
  if (currentPrice != null && Number.isFinite(currentPrice)) validPrices.push(currentPrice)
  if (costPrice != null && Number.isFinite(costPrice)) validPrices.push(costPrice)
  if (stopLossPrice != null && Number.isFinite(stopLossPrice)) validPrices.push(stopLossPrice)
  if (targetPrice != null && Number.isFinite(targetPrice)) validPrices.push(targetPrice)

  const minVal = Math.min(...validPrices) * 0.98
  const maxVal = Math.max(...validPrices) * 1.02
  const range = maxVal - minVal || 1

  const toPct = (val: number) => Math.min(Math.max(((val - minVal) / range) * 100, 4), 96)

  const supportPct = toPct(supportPrice)
  const resistancePct = toPct(resistancePrice)
  const currentPct = currentPrice != null && Number.isFinite(currentPrice) ? toPct(currentPrice) : null
  const costPct = costPrice != null && Number.isFinite(costPrice) ? toPct(costPrice) : null
  const stopLossPct = stopLossPrice != null && Number.isFinite(stopLossPrice) ? toPct(stopLossPrice) : null
  const targetPct = targetPrice != null && Number.isFinite(targetPrice) ? toPct(targetPrice) : null

  const pnlPct =
    costPrice != null && currentPrice != null && costPrice > 0
      ? safeFixed(((currentPrice - costPrice) / costPrice) * 100, 2)
      : null
  const isLoss = pnlPct !== null && Number(pnlPct) < 0
  // 模型自评：只保留原始 x/10，非法值直接视为缺失（绝不兜底成百分比）
  const selfRating = normalizeSelfRating(modelConfidence)

  return (
    <div data-testid="copilot-decision-compass" className="card-surface rounded-lg bg-s2 overflow-hidden my-2 text-[12px]">
      {/* 头部：综合裁决徽章与置信度 */}
      <div className="flex items-center justify-between border-b border-border bg-s3 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="flex items-center gap-1.5 rounded-md bg-role-watch/20 px-2 py-0.5 font-medium text-role-watch text-[12px]">
            <span className="relative flex h-1.5 w-1.5">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-role-watch opacity-75" />
              <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-role-watch" />
            </span>
            {biasLabel}
          </span>
          {selfRating != null ? (
            <span
              data-testid="copilot-compass-model-confidence"
              className="text-[12px] text-muted-foreground"
              title="模型自评：模型自己对结论把握的主观打分，非统计置信度或准确率"
            >
              {MODEL_SELF_RATING_LABEL}{' '}
              <strong className="text-foreground font-mono">{formatSelfRating(selfRating)}/10</strong>
            </span>
          ) : null}
        </div>
        <span className="text-[12px] font-mono text-role-watch flex items-center gap-1">
          <Crosshair className="w-3.5 h-3.5" />
          多维依据综合裁决
        </span>
      </div>

      {/* 中部：关键价位区间可视化罗盘 */}
      <div className="border-b border-border bg-s1 p-3 space-y-2">
        <div className="flex items-center justify-between text-[12px]">
          <span className="text-muted-foreground font-medium">
            {tickerName ? `${tickerName}${ticker ? ` (${ticker})` : ''}` : ticker || '关键点位'} 价位罗盘与多空分界
          </span>
          {pnlPct != null ? (
            <span
              className={`font-mono font-semibold flex items-center gap-1 ${
                isLoss ? 'text-stock-down' : 'text-stock-up'
              }`}
            >
              {isLoss ? <TrendingDown className="w-3.5 h-3.5" /> : <TrendingUp className="w-3.5 h-3.5" />}
              持仓盈亏: {pnlPct}%
            </span>
          ) : null}
        </div>

        {/* 水平区间轴 */}
        <div className="py-2.5">
          <div className="relative h-2 w-full rounded-full bg-s3 flex items-center">
            {/* 止损位标记（若有） */}
            {stopLossPct != null && (
              <div
                className="absolute h-2.5 w-2.5 -ml-1 rounded-full bg-rose-500"
                style={{ left: `${stopLossPct}%` }}
                title={`止损位: ${safeFixed(stopLossPrice, 2)}`}
              />
            )}
            {/* 支撑位标记 */}
            <div
              className="absolute h-2.5 w-2.5 -ml-1 rounded-full text-stock-down bg-current"
              style={{ left: `${supportPct}%` }}
              title={`支撑位: ${safeFixed(supportPrice, 2)}`}
            />
            {/* 现价点位 (若有) */}
            {currentPct != null && (
              <div
                className="absolute h-3.5 w-3.5 -ml-1.5 rounded-full bg-role-watch z-10 shadow-[0_0_8px_hsl(var(--role-watch))]"
                style={{ left: `${currentPct}%` }}
                title={`现价: ${safeFixed(currentPrice, 2)}`}
              />
            )}
            {/* 成本位标记 (若有) */}
            {costPct != null && (
              <div
                className="absolute h-2.5 w-2.5 -ml-1 rounded-full bg-amber-500"
                style={{ left: `${costPct}%` }}
                title={`持仓成本价: ${safeFixed(costPrice, 2)}`}
              />
            )}
            {/* 阻力位标记 */}
            <div
              className="absolute h-2.5 w-2.5 -ml-1 rounded-full text-stock-up bg-current"
              style={{ left: `${resistancePct}%` }}
              title={`阻力位: ${safeFixed(resistancePrice, 2)}`}
            />
            {/* 目标位标记（若有） */}
            {targetPct != null && (
              <div
                className="absolute h-2.5 w-2.5 -ml-1 rounded-full bg-emerald-500"
                style={{ left: `${targetPct}%` }}
                title={`目标位: ${safeFixed(targetPrice, 2)}`}
              />
            )}
          </div>

          {/* 标尺数值对照 */}
          <div className="flex justify-between text-[12px] font-mono pt-2 text-muted-foreground">
            {stopLossPrice != null ? (
              <div className="text-rose-400">
                <div>止损 {safeFixed(stopLossPrice, 2)}</div>
                <div className="text-[12px] text-muted-foreground">止损点</div>
              </div>
            ) : null}
            <div className="text-stock-down">
              <div>支撑 {safeFixed(supportPrice, 2)}</div>
              {currentPrice != null && currentPrice > 0 ? (
                <div className="text-[12px] text-muted-foreground">
                  {safeFixed(((supportPrice - currentPrice) / currentPrice) * 100, 1)}%
                </div>
              ) : null}
            </div>
            {currentPrice != null ? (
              <div className="text-role-watch font-bold text-center">
                <div>现价 {safeFixed(currentPrice, 2)}</div>
                <div className="text-[12px] text-muted-foreground">基准价</div>
              </div>
            ) : null}
            {costPrice != null ? (
              <div className="text-amber-500 text-center">
                <div>成本 {safeFixed(costPrice, 2)}</div>
                <div className="text-[12px] text-muted-foreground">保本位</div>
              </div>
            ) : null}
            <div className="text-stock-up text-right">
              <div>阻力 {safeFixed(resistancePrice, 2)}</div>
              {currentPrice != null && currentPrice > 0 ? (
                <div className="text-[12px] text-muted-foreground">
                  +{safeFixed(((resistancePrice - currentPrice) / currentPrice) * 100, 1)}%
                </div>
              ) : null}
            </div>
            {targetPrice != null ? (
              <div className="text-emerald-400 text-right">
                <div>目标 {safeFixed(targetPrice, 2)}</div>
                <div className="text-[12px] text-muted-foreground">目标位</div>
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {/* 战术操盘预案 */}
      {tactics && tactics.length > 0 && (
        <div className="p-3 bg-s2 space-y-1.5 text-[12px] text-foreground border-b border-border">
          <div className="font-semibold text-foreground">操盘战术预案：</div>
          <ul className="space-y-1 text-[12px] leading-relaxed">
            {tactics.map((tactic, idx) => (
              <li key={idx} className="flex items-start gap-1.5">
                <span className="font-bold text-role-watch">{idx + 1}.</span>
                <span>{tactic}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 底部快捷操作 */}
      <div className="flex flex-wrap items-center justify-between bg-s3 p-2.5 gap-2">
        <div className="flex flex-wrap items-center gap-2">
          {onDrawOnChart && (
            <button
              type="button"
              onClick={onDrawOnChart}
              className="flex items-center gap-1 rounded bg-role-watch/20 hover:bg-role-watch/30 px-2.5 py-1 text-[12px] font-medium text-role-watch transition"
            >
              <ArrowUpRight className="w-3.5 h-3.5" />
              在 K 线叠加画线
            </button>
          )}
          {onSetAlert && (
            <button
              type="button"
              onClick={() => onSetAlert(supportPrice)}
              className="flex items-center gap-1 rounded bg-s1 hover:bg-s2 px-2.5 py-1 text-[12px] text-foreground border border-border transition"
            >
              <Bell className="w-3.5 h-3.5 text-muted-foreground" />
              设 {safeFixed(supportPrice, 2)} 支撑破位告警
            </button>
          )}
        </div>
        {onCopy && (
          <button
            type="button"
            onClick={onCopy}
            className="text-[12px] text-muted-foreground hover:text-foreground transition"
          >
            复制战报
          </button>
        )}
      </div>
    </div>
  )
}
