// Change password unified logic (2026-08-17 close-loop fix A P1-8)
//
// Previously AccountMenu.tsx and Profile.tsx had duplicate implementations of
// change-password logic, with inconsistent copy / validation / error handling.
// Extract to a common helper; business callers manage their own UI state.

import { authApi } from '@panwatch/api'

export interface ChangePasswordParams {
  oldPwd: string
  newPwd: string
  confirmPwd: string
  onError: (msg: string | null) => void
  onSuccess: () => void
  onLoadingChange: (loading: boolean) => void
}

/**
 * Unified change-password logic:
 * 1. Validate new password length >= 8
 * 2. Validate new password matches confirm
 * 3. Call authApi.changePassword
 * 4. On success: clear fields (via onSuccess), close dialog
 * 5. On error: surface backend message via onError
 *
 * Usage:
 *   const handleChangePassword = async () => {
 *     await submitChangePassword({
 *       oldPwd, newPwd, confirmPwd,
 *       onError: setPwdError,
 *       onSuccess: () => { setOldPwd(''); setNewPwd(''); setConfirmPwd('') },
 *       onLoadingChange: setChangingPwd,
 *     })
 *   }
 */
export async function submitChangePassword(p: ChangePasswordParams): Promise<void> {
  if (p.newPwd.length < 8) {
    p.onError('新密码至少 8 位')
    return
  }
  if (p.newPwd !== p.confirmPwd) {
    p.onError('两次输入的密码不一致')
    return
  }
  p.onLoadingChange(true)
  p.onError(null)
  try {
    await authApi.changePassword(p.oldPwd, p.newPwd)
    p.onSuccess()
  } catch (e) {
    p.onError(e instanceof Error ? e.message : '修改失败, 请重试')
  } finally {
    p.onLoadingChange(false)
  }
}
