import ThsAccountCard from '@panwatch/biz-ui/components/ThsAccountCard'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { DATA_SOURCE_KEYS } from './types'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { KeyRound } from 'lucide-react'
import { Pencil } from 'lucide-react'
import { SECRET_MASK } from './types'
import { STOCK_LINK_OPTIONS } from './types'
import { useSettings } from './context'
import { Card } from '@panwatch/base-ui/components/ui/card'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import SectionHeader from '@panwatch/biz-ui/components/SectionHeader'
import { useI18n } from '@/hooks/useI18n'

export function GeneralSettingsSection({ activeId }: { activeId?: string }) {
  const { t } = useI18n()
  const {
    settings,
    load,
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
  const matches = (id: string) => (!activeId || activeId === id) && sectionMatches(id)
  return (
    <>
  {/* General Settings */}
  {isOwner && (
    <>
    {/* 接口 Key 区块(数据源凭证维护) */}
    {matches('sec-keys') && <section id="sec-keys" className="analysis-section">
      <div className="flex items-start justify-between mb-4 gap-3">
        <div>
          <SectionHeader title={t('settings.sections.keys')} className="mb-1" />
          <p className="text-[12px] text-muted-foreground mt-1">{t('settings.sections.keysDesc')}</p>
          <div className="flex flex-wrap gap-2 mt-2">
            {keyDataSources.filter(s => (s.key_count ?? 0) > 0).map(s => (
              <Badge key={s.id} variant="default" title={`${s.name} · ${s.provider}`}>
                {t('settings.sections.keyCount', { provider: s.provider, n: s.key_count ?? 0 })}
              </Badge>
            ))}
            {keyDataSources.every(s => (s.key_count ?? 0) === 0) && (
              <span className="text-[12px] text-muted-foreground">{t('settings.sections.singleKey')}</span>
            )}
          </div>
        </div>
      </div>
      <div className="space-y-2">
        {settings.length === 0 ? <div className="py-4 text-[14px] text-muted-foreground">未读取到配置数据。<Button variant="ghost" size="sm" onClick={() => void load()}>重新加载</Button></div> : DATA_SOURCE_KEYS.map(item => {
          const setting = settings.find(s => s.key === item.key)
          const configured = !!setting && setting.value === SECRET_MASK
          return (
            <Card key={item.key} variant="plain" className="flex items-center justify-between gap-3 px-3 py-2.5 min-h-[44px]">
              <div className="min-w-0 flex items-center gap-2.5">
                <KeyRound className="w-3.5 h-3.5 text-primary flex-shrink-0" />
                <div className="min-w-0">
                  <span className="text-[12px] font-medium text-foreground">{item.name}</span>
                  <p className="text-[12px] text-muted-foreground truncate" title={item.desc}>{item.desc}</p>
                </div>
              </div>
              <div className="flex items-center gap-1.5 flex-shrink-0">
                <Badge variant={configured ? 'success' : 'outline'}>
                  {configured ? t('common.configured') : t('common.unconfigured')}
                </Badge>
                <Button size="sm" variant="secondary" className="h-8 px-2 text-[12px] min-h-[44px]" onClick={() => openKeyDialog(item.key)}>
                  {t('common.manage')}
                </Button>
              </div>
            </Card>
          )
        })}
      </div>
    </section>}
    {/* 同花顺账号维护(v0.5.3): 自包含卡片(模式/登录态/登出/能力一览) */}
    {matches('sec-ths') && <section id="sec-ths" className="analysis-section">
      <ThsAccountCard />
    </section>}
    {matches('sec-system') && <section id="sec-system" className="analysis-section">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-3 mb-4 md:mb-5">
        <div>
          <SectionHeader title={t('settings.sections.system')} className="mb-1" />
          <p className="text-[12px] text-muted-foreground mt-1">{t('settings.sections.systemDesc')}</p>
        </div>
        <div className="flex items-center gap-2">
          <Input
            value={systemQuery}
            onChange={e => setSystemQuery(e.target.value)}
            placeholder={t('settings.searchPlaceholder')}
            className="h-9 w-full md:w-[320px] min-h-[44px]"
           aria-label={t('settings.searchPlaceholder')}/>
          {health?.timezone ? (
            <div className="card-surface hidden md:flex px-2.5 h-9 items-center rounded-md bg-s2 text-[12px] text-muted-foreground">
              TZ <span className="ml-1 font-mono text-foreground">{health.timezone}</span>
            </div>
          ) : null}
        </div>
      </div>

      <div className="space-y-2">
        {filteredSettings.length === 0 && <div className="py-4 text-[14px] text-muted-foreground">{settings.length === 0 ? '未读取到系统参数，请重新加载。' : '没有匹配的系统参数。'}{settings.length === 0 && <Button variant="ghost" size="sm" onClick={() => void load()}>重新加载</Button>}</div>}
        {filteredSettings.map(setting => {
          const currentValue = edited[setting.key] ?? setting.value
          const isChanged = setting.key in edited
          const summary = setting.key === 'stock_link_platform'
            ? (STOCK_LINK_OPTIONS[currentValue] ?? currentValue) || t('common.notSet')
            : currentValue || t('common.notSet')
          return (
            <Card key={setting.key} variant="plain" className="flex items-center justify-between gap-3 px-3 py-2.5 min-h-[44px]">
              <div className="min-w-0">
                <span className="text-[12px] font-medium text-foreground">{setting.description || setting.key}</span>
                <p className="text-[12px] text-muted-foreground truncate font-mono" title={summary}>{summary}</p>
              </div>
              <div className="flex items-center gap-1.5 flex-shrink-0">
                {isChanged && <span className="text-[12px] text-amber-700 dark:text-amber-400">{t('common.unsaved')}</span>}
                <Button size="sm" variant="secondary" className="h-8 px-2 text-[12px] min-h-[44px]" onClick={() => openSysDialog(setting)}>
                  <Pencil className="w-3 h-3" /> {t('common.edit')}
                </Button>
              </div>
            </Card>
          )
        })}
      </div>
    </section>}
    </>
  )}
    </>
  )
}
