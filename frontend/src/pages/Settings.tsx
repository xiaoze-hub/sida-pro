import { SectionWorkspace, SectionPanel, useSectionRoute, type WorkspaceSectionItem } from '@/components/SectionWorkspace'
import { LlmUsageSection } from '@/components/settings/LlmUsageSection'
import { SettingsContext } from './settings/context'
import { useSettingsState } from './settings/useSettingsState'
import { useSettingsData } from './settings/useSettingsData'
import { useSettingsDerived } from './settings/useSettingsDerived'
import { useSettingsActions } from './settings/useSettingsActions'
import { SettingsHero } from './settings/SettingsHero'
import { SearchEmptyState } from './settings/SearchEmptyState'
import { AiSection } from './settings/AiSection'
import { NotifySection } from './settings/NotifySection'
import { MyServicesSection } from './settings/MyServicesSection'
import { UsersSection } from './settings/UsersSection'
import { GeneralSettingsSection } from './settings/GeneralSettingsSection'
import { AppearanceSection } from './settings/AppearanceSection'
import { FreeTierSection } from '@/components/settings/FreeTierSection'
import { PackSection } from './settings/PackSection'
import { FeedbackSection } from './settings/FeedbackSection'
import { AiDialogs } from './settings/AiDialogs'
import { NotifyDialogs } from './settings/NotifyDialogs'
import { KeyDialogs } from './settings/KeyDialogs'
import { SettingsFooter } from './settings/SettingsFooter'
import { SettingsLoading } from './settings/SettingsLoading'

export default function SettingsPage() {
  const state = useSettingsState()
  const data = useSettingsData(state)
  const derived = useSettingsDerived(state, data)
  const actions = useSettingsActions(state, data)
  const ctx = { ...state, ...data, ...derived, ...actions }
  const sections: WorkspaceSectionItem[] = [
    ...(derived.isOwner ? [{ id: 'sec-ai', label: 'AI 服务与模型', group: '分析能力', hint: `${state.services.length} 个服务商` }] : []),
    { id: 'sec-my-services', label: '我的 AI 服务', group: '分析能力' },
    { id: 'sec-notify', label: '通知渠道', group: '个人偏好' },
    { id: 'sec-subscriptions', label: '报告订阅', group: '个人偏好' },
    { id: 'sec-appearance', label: '外观与布局', group: '个人偏好' },
    ...(derived.isOwner ? [
      { id: 'sec-keys', label: '数据源凭证', group: '平台管理' },
      { id: 'sec-ths', label: '同花顺连接', group: '平台管理' },
      { id: 'sec-system', label: '系统参数', group: '平台管理' },
      { id: 'sec-llm-usage', label: 'AI 用量', group: '平台管理' },
      { id: 'sec-free-tier', label: '试用与限额', group: '平台管理' },
      { id: 'sec-pack', label: '配置导入导出', group: '平台管理' },
      { id: 'sec-users', label: '用户管理', group: '平台管理' },
    ] : []),
    { id: 'sec-feedback', label: '分析反馈', group: '帮助' },
  ].filter(item => !state.hasGlobalQuery || state.sectionMatches(item.id))
  const [selected, select] = useSectionRoute(sections, derived.isOwner ? 'sec-ai' : 'sec-my-services')

  return (
    <SettingsContext.Provider value={ctx}>
      {state.loading ? (
        <SettingsLoading />
      ) : (
      <div className="sida-page-enter">
        <SettingsHero />
        <div className="mt-5">
          {sections.length === 0 ? <SearchEmptyState /> : <SectionWorkspace items={sections} value={selected} onChange={select} label="设置分类">
            <SectionPanel id="sec-ai"><AiSection /></SectionPanel>
            <SectionPanel id="sec-my-services"><MyServicesSection /></SectionPanel>
            <SectionPanel id="sec-notify"><NotifySection /></SectionPanel>
            <SectionPanel id="sec-subscriptions"><UsersSection activeId={selected} /></SectionPanel>
            <SectionPanel id="sec-users"><UsersSection activeId={selected} /></SectionPanel>
            <SectionPanel id="sec-llm-usage"><LlmUsageSection /></SectionPanel>
            <SectionPanel id="sec-appearance"><AppearanceSection /></SectionPanel>
            {['sec-keys', 'sec-ths', 'sec-system'].includes(selected) && <GeneralSettingsSection activeId={selected} />}
            <SectionPanel id="sec-free-tier"><FreeTierSection /></SectionPanel>
            <SectionPanel id="sec-pack"><PackSection /></SectionPanel>
            <SectionPanel id="sec-feedback"><FeedbackSection /></SectionPanel>
          </SectionWorkspace>}
        </div>
        <AiDialogs />
        <NotifyDialogs />
        <KeyDialogs />
        <SettingsFooter />
      </div>
      )}
    </SettingsContext.Provider>
  )
}
