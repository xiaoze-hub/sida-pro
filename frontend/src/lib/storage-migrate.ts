/**
 * 旧品牌键迁移（2026-10-27 panwatch → sida）。
 *
 * 全站 localStorage 键从 `panwatch_*` 改名为 `sida_*`。如果只是改字面量，
 * 老用户的主题、自选视图、刷新间隔、引导气泡状态会**全部读不到**，表现为
 * "我设置过的东西怎么没了"。所以启动时做一次前缀搬迁：
 *
 *   panwatch_xxx  →  sida_xxx      （新键已存在则以新键为准，不覆盖）
 *   panwatch:xxx  →  sida:xxx
 *
 * 迁完保留旧键（不删），一个版本周期后人工清理，避免迁移写坏就无处回退。
 */

const LEGACY_PREFIXES = ['panwatch', 'PanWatch', 'PANWATCH'] as const
const CURRENT_PREFIX = 'sida'

let migrated = false

export function migrateLegacyStorage(): number {
  if (migrated) return 0
  migrated = true
  if (typeof window === 'undefined' || !window.localStorage) return 0

  let moved = 0
  try {
    const keys: string[] = []
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i)
      if (k) keys.push(k)
    }
    for (const key of keys) {
      const legacy = LEGACY_PREFIXES.find((p) => key.startsWith(p))
      if (!legacy) continue
      const next = CURRENT_PREFIX + key.slice(legacy.length)
      if (next === key) continue
      const value = localStorage.getItem(key)
      if (value === null) continue
      if (localStorage.getItem(next) === null) {
        localStorage.setItem(next, value)
        moved += 1
      }
    }
  } catch {
    // 隐私模式 / 配额满 → 静默放弃，功能退化为"设置未记住"，不阻断启动
  }
  return moved
}

export const STORAGE_PREFIX = CURRENT_PREFIX
