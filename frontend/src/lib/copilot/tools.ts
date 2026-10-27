/** 工具目录接口（参谋工作台·插件面板）。走 fetchAPI，自带 /api 前缀。 */
import { fetchAPI } from '@sida/api'

export interface ChatToolRow {
  name: string
  description: string
  /** 口径：该工具数据来自哪套口径。五个数据源不可换算，逐条展示，**不允许**汇总。 */
  caliber: string
  requires: string
  charges_budget: boolean
  has_fallback: boolean
  /** 挂载它的配方 id；空数组 = 没有任何配方在用这个工具（要能看出来）。 */
  recipes: string[]
}

export interface ChatToolCatalog {
  total: number
  mounted: number
  tools: ChatToolRow[]
}

export const getChatToolCatalog = (signal?: AbortSignal) =>
  fetchAPI<ChatToolCatalog>('/copilot/tools', { signal })
