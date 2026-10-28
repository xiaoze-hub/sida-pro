import React from 'react'
import { Layers, Activity, LineChart, Flame, Building2 } from 'lucide-react'

export interface DimensionPerspective {
  role: 'technical' | 'funds' | 'theme' | 'fundamentals'
  agentName: string
  score?: number
  status: string
  conclusion: string
}

export type AgentPerspective = DimensionPerspective

export interface MultiDimensionEvidenceProps {
  perspectives?: DimensionPerspective[]
}

export type MultiAgentDebateProps = MultiDimensionEvidenceProps

/**
 * 多维依据透视组件 (SIDA Pro Multi-Dimension Evidence Panel)
 * 显性化呈现技术面、资金面、题材与基本面等多维度的真实证据链分解。
 * 如实反映单次推演的多维度依据，杜绝虚构智能体集群。无视角时返回 null。
 */
export const MultiDimensionEvidencePanel: React.FC<MultiDimensionEvidenceProps> = ({
  perspectives,
}) => {
  if (!perspectives || perspectives.length === 0) {
    return null
  }

  return (
    <div
      data-testid="copilot-multi-dimension-evidence"
      data-legacy-testid="copilot-multi-agent-debate"
      className="card-surface rounded-lg bg-s2 p-3 space-y-2 text-[12px]"
    >
      <div className="flex items-center justify-between">
        <span className="text-[12px] font-semibold text-foreground flex items-center gap-1.5">
          <Layers className="w-3.5 h-3.5 text-role-watch" />
          多维依据透视 ({perspectives.length} 维度)
        </span>
        <span className="text-[12px] text-muted-foreground font-mono">维度研判分解</span>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        {perspectives.map((p, i) => (
          <div key={i} className="p-2.5 rounded-lg bg-s1 space-y-1">
            <div className="flex items-center gap-1 text-[12px] text-muted-foreground">
              {p.role === 'technical' && <LineChart className="w-3.5 h-3.5 text-amber-500" />}
              {p.role === 'funds' && <Activity className="w-3.5 h-3.5 text-stock-down" />}
              {p.role === 'theme' && <Flame className="w-3.5 h-3.5 text-stock-up" />}
              {p.role === 'fundamentals' && <Building2 className="w-3.5 h-3.5 text-role-watch" />}
              <span className="font-medium text-foreground">{p.agentName}</span>
            </div>
            {p.score != null ? (
              <div
                className={`font-bold font-mono text-[12px] ${
                  p.score >= 70
                    ? 'text-stock-up'
                    : p.score >= 50
                    ? 'text-amber-500'
                    : 'text-stock-down'
                }`}
              >
                {p.score}分 · {p.status}
              </div>
            ) : (
              <div className="font-medium text-[12px] text-role-watch">
                {p.status}
              </div>
            )}
            <div className="text-[12px] text-muted-foreground line-clamp-2" title={p.conclusion}>
              {p.conclusion}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

/** 兼容别名导出 */
export const MultiAgentDebatePanel = MultiDimensionEvidencePanel
