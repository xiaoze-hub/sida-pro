/**
 * 登录身份生命周期状态机(纯函数, 2026-11-08) —— 供壳层/认证守卫共用。
 *
 * 故障背景(跨标签页换号的隐私残留):
 *   工作台页签是**真 keep-alive**(`TabPane` 隐藏不卸), 页面私有 `useState` 一直活着。
 *   另一个标签页登录/登出只改 `localStorage`, 本标签页原来只在**路由变化**时复检登录态 ——
 *   于是同一个人把电脑交给下一个人(或两端各登一个号)时, 新用户会直接看到上一个用户
 *   留在常驻面板里的页面与本地状态。
 *
 * 本模块只回答两个问题:
 *   ① 现在**能不能**渲染私有页面(ready), 还是必须**封锁**;
 *   ② 凭证变了以后, 是不是要**先封锁一拍、确认完再放行**(换人 = 要, 同人换 token = 不要)。
 *
 * fail-closed 口径: 没有凭证 / 凭证解不出账号 ⇒ 封锁。宁可多一次登录, 不猜"应该是同一个人"。
 */

export type AuthGatePhase = 'ready' | 'resolving' | 'blocked'

/** 封锁原因: `rotating` 正在重新确认; `signed-out` 无凭证; `unrecognized` 凭证不可解析。 */
export type AuthGateReason = 'rotating' | 'signed-out' | 'unrecognized'

export interface AuthGateSnapshot {
  phase: AuthGatePhase
  /** 当前渲染世界所属账号(`u:<sub>`, **不含 token**); 非 ready 一律 null */
  userId: string | null
  reason: AuthGateReason | null
}

/**
 * 会改变登录身份的存储键(单一事实源): 跨标签页登录/登出/换号都落在这几个键上。
 * `key === null` 表示另一个标签页调用了 `localStorage.clear()`, 同样按身份变化处理。
 */
export const CREDENTIAL_STORAGE_KEYS: ReadonlySet<string> = new Set([
  'token',
  'token_expires',
  'user',
])

/**
 * 由"有没有凭证 + 凭证解出的账号"算快照。
 * `userId` 只由 JWT claim 组成(见 `@sida/api` 的 `getAuthIdentity`), 这里不重复解析。
 */
export function gateSnapshot(hasCredential: boolean, userId: string | null): AuthGateSnapshot {
  if (!hasCredential) return { phase: 'blocked', userId: null, reason: 'signed-out' }
  if (!userId) return { phase: 'blocked', userId: null, reason: 'unrecognized' }
  return { phase: 'ready', userId, reason: null }
}

/**
 * 凭证换代后是否需要"先封锁一拍"。
 *
 * 只有**换人**才需要: 旧面板里是别人的页面/别人的 useState, 必须在新世界渲染前彻底消失。
 * 同一个 `sub` 换发 token(撤销会话后重登 / 静默续期 / 同标签页刷新凭证)是**本人**的状态,
 * 重挂会把输入框、展开态、滚动位置全丢掉 —— 那是回归, 不是修隐私。
 */
export function needsIdentityHold(prev: AuthGateSnapshot, next: AuthGateSnapshot): boolean {
  return prev.phase === 'ready' && prev.userId !== next.userId
}

/** 过渡态快照: 不携带任何账号信息(不能借过渡块把上一个用户的名字/角色带出来)。 */
export function rotatingSnapshot(): AuthGateSnapshot {
  return { phase: 'resolving', userId: null, reason: 'rotating' }
}
