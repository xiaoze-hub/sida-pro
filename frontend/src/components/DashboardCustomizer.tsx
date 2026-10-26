import { ChevronDown, ChevronUp, RotateCcw } from 'lucide-react'

import { Button } from '@panwatch/base-ui/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogClose,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@panwatch/base-ui/components/ui/dialog'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import {
  DASHBOARD_GROUP_LABEL,
  DASHBOARD_MODULES,
  isHidden,
  orderIndex,
  type DashboardGroup,
  type DashboardLayout,
  type DashboardModuleId,
} from '@/lib/dashboard-layout'

/**
 * 看板轻量定制 (A1, 2026-09-10): 模块显隐 + 分区内上下排序, 偏好存本机浏览器。
 * 不做自由拖拽(设计语言冲突, 方案 §三 A1 轻量版口径)。
 */
const GROUP_ORDER: DashboardGroup[] = ['main', 'duo', 'workspace']

export interface DashboardCustomizerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  layout: DashboardLayout
  onToggle: (id: DashboardModuleId) => void
  onMove: (id: DashboardModuleId, dir: -1 | 1) => void
  onReset: () => void
}

export function DashboardCustomizer({
  open,
  onOpenChange,
  layout,
  onToggle,
  onMove,
  onReset,
}: DashboardCustomizerProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[min(840px,calc(100dvh-2rem))] md:max-h-[min(840px,calc(100dvh-2rem))] max-w-[520px] flex-col overflow-hidden p-0">
        <DialogHeader className="mb-0 shrink-0 border-b border-border px-5 py-4 pr-14">
          <DialogTitle>自定义看板</DialogTitle>
          <DialogDescription>选择显示的模块；用上下箭头调整分区内的顺序。修改即时生效。</DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4" data-testid="dashboard-customizer-list">
          {GROUP_ORDER.map((g) => {
            const mods = DASHBOARD_MODULES.filter((m) => m.group === g).sort(
              (a, b) => orderIndex(layout, a.id) - orderIndex(layout, b.id),
            )
            return (
              <section key={g} data-group={g}>
                <div className="mb-1.5 text-[12px] font-semibold text-muted-foreground">
                  {DASHBOARD_GROUP_LABEL[g]}
                </div>
                <div className="space-y-1">
                  {mods.map((m, i) => {
                    const hidden = isHidden(layout, m.id)
                    return (
                      <div
                        key={m.id}
                        data-module={m.id}
                        className="flex min-h-12 items-center gap-3 rounded-lg bg-s2 px-3 py-2"
                      >
                        <Switch
                          checked={!hidden}
                          onCheckedChange={() => onToggle(m.id)}
                          aria-label={`显示 ${m.label}`}
                        />
                        <span
                          className={`flex-1 text-[14px] ${
                            hidden ? 'text-muted-foreground line-through' : 'text-foreground'
                          }`}
                        >
                          {m.label}
                        </span>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-9 w-9"
                          disabled={i === 0}
                          onClick={() => onMove(m.id, -1)}
                          title="上移"
                          aria-label={`上移 ${m.label}`}
                        >
                          <ChevronUp className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-9 w-9"
                          disabled={i === mods.length - 1}
                          onClick={() => onMove(m.id, 1)}
                          title="下移"
                          aria-label={`下移 ${m.label}`}
                        >
                          <ChevronDown className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    )
                  })}
                </div>
              </section>
            )
          })}
        </div>
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-border bg-s2 px-5 py-4" data-testid="dashboard-customizer-footer">
          <span className="text-[12px] text-muted-foreground">偏好保存在当前浏览器</span>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={onReset}><RotateCcw className="mr-1.5 h-3.5 w-3.5" />重置</Button>
            <DialogClose asChild><Button size="sm">完成</Button></DialogClose>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default DashboardCustomizer
