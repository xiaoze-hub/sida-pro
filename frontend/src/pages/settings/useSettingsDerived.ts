import { SECRET_MASK } from './types'
import { SECRET_SETTING_KEYS } from './types'
import type { useSettingsState } from './useSettingsState'
import type { useSettingsData } from './useSettingsData'

export function useSettingsDerived(
  state: ReturnType<typeof useSettingsState>,
  data: ReturnType<typeof useSettingsData>,
) {
  const {
    settings,
    services,
    channels,
    health,
    currentUser,
    systemQuery,
  } = state
  const {

  } = data
const allModels = services.flatMap(s => s.models || [])
const defaultModel = allModels.find(m => m.is_default)
const defaultChannel = channels.find(c => c.is_default)
const enabledChannels = channels.filter(c => c.enabled)
// 场景分配下拉选项: 模型池全部模型(模型名 + 服务商名 + 功能徽标)
const sceneModelOptions = services.flatMap(svc =>
  (svc.models || []).map(m => ({
    id: m.id,
    label: `${m.name} · ${svc.name}`,
    caps: m.capabilities || [],
  })),
)
// 场景下拉选项生成器: vision 场景把含视觉能力的模型排前面
const sceneOptionsFor = (scene: string) => {
  if (scene !== 'vision') return sceneModelOptions
  const hasVision = (c: string[]) => (c || []).includes('vision')
  return [...sceneModelOptions].sort(
    (a, b) => (hasVision(b.caps) ? 1 : 0) - (hasVision(a.caps) ? 1 : 0),
  )
}

const filteredSettings = settings.filter(s => {
  // 敏感接口 key 由独立"接口 Key"区块管理,系统区块不重复展示
  if (SECRET_SETTING_KEYS.has(s.key)) return false
  const q = systemQuery.trim().toLowerCase()
  if (!q) return true
  return (s.description || '').toLowerCase().includes(q) || (s.key || '').toLowerCase().includes(q)
})

// 权限细化(2026-08-16): 平台级管理区块仅 owner 可见, member 只见个人配置
const isOwner = currentUser?.role === 'owner'

// 按“重要性”排序：常用优先，低频靠后
const jumpItems: Array<{ id: string; label: string; hint?: string }> = [
  ...(isOwner ? [{ id: 'sec-ai', label: 'AI', hint: `${services.length} 服务 / ${allModels.length} 模型` } as const] : []),
  { id: 'sec-notify', label: '通知', hint: `${enabledChannels.length}/${channels.length} 启用` },
  ...(isOwner ? [{ id: 'sec-keys', label: '接口Key', hint: `${settings.filter(s => SECRET_SETTING_KEYS.has(s.key) && s.value === SECRET_MASK).length}/${SECRET_SETTING_KEYS.size} 已配` } as const] : []),
  ...(isOwner ? [{ id: 'sec-system', label: '系统', hint: health?.timezone ? `TZ ${health.timezone}` : undefined } as const] : []),
  ...(isOwner ? [{ id: 'sec-pack', label: '配置包' } as const] : []),
  { id: 'sec-feedback', label: '反馈' },
]

const scrollTo = (id: string) => {
  const el = document.getElementById(id)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}


  return {
    allModels,
    defaultModel,
    defaultChannel,
    enabledChannels,
    sceneOptionsFor,
    filteredSettings,
    isOwner,
    jumpItems,
    scrollTo,
  }
}
