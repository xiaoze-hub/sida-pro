import { Button } from '@panwatch/base-ui/components/ui/button'
import { Download } from 'lucide-react'
import { FileJson } from 'lucide-react'
import { Select } from '@panwatch/base-ui/components/ui/select'
import { SelectContent } from '@panwatch/base-ui/components/ui/select'
import { SelectItem } from '@panwatch/base-ui/components/ui/select'
import { SelectTrigger } from '@panwatch/base-ui/components/ui/select'
import { SelectValue } from '@panwatch/base-ui/components/ui/select'
import { Upload } from 'lucide-react'
import { useSettings } from './context'

export function PackSection() {
  const {
    sectionMatches,
    importMode,
    setImportMode,
    importing,
    exporting,
    importFileRef,
    toast,
    builtinTemplates,
    exportTemplate,
    importTemplate,
    isOwner,
  } = useSettings()
  return (
    <>
  {/* Config Pack (Templates) */}
  {isOwner && (
  <section id="sec-pack" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-7" style={{ display: sectionMatches('sec-pack') ? undefined : 'none' }}>
    <div className="flex items-start justify-between mb-4 gap-3">
      <div>
        <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">配置包</h3>
        <p className="text-[11px] text-muted-foreground mt-1">一键导入/导出 Agent、关注列表与系统设置</p>
      </div>
      <div className="flex items-center gap-2">
        <Button variant="secondary" size="sm" className="h-8" onClick={exportTemplate} disabled={exporting}>
          <Download className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">导出</span>
        </Button>
        <Button
          variant="secondary"
          size="sm"
          className="h-8"
          onClick={() => importFileRef.current?.click()}
          disabled={importing}
        >
          <Upload className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">导入</span>
        </Button>
      </div>
    </div>

    <div className="flex items-center gap-2 mb-4">
      <div className="text-[11px] text-muted-foreground">导入模式</div>
      <Select value={importMode} onValueChange={(v) => setImportMode(v as any)}>
        <SelectTrigger className="h-8 w-[160px] text-[12px]">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="merge">合并更新（推荐）</SelectItem>
          <SelectItem value="replace">替换（仅覆盖配置包包含项）</SelectItem>
        </SelectContent>
      </Select>
    </div>

    <input
      ref={importFileRef}
      type="file"
      accept="application/json"
      className="hidden"
      onChange={async (e) => {
        const file = e.target.files?.[0]
        e.target.value = ''
        if (!file) return
        try {
          const text = await file.text()
          const payload = JSON.parse(text)
          await importTemplate(payload)
        } catch {
          toast('配置包解析失败', 'error')
        }
      }}
    />

    <div className="rounded-md border border-border/40 bg-accent/20 p-3">
      <div className="flex items-center gap-2 text-[12px] font-semibold text-foreground">
        <FileJson className="w-4 h-4 text-muted-foreground" />
        官方模板
      </div>
      <div className="mt-2 grid grid-cols-1 md:grid-cols-3 gap-2">
        {builtinTemplates.map(t => (
          <div key={t.name} className="rounded-md border border-border/40 bg-background/30 p-3">
            <div className="flex items-center justify-between">
              <div className="text-[12px] font-semibold text-foreground">{t.name}</div>
              <Button
                size="sm"
                className="h-7"
                onClick={() => importTemplate(t.payload)}
                disabled={importing}
              >
                <span className="text-[12px]">应用</span>
              </Button>
            </div>
            <div className="mt-1 text-[11px] text-muted-foreground">{t.desc}</div>
          </div>
        ))}
      </div>
    </div>
  </section>
  )}

    </>
  )
}
