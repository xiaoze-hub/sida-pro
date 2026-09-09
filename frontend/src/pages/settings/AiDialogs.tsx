import { Button } from '@panwatch/base-ui/components/ui/button'
import { CapBadges } from '@/components/settings/CapBadges'
import { Check } from 'lucide-react'
import { Dialog } from '@panwatch/base-ui/components/ui/dialog'
import { DialogContent } from '@panwatch/base-ui/components/ui/dialog'
import { DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { DialogHeader } from '@panwatch/base-ui/components/ui/dialog'
import { DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Eye } from 'lucide-react'
import { EyeOff } from 'lucide-react'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { MODEL_CAP_META } from '@/components/settings/CapBadges'
import { MODEL_CAP_ORDER } from '@/components/settings/CapBadges'
import { Pencil } from 'lucide-react'
import { Play } from 'lucide-react'
import { Plus } from 'lucide-react'
import { Radar } from 'lucide-react'
import { Select } from '@panwatch/base-ui/components/ui/select'
import { SelectContent } from '@panwatch/base-ui/components/ui/select'
import { SelectItem } from '@panwatch/base-ui/components/ui/select'
import { SelectTrigger } from '@panwatch/base-ui/components/ui/select'
import { SelectValue } from '@panwatch/base-ui/components/ui/select'
import { Star } from 'lucide-react'
import { Trash2 } from 'lucide-react'
import { useSettings } from './context'

export function AiDialogs() {
  const {
    services,
    serviceDialogOpen,
    setServiceDialogOpen,
    serviceForm,
    setServiceForm,
    editServiceId,
    serviceKeyVisible,
    setServiceKeyVisible,
    mySvcDialogOpen,
    setMySvcDialogOpen,
    mySvcForm,
    setMySvcForm,
    editMySvcId,
    mySvcKeyVisible,
    setMySvcKeyVisible,
    mySvcSaving,
    modelDialogOpen,
    setModelDialogOpen,
    modelForm,
    setModelForm,
    editModelId,
    modelsDialogOpen,
    batchOpen,
    setBatchOpen,
    batchCandidates,
    batchChecked,
    setBatchChecked,
    batchDefault,
    setBatchDefault,
    submittingBatch,
    discoveringService,
    testingModel,
    saveService,
    discoverForService,
    submitBatchModels,
    saveMySvc,
    closeModelsDialog,
    modelsSvc,
    openModelDialog,
    saveModel,
    deleteModel,
    setDefaultModel,
    testModel,
  } = useSettings()
  return (
    <>
{/* Service Dialog */}
<Dialog open={serviceDialogOpen} onOpenChange={setServiceDialogOpen}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>{editServiceId ? '编辑 AI 服务商' : '添加 AI 服务商'}</DialogTitle>
      <DialogDescription>配置 AI 服务商的 API 连接信息</DialogDescription>
    </DialogHeader>
    <div className="space-y-4 mt-2">
      <div>
        <Label>名称</Label>
        <Input
          value={serviceForm.name}
          onChange={e => setServiceForm({ ...serviceForm, name: e.target.value })}
          placeholder="如 OpenAI、智谱、DeepSeek"
         aria-label="如 OpenAI、智谱、DeepSeek"/>
      </div>
      <div>
        <Label>Base URL</Label>
        <Input
          value={serviceForm.base_url}
          onChange={e => setServiceForm({ ...serviceForm, base_url: e.target.value })}
          placeholder="https://api.openai.com/v1"
          className="font-mono"
         aria-label="https://api.openai.com/v1"/>
      </div>
      <div>
        <Label>API Key</Label>
        <div className="relative">
          <Input
            type={serviceKeyVisible ? 'text' : 'password'}
            value={serviceForm.api_key}
            onChange={e => setServiceForm({ ...serviceForm, api_key: e.target.value })}
            placeholder="sk-..."
            className="font-mono pr-10"
           aria-label="sk-..."/>
          <Button
            type="button" variant="ghost" size="icon"
            className="absolute right-1 top-1/2 -translate-y-1/2 h-8 w-8"
            onClick={() => setServiceKeyVisible(!serviceKeyVisible)}
          >
            {serviceKeyVisible ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
          </Button>
        </div>
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={() => setServiceDialogOpen(false)}>取消</Button>
        <Button onClick={saveService} disabled={!serviceForm.name || !serviceForm.base_url}>
          {editServiceId ? '保存' : '创建'}
        </Button>
      </div>
    </div>
  </DialogContent>
</Dialog>

{/* 我的服务商(BYOK) Dialog */}
<Dialog open={mySvcDialogOpen} onOpenChange={setMySvcDialogOpen}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>{editMySvcId ? '编辑我的服务商' : '添加我的服务商'}</DialogTitle>
      <DialogDescription>用自己的 API Key 配置服务商，仅你自己可见（不影响平台配置）</DialogDescription>
    </DialogHeader>
    <div className="space-y-4 mt-2">
      <div>
        <Label>名称</Label>
        <Input
          value={mySvcForm.name}
          onChange={e => setMySvcForm({ ...mySvcForm, name: e.target.value })}
          placeholder="如 OpenAI、DeepSeek"
         aria-label="如 OpenAI、DeepSeek"/>
      </div>
      <div>
        <Label>Base URL</Label>
        <Input
          value={mySvcForm.base_url}
          onChange={e => setMySvcForm({ ...mySvcForm, base_url: e.target.value })}
          placeholder="https://api.openai.com/v1"
          className="font-mono"
         aria-label="https://api.openai.com/v1"/>
      </div>
      <div>
        <Label>API Key</Label>
        <div className="relative">
          <Input
            type={mySvcKeyVisible ? 'text' : 'password'}
            value={mySvcForm.api_key}
            onChange={e => setMySvcForm({ ...mySvcForm, api_key: e.target.value })}
            placeholder={editMySvcId ? '留空或保持 ******** 则不修改' : 'sk-...'}
            className="font-mono pr-10"
          />
          <Button
            type="button" variant="ghost" size="icon"
            className="absolute right-1 top-1/2 -translate-y-1/2 h-8 w-8"
            onClick={() => setMySvcKeyVisible(!mySvcKeyVisible)}
          >
            {mySvcKeyVisible ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
          </Button>
        </div>
      </div>
      <div className="rounded-md border border-border/50 bg-accent/20 p-3 space-y-3">
        <p className="text-[11px] font-medium text-foreground">模型（单模型简化配置，可留空稍后补充）</p>
        <div>
          <Label>模型标识</Label>
          <Input
            value={mySvcForm.model}
            onChange={e => setMySvcForm({ ...mySvcForm, model: e.target.value })}
            placeholder="gpt-4o / deepseek-chat"
            className="font-mono"
           aria-label="gpt-4o / deepseek-chat"/>
        </div>
        <div>
          <Label>显示名称 <span className="text-muted-foreground font-normal">(选填，默认同模型标识)</span></Label>
          <Input
            value={mySvcForm.model_name}
            onChange={e => setMySvcForm({ ...mySvcForm, model_name: e.target.value })}
            placeholder="不填则使用模型标识"
           aria-label="不填则使用模型标识"/>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <Label>使用场景</Label>
            <Select value={mySvcForm.scene} onValueChange={v => setMySvcForm({ ...mySvcForm, scene: v })}>
              <SelectTrigger className="h-9 text-[12px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="chat">日常对话</SelectItem>
                <SelectItem value="trading_agents">深度分析</SelectItem>
                <SelectItem value="reports">报告生成</SelectItem>
                <SelectItem value="referee">AI 裁判</SelectItem>
                <SelectItem value="selfcheck">自检</SelectItem>
                <SelectItem value="insights">机会评分</SelectItem>
                <SelectItem value="vision">视觉(图片识别)</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div>
            <Label>能力</Label>
            <div className="flex flex-wrap items-center gap-1.5 pt-1">
              {(['chat', 'vision', 'tools'] as const).map(cap => (
                <button
                  key={cap}
                  type="button"
                  onClick={() => setMySvcForm({
                    ...mySvcForm,
                    capabilities: mySvcForm.capabilities.includes(cap)
                      ? mySvcForm.capabilities.filter(c => c !== cap)
                      : [...mySvcForm.capabilities, cap],
                  })}
                  className={`px-2 py-1 rounded-md text-[11px] border transition-colors ${
                    mySvcForm.capabilities.includes(cap)
                      ? 'border-primary/40 bg-primary/10 text-primary'
                      : 'border-border/60 bg-background/60 text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {cap === 'chat' ? '对话' : cap === 'vision' ? '视觉' : '工具'}
                </button>
              ))}
            </div>
          </div>
        </div>
        <label className="flex items-center gap-2 text-[12px] text-muted-foreground cursor-pointer select-none">
          <input
            type="checkbox"
            checked={mySvcForm.is_default}
            onChange={e => setMySvcForm({ ...mySvcForm, is_default: e.target.checked })}
            className="accent-primary"
          />
          设为默认模型
        </label>
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={() => setMySvcDialogOpen(false)}>取消</Button>
        <Button onClick={saveMySvc} disabled={!mySvcForm.name.trim() || !mySvcForm.base_url.trim() || mySvcSaving}>
          {mySvcSaving ? '保存中…' : editMySvcId ? '保存' : '创建'}
        </Button>
      </div>
    </div>
  </DialogContent>
</Dialog>

{/* Model Dialog */}
<Dialog open={modelDialogOpen} onOpenChange={setModelDialogOpen}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>{editModelId ? '编辑模型' : '添加模型'}</DialogTitle>
      <DialogDescription>配置 AI 模型</DialogDescription>
    </DialogHeader>
    <div className="space-y-4 mt-2">
      <div>
        <Label>所属服务商</Label>
        <Select
          value={modelForm.service_id?.toString() ?? ''}
          onValueChange={val => setModelForm({ ...modelForm, service_id: val ? parseInt(val) : null })}
        >
          <SelectTrigger>
            <SelectValue placeholder="选择服务商" />
          </SelectTrigger>
          <SelectContent>
            {services.map(s => (
              <SelectItem key={s.id} value={s.id.toString()}>{s.name}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div>
        <Label>显示名称 <span className="text-muted-foreground font-normal">(选填，默认同模型标识)</span></Label>
        <Input
          value={modelForm.name}
          onChange={e => setModelForm({ ...modelForm, name: e.target.value })}
          placeholder="不填则使用模型标识"
         aria-label="不填则使用模型标识"/>
      </div>
      <div>
        <Label>模型标识 <span className="text-muted-foreground font-normal">(可用服务商上的「嗅探」批量发现)</span></Label>
        <Input
          value={modelForm.model}
          disabled={!modelForm.service_id}
          onChange={e => setModelForm({ ...modelForm, model: e.target.value })}
          placeholder={modelForm.service_id ? 'gpt-4o / glm-4-flash' : '请先选择服务商'}
          className="font-mono"
        />
      </div>
      <div>
        <Label>功能 <span className="text-muted-foreground font-normal">(多选，决定模型可用于哪些场景)</span></Label>
        <div className="flex flex-wrap gap-1.5">
          {MODEL_CAP_ORDER.map(cap => {
            const meta = MODEL_CAP_META[cap]
            const checked = modelForm.capabilities.includes(cap)
            return (
              <button
                key={cap}
                type="button"
                onClick={() => setModelForm({
                  ...modelForm,
                  capabilities: checked
                    ? modelForm.capabilities.filter(c => c !== cap)
                    : [...modelForm.capabilities, cap],
                })}
                className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] transition-colors ${
                  checked
                    ? `${meta.badge} border-transparent`
                    : 'border-border/60 bg-background/60 text-muted-foreground hover:text-foreground'
                }`}
              >
                {checked && <Check className="h-3 w-3" strokeWidth={3} />}
                {meta.label}
              </button>
            )
          })}
        </div>
      </div>
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={() => setModelDialogOpen(false)}>取消</Button>
        <Button onClick={saveModel} disabled={!modelForm.model || !modelForm.service_id}>
          {editModelId ? '保存' : '创建'}
        </Button>
      </div>
    </div>
  </DialogContent>
</Dialog>

{/* 模型管理 Dialog(第二窗口): 某服务商下的模型列表 + 添加/嗅探/删除 */}
<Dialog open={modelsDialogOpen} onOpenChange={open => { if (!open) closeModelsDialog() }}>
  <DialogContent className="max-w-2xl">
    {modelsSvc && (
      <>
        <DialogHeader>
          <DialogTitle>管理模型 · {modelsSvc.name}</DialogTitle>
          <DialogDescription className="font-mono text-[11px]">{modelsSvc.base_url}</DialogDescription>
        </DialogHeader>
        <div className="mb-3 flex items-center justify-between gap-2">
          <span className="text-[11px] text-muted-foreground">
            共 <span className="font-mono text-foreground">{modelsSvc.models.length}</span> 个模型
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="secondary" size="sm" className="h-8 text-[12px]"
              disabled={discoveringService === modelsSvc.id}
              onClick={() => discoverForService(modelsSvc.id)}
            >
              <Radar className={`w-3.5 h-3.5 ${discoveringService === modelsSvc.id ? 'animate-pulse' : ''}`} />
              嗅探
            </Button>
            <Button size="sm" className="h-8 text-[12px]" onClick={() => openModelDialog(modelsSvc.id)}>
              <Plus className="w-3.5 h-3.5" /> 添加模型
            </Button>
          </div>
        </div>
        {modelsSvc.models.length === 0 ? (
          <p className="py-8 text-center text-[12px] text-muted-foreground">
            暂无模型，点击「添加模型」或「嗅探」自动发现
          </p>
        ) : (
          <div className="max-h-[55vh] space-y-1.5 overflow-y-auto scrollbar pr-1">
            {modelsSvc.models.map(m => (
              <div key={m.id} className="flex items-center justify-between gap-2 rounded-md border border-border/50 bg-background/60 px-3 py-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5">
                    {m.is_default && <Star className="w-3 h-3 text-amber-700 dark:text-amber-500 flex-shrink-0" />}
                    <span className="text-[12px] font-medium text-foreground truncate">{m.name}</span>
                    <CapBadges caps={m.capabilities} />
                  </div>
                  <p className="text-[10px] text-muted-foreground font-mono truncate">{m.model}</p>
                </div>
                <div className="flex items-center gap-0.5 flex-shrink-0">
                  <Button
                    variant="ghost" size="icon" className="h-6 w-6"
                    onClick={() => testModel(m.id)}
                    disabled={testingModel === m.id}
                    title="测试模型"
                  >
                    {testingModel === m.id ? (
                      <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                    ) : (
                      <Play className="w-3 h-3" />
                    )}
                  </Button>
                  {!m.is_default && (
                    <Button variant="ghost" size="icon" className="h-6 w-6" onClick={() => setDefaultModel(m.id)} title="设为默认">
                      <Star className="w-3 h-3" />
                    </Button>
                  )}
                  <Button variant="ghost" size="icon" className="h-6 w-6" onClick={() => openModelDialog(modelsSvc.id, m)} title="编辑模型">
                    <Pencil className="w-3 h-3" />
                  </Button>
                  <Button variant="ghost" size="icon" className="h-6 w-6 hover:text-destructive" onClick={() => deleteModel(m.id)} title="删除模型">
                    <Trash2 className="w-3 h-3" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </>
    )}
  </DialogContent>
</Dialog>

{/* 批量选择嗅探到的模型 */}
<Dialog open={batchOpen} onOpenChange={setBatchOpen}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>发现 {batchCandidates.length} 个模型</DialogTitle>
      <DialogDescription>勾选要添加的模型，并可指定一个默认模型</DialogDescription>
    </DialogHeader>
    <div className="mt-3 flex items-center justify-between px-0.5 text-xs text-muted-foreground">
      <span>已选 <span className="font-mono text-foreground">{batchChecked.size}</span> / {batchCandidates.length}</span>
      <button
        type="button"
        className="hover:text-foreground"
        onClick={() => setBatchChecked(
          batchChecked.size === batchCandidates.length ? new Set() : new Set(batchCandidates),
        )}
      >
        {batchChecked.size === batchCandidates.length ? '取消全选' : '全选'}
      </button>
    </div>
    <div className="mt-1.5 max-h-80 space-y-1.5 overflow-y-auto scrollbar pr-1">
      {batchCandidates.map(id => {
        const checked = batchChecked.has(id)
        const isDefault = batchDefault === id
        return (
          <div
            key={id}
            onClick={() => {
              const next = new Set(batchChecked)
              if (checked) { next.delete(id); if (isDefault) setBatchDefault('') }
              else next.add(id)
              setBatchChecked(next)
            }}
            className={`flex cursor-pointer items-center justify-between gap-3 rounded-md border px-3 py-2.5 transition-colors ${
              checked ? 'border-primary/60 bg-primary/10' : 'border-border/50 hover:border-border hover:bg-muted/40'
            }`}
          >
            <div className="flex min-w-0 items-center gap-2.5">
              <span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${
                checked ? 'border-primary bg-primary text-primary-foreground' : 'border-muted-foreground/40'
              }`}>
                {checked && <Check className="h-3 w-3" strokeWidth={3} />}
              </span>
              <span className="truncate font-mono text-sm">{id}</span>
            </div>
            <button
              type="button"
              onClick={e => {
                e.stopPropagation()
                if (isDefault) { setBatchDefault('') }
                else {
                  setBatchDefault(id)
                  if (!checked) { const next = new Set(batchChecked); next.add(id); setBatchChecked(next) }
                }
              }}
              className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[11px] transition-colors ${
                isDefault ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted hover:text-foreground'
              }`}
            >
              <Star className={`h-3 w-3 ${isDefault ? 'fill-current' : ''}`} />
              {isDefault ? '默认' : '设默认'}
            </button>
          </div>
        )
      })}
    </div>
    <div className="flex justify-end gap-2 pt-2">
      <Button variant="ghost" onClick={() => setBatchOpen(false)}>跳过</Button>
      <Button onClick={submitBatchModels} disabled={batchChecked.size === 0 || submittingBatch}>
        {submittingBatch ? '添加中…' : `添加 ${batchChecked.size} 个`}
      </Button>
    </div>
  </DialogContent>
</Dialog>

    </>
  )
}
