import { Button } from '@panwatch/base-ui/components/ui/button'

/**
 * 后台长列表的分页条（2026-10-24，Admin 20598px 页高治理）。
 *
 * 只做「翻页 + 报总数」两件事，不猜业务：
 *   · **总数必须由调用方给全量长度**，绝不能用「本页条数」冒充 —— 那会让「共 20 人」
 *     在翻到第 2 页时立刻穿帮。
 *   · 按钮统一 28px（后台是全站可点目标最密的页面，880 个 <32px 是主要问题）。
 */
export default function Pager({
  total,
  page,
  pageSize,
  onPage,
  unit = '条',
}: {
  total: number
  /** 1-based */
  page: number
  pageSize: number
  onPage: (page: number) => void
  unit?: string
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1
  const end = Math.min(total, page * pageSize)
  if (total <= pageSize) {
    return (
      <div className="flex items-center justify-end px-1 py-2 text-[12px] text-muted-foreground">
        共 {total} {unit}
      </div>
    )
  }
  return (
    <div className="flex items-center justify-end gap-2 px-1 py-2 text-[12px] text-muted-foreground">
      <span className="tabular-nums">
        {start}–{end} / 共 {total} {unit}
      </span>
      <Button
        variant="outline"
        size="sm"
        className="h-7 px-2 text-[12px]"
        disabled={page <= 1}
        onClick={() => onPage(page - 1)}
        data-testid="pager-prev"
      >
        上一页
      </Button>
      <span className="tabular-nums">
        {page} / {pages}
      </span>
      <Button
        variant="outline"
        size="sm"
        className="h-7 px-2 text-[12px]"
        disabled={page >= pages}
        onClick={() => onPage(page + 1)}
        data-testid="pager-next"
      >
        下一页
      </Button>
    </div>
  )
}
