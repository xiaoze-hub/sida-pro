import { fetchAPI } from './client'

export interface ResetToSeedDeletedItem {
  id: number
  type: string
  provider: string
  name: string
}

export interface ResetToSeedSeededItem {
  name: string
  type: string
  provider: string
}

export interface ResetToSeedResult {
  deleted: ResetToSeedDeletedItem[]
  seeded_missing: ResetToSeedSeededItem[]
}

/** 数据源"恢复默认":删孤儿数据源行 + 补缺失默认 + 保留用户有效自定义/凭证。 */
export const resetDataSourcesToSeed = () =>
  fetchAPI<ResetToSeedResult>('/datasources/reset-to-seed', { method: 'POST' })


// ── v0.4.37 P0 派活 2 整合: 数据源健康 API 封装 ──────────────────────────

/** 健康检查单项 (匹配 src/web/api/datasources.py: DatasourceHealthItem). */
export interface SourceHealthItem {
  id: string
  name?: string
  status: 'connected' | 'degraded' | 'down' | 'unknown'
  last_check_at?: number
  detail?: string | null
  icons?: string[]
}

/** 健康检查返回结构. */
export interface SourceHealthResp {
  checked_at: number
  items: SourceHealthItem[]
}

export const datasourcesApi = {
  /** 4 个 L4 逻辑源 (探测式, 决定事件图标灰显). */
  health: <T = SourceHealthResp>() =>
    fetchAPI<T>('/datasources/health'),
  /** 通用 data_sources 表累计统计 (配置源好没好). */
  dataSourcesHealth: <T = SourceHealthResp>() =>
    fetchAPI<T>('/datasources/health/data-sources'),
  /** 单个逻辑源. */
  singleHealth: (id: string) =>
    fetchAPI<SourceHealthItem>(`/datasources/health/${encodeURIComponent(id)}`),
}
