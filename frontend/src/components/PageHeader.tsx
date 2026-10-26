import type { ReactNode } from 'react'

/** 页面只保留一个主标题；主操作与数据时点分别在右侧和描述区。 */
export default function PageHeader({ title, description, actions, metadata }: {
  title: string
  description?: ReactNode
  metadata?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="page-header">
      <div className="min-w-0">
        <h1 className="page-title">{title}</h1>
        {description && <div className="page-description">{description}</div>}
      </div>
      {actions && <div className="card-surface flex flex-wrap items-center gap-1 rounded-lg bg-card p-1">{actions}</div>}
      {metadata && <div className="flex w-full flex-wrap items-center gap-2">{metadata}</div>}
    </header>
  )
}
