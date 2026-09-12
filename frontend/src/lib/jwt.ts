/** JWT claims 解析(前端展示用途; 授权最终看后端)。
 *
 * 从 App.tsx 抽出(2026-09-12), 供"角色决定可见操作"的组件复用(如情绪周期回填按钮)。
 * 容错要点(原 M-2 修复): 非法 base64 / 只有两段 / payload 非对象 → 一律返回 null。
 */
export function safeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    if (!token) return null
    const parts = token.split('.')
    if (parts.length < 2) return null
    // base64url → base64: '-' '_' 替换成 '+' '/', 补 '='
    let s = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    while (s.length % 4) s += '='
    const obj = JSON.parse(atob(s))
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? (obj as Record<string, unknown>) : null
  } catch {
    return null
  }
}

export function getJwtUsername(): string | null {
  const p = safeJwtPayload(localStorage.getItem('token') || '')
  const u = p?.username
  return typeof u === 'string' ? u : null
}

/** owner | member | guest; 解不出返回 null。 */
export function getJwtRole(): string | null {
  const p = safeJwtPayload(localStorage.getItem('token') || '')
  const r = p?.role
  return typeof r === 'string' ? r : null
}

export function isDemoUser(): boolean {
  return getJwtUsername() === 'demo'
}

/** guest 角色: role==guest 或 demo 账号(后端口径 username=="demo" || role=="guest")。 */
export function isGuestUser(): boolean {
  return getJwtRole() === 'guest' || isDemoUser()
}
