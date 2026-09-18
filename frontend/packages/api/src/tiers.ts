/**
 * 档位对比 API 客户端(P2-4, 2026-09-18)。
 *
 * 后端契约:
 *  · `GET  /tiers`(完整路径 `/api/tiers`) —— **公开**(无需登录); 内容由后端从权限定义现读现拼,
 *                                前端**不许**硬编码权限清单(会漂移)。
 *  · `POST /pro/apply`(完整路径 `/api/pro/apply`) —— 提交 Pro 申请(需登录; 同一用户仅一条 pending)。
 *  · `GET  /pro/apply/status`(完整路径) —— 查自己的申请状态。
 *
 * ⚠️ **路径不带 `/api` 前缀**: `fetchAPI` 的 baseURL 已经是 `…/api`, 再写 `/api/xxx` 会变成
 * `/api/api/xxx` → 404(**生产实测踩过**: 页面因此一直显示"档位信息暂时取不到")。
 *
 * 诚实口径: 内测期不收费 → 响应里**没有**价格字段(`billing_enabled: false`),
 * 页面因此不能也不该显示任何金额。
 */
import { fetchAPI } from './client'

export interface TierGroup {
  group: string
  items: string[]
}

export interface TierRow {
  key: string
  label: string
  groups: TierGroup[]
  /** 该档位权限点总数(后端给, 前端不自己数) */
  count: number
}

export interface TierLimits {
  watchlist_max: number
  alert_max: number
  trial_daily_limit: number
}

export interface TiersResp {
  tiers: TierRow[]
  /** 免费档运行时可调上限的**实时值**; 读不到时为 null(页面如实说明, 不编数字) */
  member_limits: TierLimits | null
  /** 内测期恒为 false —— 有价格字段才是 True, 页面据此决定是否显示价格区 */
  billing_enabled: boolean
  note: string
  apply_endpoint: string
  limits_note: string
  demo_note: string
}

export interface ProApplication {
  id: number
  username: string
  reason: string
  /** pending / approved / rejected(后端原样给) */
  status: string
  created_at: string | null
  reviewed_at: string | null
  review_note: string
}

/** 与后端 `GET /api/pro/apply/status` 一致: 返回**最近一条**申请(可能为 null)。 */
export interface ProApplyStatus {
  role: string
  application: ProApplication | null
  has_application: boolean
}

export function fetchTiers() {
  return fetchAPI<TiersResp>('/tiers')
}

export function applyPro(reason = '', contact = '') {
  return fetchAPI<{ id?: number; status?: string }>('/pro/apply', {
    method: 'POST',
    body: JSON.stringify({ reason, contact }),
  })
}

export function fetchProApplyStatus() {
  return fetchAPI<ProApplyStatus>('/pro/apply/status')
}
