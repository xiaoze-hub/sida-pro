import React from 'react'
import { AlertTriangle, Crosshair, ArrowUpRight, TrendingUp, TrendingDown, Bell } from 'lucide-react'

export interface DecisionCompassProps {
  ticker?: string
  tickerName?: string
  currentPrice: number
  costPrice?: number
  supportPrice: number
  resistancePrice: number
  biasLabel: string
  confidence?: number
  tactics: string[]
  onDrawOnChart?: () => void
  onSetAlert?: (price: number) => void
  onCopy?: () => void
}

/**
 * 数智裁决指标罗盘组件 (SIDA Pro 顶级金融终端版)
 *
 * 将散落的文本支撑、阻力与盈亏信息结构化为直观的水平价位罗盘轴，
 * 支持一键在 K 线图叠加画线与设置破位告警。
 */
export const DecisionCompassCard: React.FC<DecisionCompassProps> = ({
  ticker = '600111',
  tickerName = '北方稀土',
  currentPrice,
  costPrice = 36.52,
  supportPrice,
  resistancePrice,
  biasLabel,
  confidence = 86,
  tactics,
  onDrawOnChart,
  onSetAlert,
  onCopy,
}) => {
  // 计算现价与成本在区间内的百分比位置
  const minVal = Math.min(supportPrice * 0.98, currentPrice, costPrice)
  const maxVal = Math.max(resistancePrice * 1.02, currentPrice, costPrice)
  const range = maxVal - minVal || 1

  const currentPct = Math.min(Math.max(((currentPrice - minVal) / range) * 100, 5), 95)
  const costPct = Math.min(Math.max(((costPrice - minVal) / range) * 100, 5), 95)
  const supportPct = Math.min(Math.max(((supportPrice - minVal) / range) * 100, 5), 95)
  const resistancePct = Math.min(Math.max(((resistancePrice - minVal) / range) * 100, 5), 95)

  const pnlPct = (((currentPrice - costPrice) / costPrice) * 100).toFixed(2)
  const isLoss = Number(pnlPct) < 0

  return (
    <div className="rounded-xl border border-cyan-500/30 bg-[#111724] overflow-hidden shadow-lg transition-all hover:border-cyan-500/50 my-2 text-xs">
      {/* 头部：综合裁决徽章与置信度 */}
      <div className="flex items-center justify-between border-b border-white/[0.08] bg-gradient-to-r from-blue-950/70 via-[#131D2D] to-transparent p-3">
        <div className="flex items-center gap-2">
          <span className="flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/20 px-2.5 py-1 font-bold text-amber-300 shadow-sm text-xs">
            <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
            {biasLabel}
          </span>
          <span className="text-[11px] text-slate-400">
            置信度 <strong className="text-slate-200 font-mono">{confidence}%</strong>
          </span>
        </div>
        <span className="text-[11px] font-mono text-cyan-400 flex items-center gap-1">
          <Crosshair className="w-3 h-3" />
          多智能体加权裁决
        </span>
      </div>

      {/* 中部：关键价位区间可视化罗盘 */}
      <div className="border-b border-white/[0.08] bg-[#0D131E] p-3 space-y-2">
        <div className="flex items-center justify-between text-[11px]">
          <span className="text-slate-400 font-medium">
            {tickerName} ({ticker}) 价位罗盘与风险敞口
          </span>
          <span
            className={`font-mono font-semibold flex items-center gap-1 ${
              isLoss ? 'text-rose-400' : 'text-emerald-400'
            }`}
          >
            {isLoss ? <TrendingDown className="w-3 h-3" /> : <TrendingUp className="w-3 h-3" />}
            持仓盈亏: {pnlPct}%
          </span>
        </div>

        {/* 水平区间轴 */}
        <div className="py-2.5">
          <div className="relative h-2 w-full rounded-full bg-slate-800 flex items-center">
            {/* 支撑位标记 */}
            <div
              className="absolute h-2.5 w-2.5 -ml-1 rounded-full bg-emerald-400 ring-2 ring-emerald-950"
              style={{ left: `${supportPct}%` }}
              title={`防守位: ${supportPrice}`}
            />
            {/* 现价点位 (带微光) */}
            <div
              className="absolute h-3.5 w-3.5 -ml-1.5 rounded-full bg-cyan-400 ring-4 ring-cyan-950 shadow-[0_0_12px_rgba(6,182,212,0.8)] z-10"
              style={{ left: `${currentPct}%` }}
              title={`现价: ${currentPrice}`}
            />
            {/* 成本位标记 */}
            <div
              className="absolute h-2.5 w-2.5 -ml-1 rounded-full bg-amber-400 ring-2 ring-amber-950"
              style={{ left: `${costPct}%` }}
              title={`成本价: ${costPrice}`}
            />
            {/* 阻力位标记 */}
            <div
              className="absolute h-2.5 w-2.5 -ml-1 rounded-full bg-rose-400 ring-2 ring-rose-950"
              style={{ left: `${resistancePct}%` }}
              title={`阻力位: ${resistancePrice}`}
            />
          </div>

          {/* 标尺数值对照 */}
          <div className="flex justify-between text-[10px] font-mono pt-2 text-slate-400">
            <div className="text-emerald-400">
              <div>防守 {supportPrice.toFixed(2)}</div>
              <div className="text-[9px] text-slate-500">
                {(((supportPrice - currentPrice) / currentPrice) * 100).toFixed(1)}%
              </div>
            </div>
            <div className="text-cyan-300 font-bold text-center">
              <div>现价 {currentPrice.toFixed(2)}</div>
              <div className="text-[9px] text-slate-500">中位蓄势</div>
            </div>
            <div className="text-amber-300 text-center">
              <div>成本 {costPrice.toFixed(2)}</div>
              <div className="text-[9px] text-slate-500">保本位</div>
            </div>
            <div className="text-rose-400 text-right">
              <div>阻力 {resistancePrice.toFixed(2)}</div>
              <div className="text-[9px] text-slate-500">
                +{(((resistancePrice - currentPrice) / currentPrice) * 100).toFixed(1)}%
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 战术操盘预案 */}
      {tactics && tactics.length > 0 && (
        <div className="p-3 bg-[#101622] space-y-1.5 text-xs text-slate-300 border-b border-white/[0.08]">
          <div className="font-semibold text-slate-200">操盘战术预案：</div>
          <ul className="space-y-1 text-[11px] leading-relaxed">
            {tactics.map((tactic, idx) => (
              <li key={idx} className="flex items-start gap-1.5">
                <span className="font-bold text-cyan-400">{idx + 1}.</span>
                <span>{tactic}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 底部快捷操作 */}
      <div className="flex items-center justify-between bg-[#0A0E17] p-2.5">
        <div className="flex items-center gap-2">
          {onDrawOnChart && (
            <button
              type="button"
              onClick={onDrawOnChart}
              className="flex items-center gap-1 rounded bg-blue-600/30 hover:bg-blue-600/50 px-2.5 py-1 text-[11px] font-medium text-cyan-300 border border-cyan-500/40 transition"
            >
              <ArrowUpRight className="w-3 h-3" />
              在 K 线叠加画线
            </button>
          )}
          {onSetAlert && (
            <button
              type="button"
              onClick={() => onSetAlert(supportPrice)}
              className="flex items-center gap-1 rounded bg-[#141C2A] hover:bg-[#1A2536] px-2.5 py-1 text-[11px] text-slate-300 border border-white/[0.08] transition"
            >
              <Bell className="w-3 h-3" />
              设 {supportPrice.toFixed(2)} 破位告警
            </button>
          )}
        </div>
        {onCopy && (
          <button
            type="button"
            onClick={onCopy}
            className="text-[11px] text-slate-400 hover:text-white transition"
          >
            复制战报
          </button>
        )}
      </div>
    </div>
  )
}
