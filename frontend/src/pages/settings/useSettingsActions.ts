import { useEffect } from 'react'
import type { AIModel } from '@panwatch/api'
import type { AIService } from '@panwatch/api'
import type { MyAIService } from './types'
import type { MyModelItem } from './types'
import type { NotifyChannel } from '@panwatch/api'
import type { Setting } from './types'
import { CHANNEL_TYPE_FIELDS } from './types'
import { SECRET_SETTING_KEYS } from './types'
import { authApi } from '@panwatch/api'
import { browserNotificationsSupported } from '@/lib/browser-notifications'
import { emptyChannelForm } from './types'
import { emptyModelForm } from './types'
import { emptyMyServiceForm } from './types'
import { emptyServiceForm } from './types'
import { fetchAPI } from '@panwatch/api'
import { fileToAvatarDataUrl } from '@/hooks/use-avatar'
import { requestBrowserNotificationPermission } from '@/lib/browser-notifications'
import { saveAvatar } from '@/hooks/use-avatar'
import { setBrowserNotificationsEnabled } from '@/lib/browser-notifications'
import { showBrowserNotification } from '@/lib/browser-notifications'
import { wechatBindStart } from '@panwatch/api'
import { wechatBindStatus } from '@panwatch/api'
import { wechatBindUnbind } from '@panwatch/api'
import type { useSettingsState } from './useSettingsState'
import type { useSettingsData } from './useSettingsData'

