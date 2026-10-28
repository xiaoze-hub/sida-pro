import React from 'react'
import { Bot, Activity, LineChart, Flame, Building2 } from 'lucide-react'

export interface AgentPerspective {
  role: 'technical' | 'funds' | 'theme' | 'fundamentals'
  agentName: string
  score?: number
  status: string
  conclusion: string
}

export interface MultiAgentDebateProps {
  perspectives?: AgentPerspective[]
}

/**
 * 多智能体会诊雷达组件 (SIDA Pro TradingAgents Panel)
 * 显性化呈现技术面、资金面与题材环境多维度的独立诊断与加权证据链。
 * 纯真实数据展示，无真实 perspectives 时返回 null，杜绝虚构演示数据。
 */
export const MultiAgentDebatePanel: React.FC<MultiAgentDebateProps> = ({
  perspectives,
}) => {
  if (!perspectives || perspectives.length === 0) {
    return null
  }

  return (
    <div data-testid="copilot-multi-agent-debate" className="card-surface rounded-lg bg-s2 p-3 space-y-2 text-[12px]">
      <div className="flex items-center justify-between">
        <span className="text-[12px] font-semibold text-foreground flex items-center gap-1.5">
          <Bot className="w-3.5 h-3.5 text-role-watch" />
          多智能体会诊证据链 ({perspectives.length} 维度)
        </span>
        <span className="text-[12px] text-muted-foreground font-mono">TradingAgents 集群</span>
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
