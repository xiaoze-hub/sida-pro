import { Button } from '@panwatch/base-ui/components/ui/button'
import { DATA_SOURCE_KEYS } from './types'
import { Dialog } from '@panwatch/base-ui/components/ui/dialog'
import { DialogContent } from '@panwatch/base-ui/components/ui/dialog'
import { DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { DialogHeader } from '@panwatch/base-ui/components/ui/dialog'
import { DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Eye } from 'lucide-react'
import { EyeOff } from 'lucide-react'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { SECRET_MASK } from './types'
import { STOCK_LINK_OPTIONS } from './types'
import { Select } from '@panwatch/base-ui/components/ui/select'
import { SelectContent } from '@panwatch/base-ui/components/ui/select'
import { SelectItem } from '@panwatch/base-ui/components/ui/select'
import { SelectTrigger } from '@panwatch/base-ui/components/ui/select'
import { SelectValue } from '@panwatch/base-ui/components/ui/select'
import { useSettings } from './context'

export function KeyDialogs() {
  const {
    settings,
    saving,
    edited,
    setEdited,
    keyDialogKey,
    keyInputVisible,
    setKeyInputVisible,
    sysDialogKey,
    setSysDialogKey,
    closeKeyDialog,
    saveKeyDialog,
    saveSysDialog,
  } = useSettings()
  return (
    <>
{/* 接口 Key 管理 Dialog(第二窗口): 单个数据源凭证编辑 */}
<Dialog open={keyDialogKey !== null} onOpenChange={open => { if (!open) closeKeyDialog() }}>
  <DialogContent className="max-w-md">
    {keyDialogKey && (() => {
      const item = DATA_SOURCE_KEYS.find(k => k.key === keyDialogKey)
      const setting = settings.find(s => s.key === keyDialogKey)
      const configured = !!setting && setting.value === SECRET_MASK
      const isChanged = keyDialogKey in edited
      return (
        <>
          <DialogHeader>
            <DialogTitle>管理接口 Key · {item?.name ?? keyDialogKey}</DialogTitle>
            <DialogDescription>{item?.desc ?? ''}。读取优先级：设置页 &gt; 环境变量 &gt; 内置默认。</DialogDescription>
          </DialogHeader>
          <div className="relative mt-1">
            <Input
              type={keyInputVisible ? 'text' : 'password'}
              value={edited[keyDialogKey] ?? ''}
              onChange={e => setEdited({ ...edited, [keyDialogKey]: e.target.value })}
              className={`font-mono pr-10 ${isChanged ? 'ring-2 ring-primary/20 border-primary/30' : ''}`}
              placeholder={configured ? '已配置（输入新 Key 可替换，留空保存不变）' : '未配置，输入接口 Key'}
            />
            {!isChanged && configured && (
              <span className="absolute right-10 top-1/2 -translate-y-1/2 text-[10px] text-emerald-700 dark:text-emerald-500">已配置</span>
            )}
            <Button
              type="button" variant="ghost" size="icon"
              className="absolute right-1 top-1/2 -translate-y-1/2 h-8 w-8"
              onClick={() => setKeyInputVisible(!keyInputVisible)}
            >
              {keyInputVisible ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </Button>
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" onClick={closeKeyDialog}>取消</Button>
            <Button onClick={() => void saveKeyDialog()} disabled={!isChanged || saving === keyDialogKey}>
              {saving === keyDialogKey ? '保存中…' : '保存'}
            </Button>
          </div>
        </>
      )
    })()}
  </DialogContent>
</Dialog>

{/* 系统设置编辑 Dialog(第二窗口) */}
<Dialog open={sysDialogKey !== null} onOpenChange={open => { if (!open) setSysDialogKey(null) }}>
  <DialogContent className="max-w-md">
    {sysDialogKey && (() => {
      const setting = settings.find(s => s.key === sysDialogKey)
      if (!setting) return null
      const isChanged = sysDialogKey in edited
      const currentValue = edited[sysDialogKey] ?? setting.value
      return (
        <>
          <DialogHeader>
            <DialogTitle>{setting.description || setting.key}</DialogTitle>
            <DialogDescription className="font-mono text-[11px]">{setting.key}</DialogDescription>
          </DialogHeader>
          {setting.key === 'stock_link_platform' ? (
            <Select
              value={currentValue || 'xueqiu'}
              onValueChange={v => setEdited({ ...edited, [sysDialogKey]: v })}
            >
              <SelectTrigger className={isChanged ? 'ring-2 ring-primary/20 border-primary/30' : ''}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(STOCK_LINK_OPTIONS).map(([val, label]) => (
                  <SelectItem key={val} value={val}>{label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <Input
              value={currentValue}
              onChange={e => setEdited({ ...edited, [sysDialogKey]: e.target.value })}
              className={`font-mono ${isChanged ? 'ring-2 ring-primary/20 border-primary/30' : ''}`}
              placeholder={setting.key}
            />
          )}
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" onClick={() => setSysDialogKey(null)}>取消</Button>
            <Button onClick={() => void saveSysDialog()} disabled={!isChanged || saving === sysDialogKey}>
              {saving === sysDialogKey ? '保存中…' : '保存'}
            </Button>
          </div>
        </>
      )
    })()}
  </DialogContent>
</Dialog>

    </>
  )
}
