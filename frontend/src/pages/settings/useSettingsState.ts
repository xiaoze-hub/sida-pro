import { useState, useRef } from 'react'
import type { AIService } from '@panwatch/api'
import type { AgentsHealth } from './types'
import type { ChannelForm } from './types'
import type { FeedbackStats } from './types'
import type { KeyDataSource } from './types'
import type { ModelForm } from './types'
import type { MyAIService } from './types'
import type { MyServiceForm } from './types'
import type { NotifyChannel } from '@panwatch/api'
import type { SceneBinding } from '@panwatch/api'
import type { ServiceForm } from './types'
import type { Setting } from './types'
import type { SubscriptionItem } from '@panwatch/api'
import type { TemplatePayload } from './types'
import type { UserInfo } from '@panwatch/api'
import type { WechatBindInfo } from '@panwatch/api'
import type { WechatBindStartResult } from '@panwatch/api'
import { browserNotificationsEnabled } from '@/lib/browser-notifications'
import { emptyChannelForm } from './types'
import { emptyModelForm } from './types'
import { emptyMyServiceForm } from './types'
import { emptyServiceForm } from './types'
import { sectionSearchHints } from './types'
import { useAvatar } from '@/hooks/use-avatar'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

export function useSettingsState() {
const [settings, setSettings] = useState<Setting[]>([])
const [keyDataSources, setKeyDataSources] = useState<KeyDataSource[]>([])
const [services, setServices] = useState<AIService[]>([])
const [channels, setChannels] = useState<NotifyChannel[]>([])
const [version, setVersion] = useState<string>('')
const [loading, setLoading] = useState(true)
const [health, setHealth] = useState<AgentsHealth | null>(null)
const [saving, setSaving] = useState<string | null>(null)
const [edited, setEdited] = useState<Record<string, string>>({})
// 多用户(2026-08-10 阶段5): 当前用户 + 订阅
const [currentUser, setCurrentUser] = useState<UserInfo | null>(null)
const [subscriptions, setSubscriptions] = useState<SubscriptionItem[]>([])
const [subLoading, setSubLoading] = useState(false)

const [systemQuery, setSystemQuery] = useState('')
// 2026-08-17: 全局搜索(覆盖所有 section)
const [globalQuery, setGlobalQuery] = useState('')
// 2026-08-17: 全局搜索 trimmed — 标题/hint/子内容匹配 + 命中数反馈(闭环修正 P0-1)
const trimmedGlobal = globalQuery.trim().toLowerCase()
const hasGlobalQuery = trimmedGlobal.length > 0
// 哪些 section id 匹配 — 由各 section 声明, 默认 fallback 只看标题
const matchedSections = new Set<string>(
  (Object.keys(sectionSearchHints) as string[]).filter(id => {
    if (!hasGlobalQuery) return true
    const hints = sectionSearchHints[id] || []
    return hints.some(h => h.toLowerCase().includes(trimmedGlobal))
  })
)
const sectionMatches = (id: string) =>
  !hasGlobalQuery || matchedSections.has(id)
const matchCount = hasGlobalQuery ? matchedSections.size : Object.keys(sectionSearchHints).length
// sectionMatches 移到 module 顶层(被 LlmUsageSection 复用)

// 接口 Key 管理第二窗口(Dialog): 单个数据源凭证编辑
const [keyDialogKey, setKeyDialogKey] = useState<string | null>(null)
const [keyInputVisible, setKeyInputVisible] = useState(false)
// 系统设置编辑第二窗口(Dialog)
const [sysDialogKey, setSysDialogKey] = useState<string | null>(null)

// Service dialog
const [serviceDialogOpen, setServiceDialogOpen] = useState(false)
const [serviceForm, setServiceForm] = useState<ServiceForm>(emptyServiceForm)
const [editServiceId, setEditServiceId] = useState<number | null>(null)
const [serviceKeyVisible, setServiceKeyVisible] = useState(false)

// BYOK(我的服务商) dialog
const [myServices, setMyServices] = useState<MyAIService[]>([])
const [myServicesLoading, setMyServicesLoading] = useState(true)
const [mySvcDialogOpen, setMySvcDialogOpen] = useState(false)
const [mySvcForm, setMySvcForm] = useState<MyServiceForm>(emptyMyServiceForm)
const [editMySvcId, setEditMySvcId] = useState<number | null>(null)
const [mySvcKeyVisible, setMySvcKeyVisible] = useState(false)
const [mySvcSaving, setMySvcSaving] = useState(false)

// Model dialog
const [modelDialogOpen, setModelDialogOpen] = useState(false)
const [modelForm, setModelForm] = useState<ModelForm>(emptyModelForm)
const [editModelId, setEditModelId] = useState<number | null>(null)

// 模型管理第二窗口(Dialog): 查看/增删某服务商下的模型
const [modelsDialogOpen, setModelsDialogOpen] = useState(false)
const [modelsDialogServiceId, setModelsDialogServiceId] = useState<number | null>(null)

// 批量选择嗅探到的模型
const [batchOpen, setBatchOpen] = useState(false)
const [batchServiceId, setBatchServiceId] = useState<number | null>(null)
const [batchCandidates, setBatchCandidates] = useState<string[]>([])
const [batchChecked, setBatchChecked] = useState<Set<string>>(new Set())
const [batchDefault, setBatchDefault] = useState<string>('')
const [submittingBatch, setSubmittingBatch] = useState(false)
const [discoveringService, setDiscoveringService] = useState<number | null>(null)

// 场景分配(统一 LLM 配置中心): 6 场景 × 模型绑定
const [sceneBindings, setSceneBindings] = useState<SceneBinding[]>([])
const [sceneBindingsLoading, setSceneBindingsLoading] = useState(true)
const [bindingSaving, setBindingSaving] = useState<string | null>(null)

// Channel dialog
const [channelDialogOpen, setChannelDialogOpen] = useState(false)
const [channelForm, setChannelForm] = useState<ChannelForm>(emptyChannelForm)
const [editChannelId, setEditChannelId] = useState<number | null>(null)
const [channelKeyVisible, setChannelKeyVisible] = useState(false)
const [testing, setTesting] = useState<number | null>(null)
const [testingModel, setTestingModel] = useState<number | null>(null)
const [browserPushEnabled, setBrowserPushEnabled] = useState(browserNotificationsEnabled)
const [browserPushTesting, setBrowserPushTesting] = useState(false)

// 扫码绑定个人微信(iLink 渠道)
const [wechatBindInfo, setWechatBindInfo] = useState<WechatBindInfo | null>(null)
const [wechatBindStarting, setWechatBindStarting] = useState(false)
const [wechatUnbinding, setWechatUnbinding] = useState(false)
const [wechatQrOpen, setWechatQrOpen] = useState(false)
const [wechatQr, setWechatQr] = useState<WechatBindStartResult | null>(null)
const [wechatQrStatus, setWechatQrStatus] = useState<'waiting' | 'success' | 'scaned' | 'expired'>('waiting')
const wechatPollRef = useRef<number | null>(null)

// 头像
const avatar = useAvatar()
const avatarFileRef = useRef<HTMLInputElement | null>(null)
const [avatarSaving, setAvatarSaving] = useState(false)

// Templates (config pack)
const [importMode, setImportMode] = useState<'merge' | 'replace'>('merge')
const [importing, setImporting] = useState(false)
const [exporting, setExporting] = useState(false)

// Feedback stats
const [fbStats, setFbStats] = useState<FeedbackStats | null>(null)
const [fbLoading, setFbLoading] = useState(false)

const importFileRef = useRef<HTMLInputElement | null>(null)

const { toast } = useToast()

const builtinTemplates: Array<{ name: string; desc: string; payload: TemplatePayload }> = [
  {
    name: '保守',
    desc: '低打扰：盘中更严格触发，静默时段建议开启',
    payload: {
      version: 1,
      settings: {
        notify_quiet_hours: '23:00-07:00',
        notify_retry_attempts: '2',
        notify_retry_backoff_seconds: '2',
      },
      agents: [
        { name: 'premarket_outlook', enabled: true, schedule: '30 8 * * 1-5', execution_mode: 'batch' },
        { name: 'daily_report', enabled: true, schedule: '30 15 * * 1-5', execution_mode: 'batch' },
        { name: 'intraday_monitor', enabled: true, schedule: '*/10 9-15 * * 1-5', execution_mode: 'single', config: { event_only: true, price_alert_threshold: 4.0, volume_alert_ratio: 2.5, throttle_minutes: 45 } },
      ],
    },
  },
  {
    name: '均衡',
    desc: '默认推荐：兼顾覆盖与打扰',
    payload: {
      version: 1,
      settings: {
        notify_retry_attempts: '2',
        notify_retry_backoff_seconds: '2',
      },
      agents: [
        { name: 'premarket_outlook', enabled: true, schedule: '30 8 * * 1-5', execution_mode: 'batch' },
        { name: 'daily_report', enabled: true, schedule: '30 15 * * 1-5', execution_mode: 'batch' },
        { name: 'intraday_monitor', enabled: true, schedule: '*/5 9-15 * * 1-5', execution_mode: 'single', config: { event_only: true, price_alert_threshold: 3.0, volume_alert_ratio: 2.0, throttle_minutes: 30 } },
      ],
    },
  },
  {
    name: '激进',
    desc: '更高频：更早捕捉变化，适合短线盯盘',
    payload: {
      version: 1,
      settings: {
        notify_retry_attempts: '3',
        notify_retry_backoff_seconds: '1',
      },
      agents: [
        { name: 'premarket_outlook', enabled: true, schedule: '10 8 * * 1-5', execution_mode: 'batch' },
        { name: 'daily_report', enabled: true, schedule: '10 15 * * 1-5', execution_mode: 'batch' },
        { name: 'intraday_monitor', enabled: true, schedule: '*/3 9-15 * * 1-5', execution_mode: 'single', config: { event_only: true, price_alert_threshold: 2.0, volume_alert_ratio: 1.8, throttle_minutes: 20 } },
      ],
    },
  },
]

  return {
    loading,
    settings,
    setSettings,
    keyDataSources,
    setKeyDataSources,
    services,
    setServices,
    channels,
    setChannels,
    version,
    setVersion,
    setLoading,
    health,
    setHealth,
    saving,
    setSaving,
    edited,
    setEdited,
    currentUser,
    setCurrentUser,
    subscriptions,
    setSubscriptions,
    subLoading,
    setSubLoading,
    systemQuery,
    setSystemQuery,
    globalQuery,
    setGlobalQuery,
    trimmedGlobal,
    hasGlobalQuery,
    sectionMatches,
    matchCount,
    keyDialogKey,
    setKeyDialogKey,
    keyInputVisible,
    setKeyInputVisible,
    sysDialogKey,
    setSysDialogKey,
    serviceDialogOpen,
    setServiceDialogOpen,
    serviceForm,
    setServiceForm,
    editServiceId,
    setEditServiceId,
    serviceKeyVisible,
    setServiceKeyVisible,
    myServices,
    setMyServices,
    myServicesLoading,
    setMyServicesLoading,
    mySvcDialogOpen,
    setMySvcDialogOpen,
    mySvcForm,
    setMySvcForm,
    editMySvcId,
    setEditMySvcId,
    mySvcKeyVisible,
    setMySvcKeyVisible,
    mySvcSaving,
    setMySvcSaving,
    modelDialogOpen,
    setModelDialogOpen,
    modelForm,
    setModelForm,
    editModelId,
    setEditModelId,
    modelsDialogOpen,
    setModelsDialogOpen,
    modelsDialogServiceId,
    setModelsDialogServiceId,
    batchOpen,
    setBatchOpen,
    batchServiceId,
    setBatchServiceId,
    batchCandidates,
    setBatchCandidates,
    batchChecked,
    setBatchChecked,
    batchDefault,
    setBatchDefault,
    submittingBatch,
    setSubmittingBatch,
    discoveringService,
    setDiscoveringService,
    sceneBindings,
    setSceneBindings,
    sceneBindingsLoading,
    setSceneBindingsLoading,
    bindingSaving,
    setBindingSaving,
    channelDialogOpen,
    setChannelDialogOpen,
    channelForm,
    setChannelForm,
    editChannelId,
    setEditChannelId,
    channelKeyVisible,
    setChannelKeyVisible,
    testing,
    setTesting,
    testingModel,
    setTestingModel,
    browserPushEnabled,
    setBrowserPushEnabled,
    browserPushTesting,
    setBrowserPushTesting,
    wechatBindInfo,
    setWechatBindInfo,
    wechatBindStarting,
    setWechatBindStarting,
    wechatUnbinding,
    setWechatUnbinding,
    wechatQrOpen,
    setWechatQrOpen,
    wechatQr,
    setWechatQr,
    wechatQrStatus,
    setWechatQrStatus,
    wechatPollRef,
    avatar,
    avatarFileRef,
    avatarSaving,
    setAvatarSaving,
    importMode,
    setImportMode,
    importing,
    setImporting,
    exporting,
    setExporting,
    fbStats,
    setFbStats,
    fbLoading,
    setFbLoading,
    importFileRef,
    toast,
    builtinTemplates,
  }
}
