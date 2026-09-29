import { fetchAPI } from './client'

/**
 * 免费档(免费级别)管理 —— owner 专属(2026-09-18)。
 *
 * 后端: `GET/PUT /api/admin/free-tier`。改完 **30s 内**全节点热生效, 不用发版/重启。
 * 非 owner 调用会拿到 403(面板据此自行隐藏)。
 */

/** pro 专属功能(view_forecast=数智决策三指标 / view_auction=集合竞价池 等) */
export interface FreeTierFeature {
  perm: string
  label: string
  /** 当前是否在免费档里(member 可试用) */
  trial: boolean
  trial_label?: string | null
}

export interface FreeTierSkill {
  name: string
  /** 代码内置档位 */
  builtin_tier: 'free' | 'trial' | 'pro'
  /** 实际生效档位(可能被覆盖) */
  effective_tier: 'free' | 'trial' | 'pro'
  overridden: boolean
}

export interface FreeTierTierLimit {
  tier: 'free' | 'trial' | 'pro'
  /** 当前生效的档位日限(表 + 面板覆盖后) */
  daily_limit: number | null
  burst_limit: number | null
  refill_per_min: number | null
}

export interface FreeTierConfig {
  trial_features: Record<string, string>
  trial_daily_limit: number
  member_watchlist_max: number
  member_alert_max: number
  skill_tier_overrides: Record<string, string>
  /** B3: 同账号同时在线设备数上限(默认 2) */
  max_sessions_per_user: number
  /** B3: 游客限流策略 */
  guest_strategy: { watchlist_limit: number; get_hourly_limit: number }
  /** B3: skill 档位限额覆盖(空 = 用 tier_configs 表 / 代码默认) */
  tier_limits: Record<string, Partial<Record<'daily_limit' | 'burst_limit' | 'refill_per_min', number>>>
}

export interface FreeTierResponse {
  config: FreeTierConfig
  defaults: FreeTierConfig
  catalog: { features: FreeTierFeature[]; skills: FreeTierSkill[]; tiers: FreeTierTierLimit[] }
  cache_ttl_seconds: number
}

export interface FreeTierPatch {
  /** {权限点: 中文名} 或 [权限点]; {} / [] = 不给任何试用 */
  trial_features?: Record<string, string> | string[]
  trial_daily_limit?: number
  member_watchlist_max?: number
  member_alert_max?: number
  skill_tier_overrides?: Record<string, string>
  /** B3: 平台限额三组 */
  max_sessions_per_user?: number
  guest_strategy?: { watchlist_limit?: number; get_hourly_limit?: number }
  tier_limits?: Record<string, Partial<Record<'daily_limit' | 'burst_limit' | 'refill_per_min', number>>>
}

export const freeTierApi = {
  get: () => fetchAPI<FreeTierResponse>('/admin/free-tier', { cacheMode: 'reload' }),
  update: (patch: FreeTierPatch) =>
    fetchAPI<{ config: FreeTierConfig; catalog: FreeTierResponse['catalog'] }>(
      '/admin/free-tier',
      { method: 'PUT', body: JSON.stringify(patch) },
    ),
}
