/**
 * 提案审核台（独立页，2026-10-25）。
 *
 * 侧栏 320px 里只能看个大概：提案的价值判断要看「同一个维度被提过几次、理由是什么、
 * 现有工具能不能覆盖」，这些在窄栏里会被折叠掉。本页把 `ProposalPanel` 放在整幅宽度上，
 * 逻辑一个字不改 —— 采集层仍然是「只记录、人工审核、采纳 ≠ 生效」。
 *
 * 为什么是薄壳而不是重写：`ProposalPanel` + `lib/copilot/proposals.ts` 已有 5 例单测钉住
 * 排序/空值/诚信文案，复制一套逻辑必然分叉。
 */
import { ProposalPanel } from '@/components/copilot/ProposalPanel'

export default function ProposalsPage() {
  return (
    <div className="mx-auto flex w-full max-w-[1100px] flex-col gap-4 p-4">
      <header>
        <h1 className="text-[20px] font-semibold text-foreground">能力提案</h1>
        <p className="mt-1 text-[12px] text-muted-foreground">
          参谋在对话里发现配方没覆盖的分析维度时，会记成提案等你裁决。
          <span className="text-foreground">采纳只改状态，不改配方</span>
          ——配方仍要人工改 YAML 再发版，所以采纳后的能力不会立刻出现在回答里。
        </p>
      </header>
      <ProposalPanel />
    </div>
  )
}
