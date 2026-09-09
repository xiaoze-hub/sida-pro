import ThsAccountCard from '@panwatch/biz-ui/components/ThsAccountCard'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { DATA_SOURCE_KEYS } from './types'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { KeyRound } from 'lucide-react'
import { Pencil } from 'lucide-react'
import { SECRET_MASK } from './types'
import { STOCK_LINK_OPTIONS } from './types'
import { useSettings } from './context'

export function GeneralSettingsSection() {
  const {
    settings,
    keyDataSources,
    health,
    edited,
    systemQuery,
    setSystemQuery,
    sectionMatches,
    filteredSettings,
    isOwner,
    openKeyDialog,
    openSysDialog,
  } = useSettings()
  return (
    <>
  {/* General Settings */}
  {isOwner && settings.length > 0 && (
    <>
    {/* 接口 Key 区块(数据源凭证维护) */}
    <section id="sec-keys" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-12" style={{ display: sectionMatches('sec-keys') ? undefined : 'none' }}>
      <div className="flex items-start justify-between mb-4 gap-3">
        <div>
          <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">接口 Key</h3>
          <p className="text-[11px] text-muted-foreground mt-1">数据源接口凭证，保存在本机数据库，修改后立即生效（无需重启）。</p>
          <div className="flex flex-wrap gap-2 mt-2">
            {keyDataSources.filter(s => (s.key_count ?? 0) > 0).map(s => (
              <span key={s.id} className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-primary" title={`${s.name} · ${s.provider}`}>
                {s.provider} · {s.key_count} 个 Key
              </span>
            ))}
            {keyDataSources.every(s => (s.key_count ?? 0) === 0) && (
              <span className="text-[10px] text-muted-foreground">当前接口 Key 为单 Key 配置</span>
            )}
          </div>
        </div>
      </div>
      <div className="space-y-2">
        {DATA_SOURCE_KEYS.map(item => {
          const setting = settings.find(s => s.key === item.key)
          const configured = !!setting && setting.value === SECRET_MASK
          return (
            <div key={item.key} className="flex items-center justify-between gap-3 border-b border-border/40 px-1 py-2.5">
              <div className="min-w-0 flex items-center gap-2.5">
                <KeyRound className="w-3.5 h-3.5 text-primary flex-shrink-0" />
                <div className="min-w-0">
                  <span className="text-[12px] font-medium text-foreground">{item.name}</span>
                  <p className="text-[10px] text-muted-foreground truncate">{item.desc}</p>
                </div>
              </div>
              <div className="flex items-center gap-1.5 flex-shrink-0">
                <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] ${
                  configured
                    ? 'border-emerald-700/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                    : 'border-amber-700/25 bg-amber-500/10 text-amber-700 dark:text-amber-400'
                }`}>
                  {configured ? '已配置' : '未配置'}
                </span>
                <Button size="sm" variant="secondary" className="h-7 px-2 text-[11px]" onClick={() => openKeyDialog(item.key)}>
                  管理
                </Button>
              </div>
            </div>
          )
        })}
      </div>
    </section>
    {/* 同花顺账号维护(v0.5.3): 自包含卡片(模式/登录态/登出/能力一览) */}
    <section id="sec-ths" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-12" style={{ display: sectionMatches('sec-ths') ? undefined : 'none' }}>
      <ThsAccountCard />
    </section>
    <section id="sec-system" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-12" style={{ display: sectionMatches('sec-system') ? undefined : 'none' }}>
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-3 mb-4 md:mb-5">
        <div>
          <h3 className="text-[12px] md:text-[13px] font-semibold text-foreground">系统</h3>
          <p className="text-[11px] text-muted-foreground mt-1">偏好与高级选项。修改后立即生效。</p>
        </div>
        <div className="flex items-center gap-2">
          <Input
            value={systemQuery}
            onChange={e => setSystemQuery(e.target.value)}
            placeholder="搜索设置项（描述 / key）"
            className="h-9 w-full md:w-[320px]"
           aria-label="搜索设置项（描述 / key）"/>
          {health?.timezone ? (
            <div className="hidden md:flex px-2.5 h-9 items-center rounded-md border border-border/50 bg-accent/20 text-[11px] text-muted-foreground">
              TZ <span className="ml-1 font-mono text-foreground/90">{health.timezone}</span>
            </div>
          ) : null}
        </div>
      </div>

      <div className="space-y-2">
        {filteredSettings.map(setting => {
          const currentValue = edited[setting.key] ?? setting.value
          const isChanged = setting.key in edited
          const summary = setting.key === 'stock_link_platform'
            ? (STOCK_LINK_OPTIONS[currentValue] ?? currentValue) || '未设置'
            : currentValue || '未设置'
          return (
            <div key={setting.key} className="flex items-center justify-between gap-3 border-b border-border/40 px-1 py-2.5">
              <div className="min-w-0">
                <span className="text-[12px] font-medium text-foreground">{setting.description || setting.key}</span>
                <p className="text-[10px] text-muted-foreground truncate font-mono">{summary}</p>
              </div>
              <div className="flex items-center gap-1.5 flex-shrink-0">
                {isChanged && <span className="text-[10px] text-amber-700 dark:text-amber-400">未保存</span>}
                <Button size="sm" variant="secondary" className="h-7 px-2 text-[11px]" onClick={() => openSysDialog(setting)}>
                  <Pencil className="w-3 h-3" /> 编辑
                </Button>
              </div>
            </div>
          )
        })}
      </div>
    </section>
    </>
  )}
    </>
  )
}
