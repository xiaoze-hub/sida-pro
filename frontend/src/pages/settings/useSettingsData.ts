import { useEffect, useCallback } from 'react'
import type { AIService } from '@panwatch/api'
import type { AgentsHealth } from './types'
import type { FeedbackStats } from './types'
import type { KeyDataSource } from './types'
import type { MyAIService } from './types'
import type { NotifyChannel } from '@panwatch/api'
import type { SceneBinding } from '@panwatch/api'
import type { Setting } from './types'
import type { TemplatePayload } from './types'
import type { UserInfo } from '@panwatch/api'
import { authApi } from '@panwatch/api'
import { fetchAPI } from '@panwatch/api'
import { listSceneBindings } from '@panwatch/api'
import { setSceneBinding } from '@panwatch/api'
import { wechatBindGet } from '@panwatch/api'
import type { useSettingsState } from './useSettingsState'

export function useSettingsData(state: ReturnType<typeof useSettingsState>) {
  const {
    setSettings,
    setKeyDataSources,
    setServices,
    setChannels,
    setVersion,
    setLoading,
    setHealth,
    setCurrentUser,
    setSubscriptions,
    setSubLoading,
    setMyServices,
    setMyServicesLoading,
    setSceneBindings,
    setSceneBindingsLoading,
    setBindingSaving,
    setWechatBindInfo,
    importMode,
    setImporting,
    setExporting,
    setFbStats,
    setFbLoading,
    toast,
  } = state
// 微信绑定状态(静默加载, 失败不阻塞设置页)
const loadWechatBind = useCallback(async () => {
  try {
    const info = await wechatBindGet()
    setWechatBindInfo(info)
  } catch { /* 后端未实现/未绑定时不阻塞设置页 */ }
}, [setWechatBindInfo])

const load = useCallback(async () => {
  try {
    // 2026-09-01 审计修复: 改为 allSettled, 单个接口失败(如 /datasources 曾因缺列 500)
    // 不再拖垮整页 —— 失败的接口降级为空/默认值, 其余正常渲染。
    // 2026-09-01 audit fix: allSettled 单接口失败不拖垮整页。`as const` + Promise.allSettled<unknown[]>
    // 让 TS 把 fetchAPI<T>() 推断为可变参数, 然后逐元素解构时再按位置 cast 回具体类型。
    const results = await Promise.allSettled([
      fetchAPI<Setting[]>('/settings', { cacheMode: 'reload' }),
      fetchAPI<KeyDataSource[]>('/datasources', { cacheMode: 'reload' }),
      fetchAPI<AIService[]>('/providers/services', { cacheMode: 'reload' }),
      fetchAPI<NotifyChannel[]>('/channels', { cacheMode: 'reload' }),
      fetchAPI<{ version: string }>('/version'),
      fetchAPI<AgentsHealth>('/agents/health'),
      listSceneBindings(),
      // BYOK: 用户自己的服务商(demo 账号后端 403, 静默降级为空列表)
      fetchAPI<MyAIService[]>('/my-ai-services', { cacheMode: 'reload' }).catch(() => [] as MyAIService[]),
    ] as unknown as Parameters<typeof Promise.allSettled>);
    const [
      settingsData, keyDataSourcesData, servicesData, channelsData,
      versionData, healthData, sceneBindingsData, myServicesData,
    ] = results.map((r) => (r.status === 'fulfilled' ? r.value : undefined)) as [
      Setting[] | undefined, KeyDataSource[] | undefined, AIService[] | undefined,
      NotifyChannel[] | undefined, { version: string } | undefined,
      AgentsHealth | undefined, SceneBinding[] | undefined, MyAIService[] | undefined,
    ];
    setSettings(settingsData ?? [])
    setKeyDataSources(keyDataSourcesData ?? [])
    setServices(servicesData ?? [])
    setChannels(channelsData ?? [])
    setVersion(versionData?.version ?? '')
    setHealth(healthData ?? null)
    setSceneBindings(sceneBindingsData ?? [])
    setSceneBindingsLoading(false)
    setMyServices(myServicesData ?? [])
    setMyServicesLoading(false)
    // 个人微信绑定状态(静默加载,失败不阻塞)
    void loadWechatBind()
  } catch (e) {
    console.error(e)
  } finally {
    setLoading(false)
  }
}, [loadWechatBind, setChannels, setHealth, setKeyDataSources, setLoading, setMyServices, setMyServicesLoading, setSceneBindings, setSceneBindingsLoading, setServices, setSettings, setVersion])

// 场景分配: 下拉选中 → 绑定/解绑模型(None=回落默认模型)
// Radix Select 不允许空字符串 value, 用哨兵值表示"默认模型"(解绑)
const SCENE_DEFAULT_VALUE = '__default__'
const handleSceneChange = async (scene: string, value: string) => {
  setBindingSaving(scene)
  try {
    const updated = await setSceneBinding(scene, value === SCENE_DEFAULT_VALUE ? null : Number(value))
    setSceneBindings(prev => prev.map(b => (b.scene === scene ? updated : b)))
    toast(value === SCENE_DEFAULT_VALUE ? '已解绑，该场景回落默认模型' : `已绑定: ${updated.model_name || ''}`, 'success')
  } catch (e) {
    toast(e instanceof Error ? `绑定失败: ${e.message}` : '绑定失败，请重试', 'error')
  } finally {
    setBindingSaving(null)
  }
}

const downloadJson = (name: string, obj: any) => {
  try {
    const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = name
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch {
    // ignore
  }
}

const exportTemplate = async () => {
  setExporting(true)
  try {
    const data = await fetchAPI<TemplatePayload>('/templates/export')
    const date = new Date().toISOString().slice(0, 10)
    downloadJson(`panwatch-config-${date}.json`, data)
    toast('配置包已导出', 'success')
  } catch (e) {
    toast(e instanceof Error ? e.message : '导出失败', 'error')
  } finally {
    setExporting(false)
  }
}

const importTemplate = async (payload: TemplatePayload) => {
  setImporting(true)
  try {
    const resp = await fetchAPI<any>(`/templates/import?mode=${importMode}`, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
    toast('配置包已导入', 'success')
    // refresh
    await load()
    return resp
  } catch (e) {
    toast(e instanceof Error ? e.message : '导入失败', 'error')
    return null
  } finally {
    setImporting(false)
  }
}

const loadFeedbackStats = useCallback(async () => {
  setFbLoading(true)
  try {
    const stats = await fetchAPI<FeedbackStats>('/feedback/stats?days=14')
    setFbStats(stats)
  } catch (e) {
    console.error(e)
    setFbStats(null)
  } finally {
    setFbLoading(false)
  }
}, [setFbLoading, setFbStats])

useEffect(() => { load(); loadFeedbackStats() }, [load, loadFeedbackStats])



// 多用户: 当前用户 + 订阅(2026-08-10 阶段5)
useEffect(() => {
  try {
    const raw = localStorage.getItem('user')
    if (raw) setCurrentUser(JSON.parse(raw) as UserInfo)
  } catch { /* ignore */ }
  // 刷新用户信息(后端为准)
  authApi.me().then(d => {
    setCurrentUser(d.user)
    localStorage.setItem('user', JSON.stringify(d.user))
  }).catch(() => {})
  // 订阅
  setSubLoading(true)
  authApi.listSubscriptions().then(d => {
    setSubscriptions(d.subscriptions || [])
  }).catch(() => {}).finally(() => setSubLoading(false))
}, [setCurrentUser, setSubLoading, setSubscriptions])

  return {
    loadWechatBind,
    load,
    SCENE_DEFAULT_VALUE,
    handleSceneChange,
    exportTemplate,
    importTemplate,
    loadFeedbackStats,
  }
}
