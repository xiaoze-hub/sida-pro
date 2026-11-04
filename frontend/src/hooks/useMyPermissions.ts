import { useCallback, useEffect, useState } from 'react'
import { getMyPermissions } from '@sida/api'

/** 权限以当前登录凭据为界；失败不放行，也不能保留上个账号的数据。 */
export function useMyPermissions(token: string | null) {
  const [permissions, setPermissions] = useState<Set<string> | null>(null)
  const [failed, setFailed] = useState(false)
  const [loadedToken, setLoadedToken] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const retry = useCallback(() => setAttempt((value) => value + 1), [])

  useEffect(() => {
    let active = true
    setPermissions(null)
    setFailed(false)
    setLoadedToken(null)
    if (!token) return () => { active = false }
    void getMyPermissions().then((result) => {
      if (!active) return
      setLoadedToken(token)
      if (!result || !Array.isArray(result.effective)) {
        setFailed(true)
        return
      }
      setPermissions(new Set(result.effective))
    }).catch(() => {
      if (active) {
        setLoadedToken(token)
        setFailed(true)
      }
    })
    return () => { active = false }
  }, [token, attempt])

  return {
    permissions: token && token === loadedToken ? permissions : null,
    failed: Boolean(token && token === loadedToken && failed),
    retry,
  }
}
