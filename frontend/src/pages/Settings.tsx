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

  return (
    <SettingsContext.Provider value={ctx}>
      {state.loading ? (
        <SettingsLoading />
      ) : (
      <div className="sida-page-enter">
        <SettingsHero />
        <div className="mt-6 grid grid-cols-1 lg:grid-cols-12 gap-6">
          <SearchEmptyState />
          <AiSection />
          <NotifySection />
          <MyServicesSection />
          <UsersSection />
          <LlmUsageSection />
          <GeneralSettingsSection />
          <PackSection />
          <FeedbackSection />
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
