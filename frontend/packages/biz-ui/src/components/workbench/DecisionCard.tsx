import DecisionPioneerCard from '@panwatch/biz-ui/components/DecisionPioneerCard'
import ResonanceVerdictPanel from '@panwatch/biz-ui/components/ResonanceVerdictPanel'

/**
 * 「数智决策」合并卡(工作台 v2 三合一, spec §4.2 / 去重表第 1 项)。
 *
 * 去重点: `decision_indicators`(三指标读数)与 `resonance_verdict`(规则三灯 + AI 判定)
 * 在右栏同归 `rail.decision` —— 全工作台**只此一处**渲染, 旧行情页/旧详情模态的同源块删除。
 *
 * 纯组合, 零新增取数: 上半 `DecisionPioneerCard`(GET /decision-pioneer/{s}, 30s 轮询),
 * 下半 `ResonanceVerdictPanel`(GET /resonance/symbol/{s} 挂载即取 + 「AI 分析」按需 POST)。
 * 两个子组件的内部实现与各自轮询/缓存策略一律复用，不在本卡重写、不重复请求。
 */

export default function DecisionCard({ symbol, market }: { symbol: string; market: string }) {
  return (
    <div className="rounded border border-border/60 p-2">
      <div className="text-[12px] font-semibold">数智决策</div>

      {/* 上半: 三指标读数(GS策略 × AI机构活跃度 × L2主力净流入) */}
      <section>
        <div className="text-[11px] text-muted-foreground">三指标读数</div>
        <DecisionPioneerCard symbol={symbol} market={market} />
      </section>

      {/* 细分隔线: 读数 vs 判定 */}
      <div className="my-2 border-t border-border/40" />

      {/* 下半: 共振判定(规则三灯 + AI 分析) */}
      <section>
        <div className="text-[11px] text-muted-foreground">共振判定</div>
        {/* mt-3 与上半 DecisionPioneerCard 自带的 mt-3 对齐, 两段标题→内容的间距一致 */}
        <div className="mt-3">
          <ResonanceVerdictPanel symbol={symbol} />
        </div>
      </section>
    </div>
  )
}
