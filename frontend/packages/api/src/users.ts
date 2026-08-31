import { fetchAPI } from './client'

export interface PermissionInfo {
  key: string
  label: string
  group: string
}

export interface MyPermissions {
  username: string
  role: string
  granted: string[]
  role_defaults: string[]
  effective: string[]
  all_permissions: PermissionInfo[]
}

/** 当前用户自己的模块权限(导航过滤用)。失败返回 null(调用方回退角色判断)。 */
export async function getMyPermissions(): Promise<MyPermissions | null> {
  try {
    return await fetchAPI<MyPermissions>('/users/me/permissions')
  } catch {
    return null
  }
}
