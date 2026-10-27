import type { ReactNode } from 'react'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@sida/base-ui/components/ui/dialog'

/** 用户主动打开的上下文详情；正文独立滚动，关闭后回到原触发控件。 */
export function SidePanel({ open, onOpenChange, title, description, children, footer }: {
  open: boolean; onOpenChange: (open: boolean) => void; title: string; description?: string; children: ReactNode; footer?: ReactNode
}) {
  return <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent placement="right" className="flex flex-col overflow-hidden p-0">
      <DialogHeader className="mb-0 shrink-0 border-b border-border p-5 pr-14"><DialogTitle>{title}</DialogTitle><DialogDescription>{description || '查看详情，关闭后返回当前页面。'}</DialogDescription></DialogHeader>
      <div className="min-h-0 flex-1 overflow-y-auto p-5">{children}</div>
      {footer && <div className="shrink-0 border-t border-border bg-s2 p-4">{footer}</div>}
    </DialogContent>
  </Dialog>
}