export function useSettingsActions(
  state: ReturnType<typeof useSettingsState>,
  data: ReturnType<typeof useSettingsData>,
) {
  const {
    settings,
    services,
    setSaving,
    edited,
    setEdited,
    currentUser,
    subscriptions,
    setSubscriptions,
    keyDialogKey,
    setKeyDialogKey,
    setKeyInputVisible,
    sysDialogKey,
    setSysDialogKey,
    setServiceDialogOpen,
    serviceForm,
    setServiceForm,
    editServiceId,
    setEditServiceId,
    setServiceKeyVisible,
    setMyServices,
    setMySvcDialogOpen,
    mySvcForm,
    setMySvcForm,
    editMySvcId,
    setEditMySvcId,
    setMySvcKeyVisible,
    setMySvcSaving,
    setModelDialogOpen,
    modelForm,
    setModelForm,
    editModelId,
    setEditModelId,
    setModelsDialogOpen,
    modelsDialogServiceId,
    setModelsDialogServiceId,
    setBatchOpen,
    batchServiceId,
    setBatchServiceId,
    setBatchCandidates,
    batchChecked,
    setBatchChecked,
    batchDefault,
    setBatchDefault,
    setSubmittingBatch,
    setDiscoveringService,
    setChannelDialogOpen,
    channelForm,
    setChannelForm,
    editChannelId,
    setEditChannelId,
    setChannelKeyVisible,
    setTesting,
    setTestingModel,
    setBrowserPushEnabled,
    setBrowserPushTesting,
    setWechatBindInfo,
    setWechatBindStarting,
    setWechatUnbinding,
    setWechatQrOpen,
    setWechatQr,
    setWechatQrStatus,
    wechatPollRef,
    setAvatarSaving,
    toast,
  } = state
  const {
    loadWechatBind,
    load,
  } = data
const toggleSubscription = async (reportType: string) => {
  const target = subscriptions.find(s => s.report_type === reportType)
  if (!target) return
  const next = !target.enabled
  setSubscriptions(prev => prev.map(s => s.report_type === reportType ? { ...s, enabled: next } : s))
  try {
    await authApi.updateSubscription(reportType, next)
  } catch (e) {
    setSubscriptions(prev => prev.map(s => s.report_type === reportType ? { ...s, enabled: !next } : s))
    toast(e instanceof Error ? e.message : '更新失败', 'error')
  }
}

const onPickAvatar = async (e: React.ChangeEvent<HTMLInputElement>) => {
  const file = e.target.files?.[0]
  e.target.value = '' // 允许重复选择同一文件
  if (!file) return
  setAvatarSaving(true)
  try {
    const dataUrl = await fileToAvatarDataUrl(file)
    await saveAvatar(dataUrl)
    toast('头像已更新', 'success')
  } catch (err) {
    console.error('[Settings] 头像保存失败:', err)  // 2026-08-17: 闭环修正 P0-5 — 留痕便于排障
    toast(err instanceof Error ? err.message : '头像保存失败', 'error')
  } finally {
    setAvatarSaving(false)
  }
}


const handleSave = async (key: string): Promise<boolean> => {
  setSaving(key)
  try {
    const existing = settings.find(s => s.key === key)?.value
    const next = edited[key] ?? existing
    // 敏感 key:留空 = 不修改(防止覆盖已配置的 token)
    if (SECRET_SETTING_KEYS.has(key) && !next) {
      const newEdited = { ...edited }
      delete newEdited[key]
      setEdited(newEdited)
      return true
    }
    await fetchAPI(`/settings/${key}`, {
      method: 'PUT',
      body: JSON.stringify({ value: next }),
    })
    const newEdited = { ...edited }
    delete newEdited[key]
    setEdited(newEdited)
    load()
    return true
  } catch {
    toast('保存失败', 'error')
    return false
  } finally {
    setSaving(null)
  }
}

// 接口 Key 管理 Dialog 打开/关闭
const openKeyDialog = (key: string) => {
  // 打开即清空该 key 的未保存编辑,以当前 DB 状态为准
  setEdited(prev => {
    const next = { ...prev }
    delete next[key]
    return next
  })
  setKeyInputVisible(false)
  setKeyDialogKey(key)
}
const closeKeyDialog = () => {
  setKeyDialogKey(null)
  setKeyInputVisible(false)
}
const saveKeyDialog = async () => {
  if (!keyDialogKey) return
  const ok = await handleSave(keyDialogKey)
  if (ok) {
    closeKeyDialog()
    toast('接口 Key 已保存', 'success')
  }
}

// 系统设置编辑 Dialog
const openSysDialog = (setting: Setting) => {
  setEdited(prev => {
    const next = { ...prev }
    delete next[setting.key]
    return next
  })
  setSysDialogKey(setting.key)
}
const saveSysDialog = async () => {
  if (!sysDialogKey) return
  const ok = await handleSave(sysDialogKey)
  if (ok) {
    setSysDialogKey(null)
    toast('设置已保存', 'success')
  }
}

// Service CRUD
const openServiceDialog = (svc?: AIService) => {
  if (svc) {
    setServiceForm({ name: svc.name, base_url: svc.base_url, api_key: svc.api_key })
    setEditServiceId(svc.id)
  } else {
    setServiceForm(emptyServiceForm)
    setEditServiceId(null)
  }
  setServiceKeyVisible(false)
  setServiceDialogOpen(true)
}

const saveService = async () => {
  try {
    let serviceId = editServiceId
    if (editServiceId) {
      await fetchAPI(`/providers/services/${editServiceId}`, { method: 'PUT', body: JSON.stringify(serviceForm) })
    } else {
      const created = await fetchAPI<AIService>('/providers/services', { method: 'POST', body: JSON.stringify(serviceForm) })
      serviceId = created.id
    }
    setServiceDialogOpen(false)
    await load()
    if (!editServiceId && serviceId) {
      try {
        const res = await fetchAPI<{ models: string[] }>(
          `/providers/services/${serviceId}/discover-models`,
          { method: 'POST' },
        )
        const found = res.models.filter(Boolean)
        if (found.length > 0) {
          setBatchServiceId(serviceId)
          setBatchCandidates(found)
          setBatchChecked(new Set())
          setBatchDefault('')
          setBatchOpen(true)
        } else {
          toast('服务商已保存，未自动发现模型，可手动添加', 'info')
        }
      } catch (e) {
        toast(
          e instanceof Error
            ? `服务商已保存，自动嗅探失败：${e.message}，可手动添加模型`
            : '服务商已保存，该服务商暂不支持自动嗅探，可手动添加模型',
          'info',
        )
      }
    }
  } catch (e) {
    toast(e instanceof Error ? e.message : '保存失败', 'error')
  }
}

// 手动对某服务商嗅探并打开批量选择框(排除已添加的模型)
const discoverForService = async (serviceId: number) => {
  setDiscoveringService(serviceId)
  try {
    const res = await fetchAPI<{ models: string[] }>(
      `/providers/services/${serviceId}/discover-models`,
      { method: 'POST' },
    )
    const svc = services.find(s => s.id === serviceId)
    const added = new Set((svc?.models || []).map(m => m.model))
    const found = res.models.filter(Boolean).filter(id => !added.has(id))
    if (found.length === 0) {
      toast('未发现可新增的模型', 'info')
      return
    }
    setBatchServiceId(serviceId)
    setBatchCandidates(found)
    setBatchChecked(new Set())
    setBatchDefault('')
    setBatchOpen(true)
  } catch (e) {
    toast(e instanceof Error ? e.message : '该服务商暂不支持自动嗅探', 'error')
  } finally {
    setDiscoveringService(null)
  }
}

const submitBatchModels = async () => {
  if (!batchServiceId) return
  const models = Array.from(batchChecked).map(m => ({
    name: '',
    model: m,
    is_default: m === batchDefault,
  }))
  if (models.length === 0) { setBatchOpen(false); return }
  setSubmittingBatch(true)
  try {
    await fetchAPI(`/providers/services/${batchServiceId}/models/batch`, {
      method: 'POST',
      body: JSON.stringify({ models }),
    })
    setBatchOpen(false)
    toast(`已添加 ${models.length} 个模型`, 'success')
    load()
  } catch (e) {
    toast(e instanceof Error ? e.message : '批量添加失败', 'error')
  } finally {
    setSubmittingBatch(false)
  }
}

const deleteService = async (id: number) => {
  if (!confirm('删除服务商将同时删除其下所有模型，确定？')) return
  try {
    await fetchAPI(`/providers/services/${id}`, { method: 'DELETE' })
    load()
  } catch (e) {
    toast(e instanceof Error ? e.message : '删除失败', 'error')
  }
}

// ── BYOK(我的服务商): 用户自定义 LLM 服务商, 用自己的 API Key(2026-08-15) ──
const isDemo = currentUser?.username === 'demo'
const reloadMyServices = async () => {
  try {
    const data = await fetchAPI<MyAIService[]>('/my-ai-services', { cacheMode: 'reload' })
    setMyServices(data)
  } catch (e) {
    toast(e instanceof Error ? e.message : '加载我的服务商失败', 'error')
  }
}
const openMySvcDialog = (svc?: MyAIService) => {
  if (svc) {
    const m = svc.models?.[0]
    setMySvcForm({
      name: svc.name,
      base_url: svc.base_url,
      api_key: svc.api_key,
      model_name: m?.name || '',
      model: m?.model || '',
      is_default: m?.is_default ?? true,
      scene: m?.scene || 'chat',
      capabilities: m?.capabilities || [],
    })
    setEditMySvcId(svc.id)
  } else {
    setMySvcForm(emptyMyServiceForm)
    setEditMySvcId(null)
  }
  setMySvcKeyVisible(false)
  setMySvcDialogOpen(true)
}
const saveMySvc = async () => {
  if (!mySvcForm.name.trim() || !mySvcForm.base_url.trim()) return
  setMySvcSaving(true)
  try {
    const models: MyModelItem[] = mySvcForm.model.trim()
      ? [{
          name: mySvcForm.model_name.trim() || mySvcForm.model.trim(),
          model: mySvcForm.model.trim(),
          is_default: mySvcForm.is_default,
          scene: mySvcForm.scene,
          capabilities: mySvcForm.capabilities,
        }]
      : []
    const payload = {
      name: mySvcForm.name.trim(),
      base_url: mySvcForm.base_url.trim(),
      api_key: mySvcForm.api_key,
      models,
    }
    if (editMySvcId) {
      await fetchAPI(`/my-ai-services/${editMySvcId}`, { method: 'PUT', body: JSON.stringify(payload) })
      toast('已保存', 'success')
    } else {
      await fetchAPI('/my-ai-services', { method: 'POST', body: JSON.stringify(payload) })
      toast('已添加，调用时将优先使用你的服务商', 'success')
    }
    setMySvcDialogOpen(false)
    reloadMyServices()
  } catch (e) {
    toast(e instanceof Error ? e.message : '保存失败', 'error')
  } finally {
    setMySvcSaving(false)
  }
}
const deleteMySvc = async (id: number) => {
  if (!confirm('删除后将无法使用该服务商调用，确定？')) return
  try {
    await fetchAPI(`/my-ai-services/${id}`, { method: 'DELETE' })
    toast('已删除', 'success')
    reloadMyServices()
  } catch (e) {
    toast(e instanceof Error ? e.message : '删除失败', 'error')
  }
}

// Model CRUD
const openModelsDialog = (svc: AIService) => {
  setModelsDialogServiceId(svc.id)
  setModelsDialogOpen(true)
}

const closeModelsDialog = () => {
  setModelsDialogOpen(false)
  setModelsDialogServiceId(null)
}

// 模型管理 Dialog 当前服务商(删除服务商后自动失效)
const modelsSvc = services.find(s => s.id === modelsDialogServiceId) ?? null

const openModelDialog = (serviceId?: number, model?: AIModel) => {
  if (model) {
    setModelForm({
      name: model.name,
      service_id: model.service_id,
      model: model.model,
      capabilities: model.capabilities || [],
    })
    setEditModelId(model.id)
  } else {
    setModelForm({ ...emptyModelForm, service_id: serviceId ?? null })
    setEditModelId(null)
  }
  setModelDialogOpen(true)
}

const saveModel = async () => {
  try {
    const payload = { ...modelForm, capabilities: modelForm.capabilities || [] }
    if (editModelId) {
      await fetchAPI(`/providers/models/${editModelId}`, { method: 'PUT', body: JSON.stringify(payload) })
    } else {
      await fetchAPI('/providers/models', { method: 'POST', body: JSON.stringify(payload) })
    }
    setModelDialogOpen(false)
    load()
  } catch (e) {
    toast(e instanceof Error ? e.message : '保存失败', 'error')
  }
}

const deleteModel = async (id: number) => {
  if (!confirm('确定删除此模型？')) return
  try {
    await fetchAPI(`/providers/models/${id}`, { method: 'DELETE' })
    load()
  } catch (e) {
    toast(e instanceof Error ? e.message : '删除失败', 'error')
  }
}

const setDefaultModel = async (id: number) => {
  try {
    await fetchAPI(`/providers/models/${id}`, { method: 'PUT', body: JSON.stringify({ is_default: true }) })
    load()
  } catch {
    toast('设置失败', 'error')
  }
}

const testModel = async (id: number) => {
  setTestingModel(id)
  try {
    await fetchAPI(`/providers/models/${id}/test`, { method: 'POST' })
    toast('模型测试成功', 'success')
  } catch (e) {
    toast(e instanceof Error ? e.message : '测试失败', 'error')
  } finally {
    setTestingModel(null)
  }
}

// Channel CRUD
const openChannelDialog = (channel?: NotifyChannel) => {
  if (channel) {
    setChannelForm({
      name: channel.name,
      type: channel.type,
      config: channel.config ? { ...channel.config } : {},
    })
    setEditChannelId(channel.id)
  } else {
    setChannelForm(emptyChannelForm)
    setEditChannelId(null)
  }
  setChannelKeyVisible(false)
  setChannelDialogOpen(true)
}

const saveChannel = async () => {
  const payload = {
    name: channelForm.name,
    type: channelForm.type,
    config: channelForm.config,
  }
  try {
    let savedChannel: NotifyChannel
    if (editChannelId) {
      savedChannel = await fetchAPI<NotifyChannel>(`/channels/${editChannelId}`, { method: 'PUT', body: JSON.stringify(payload) })
    } else {
      savedChannel = await fetchAPI<NotifyChannel>('/channels', { method: 'POST', body: JSON.stringify(payload) })
    }
    setTesting(savedChannel.id)
    try {
      const result = await fetchAPI<{ message?: string }>(`/channels/${savedChannel.id}/test`, { method: 'POST' })
      toast(result?.message || '渠道已保存并通过测试', 'success')
    } catch (e) {
      setEditChannelId(savedChannel.id)
      load()
      toast(`渠道已保存，但测试失败：${e instanceof Error ? e.message : '未知错误'}`, 'error')
      return
    } finally {
      setTesting(null)
    }
    setChannelDialogOpen(false)
    load()
  } catch (e) {
    toast(e instanceof Error ? e.message : '保存失败', 'error')
  }
}

const isChannelFormValid = () => {
  if (!channelForm.name) return false
  const typeDef = CHANNEL_TYPE_FIELDS[channelForm.type]
  if (!typeDef) return false
  return typeDef.fields
    .filter(f => f.required)
    .every(f => !!channelForm.config[f.key]?.trim())
}

const deleteChannel = async (id: number) => {
  if (!confirm('确定删除此通知渠道？')) return
  try {
    await fetchAPI(`/channels/${id}`, { method: 'DELETE' })
    load()
  } catch (e) {
    toast(e instanceof Error ? e.message : '删除失败', 'error')
  }
}

const setDefaultChannel = async (id: number) => {
  try {
    await fetchAPI(`/channels/${id}`, { method: 'PUT', body: JSON.stringify({ is_default: true }) })
    load()
  } catch {
    toast('设置失败', 'error')
  }
}

const toggleChannelEnabled = async (channel: NotifyChannel) => {
  try {
    await fetchAPI(`/channels/${channel.id}`, { method: 'PUT', body: JSON.stringify({ enabled: !channel.enabled }) })
    load()
  } catch {
    toast('操作失败', 'error')
  }
}

const testChannel = async (id: number) => {
  setTesting(id)
  try {
    const result = await fetchAPI<{ message?: string }>(`/channels/${id}/test`, { method: 'POST' })
    toast(result?.message || '测试通知已发送', 'success')
  } catch (e) {
    toast(e instanceof Error ? e.message : '测试失败', 'error')
  } finally {
    setTesting(null)
  }
}

// ── 扫码绑定个人微信(iLink 渠道) ──
const stopWechatPoll = () => {
  if (wechatPollRef.current !== null) {
    window.clearInterval(wechatPollRef.current)
    wechatPollRef.current = null
  }
}

const startWechatPoll = (qrcode: string) => {
  stopWechatPoll()
  wechatPollRef.current = window.setInterval(async () => {
    try {
      const st = await wechatBindStatus(qrcode)
      setWechatQrStatus(st.status)
      if (st.status === 'success') {
        stopWechatPoll()
        setWechatQrOpen(false)
        toast('微信绑定成功', 'success')
        void loadWechatBind()
        load()
      } else if (st.status === 'expired') {
        stopWechatPoll()
      }
    } catch { /* 网络抖动忽略，下轮重试 */ }
  }, 3000)
}

const startWechatBind = async () => {
  setWechatBindStarting(true)
  try {
    const res = await wechatBindStart()
    setWechatQr(res)
    setWechatQrStatus('waiting')
    setWechatQrOpen(true)
    startWechatPoll(res.qrcode)
  } catch (e) {
    toast(e instanceof Error ? e.message : '发起绑定失败，请稍后重试', 'error')
  } finally {
    setWechatBindStarting(false)
  }
}

const closeWechatQr = () => {
  stopWechatPoll()
  setWechatQrOpen(false)
}

const copyWechatLink = async (url: string) => {
  try {
    await navigator.clipboard.writeText(url)
    toast('链接已复制，请在微信中打开', 'success')
  } catch {
    toast('复制失败，请手动复制链接', 'error')
  }
}

const unbindWechat = async () => {
  if (!confirm('确定解除个人微信绑定？解除后将无法通过微信接收通知。')) return
  setWechatUnbinding(true)
  try {
    await wechatBindUnbind()
    toast('已解除微信绑定', 'success')
    setWechatBindInfo(null)
    load()
  } catch (e) {
    toast(e instanceof Error ? e.message : '解除绑定失败', 'error')
  } finally {
    setWechatUnbinding(false)
  }
}

const toggleBrowserPush = async (enabled: boolean) => {
  if (!enabled) {
    setBrowserNotificationsEnabled(false)
    setBrowserPushEnabled(false)
    toast('电脑 Web 推送已关闭', 'info')
    return
  }
  if (!browserNotificationsSupported()) {
    toast('当前浏览器或访问地址不支持系统通知，请使用 HTTPS 或 localhost', 'error')
    return
  }
  const permission = await requestBrowserNotificationPermission()
  if (permission !== 'granted') {
    setBrowserNotificationsEnabled(false)
    setBrowserPushEnabled(false)
    toast('浏览器未授予通知权限，请在网站权限中允许通知', 'error')
    return
  }
  try {
    const latest = await fetchAPI<{ items: Array<{ id: number }> }>('/notifications?limit=1')
    const baselineId = latest?.items?.[0]?.id || 0
    setBrowserNotificationsEnabled(true, baselineId)
    setBrowserPushEnabled(true)
    await showBrowserNotification({
      id: Date.now(),
      title: 'SIDA 电脑推送已开启',
      body: '页面打开或在后台运行时，新消息会直接显示为电脑系统通知。',
      link: '/settings',
    })
    toast('电脑 Web 推送已开启', 'success')
  } catch (e) {
    setBrowserNotificationsEnabled(false)
    setBrowserPushEnabled(false)
    toast(e instanceof Error ? e.message : '电脑 Web 推送开启失败', 'error')
  }
}

const testBrowserPush = async () => {
  setBrowserPushTesting(true)
  try {
    const shown = await showBrowserNotification({
      id: Date.now(),
      title: 'SIDA 电脑推送测试',
      body: '如果你看到这条系统通知，说明 Web 推送已正常工作。',
      link: '/settings',
    })
    toast(shown ? '电脑测试通知已发送' : '浏览器通知权限不可用', shown ? 'success' : 'error')
  } finally {
    setBrowserPushTesting(false)
  }
}


// 卸载时停止扫码轮询
// eslint-disable-next-line react-hooks/exhaustive-deps -- 仅卸载时停止轮询
  useEffect(() => () => stopWechatPoll(), [])

  return {
    toggleSubscription,
    onPickAvatar,
    openKeyDialog,
    closeKeyDialog,
    saveKeyDialog,
    openSysDialog,
    saveSysDialog,
    openServiceDialog,
    saveService,
    discoverForService,
    submitBatchModels,
    deleteService,
    isDemo,
    openMySvcDialog,
    saveMySvc,
    deleteMySvc,
    openModelsDialog,
    closeModelsDialog,
    modelsSvc,
    openModelDialog,
    saveModel,
    deleteModel,
    setDefaultModel,
    testModel,
    openChannelDialog,
    saveChannel,
    isChannelFormValid,
    deleteChannel,
    setDefaultChannel,
    toggleChannelEnabled,
    testChannel,
    startWechatBind,
    closeWechatQr,
    copyWechatLink,
    unbindWechat,
    toggleBrowserPush,
    testBrowserPush,
  }
}
