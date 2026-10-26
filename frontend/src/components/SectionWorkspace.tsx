import { createContext, useContext, useId, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'

export interface WorkspaceSectionItem { id: string; label: string; hint?: string; icon?: ReactNode; group?: string }
const SectionContext = createContext<{ active: string } | null>(null)

/** 分类与当前内容一一对应；导航保留在视野内，移动端以选择器节省空间。 */
export function SectionWorkspace({ items, value, onChange, label, children }: {
  items: WorkspaceSectionItem[]; value: string; onChange: (id: string) => void; label: string; children: ReactNode
}) {
  const selectId = useId()
  return <SectionContext.Provider value={{ active: value }}>
    <div className="grid min-w-0 items-start gap-5 lg:grid-cols-[208px_minmax(0,1fr)]" data-section-workspace>
      <div className="lg:hidden">
        <label htmlFor={selectId} className="mb-2 block text-[12px] font-medium text-muted-foreground">{label}</label>
        <select id={selectId} value={value} onChange={e => onChange(e.target.value)} className="h-11 w-full rounded-lg border border-input bg-card px-3 text-[14px] text-foreground">
          {items.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select>
      </div>
      <nav aria-label={label} className="card sticky top-16 hidden max-h-[calc(100dvh-6rem)] space-y-1 overflow-y-auto p-2 lg:block">
        {items.map((item, index) => <div key={item.id}>
          {item.group && item.group !== items[index - 1]?.group && <p className="px-3 pb-2 pt-3 text-[12px] font-medium text-muted-foreground">{item.group}</p>}
          <button type="button" aria-current={item.id === value ? 'page' : undefined} onClick={() => onChange(item.id)} className={`flex min-h-11 w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-[14px] transition-colors ${item.id === value ? 'bg-s3 font-semibold text-accent-foreground' : 'text-muted-foreground hover:bg-s2 hover:text-foreground'}`}>
            {item.icon}<span className="min-w-0 flex-1"><span className="block">{item.label}</span>{item.hint && <span className="mt-0.5 block text-[12px] font-normal text-muted-foreground">{item.hint}</span>}</span><ChevronRight className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          </button>
        </div>)}
      </nav>
      <div className="min-w-0 space-y-5" data-section-content>{children}</div>
    </div>
  </SectionContext.Provider>
}

export function useSectionVisible(id?: string) {
  const context = useContext(SectionContext)
  return !id || !context || context.active === id
}
export function SectionPanel({ id, children }: { id: string; children: ReactNode }) {
  return useSectionVisible(id) ? <>{children}</> : null
}

/** 与所属页签共用 URL，但不覆盖 tab 等现有参数。 */
export function useSectionRoute(items: WorkspaceSectionItem[], defaultId: string) {
  const [params, setParams] = useSearchParams()
  const requested = params.get('section')
  const selected = items.some(item => item.id === requested) ? requested! : items.find(item => item.id === defaultId)?.id ?? items[0]?.id ?? ''
  const select = (id: string) => {
    if (!items.some(item => item.id === id)) return
    const next = new URLSearchParams(params)
    next.set('section', id)
    setParams(next)
  }
  return [selected, select] as const
}
