import { Button } from '@panwatch/base-ui/components/ui/button'
import { Cpu } from 'lucide-react'
import { Pencil } from 'lucide-react'
import { Plus } from 'lucide-react'
import { Trash2 } from 'lucide-react'
import { useSettings } from './context'

export function MyServicesSection() {
  const {
    sectionMatches,
    myServices,
    myServicesLoading,
    isDemo,
    openMySvcDialog,
    deleteMySvc,
  } = useSettings()
  return (
    <>
  {/* 我的服务商(BYOK): 用户自定义 LLM 服务商, 用自己的 API Key(2026-08-15) */}
  <section id="sec-my-services" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-12" style={{ display: sectionMatches('sec-my-services') ? undefined : 'none' }}>
    <div className="flex items-start justify-between mb-4 md:mb-5 gap-3">
      <div>
        <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">我的服务商 (BYOK)</h3>
        <p className="text-[11px] text-muted-foreground mt-1">
          配置你自己的模型服务商，使用你自己的 API Key 调用（不影响平台配置）。demo 账号不可用。
        </p>
      </div>
      {!isDemo && (
        <Button size="sm" className="h-8" onClick={() => openMySvcDialog()}>
          <Plus className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">添加我的服务商</span>
        </Button>
      )}
    </div>

    {isDemo ? (
      <div className="rounded-md border border-border/50 bg-accent/20 p-3.5 text-[12px] text-muted-foreground">
        演示账号为只读浏览模式，不可配置自己的服务商。
      </div>
    ) : myServicesLoading ? (
      <p className="text-[13px] text-muted-foreground text-center py-6">加载中...</p>
    ) : myServices.length === 0 ? (
      <p className="text-[13px] text-muted-foreground text-center py-6">暂无我的服务商，点击"添加我的服务商"创建</p>
    ) : (
      <div className="space-y-2">
        {myServices.map(svc => (
          <div key={svc.id} className="flex items-center justify-between gap-3 border-b border-border/40 px-1 py-2.5">
            <div className="min-w-0 flex items-center gap-2.5">
              <Cpu className="w-3.5 h-3.5 text-primary flex-shrink-0" />
              <div className="min-w-0">
                <span className="text-[12px] font-medium text-foreground">{svc.name}</span>
                <p className="text-[10px] text-muted-foreground truncate font-mono">{svc.base_url}</p>
              </div>
            </div>
            <div className="flex items-center gap-1.5 flex-shrink-0">
              <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] ${
                svc.api_key
                  ? 'border-emerald-700/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                  : 'border-amber-700/25 bg-amber-500/10 text-amber-700 dark:text-amber-400'
              }`}>
                {svc.api_key ? '********' : '未配置 Key'}
              </span>
              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openMySvcDialog(svc)} title="编辑我的服务商">
                <Pencil className="w-3.5 h-3.5" />
              </Button>
              <Button variant="ghost" size="icon" className="h-7 w-7 hover:text-destructive" onClick={() => deleteMySvc(svc.id)} title="删除我的服务商">
                <Trash2 className="w-3.5 h-3.5" />
              </Button>
            </div>
          </div>
        ))}
      </div>
    )}
  </section>
    </>
  )
}
