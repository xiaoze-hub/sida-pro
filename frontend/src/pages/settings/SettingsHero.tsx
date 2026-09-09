import { Button } from '@panwatch/base-ui/components/ui/button'
import { Cpu } from 'lucide-react'
import { Download } from 'lucide-react'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Search } from 'lucide-react'
import { Upload } from 'lucide-react'
import { User } from 'lucide-react'
import { useSettings } from './context'

export function SettingsHero() {
  const {
    services,
    channels,
    globalQuery,
    setGlobalQuery,
    trimmedGlobal,
    hasGlobalQuery,
    matchCount,
    avatar,
    avatarFileRef,
    avatarSaving,
    exporting,
    exportTemplate,
    allModels,
    defaultModel,
    defaultChannel,
    enabledChannels,
    isOwner,
    jumpItems,
    scrollTo,
    onPickAvatar,
  } = useSettings()
  return (
    <>
{/* Hero */}
<div className="relative overflow-hidden border-b border-border/40 p-5 md:p-7">
  <div className="pointer-events-none absolute inset-0 bg-gradient-to-br from-primary/10 via-transparent to-accent/30" />
  <div className="relative flex flex-col md:flex-row md:items-end md:justify-between gap-4">
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        <input ref={avatarFileRef} type="file" accept="image/*" className="hidden" onChange={onPickAvatar} />
        <button
          type="button"
          onClick={() => avatarFileRef.current?.click()}
          disabled={avatarSaving}
          title="点击上传头像"
          className="group relative h-9 w-9 rounded-full overflow-hidden bg-gradient-to-br from-primary to-primary/70 text-white shadow-sm flex items-center justify-center ring-1 ring-border/40 hover:ring-primary/40 transition-shadow shrink-0"
        >
          {avatar ? (
            <img src={avatar} alt="头像" className="w-full h-full object-cover" />
          ) : (
            <User className="w-4 h-4" />
          )}
          <span className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 group-hover:opacity-100 transition-opacity">
            <Upload className="w-3.5 h-3.5 text-white" />
          </span>
        </button>
        <span className="mx-1 hidden h-4 w-px bg-border/50 sm:block" />
        {isOwner && (
          <>
            <div className="px-2.5 py-1 rounded-full bg-background/70 border border-border/50 text-[11px] text-muted-foreground">
              <span className="font-mono text-foreground/90">{services.length}</span> 服务商
            </div>
            <div className="px-2.5 py-1 rounded-full bg-background/70 border border-border/50 text-[11px] text-muted-foreground">
              <span className="font-mono text-foreground/90">{allModels.length}</span> 模型
            </div>
          </>
        )}
        <div className="px-2.5 py-1 rounded-full bg-background/70 border border-border/50 text-[11px] text-muted-foreground">
          <span className="font-mono text-foreground/90">{enabledChannels.length}</span>/<span className="font-mono">{channels.length}</span> 渠道启用
        </div>
        {isOwner && defaultModel ? (
          <div className="px-2.5 py-1 rounded-full bg-background/70 border border-border/50 text-[11px] text-muted-foreground">
            默认模型 <span className="font-mono text-foreground/90">{defaultModel.model}</span>
          </div>
        ) : null}
        {defaultChannel ? (
          <div className="px-2.5 py-1 rounded-full bg-background/70 border border-border/50 text-[11px] text-muted-foreground">
            默认通知 <span className="text-foreground/90">{defaultChannel.name}</span>
          </div>
        ) : null}
      </div>
    </div>

    {isOwner && (
      <div className="flex flex-col sm:flex-row gap-2">
        <Button variant="secondary" size="sm" className="h-9" onClick={exportTemplate} disabled={exporting}>
          <Download className="w-3.5 h-3.5" /> 导出配置包
        </Button>
        <Button size="sm" className="h-9" onClick={() => scrollTo('sec-ai')}>
          <Cpu className="w-3.5 h-3.5" /> 配置 AI
        </Button>
      </div>
    )}
  </div>

  {/* Global Search: 2026-08-17 跨 section 搜索 — 同时跳到目标并自动展开 */}
  <div className="relative mt-4">
    <Input
      value={globalQuery}
      onChange={e => setGlobalQuery(e.target.value)}
      placeholder="全局搜索设置项 / 数据源 / AI 服务..."
      className="h-9 w-full md:max-w-md pl-9"
     aria-label="全局搜索设置项 / 数据源 / AI 服务..."/>
    <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground pointer-events-none" />
    {/* 2026-08-17: end 添加搜索图标与清空按钮 */}
    {globalQuery.trim() && (
      <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-1">
        <button
          type="button"
          onClick={() => setGlobalQuery('')}
          className="text-[11px] text-muted-foreground hover:text-foreground transition-colors"
        >
          清空
        </button>
      </div>
    )}
  </div>

  {/* Jump pills */}
  <div className="relative mt-4 flex flex-wrap gap-2">
    {jumpItems.map(it => (
      <button
        key={it.id}
        onClick={() => scrollTo(it.id)}
        className="group flex items-center gap-2 rounded-full border border-border/50 bg-background/70 px-3 py-1.5 text-[11px] text-muted-foreground hover:text-foreground hover:border-primary/30 transition-colors"
      >
        <span className="font-medium text-foreground/90 group-hover:text-foreground">{it.label}</span>
        {it.hint ? <span className="opacity-60">{it.hint}</span> : null}
      </button>
    ))}
    {/* 2026-08-17: 清空搜索 快捷按钮 */}
    {trimmedGlobal && (
      <div className="ml-auto flex items-center gap-2 text-[11px]">
        <button
          type="button"
          onClick={() => setGlobalQuery('')}
          className="text-muted-foreground hover:text-foreground transition-colors"
        >
          清空搜索 ×
        </button>
        {hasGlobalQuery && (
          <span className="ml-1 text-[11px] text-muted-foreground/70">
            · {matchCount} 个区块匹配
          </span>
        )}
      </div>
    )}
  </div>
</div>
    </>
  )
}
