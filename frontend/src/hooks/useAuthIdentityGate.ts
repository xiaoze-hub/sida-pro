/**
 * 登录身份生命周期(React 绑定, 2026-11-08)。
 *
 * 订阅三条信号源, 全部收敛到同一个判据(见 `lib/identity-lifecycle.ts`):
 *   · `storage` —— **另一个标签页**登录/登出/换号/清空存储(跨 tab 的主路径);
 *   · `AUTH_IDENTITY_EVENT` —— **本标签页**登录/登出/撤销会话/注销(登录页不刷新的 SPA 换号);
 *   · 挂载时对齐一次 —— 本组件挂载前可能已经换过身份(如登录页刚卸载)。
 *
 * 时序(为什么需要"封锁一拍"):
 *   换人事件到达时**立即**把快照切到 `resolving` 并渲染过渡块, 旧面板在这一帧就卸载
 *   (它们的 `useState` 与在途请求的 setState 一起失效); 下一拍再读取真实凭证确认。
 *   不这样做的话, storage 事件与 React 提交之间会有一帧继续显示旧用户页面, 而这一帧
 *   正是轮询/在途请求用新 token 把新数据写进旧界面的窗口。
 *
 * 只按**凭证字符串 + 账号 claim** 判断变化; token 原文只在本模块内存里用于比较,
 * 不进快照、不进 DOM、不进日志。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { AUTH_IDENTITY_EVENT, getAuthIdentity, getRequestToken } from '@sida/api'

import {
  CREDENTIAL_STORAGE_KEYS,
  gateSnapshot,
  needsIdentityHold,
  rotatingSnapshot,
  type AuthGateSnapshot,
} from '@/lib/identity-lifecycle'

function snapshotNow(): AuthGateSnapshot {
  return gateSnapshot(getRequestToken() !== null, getAuthIdentity())
}

export function useAuthIdentityGate(): AuthGateSnapshot {
  const [snapshot, setSnapshot] = useState<AuthGateSnapshot>(snapshotNow)
  // ref 里放"最新已提交快照", 事件回调不依赖闭包里的旧值(否则连续两次换人会拿旧 userId 比较)
  const snapshotRef = useRef(snapshot)
  snapshotRef.current = snapshot
  const credentialRef = useRef<string | null>(getRequestToken())
  const holdTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const commit = useCallback(() => {
    holdTimerRef.current = null
    credentialRef.current = getRequestToken()
    const next = snapshotNow()
    snapshotRef.current = next
    setSnapshot(next)
  }, [])

  const onCredentialSignal = useCallback(() => {
    const credential = getRequestToken()
    if (credential === credentialRef.current) return
    credentialRef.current = credential
    const next = snapshotNow()
    if (needsIdentityHold(snapshotRef.current, next)) {
      holdTimerRef.current && clearTimeout(holdTimerRef.current)
      const hold = rotatingSnapshot()
      snapshotRef.current = hold
      setSnapshot(hold)
      // 下一拍确认: 让 storage 的后续写入(token_expires / user)落完, 也给"旧面板已卸载"一个真实帧
      holdTimerRef.current = setTimeout(commit, 0)
      return
    }
    snapshotRef.current = next
    setSnapshot(next)
  }, [commit])

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || CREDENTIAL_STORAGE_KEYS.has(event.key)) onCredentialSignal()
    }
    window.addEventListener('storage', onStorage)
    window.addEventListener(AUTH_IDENTITY_EVENT, onCredentialSignal)
    // 挂载对齐: 首帧渲染与 effect 之间凭证可能已被另一个标签页改掉
    onCredentialSignal()
    return () => {
      window.removeEventListener('storage', onStorage)
      window.removeEventListener(AUTH_IDENTITY_EVENT, onCredentialSignal)
      if (holdTimerRef.current != null) {
        clearTimeout(holdTimerRef.current)
        holdTimerRef.current = null
      }
    }
  }, [onCredentialSignal])

  return snapshot
}
