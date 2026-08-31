import { fetchAPI } from './client'

export interface ReportItem {
  job_id: string
  job_name: string
  file: string
  size: number
  mtime: number
  mtime_iso: string
  title_preview: string
}

export interface ReportListResponse {
  items: ReportItem[]
  total: number
  jobs: { job_id: string; job_name: string }[]
}

export interface ReportContentResponse {
  job_id: string
  file: string
  content: string
}

export const reportsApi = {
  list: (params?: { job_id?: string; limit?: number; cacheMode?: 'reload' | number | false }) =>
    fetchAPI<ReportListResponse>(
      `/reports/list?${new URLSearchParams({
        ...(params?.job_id && { job_id: params.job_id }),
        ...(params?.limit && { limit: String(params.limit) }),
      } as Record<string, string>).toString()}`,
      // 首页 30s 轮询需要绕过前端 GET 缓存(cacheMode: 'reload')拿到新报告
      params?.cacheMode !== undefined ? { cacheMode: params.cacheMode } : undefined,
    ),

  content: (job_id: string, file: string) =>
    fetchAPI<ReportContentResponse>(`/reports/content?job_id=${encodeURIComponent(job_id)}&file=${encodeURIComponent(file)}`),
}
