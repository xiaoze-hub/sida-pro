import { createContext, useSyncExternalStore } from 'react'
import type { AnimationItem } from 'lottie-web'

export type AssistantAvatarId = 'flow' | 'foriday'
export const AssistantActivityContext = createContext(false)
export const ASSISTANT_AVATARS = [
  { id: 'flow', label: 'Flow', src: '/assistant/flow.json', idleFrame: 90 },
  { id: 'foriday', label: 'Foriday', src: '/assistant/foriday.json', idleFrame: 60 },
] as const
const EVENT = 'sida:assistant-avatar'
let fallback: AssistantAvatarId = 'foriday'

function storageKey() {
  try {
    const user = JSON.parse(localStorage.getItem('user') || '{}')
    return `sida_assistant_avatar:${user.id ?? 'local'}`
  } catch { return 'sida_assistant_avatar:local' }
}
function read(): AssistantAvatarId {
  try { return localStorage.getItem(storageKey()) === 'flow' ? 'flow' : 'foriday' } catch { return fallback }
}
export function setAssistantAvatar(id: AssistantAvatarId) {
  fallback = id
  try { localStorage.setItem(storageKey(), id) } catch { /* 浏览器不允许持久化时仍同步本窗口 */ }
  window.dispatchEvent(new Event(EVENT))
}
const subscribe = (listener: () => void) => {
  window.addEventListener(EVENT, listener)
  window.addEventListener('storage', listener)
  return () => { window.removeEventListener(EVENT, listener); window.removeEventListener('storage', listener) }
}
export function useAssistantAvatar() {
  return useSyncExternalStore(subscribe, read, () => 'foriday' as const)
}

const assets = new Map<AssistantAvatarId, Promise<Record<string, unknown>>>()
export async function loadAssistantAnimation(id: AssistantAvatarId) {
  const spec = ASSISTANT_AVATARS.find(item => item.id === id)!
  if (!assets.has(id)) assets.set(id, fetch(spec.src).then(async response => {
    if (!response.ok) throw new Error('Animation asset unavailable')
    const data = await response.json()
    if (!Array.isArray(data.layers)) throw new Error('Invalid animation asset')
    return data as Record<string, unknown>
  }).catch(error => { assets.delete(id); throw error }))
  const data = await assets.get(id)!
  const { default: lottie } = await import('lottie-web')
  // Lottie 会修改含 repeater 的数据；每个实例使用独立副本。
  return { lottie, data: structuredClone(data), idleFrame: spec.idleFrame }
}
export function setAvatarPlayback(animation: AnimationItem, playing: boolean, idleFrame: number, busy = false, initialize = false) {
  animation.setSpeed(busy ? 1 : 0.55)
  if (initialize) {
    if (playing) animation.goToAndPlay(idleFrame, true)
    else animation.goToAndStop(idleFrame, true)
  } else if (playing) animation.play()
  else animation.pause()
}
