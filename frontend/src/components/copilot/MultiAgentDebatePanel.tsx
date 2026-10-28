import React from 'react'
import { Bot, ShieldCheck, Activity, LineChart, Flame } from 'lucide-react'

export interface AgentPerspective {
  role: 'technical' | 'funds' | 'theme'
  agentName: string
  score: number
  status: string
  conclusion: string
}

export interface MultiAgentDebateProps {
  perspectives?: AgentPerspective[]
}

const DEFAULT_PERSPECTIVES: AgentPerspective[] = [
  {
    role: 'technical',
    agentName: '技术面 Agent',
    score: 62,
    status: '中性震荡',
    conclusion: '缩量回踩60日均线未破位',
  },
  {
    role: 'funds',
    agentName: '资金与暗盘',
    score: 48,
    status: '小幅净出',
    conclusion: '早盘主力净出1280万，未见恶性砸盘',
  },
  {
    role: 'theme',
    agentName: '板块与题材',
    score: 75,
    status: '相对活跃',
    conclusion: '稀土板块承接良好，梯队保持连板',
  },
]

/**
 * 多智能体会诊雷达组件 (SIDA Pro TradingAgents Panel)
 * 显性化呈现技术面、资金面与题材环境三大维度的独立诊断与加权证据链
 */
export const MultiAgentDebatePanel: React.FC<MultiAgentDebateProps> = ({
  perspectives = DEFAULT_PERSPECTIVES,
}) => {
  return (
    <div className="p-3 border-b border-white/[0.08] bg-[#101622] space-y-2 text-xs">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold text-slate-300 flex items-center gap-1.5">
          <Bot className="w-3.5 h-3.5 text-cyan-400" />
          多智能体会诊证据链 (3 票一致)
        </span>
        <span className="text-[10px] text-slate-500 font-mono">TradingAgents 集群</span>
      </div>
      <div className="grid grid-cols-3 gap-1.5">
        {perspectives.map((p, i) => (
          <div key={i} className="p-2 rounded bg-[#141C2A] border border-white/[0.08]">
            <div className="flex items-center gap-1 text-[10px] text-slate-400">
              {p.role === 'technical' && <LineChart className="w-3 h-3 text-amber-400" />}
              {p.role === 'funds' && <Activity className="w-3 h-3 text-rose-400" />}
              {p.role === 'theme' && <Flame className="w-3 h-3 text-emerald-400" />}
              <span>{p.agentName}</span>
            </div>
            <div
              className={`font-bold font-mono text-xs mt-0.5 ${
                p.score >= 70
                  ? 'text-emerald-300'
                  : p.score >= 50
                  ? 'text-amber-300'
                  : 'text-rose-300'
              }`}
            >
              {p.score}分 · {p.status}
            </div>
            <div className="text-[10px] text-slate-500 mt-1 truncate" title={p.conclusion}>
              {p.conclusion}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
