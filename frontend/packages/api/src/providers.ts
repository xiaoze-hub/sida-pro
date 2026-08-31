import { fetchAPI } from './client'

/** 场景→模型绑定(统一 LLM 配置中心)。model_id=null 表示未绑定(使用点回落默认模型)。 */
export interface SceneBinding {
  scene: string
  display_name: string
  description: string
  model_id: number | null
  model_name: string | null
  service_id: number | null
  is_bound: boolean
}

/** 全部场景的绑定状态(含未绑定场景), 前端据此渲染"场景分配" UI。 */
export const listSceneBindings = () =>
  fetchAPI<SceneBinding[]>('/providers/scene-bindings', { cacheMode: 'reload' })

/** 绑定场景到指定模型; modelId=null 解绑(回落默认模型)。 */
export const setSceneBinding = (scene: string, modelId: number | null) =>
  fetchAPI<SceneBinding>(`/providers/scene-bindings/${scene}`, {
    method: 'PUT',
    body: JSON.stringify({ model_id: modelId }),
  })
