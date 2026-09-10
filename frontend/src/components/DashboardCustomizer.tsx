import { ChevronDown, ChevronUp, RotateCcw } from 'lucide-react'

import { Button } from '@panwatch/base-ui/components/ui/button'
import {
  Dialog,
  DialogContent,
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
      <DialogContent className="max-w-[460px]">
        <DialogHeader>
          <DialogTitle>自定义看板</DialogTitle>
          <DialogDescription>显隐与排序偏好保存在本机浏览器; 排序在分区内生效</DialogDescription>
        </DialogHeader>
        <div className="mt-2 max-h-[60vh] space-y-4 overflow-y-auto pr-1">
          {GROUP_ORDER.map((g) => {
            const mods = DASHBOARD_MODULES.filter((m) => m.group === g).sort(
              (a, b) => orderIndex(layout, a.id) - orderIndex(layout, b.id),
            )
            return (
              <section key={g} data-group={g}>
                <div className="mb-1.5 text-[11px] font-semibold text-muted-foreground">
                  {DASHBOARD_GROUP_LABEL[g]}
                </div>
                <div className="space-y-1">
                  {mods.map((m, i) => {
                    const hidden = isHidden(layout, m.id)
                    return (
                      <div
                        key={m.id}
                        data-module={m.id}
                        className="flex items-center gap-2 rounded-md border border-border/40 px-2.5 py-1.5"
                      >
                        <Switch
                          checked={!hidden}
                          onCheckedChange={() => onToggle(m.id)}
                          aria-label={`显示 ${m.label}`}
                        />
                        <span
                          className={`flex-1 text-[12px] ${
                            hidden ? 'text-muted-foreground/60 line-through' : 'text-foreground/90'
                          }`}
                        >
                          {m.label}
                        </span>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6"
                          disabled={i === 0}
                          onClick={() => onMove(m.id, -1)}
                          title="上移"
                        >
                          <ChevronUp className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6"
                          disabled={i === mods.length - 1}
                          onClick={() => onMove(m.id, 1)}
                          title="下移"
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
        <div className="mt-3 flex items-center justify-between">
          <span className="text-[11px] text-muted-foreground">全部隐藏也允许——空看板比假数据诚实</span>
          <Button variant="outline" size="sm" className="h-8 text-[12px]" onClick={onReset}>
            <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
            重置
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default DashboardCustomizer
