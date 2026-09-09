import { Button } from '@panwatch/base-ui/components/ui/button'
import { CapBadges } from '@/components/settings/CapBadges'
import { Cpu } from 'lucide-react'
import { Pencil } from 'lucide-react'
import { Plus } from 'lucide-react'
import { Select } from '@panwatch/base-ui/components/ui/select'
import { SelectContent } from '@panwatch/base-ui/components/ui/select'
import { SelectItem } from '@panwatch/base-ui/components/ui/select'
import { SelectTrigger } from '@panwatch/base-ui/components/ui/select'
import { SelectValue } from '@panwatch/base-ui/components/ui/select'
import { Trash2 } from 'lucide-react'
import { useSettings } from './context'

export function AiSection() {
  const {
    services,
    sectionMatches,
    sceneBindings,
    sceneBindingsLoading,
    bindingSaving,
    SCENE_DEFAULT_VALUE,
    handleSceneChange,
    sceneOptionsFor,
    isOwner,
    openServiceDialog,
    deleteService,
    openModelsDialog,
  } = useSettings()
  return (
    <>
  {/* AI Services + Models Section */}
  {isOwner && (
  <section id="sec-ai" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-7" style={{ display: sectionMatches('sec-ai') ? undefined : 'none' }}>
    <div className="flex items-start justify-between mb-4 md:mb-5 gap-3">
      <div>
        <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">AI 服务商 & 模型</h3>
        <p className="text-[11px] text-muted-foreground mt-1">连接你的 AI 服务并设置默认模型</p>
      </div>
      <Button size="sm" className="h-8" onClick={() => openServiceDialog()}>
        <Plus className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">添加服务商</span>
      </Button>
    </div>
    {services.length === 0 ? (
      <p className="text-[13px] text-muted-foreground text-center py-6">暂无 AI 服务商，点击"添加服务商"创建</p>
    ) : (
      <div className="space-y-2">
        {services.map(svc => (
          <div key={svc.id} className="flex items-center justify-between gap-3 border-b border-border/40 px-1 py-2.5">
            <div className="min-w-0 flex items-center gap-2.5">
              <Cpu className="w-3.5 h-3.5 text-primary flex-shrink-0" />
              <div className="min-w-0">
                <span className="text-[12px] font-medium text-foreground">{svc.name}</span>
                <p className="text-[10px] text-muted-foreground truncate font-mono">{svc.base_url}</p>
              </div>
            </div>
            <div className="flex items-center gap-1.5 flex-shrink-0">
              <span className="inline-flex items-center rounded-full border border-border/60 bg-background/60 px-2 py-0.5 text-[10px] text-muted-foreground">
                {svc.models.length} 模型
              </span>
              <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] ${
                svc.api_key
                  ? 'border-emerald-700/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                  : 'border-amber-700/25 bg-amber-500/10 text-amber-700 dark:text-amber-400'
              }`}>
                {svc.api_key ? '已启用' : '未配置 Key'}
              </span>
              <Button size="sm" variant="secondary" className="h-7 px-2 text-[11px]" onClick={() => openModelsDialog(svc)}>
                管理
              </Button>
              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => openServiceDialog(svc)} title="编辑服务商">
                <Pencil className="w-3.5 h-3.5" />
              </Button>
              <Button variant="ghost" size="icon" className="h-7 w-7 hover:text-destructive" onClick={() => deleteService(svc.id)} title="删除服务商">
                <Trash2 className="w-3.5 h-3.5" />
              </Button>
            </div>
          </div>
        ))}
      </div>
    )}

    {/* 场景分配(统一 LLM 配置中心): 每个使用点绑定模型池里的模型 */}
    <div className="mt-5 pt-4 border-t">
      <div className="mb-3">
        <h4 className="text-[12px] font-semibold text-foreground">场景分配</h4>
        <p className="text-[11px] text-muted-foreground mt-0.5">各 AI 使用点绑定的模型，未绑定则使用默认模型</p>
      </div>
      {sceneBindingsLoading ? (
        <p className="text-[11px] text-muted-foreground text-center py-3">加载中...</p>
      ) : sceneBindings.length === 0 ? (
        <p className="text-[11px] text-muted-foreground text-center py-3">暂无场景数据</p>
      ) : (
        <div className="space-y-2">
          {sceneBindings.map(b => (
            <div key={b.scene} className="flex items-center justify-between gap-3 border-b border-border/40 px-1 py-2">
              <div className="min-w-0">
                <span className="text-[12px] font-semibold text-foreground">{b.display_name}</span>
                <p className="text-[11px] text-muted-foreground mt-0.5 truncate">{b.description}</p>
              </div>
              <div className="flex-shrink-0 w-[220px] sm:w-[280px]">
                {b.scene === 'vision' && (
                  <p className="mb-1 text-[10px] text-sky-700/90 dark:text-sky-400/90">优先选择含视觉(vision)能力的模型</p>
                )}
                <Select
                  value={b.model_id != null ? String(b.model_id) : SCENE_DEFAULT_VALUE}
                  onValueChange={v => handleSceneChange(b.scene, v)}
                  disabled={bindingSaving === b.scene}
                >
                  <SelectTrigger className="h-8 text-[12px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={SCENE_DEFAULT_VALUE}>默认模型</SelectItem>
                    {sceneOptionsFor(b.scene).map(opt => (
                      <SelectItem key={opt.id} value={String(opt.id)}>
                        <span className="flex items-center gap-1.5">
                          <span className="truncate">{opt.label}</span>
                          <CapBadges caps={opt.caps} />
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  </section>
  )}
    </>
  )
}
